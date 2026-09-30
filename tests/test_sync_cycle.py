"""Tests for CS2Coordinator._sync_cycle — multi-account loop and error paths.

The cycle builds its own ``httpx.Client``, so the ``httpx`` name inside
coordinator.py is swapped for a shim handing out MockTransport-backed clients:
the real Steam Market code path (rate limits, circuit-breaker) runs, but no
socket is ever opened.  Inventory HTTP is urllib-based, so it is stubbed at
``steam_inventory.fetch_inventory`` / ``check_inventory_count`` level.
"""
import datetime
import json
import os
import sys
import time
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import custom_components.cs2.coordinator as coord_mod
from custom_components.cs2.api import steam_inventory, steam_market
from custom_components.cs2.const import KNOWN_MARKETABLE_APPS, WATCHLIST_FILE
from custom_components.cs2.coordinator import CS2Coordinator, _empty_result
from custom_components.cs2.price_tracker import RollingPriceFetcher

CS2 = (730, 2, "cs2", "CS2")
DOTA = (570, 2, "dota2", "Dota 2")

ACCOUNT_A = "76561190000000001"
ACCOUNT_B = "76561190000000002"
ONE_ACCOUNT = f"{ACCOUNT_A}:Alice"
TWO_ACCOUNTS = f"{ACCOUNT_A}:Alice,{ACCOUNT_B}:Bob"

# Discovery probes every known app except Steam Cards (opt-in, appid 753).
CANDIDATE_APPS = [app for app in KNOWN_MARKETABLE_APPS if app[0] != 753]

REDLINE = "AK-47 | Redline"
DRAGON_LORE = "AWP | Dragon Lore"
ASIMOV = "AWP | Asiimov"


# ── Doublures ────────────────────────────────────────────────────────────────

class _FakeStop:
    """threading.Event stand-in — is_set()/wait() without real sleeping."""

    def __init__(self, stopped: bool = False) -> None:
        self._stopped = stopped

    def is_set(self) -> bool:
        return self._stopped

    def wait(self, timeout=None) -> bool:
        return self._stopped


