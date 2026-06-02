"""Tests for importer._sync_fetch_histories — pure sync, no HA runtime."""
import sys
import os
import threading

import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.cs2.importer import _sync_fetch_histories


# ── Helpers ───────────────────────────────────────────────────────────────────

def _item(name, price=10.0, qty=1, game_slug="cs2", appid=730):
    return {
        "name": name,
        "market_hash_name": name,
        "current_price": price,
        "quantity": qty,
        "game_slug": game_slug,
        "appid": appid,
    }


def _hist(start="2024-01-01", end="2024-01-05", price=10.0):
    from datetime import date, timedelta
    d = date.fromisoformat(start)
    last = date.fromisoformat(end)
    result = {}
    while d <= last:
        result[d.isoformat()] = price
        d += timedelta(days=1)
    return result


_PATCH_HISTORY = "custom_components.cs2.importer.fetch_item_history"
_PATCH_GAPS    = "custom_components.cs2.importer.interpolate_gaps"


# ── Normal aggregation ────────────────────────────────────────────────────────

class TestSyncFetchHistoriesAggregation:
    def test_single_item_aggregated(self):
        hist = {"2024-01-01": 10.0, "2024-01-02": 20.0}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47 | Redline", price=10.0, qty=1)],
                cookie="tok", start_date=None, min_value=0,
            )
        assert result["fetched"] == 1
        assert result["skipped"] == 0
        assert result["daily_totals"]["2024-01-01"] == pytest.approx(10.0)
        assert result["daily_totals"]["2024-01-02"] == pytest.approx(20.0)

    def test_quantity_multiplied_in_totals(self):
        hist = {"2024-01-01": 10.0}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47", price=10.0, qty=3)],
                cookie="tok", start_date=None, min_value=0,
            )
        assert result["daily_totals"]["2024-01-01"] == pytest.approx(30.0)

    def test_per_item_history_stores_unit_price(self):
        """Per-item history must store unit price, not price × qty."""
        hist = {"2024-01-01": 10.0}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47", price=10.0, qty=5)],
                cookie="tok", start_date=None, min_value=0,
            )
        assert result["per_item_histories"]["AK-47"]["2024-01-01"] == pytest.approx(10.0)

    def test_multiple_items_summed(self):
        hist_a = {"2024-01-01": 10.0}
        hist_b = {"2024-01-01": 5.0}
        with patch(_PATCH_HISTORY, side_effect=[hist_a, hist_b]), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("A"), _item("B", price=5.0)],
                cookie="tok", start_date=None, min_value=0,
            )
        assert result["daily_totals"]["2024-01-01"] == pytest.approx(15.0)
        assert result["fetched"] == 2

    def test_per_game_totals_populated(self):
        hist = {"2024-01-01": 8.0}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47", game_slug="cs2")],
                cookie="tok", start_date=None, min_value=0,
            )
        assert "cs2" in result["per_game_totals"]
        assert result["per_game_totals"]["cs2"]["2024-01-01"] == pytest.approx(8.0)

    def test_item_without_game_slug_not_in_per_game(self):
        hist = {"2024-01-01": 5.0}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47", game_slug="")],
                cookie="tok", start_date=None, min_value=0,
            )
        assert result["per_game_totals"] == {}


# ── Filtering ─────────────────────────────────────────────────────────────────

class TestSyncFetchHistoriesFiltering:
    def test_min_value_skips_cheap_items(self):
        with patch(_PATCH_HISTORY, return_value={"2024-01-01": 1.0}) as mock_h, \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("Cheap", price=0.3)],
                cookie="tok", start_date=None, min_value=1.0,
            )
        mock_h.assert_not_called()
        assert result["skipped"] == 1
        assert result["fetched"] == 0

    def test_min_value_zero_fetches_all(self):
        hist = {"2024-01-01": 0.1}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("Cheap", price=0.1)],
                cookie="tok", start_date=None, min_value=0,
            )
        assert result["fetched"] == 1

    def test_start_date_filters_old_entries(self):
        hist = {"2024-01-01": 5.0, "2024-06-01": 10.0}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47")],
                cookie="tok", start_date="2024-03-01", min_value=0,
            )
        assert "2024-01-01" not in result["daily_totals"]
        assert "2024-06-01" in result["daily_totals"]

    def test_start_date_none_fetches_all_dates(self):
        hist = {"2020-01-01": 5.0, "2024-01-01": 10.0}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47")],
                cookie="tok", start_date=None, min_value=0,
            )
        assert "2020-01-01" in result["daily_totals"]
        assert "2024-01-01" in result["daily_totals"]


# ── Empty history handling ────────────────────────────────────────────────────

