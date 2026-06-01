"""Tests for CS2Coordinator pure/helper methods — no HA event loop required."""
import sys
import os
import datetime
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.cs2.coordinator import CS2Coordinator, _empty_result


# ── Minimal coordinator factory ───────────────────────────────────────────────

def _make_coordinator(cfg=None):
    """Build a CS2Coordinator with mocked hass and entry — no real HA runtime.

    Uses object.__new__ to bypass DataUpdateCoordinator (which is a MagicMock
    in the test environment) and avoid MagicMock.__new__ interference.
    """
    hass = MagicMock()
    hass.config.config_dir = "/tmp/test_cs2"
    hass.loop = MagicMock()
    hass.bus = MagicMock()
    entry = MagicMock()
    entry.data = cfg or {"steam_ids": "76561190000000001:TestAccount"}
    entry.options = {}
    entry.entry_id = "test_entry_id"
    # object.__new__ bypasses MagicMock's custom __new__ from the mocked base class
    c = object.__new__(CS2Coordinator)
    c.hass = hass
    c.config_entry = entry
    c._cfg = entry.data
    c._active_apps = []
    c._last_discovery = None
    c._alert_state = {}
    c._inv_cooldown = {}
    c._market_rl_until = 0.0
    c._market_rl_consecutive = 0
    c._import_running = False
    c._import_progress = {}
    c._current_prices = {}
    c._previous_prices = {}
    c._last_cycle_stats = {}
    return c


# ── _previous_total ───────────────────────────────────────────────────────────

class TestPreviousTotal:
    def test_empty_previous_prices_returns_none(self):
        result = CS2Coordinator._previous_total({}, [{"name": "AK", "quantity": 1}])
        assert result is None

    def test_item_not_in_previous_skipped(self):
        result = CS2Coordinator._previous_total(
            {"AWP": 100.0},
            [{"name": "AK", "quantity": 1}],
        )
        assert result is None

    def test_quantity_multiplied(self):
        result = CS2Coordinator._previous_total(
            {"AK": 10.0},
            [{"name": "AK", "quantity": 3}],
        )
        assert result == pytest.approx(30.0)

    def test_multiple_items_summed(self):
        result = CS2Coordinator._previous_total(
            {"AK": 10.0, "AWP": 50.0},
            [{"name": "AK", "quantity": 2}, {"name": "AWP", "quantity": 1}],
        )
        assert result == pytest.approx(70.0)

    def test_item_absent_from_previous_skipped(self):
        result = CS2Coordinator._previous_total(
            {"AK": 10.0},
            [{"name": "AK", "quantity": 1}, {"name": "AWP", "quantity": 1}],
        )
        assert result == pytest.approx(10.0)

    def test_zero_total_returns_none(self):
        # All prices zero → round(0, 2) → 0 → falsy → None
        result = CS2Coordinator._previous_total(
            {"AK": 0.0},
            [{"name": "AK", "quantity": 5}],
        )
        assert result is None

    def test_result_rounded_to_2dp(self):
        result = CS2Coordinator._previous_total(
            {"AK": 3.333},
            [{"name": "AK", "quantity": 3}],
        )
        assert result == round(result, 2)


# ── _needs_discovery ──────────────────────────────────────────────────────────

class TestNeedsDiscovery:
    def test_no_active_apps_needs_discovery(self):
        c = _make_coordinator()
        c._active_apps = []
        assert c._needs_discovery() is True

    def test_no_last_discovery_needs_discovery(self):
        c = _make_coordinator()
        c._active_apps = [(730, 2, "cs2", "CS2")]
        c._last_discovery = None
        assert c._needs_discovery() is True

    def test_recent_discovery_no_need(self):
        c = _make_coordinator()
        c._active_apps = [(730, 2, "cs2", "CS2")]
        # Patch dt_util.utcnow at the coordinator module level
        now = datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc)
        c._last_discovery = datetime.datetime(2024, 5, 31, tzinfo=datetime.timezone.utc)
        import custom_components.cs2.coordinator as coord_mod
        from unittest.mock import patch
        with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
            assert c._needs_discovery() is False

    def test_old_discovery_needs_refresh(self):
        c = _make_coordinator()
        c._active_apps = [(730, 2, "cs2", "CS2")]
        now = datetime.datetime(2024, 6, 10, tzinfo=datetime.timezone.utc)
        c._last_discovery = datetime.datetime(2024, 5, 15, tzinfo=datetime.timezone.utc)
        import custom_components.cs2.coordinator as coord_mod
        from unittest.mock import patch
        with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
            assert c._needs_discovery() is True


# ── _check_price_alerts ───────────────────────────────────────────────────────