class _FakeClock:
    """Real datetimes for dt_util (mocked module in the HA-less test env)."""

    def __init__(self) -> None:
        self._now = datetime.datetime(2026, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def utcnow(self) -> datetime.datetime:
        return self._now

    def now(self, tz=None) -> datetime.datetime:
        return self._now if tz is None else self._now.astimezone(tz)


class _HttpxShim:
    """Stands in for the ``httpx`` module inside coordinator.py.

    Every ``Client()`` call returns a fresh real ``httpx.Client`` wired to a
    MockTransport, so request handling is exercised for real without network.
    """

    HTTPError = httpx.HTTPError

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[str] = []

    def Client(self, *args, **kwargs):  # noqa: N802 — mirrors httpx.Client
        shim = self

        def _handle(request: httpx.Request) -> httpx.Response:
            shim.calls.append(str(request.url))
            return shim._handler(request)

        return httpx.Client(transport=httpx.MockTransport(_handle))


class _SteamStub:
    """Records Steam inventory/market calls and replays canned answers."""

    def __init__(
        self,
        inventories: dict | None = None,
        prices: dict[str, float] | None = None,
        default_count: int = 0,
        count_by_app: dict[int, int] | None = None,
    ) -> None:
        # keys: (steam_id, appid) or steam_id alone (any app); values: items or Exception
        self.inventories = inventories or {}
        self.prices = prices or {}
        self.default_count = default_count
        self.count_by_app = count_by_app or {}
        self.inventory_calls: list[tuple[str, int]] = []
        self.count_calls: list[tuple[str, int]] = []
        self.price_calls: list[list[str]] = []

    def check_inventory_count(self, steam_id, app_id, context_id, stop=None) -> int:
        self.count_calls.append((steam_id, app_id))
        return self.count_by_app.get(app_id, self.default_count)

    def fetch_inventory(self, steam_id, app_id=730, context_id=2, stop=None) -> list[dict]:
        self.inventory_calls.append((steam_id, app_id))
        value = self.inventories.get((steam_id, app_id), self.inventories.get(steam_id, []))
        if isinstance(value, Exception):
            raise value
        return list(value)

    def fetch_prices_parallel(self, client, names, **kwargs):
        self.price_calls.append(list(names))
        on_progress = kwargs.get("on_progress")
        collected: dict[str, float] = {}
        for idx, name in enumerate(names, 1):
            price = self.prices.get(name)
            if price is not None:
                collected[name] = price
            if on_progress:
                on_progress(idx, len(names), name, price)
        return collected, False


def _make_coordinator(
    cfg: dict | None = None,
    *,
    config_dir="/tmp/test_cs2",
    active_apps=(),
    last_discovery="fresh",
    current_prices: dict[str, float] | None = None,
    inv_cooldown: dict[str, float] | None = None,
) -> CS2Coordinator:
    """Build a CS2Coordinator ready for a real _sync_cycle call (no HA runtime)."""
    hass = MagicMock()
    hass.config.config_dir = str(config_dir)
    hass.loop = MagicMock()
    hass.bus = MagicMock()
    entry = MagicMock()
    entry.data = cfg if cfg is not None else {"steam_ids": ONE_ACCOUNT}
    entry.options = {}
    entry.entry_id = "test_entry_id"
    # object.__new__ bypasses the MagicMock base installed by tests/conftest.py
    c = object.__new__(CS2Coordinator)
    c.hass = hass
    c.config_entry = entry
    c._cfg = {**entry.data, **entry.options}
    c._active_apps = list(active_apps)
    c._last_discovery = coord_mod.dt_util.utcnow() if last_discovery == "fresh" else last_discovery
    c._price_tracker = RollingPriceFetcher()
    c._entity_pictures = {}
    c._current_prices = dict(current_prices or {})
    c._previous_prices = {}
    c._price_snapshots = {}
    c._float_cache = {}
    c._alert_state = {}
    c._inv_cooldown = dict(inv_cooldown or {})
    c._market_rl_until = 0.0
    c._market_rl_consecutive = 0
    c._stale_data = None
    c._stop = _FakeStop()
    c._import_running = False
    c._import_progress = {}
    c._last_cycle_stats = {}
    return c


def _item(name: str, *, marketable: bool = True, asset_id: str = "1", picture=None) -> dict:
    """Minimal Steam inventory asset as returned by fetch_inventory."""
    return {
        "market_hash_name": name,
        "name_color": None,
        "inspect_link": None,
        "entity_picture": picture,
        "asset_id": asset_id,
        "classid": "1",
        "instanceid": "0",
        "marketable": marketable,
    }


def _private(steam_id: str) -> Exception:
    return steam_inventory.InventoryPrivateError(
        f"Steam inventory {steam_id} is private (HTTP 403)"
    )


def _write_json(directory, filename: str, data) -> None:
    (directory / filename).write_text(json.dumps(data))


def _market_price(request: httpx.Request, price: float) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "lowest_price": f"{price:.2f} €"})