class TestSyncFetchHistoriesEmpty:
    def test_empty_history_increments_skipped(self):
        with patch(_PATCH_HISTORY, return_value={}), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47")],
                cookie="tok", start_date=None, min_value=0,
            )
        assert result["skipped"] == 1
        assert result["fetched"] == 0

    def test_empty_history_not_in_per_item(self):
        with patch(_PATCH_HISTORY, return_value={}), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47")],
                cookie="tok", start_date=None, min_value=0,
            )
        assert "AK-47" not in result["per_item_histories"]

    def test_cookie_expiry_warning_after_10_consecutive(self, caplog):
        import logging
        with patch(_PATCH_HISTORY, return_value={}), \
             patch("time.sleep"), \
             caplog.at_level(logging.WARNING, logger="custom_components.cs2.importer"):
            result = _sync_fetch_histories(
                [_item(f"Item {i}", price=10.0) for i in range(12)],
                cookie="tok", start_date=None, min_value=0,
            )
        assert result["skipped"] == 12
        assert any("cookie" in r.message.lower() or "empty" in r.message.lower()
                   for r in caplog.records)

    def test_cookie_warning_not_fired_after_success(self, caplog):
        """No warning if at least one item succeeded before the streak."""
        import logging
        responses = [{"2024-01-01": 5.0}] + [{}] * 11
        with patch(_PATCH_HISTORY, side_effect=responses), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"), \
             caplog.at_level(logging.WARNING, logger="custom_components.cs2.importer"):
            _sync_fetch_histories(
                [_item(f"Item {i}", price=10.0) for i in range(12)],
                cookie="tok", start_date=None, min_value=0,
            )
        warning_texts = [r.message for r in caplog.records]
        assert not any("cookie" in t.lower() for t in warning_texts)


# ── Stop signal ───────────────────────────────────────────────────────────────

class TestSyncFetchHistoriesStop:
    def test_stop_signal_aborts_loop(self):
        stop = threading.Event()
        stop.set()
        with patch(_PATCH_HISTORY, return_value={"2024-01-01": 5.0}), \
             patch(_PATCH_GAPS, side_effect=lambda h: h):
            result = _sync_fetch_histories(
                [_item("AK-47"), _item("AWP")],
                cookie="tok", start_date=None, min_value=0, stop=stop,
            )
        assert result["fetched"] == 0

    def test_stop_after_first_item(self):
        stop = threading.Event()
        hist = {"2024-01-01": 5.0}
        call_count = 0

        def fake_fetch(http, name, cookie, stop=None, app_id=730, currency=3):
            nonlocal call_count
            call_count += 1
            stop.set()  # signal after first call
            return hist

        with patch(_PATCH_HISTORY, side_effect=fake_fetch), \
             patch(_PATCH_GAPS, side_effect=lambda h: h):
            result = _sync_fetch_histories(
                [_item("AK-47"), _item("AWP")],
                cookie="tok", start_date=None, min_value=0, stop=stop,
            )
        assert call_count == 1
        assert result["fetched"] == 1


# ── Progress callback ─────────────────────────────────────────────────────────

class TestSyncFetchHistoriesProgress:
    def test_progress_callback_called(self):
        hist = {"2024-01-01": 5.0}
        calls = []
        def cb(fetched, total, skipped):
            calls.append((fetched, total, skipped))

        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            _sync_fetch_histories(
                [_item("A"), _item("B")],
                cookie="tok", start_date=None, min_value=0, progress_cb=cb,
            )
        assert len(calls) == 2
        assert calls[0] == (1, 2, 0)
        assert calls[1] == (2, 2, 0)

    def test_progress_total_excludes_min_value_filtered(self):
        """total_to_fetch must not include items that will be skipped by min_value."""
        hist = {"2024-01-01": 5.0}
        calls = []
        def cb(fetched, total, skipped):
            calls.append(total)

        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            _sync_fetch_histories(
                [_item("Cheap", price=0.1), _item("Expensive", price=50.0)],
                cookie="tok", start_date=None, min_value=1.0, progress_cb=cb,
            )
        assert calls[0] == 1  # only 1 item above min_value


# ── Result rounding ───────────────────────────────────────────────────────────

class TestSyncFetchHistoriesRounding:
    def test_daily_totals_rounded_to_2dp(self):
        hist = {"2024-01-01": 3.333}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47", qty=3)],
                cookie="tok", start_date=None, min_value=0,
            )
        val = result["daily_totals"]["2024-01-01"]
        assert val == round(val, 2)

    def test_per_item_rounded_to_4dp(self):
        hist = {"2024-01-01": 1.23456789}
        with patch(_PATCH_HISTORY, return_value=hist), \
             patch(_PATCH_GAPS, side_effect=lambda h: h), \
             patch("time.sleep"):
            result = _sync_fetch_histories(
                [_item("AK-47")],
                cookie="tok", start_date=None, min_value=0,
            )
        val = result["per_item_histories"]["AK-47"]["2024-01-01"]
        assert val == round(val, 4)