class TestCheckPriceAlerts:
    def _fire_calls(self, c):
        """Return all call args to hass.loop.call_soon_threadsafe."""
        return c.hass.loop.call_soon_threadsafe.call_args_list

    def test_price_above_high_fires_event(self):
        c = _make_coordinator()
        c._check_price_alerts({"AK": 15.0}, {"AK": {"high": 10.0}})
        assert c.hass.loop.call_soon_threadsafe.called
        _, event_data = self._fire_calls(c)[0][0][2], self._fire_calls(c)[0][0][2]
        payload = self._fire_calls(c)[0][0][2]
        assert payload["threshold_type"] == "high"
        assert payload["current_price"] == 15.0

    def test_price_below_low_fires_event(self):
        c = _make_coordinator()
        c._check_price_alerts({"AK": 5.0}, {"AK": {"low": 8.0}})
        payload = self._fire_calls(c)[0][0][2]
        assert payload["threshold_type"] == "low"

    def test_same_state_no_duplicate_event(self):
        c = _make_coordinator()
        c._alert_state["AK"] = "high"
        c._check_price_alerts({"AK": 15.0}, {"AK": {"high": 10.0}})
        assert not c.hass.loop.call_soon_threadsafe.called

    def test_return_to_normal_no_event(self):
        c = _make_coordinator()
        c._alert_state["AK"] = "high"
        # Price back below threshold → transition high→none, no event fired
        c._check_price_alerts({"AK": 8.0}, {"AK": {"high": 10.0}})
        assert not c.hass.loop.call_soon_threadsafe.called
        assert c._alert_state["AK"] == "none"

    def test_alert_state_updated(self):
        c = _make_coordinator()
        c._check_price_alerts({"AK": 15.0}, {"AK": {"high": 10.0}})
        assert c._alert_state["AK"] == "high"

    def test_missing_price_no_event(self):
        c = _make_coordinator()
        c._check_price_alerts({}, {"AK": {"high": 10.0}})
        assert not c.hass.loop.call_soon_threadsafe.called

    def test_high_threshold_takes_priority_over_low(self):
        c = _make_coordinator()
        # Price satisfies both thresholds (unusual but possible with bad config)
        c._check_price_alerts({"AK": 20.0}, {"AK": {"high": 10.0, "low": 5.0}})
        payload = self._fire_calls(c)[0][0][2]
        assert payload["threshold_type"] == "high"

    def test_event_contains_correct_threshold_value(self):
        c = _make_coordinator()
        c._check_price_alerts({"AK": 5.0}, {"AK": {"low": 8.0}})
        payload = self._fire_calls(c)[0][0][2]
        assert payload["threshold_value"] == 8.0
        assert payload["market_hash_name"] == "AK"

    def test_multiple_items_independent(self):
        c = _make_coordinator()
        c._check_price_alerts(
            {"AK": 15.0, "AWP": 5.0},
            {"AK": {"high": 10.0}, "AWP": {"low": 8.0}},
        )
        assert c.hass.loop.call_soon_threadsafe.call_count == 2


# ── _empty_result ─────────────────────────────────────────────────────────────

class TestEmptyResult:
    def test_structure(self):
        r = _empty_result()
        assert r["global"]["total_value"] == 0.0
        assert r["items"] == []
        assert r["per_game"] == {}
        assert r["watchlist"] == []
        assert r["stale_count"] == 0
        assert r["missing_count"] == 0

    def test_global_metrics_complete(self):
        r = _empty_result()
        for key in ("total_value", "items_count", "items_total_qty",
                    "best_performer_name", "worst_performer_name"):
            assert key in r["global"]

    def test_items_count_zero(self):
        assert _empty_result()["global"]["items_count"] == 0


# ── _device_unique_id ─────────────────────────────────────────────────────────

class TestDeviceUniqueId:
    def test_same_steam_ids_same_hash(self):
        c1 = _make_coordinator({"steam_ids": "76561190000000001:A"})
        c2 = _make_coordinator({"steam_ids": "76561190000000001:A"})
        assert c1._device_unique_id == c2._device_unique_id

    def test_different_steam_ids_different_hash(self):
        c1 = _make_coordinator({"steam_ids": "76561190000000001:A"})
        c2 = _make_coordinator({"steam_ids": "76561190000000002:B"})
        assert c1._device_unique_id != c2._device_unique_id

    def test_empty_steam_ids_falls_back_to_entry_id(self):
        c = _make_coordinator({"steam_ids": ""})
        assert c._device_unique_id == c.config_entry.entry_id

    def test_hash_is_16_chars(self):
        c = _make_coordinator({"steam_ids": "76561190000000001:A"})
        assert len(c._device_unique_id) == 16