def _market_too_many_requests(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": "60"})


def _no_market_route(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch) -> _FakeClock:
    """_sync_cycle compares dt_util.utcnow() with _last_discovery."""
    clock = _FakeClock()
    monkeypatch.setattr(coord_mod, "dt_util", clock)
    return clock


@pytest.fixture(autouse=True)
def _no_market_sleep(monkeypatch) -> None:
    """Drop the inter-request pacing of steam_market (no real waiting in tests)."""
    monkeypatch.setattr(steam_market, "_sleep", lambda seconds, stop: False)


def _install_steam(monkeypatch, *, patch_market: bool = True, **kwargs) -> _SteamStub:
    """Stub the Steam APIs; ``patch_market=False`` keeps the real market client
    (used by the circuit-breaker tests, driven by the MockTransport handler)."""
    stub = _SteamStub(**kwargs)
    monkeypatch.setattr(steam_inventory, "check_inventory_count", stub.check_inventory_count)
    monkeypatch.setattr(steam_inventory, "fetch_inventory", stub.fetch_inventory)
    if patch_market:
        monkeypatch.setattr(steam_market, "fetch_prices_parallel", stub.fetch_prices_parallel)
    return stub


def _install_httpx(monkeypatch, handler) -> _HttpxShim:
    shim = _HttpxShim(handler)
    monkeypatch.setattr(coord_mod, "httpx", shim)
    return shim


def _item_by_name(result: dict, name: str) -> dict:
    return next(i for i in result["items"] if i["name"] == name)


# ── 1. Aucun jeu actif après découverte ──────────────────────────────────────

class TestNoActiveGame:
    def test_empty_result_and_no_save_payload(self, monkeypatch, tmp_path):
        stub = _install_steam(monkeypatch, default_count=0)
        shim = _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=(), last_discovery=None)

        result, payload = c._sync_cycle()

        assert result == _empty_result()
        assert payload is None
        assert result["global"]["total_value"] == 0.0
        assert result["items"] == []
        assert result["per_game"] == {}
        assert result["active_apps"] == []
        assert result["watchlist"] == []
        assert (result["stale_count"], result["missing_count"]) == (0, 0)
        assert stub.price_calls == []
        assert shim.calls == []

    def test_discovery_actually_ran(self, monkeypatch, tmp_path):
        stub = _install_steam(monkeypatch, default_count=0)
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=(), last_discovery=None)

        c._sync_cycle()

        probed = [appid for _sid, appid in stub.count_calls]
        assert probed == [app[0] for app in CANDIDATE_APPS]
        assert 753 not in probed  # Steam Cards stay opt-in
        assert stub.inventory_calls == []

    def test_discovery_reruns_while_no_active_app(self, monkeypatch, tmp_path):
        """A fresh _last_discovery does not skip discovery when nothing is active."""
        stub = _install_steam(monkeypatch, default_count=0)
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=(), last_discovery="fresh")

        c._sync_cycle()

        assert stub.count_calls

    def test_no_configured_account_short_circuits_discovery(self, monkeypatch, tmp_path):
        stub = _install_steam(monkeypatch, default_count=0)
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator({"steam_ids": ""}, config_dir=tmp_path, last_discovery=None)

        result, payload = c._sync_cycle()

        assert (result, payload) == (_empty_result(), None)
        assert stub.count_calls == []

    def test_cycle_stats_untouched_on_early_return(self, monkeypatch, tmp_path):
        _install_steam(monkeypatch, default_count=0)
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=(), last_discovery=None)

        c._sync_cycle()

        assert c._last_cycle_stats == {}

    def test_discovery_429_preserves_cached_apps(self, monkeypatch, tmp_path):
        """Sentinel -1 from check_inventory_count aborts discovery, keeps cache."""
        stub = _install_steam(
            monkeypatch,
            inventories={(ACCOUNT_A, 730): [_item(REDLINE)]},
            prices={REDLINE: 12.5},
            count_by_app={730: -1},  # -1 == 429 sentinel
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2], last_discovery=None)

        result, payload = c._sync_cycle()

        assert stub.count_calls == [(ACCOUNT_A, 730)]  # discovery stopped on the sentinel
        assert result["active_apps"] == [CS2]
        assert [i["name"] for i in result["items"]] == [REDLINE]
        assert stub.price_calls == [[REDLINE]]
        assert payload is not None


# ── 2. Compte Steam privé ne casse pas le cycle ──────────────────────────────

class TestPrivateAccountIsolation:
    def test_private_account_does_not_fail_whole_cycle(self, monkeypatch, tmp_path):
        stub = _install_steam(
            monkeypatch,
            inventories={
                (ACCOUNT_A, 730): _private(ACCOUNT_A),
                (ACCOUNT_B, 730): [_item(DRAGON_LORE, asset_id="b1")],
            },
            prices={DRAGON_LORE: 1200.0},
        )
        shim = _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(
            {"steam_ids": TWO_ACCOUNTS}, config_dir=tmp_path, active_apps=[CS2]
        )

        result, payload = c._sync_cycle()

        assert (ACCOUNT_A, 730) in stub.inventory_calls
        assert [i["name"] for i in result["items"]] == [DRAGON_LORE]
        assert result["per_game"]["cs2"]["items"][0]["current_price"] == pytest.approx(1200.0)
        assert result["global"]["total_value"] == pytest.approx(1200.0)
        assert payload is not None
        assert shim.calls == []

    def test_private_account_item_never_reaches_price_fetch(self, monkeypatch, tmp_path):
        stub = _install_steam(
            monkeypatch,
            inventories={
                (ACCOUNT_A, 730): [_item(REDLINE, asset_id="a1")],
                (ACCOUNT_B, 730): _private(ACCOUNT_B),
            },
            prices={REDLINE: 12.5},
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(
            {"steam_ids": TWO_ACCOUNTS}, config_dir=tmp_path, active_apps=[CS2]
        )

        result, _ = c._sync_cycle()

        assert stub.price_calls == [[REDLINE]]
        assert [i["name"] for i in result["items"]] == [REDLINE]

    def test_private_account_applies_no_cooldown(self, monkeypatch, tmp_path):
        """403/private is not an IP ban — no cooldown entry, unlike 401."""
        _install_steam(
            monkeypatch,
            inventories={
                (ACCOUNT_A, 730): _private(ACCOUNT_A),
                (ACCOUNT_B, 730): [_item(DRAGON_LORE)],
            },
            prices={DRAGON_LORE: 1200.0},
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(
            {"steam_ids": TWO_ACCOUNTS}, config_dir=tmp_path, active_apps=[CS2]
        )

        c._sync_cycle()

        assert c._inv_cooldown == {}
        assert c.last_cycle_stats["banned_accounts"] == 0

    def test_transient_fetch_error_isolated_like_private(self, monkeypatch, tmp_path):
        stub = _install_steam(
            monkeypatch,
            inventories={
                (ACCOUNT_A, 730): steam_inventory.InventoryFetchError("HTTP 500"),
                (ACCOUNT_B, 730): [_item(DRAGON_LORE)],
            },
            prices={DRAGON_LORE: 1200.0},
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(
            {"steam_ids": TWO_ACCOUNTS}, config_dir=tmp_path, active_apps=[CS2]
        )

        result, _ = c._sync_cycle()

        assert stub.price_calls == [[DRAGON_LORE]]
        assert [i["name"] for i in result["items"]] == [DRAGON_LORE]

    def test_all_accounts_private_returns_none_for_stale_fallback(self, monkeypatch, tmp_path):
        stub = _install_steam(
            monkeypatch,
            inventories={
                (ACCOUNT_A, 730): _private(ACCOUNT_A),
                (ACCOUNT_B, 730): _private(ACCOUNT_B),
            },
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(
            {"steam_ids": TWO_ACCOUNTS}, config_dir=tmp_path, active_apps=[CS2]
        )

        result, payload = c._sync_cycle()

        assert (result, payload) == (None, None)
        assert stub.price_calls == []
        assert len(stub.inventory_calls) == 2


# ── 3. Prix de référence (cs2_reference_prices.json) ──────────────────────────

class TestReferencePrices:
    def _refs(self, tmp_path, refs) -> None:
        _write_json(tmp_path, "cs2_reference_prices.json", refs)

    def test_missing_current_price_seeded_from_reference(self, monkeypatch, tmp_path):
        self._refs(tmp_path, {DRAGON_LORE: 1200.0, REDLINE: 90.0})
        _install_steam(
            monkeypatch,
            inventories={(ACCOUNT_A, 730): [_item(DRAGON_LORE), _item(REDLINE)]},
        )
        shim = _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(
            config_dir=tmp_path,
            active_apps=[CS2],
            current_prices={REDLINE: 42.5},
        )

        result, _ = c._sync_cycle()

        assert c._current_prices[DRAGON_LORE] == pytest.approx(1200.0)
        assert _item_by_name(result, DRAGON_LORE)["current_price"] == pytest.approx(1200.0)
        assert _item_by_name(result, DRAGON_LORE)["before_crash"] == pytest.approx(1200.0)
        assert shim.calls == []

    def test_existing_current_price_not_overwritten(self, monkeypatch, tmp_path):
        self._refs(tmp_path, {REDLINE: 90.0})
        _install_steam(monkeypatch, inventories={(ACCOUNT_A, 730): [_item(REDLINE)]})
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(
            config_dir=tmp_path, active_apps=[CS2], current_prices={REDLINE: 42.5}
        )

        result, _ = c._sync_cycle()

        assert c._current_prices[REDLINE] == pytest.approx(42.5)
        item = _item_by_name(result, REDLINE)
        assert item["current_price"] == pytest.approx(42.5)
        assert item["before_crash"] == pytest.approx(90.0)
        assert item["delta_since_crash"] == pytest.approx(42.5 - 90.0)

    def test_seeded_price_counts_as_stale_not_missing(self, monkeypatch, tmp_path):
        self._refs(tmp_path, {DRAGON_LORE: 1200.0})
        _install_steam(monkeypatch, inventories={(ACCOUNT_A, 730): [_item(DRAGON_LORE)]})
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2])

        result, _ = c._sync_cycle()

        assert result["missing_count"] == 0
        assert result["stale_count"] == 1
        assert result["global"]["total_value"] == pytest.approx(1200.0)
        assert result["global"]["items_with_price"] == 1

    def test_fresh_market_price_wins_over_reference(self, monkeypatch, tmp_path):
        self._refs(tmp_path, {REDLINE: 90.0})
        _install_steam(
            monkeypatch,
            inventories={(ACCOUNT_A, 730): [_item(REDLINE)]},
            prices={REDLINE: 12.5},
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2])

        result, _ = c._sync_cycle()

        item = _item_by_name(result, REDLINE)
        assert item["current_price"] == pytest.approx(12.5)
        assert item["before_crash"] == pytest.approx(90.0)
        assert result["stale_count"] == 0

    def test_zero_reference_price_is_not_seeded(self, monkeypatch, tmp_path):
        self._refs(tmp_path, {"Sticker | Zero": 0.0})
        _install_steam(
            monkeypatch, inventories={(ACCOUNT_A, 730): [_item("Sticker | Zero", marketable=False)]}
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2])

        result, _ = c._sync_cycle()

        item = _item_by_name(result, "Sticker | Zero")
        assert item["current_price"] is None
        assert item["before_crash"] is None
        assert result["missing_count"] == 1
        assert c._current_prices.get("Sticker | Zero", 0.0) == 0.0

    def test_non_marketable_item_tracked_when_referenced(self, monkeypatch, tmp_path):
        self._refs(tmp_path, {"Charm | Souvenir": 5.0})
        _install_steam(
            monkeypatch,
            inventories={
                (ACCOUNT_A, 730): [_item("Charm | Souvenir", marketable=False, asset_id="z1")]
            },
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2])

        result, _ = c._sync_cycle()

        assert [i["name"] for i in result["items"]] == ["Charm | Souvenir"]
        assert result["global"]["total_value"] == pytest.approx(5.0)

    def test_reference_prices_survive_current_prices_pruning(self, monkeypatch, tmp_path):
        self._refs(tmp_path, {DRAGON_LORE: 1200.0})
        _install_steam(
            monkeypatch, inventories={(ACCOUNT_A, 730): [_item(REDLINE)]}, prices={REDLINE: 12.5}
        )
        _install_httpx(monkeypatch, _no_market_route)
        c = _make_coordinator(
            config_dir=tmp_path, active_apps=[CS2], current_prices={"Sold Item": 7.0}
        )

        c._sync_cycle()

        assert "Sold Item" not in c._current_prices
        assert c._current_prices[DRAGON_LORE] == pytest.approx(1200.0)


# ── 4. Circuit-breaker Steam Market (429) ────────────────────────────────────

class TestMarketCircuitBreaker:
    def _two_games(self, monkeypatch):
        return _install_steam(
            monkeypatch,
            patch_market=False,
            inventories={
                (ACCOUNT_A, 730): [_item(REDLINE)],
                (ACCOUNT_A, 570): [_item(DRAGON_LORE)],
            },
        )

    def test_429_stops_price_calls_for_rest_of_cycle(self, monkeypatch, tmp_path):
        self._two_games(monkeypatch)
        shim = _install_httpx(monkeypatch, _market_too_many_requests)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2, DOTA])

        result, payload = c._sync_cycle()

        market_calls = [u for u in shim.calls if "priceoverview" in u]
        assert len(market_calls) == 1
        assert "appid=730" in market_calls[0]
        assert not [u for u in shim.calls if "appid=570" in u]
        assert result is not None
        assert result["global"]["total_value"] == 0.0
        assert payload is not None

    def test_429_sets_backoff_and_increments_counter(self, monkeypatch, tmp_path):
        self._two_games(monkeypatch)
        _install_httpx(monkeypatch, _market_too_many_requests)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2, DOTA])

        before = time.time()
        c._sync_cycle()

        assert c._market_rl_consecutive == 1
        assert c._market_rl_until == pytest.approx(before + 5 * 60, abs=10)
        stats = c.last_cycle_stats
        assert stats["market_rl_consecutive"] == 1
        assert stats["market_rl_until"] == pytest.approx(before + 5 * 60, abs=10)

    def test_prices_collected_before_429_are_kept(self, monkeypatch, tmp_path):
        """The aborted pass returns the prices gathered before the 429."""
        _install_steam(
            monkeypatch,
            patch_market=False,
            inventories={
                (ACCOUNT_A, 730): [_item(ASIMOV), _item(REDLINE)],
                (ACCOUNT_A, 570): [_item(DRAGON_LORE)],
            },
        )

        def _handler(request: httpx.Request) -> httpx.Response:
            return (
                _market_too_many_requests(request)
                if "Redline" in str(request.url)
                else _market_price(request, 200.0)
            )

        shim = _install_httpx(monkeypatch, _handler)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2, DOTA])

        result, _ = c._sync_cycle()

        # CS2 first: Asiimov priced, then the 429 on Redline aborts the pass.
        assert _item_by_name(result, ASIMOV)["current_price"] == pytest.approx(200.0)
        assert _item_by_name(result, REDLINE)["current_price"] is None
        assert _item_by_name(result, DRAGON_LORE)["current_price"] is None
        assert result["missing_count"] == 2
        assert c._market_rl_consecutive == 1
        assert len([u for u in shim.calls if "appid=730" in u]) == 2
        assert not [u for u in shim.calls if "appid=570" in u]

    def test_watchlist_fetch_skipped_after_429(self, monkeypatch, tmp_path):
        self._two_games(monkeypatch)
        _write_json(tmp_path, WATCHLIST_FILE, [{"market_hash_name": DRAGON_LORE, "note": "watch"}])
        shim = _install_httpx(monkeypatch, _market_too_many_requests)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2, DOTA])

        result, _ = c._sync_cycle()

        assert len([u for u in shim.calls if "priceoverview" in u]) == 1
        assert result["watchlist"][0]["market_hash_name"] == DRAGON_LORE
        assert result["watchlist"][0]["current_price"] is None

    def test_429_does_not_raise(self, monkeypatch, tmp_path):
        """The _CycleAbort raised in the worker thread stays inside the market client."""
        self._two_games(monkeypatch)
        _install_httpx(monkeypatch, _market_too_many_requests)
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2, DOTA])

        assert c._sync_cycle() is not None

    def test_existing_cooldown_skips_every_market_call(self, monkeypatch, tmp_path):
        self._two_games(monkeypatch)
        shim = _install_httpx(monkeypatch, _no_market_route)
        cooldown_until = time.time() + 120
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2, DOTA])
        c._market_rl_until = cooldown_until
        c._market_rl_consecutive = 2

        result, _ = c._sync_cycle()

        assert shim.calls == []
        assert c._market_rl_until == cooldown_until
        assert c._market_rl_consecutive == 2  # not reset while cooling down
        assert result["missing_count"] == 2
        assert result["stale_count"] == 2

    def test_clean_cycle_resets_backoff_counter(self, monkeypatch, tmp_path):
        self._two_games(monkeypatch)
        _install_httpx(monkeypatch, lambda request: _market_price(request, 10.0))
        c = _make_coordinator(config_dir=tmp_path, active_apps=[CS2, DOTA])
        c._market_rl_consecutive = 3

        result, _ = c._sync_cycle()

        assert c._market_rl_consecutive == 0
        assert c._market_rl_until == 0.0
        assert result["global"]["total_value"] == pytest.approx(20.0)
