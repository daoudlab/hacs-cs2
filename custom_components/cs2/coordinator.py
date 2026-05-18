"""DataUpdateCoordinator for CS2/Steam Inventory."""
from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import steam_inventory, steam_market
from .api.steam_market import RateLimits
from .compute import compute_item_metrics, compute_global_metrics
from .const import (
    DOMAIN,
    CONF_STEAM_IDS,
    CONF_SCAN_INTERVAL,
    CONF_STRICT_MISSING_RATIO,
    CONF_MIN_ITEM_VALUE,
    CONF_MAX_ITEMS,
    CONF_INCLUDE_TRADING_CARDS,
    CONF_FETCH_FLOATS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STRICT_RATIO,
    DEFAULT_MIN_VALUE,
    DEFAULT_MAX_ITEMS,
    KNOWN_MARKETABLE_APPS,
    STORAGE_KEY,
    STORAGE_VERSION,
    WATCHLIST_FILE,
    TARGETS_FILE,
)
from .slugify import make_slug

_LOGGER = logging.getLogger(__name__)

_DISCOVERY_INTERVAL = datetime.timedelta(days=7)
_SNAPSHOT_KEEP_DAYS = 10


def _parse_steam_ids(raw: str) -> list[tuple[str, str]]:
    accounts = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            sid, name = part.split(":", 1)
            accounts.append((sid.strip(), name.strip()))
        else:
            accounts.append((part, f"account_{part[-8:]}"))
    return accounts


def _load_json_prices(config_dir: str, filename: str) -> dict[str, float]:
    """Load buy or reference prices from {config_dir}/{filename} if present."""
    path = Path(config_dir) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {str(k): float(v) for k, v in data.items() if v is not None}
    except Exception as err:
        _LOGGER.warning("Failed to load %s: %s", path, err)
        return {}


def _load_json_list(config_dir: str, filename: str) -> list[dict]:
    """Load a JSON list from {config_dir}/{filename} if present."""
    path = Path(config_dir) / filename
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception as err:
        _LOGGER.warning("Failed to load %s: %s", path, err)
        return []


def _load_json_targets(config_dir: str, filename: str) -> dict[str, dict]:
    """Load price targets from {config_dir}/{filename} if present."""
    path = Path(config_dir) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as err:
        _LOGGER.warning("Failed to load %s: %s", path, err)
        return {}


class CS2Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches Steam inventories + prices for all detected games."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        cfg = {**entry.data, **entry.options}
        interval = cfg.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(minutes=interval),
        )
        self._cfg = cfg
        self._store = storage.Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._entity_pictures: dict[str, str] = {}
        self._current_prices: dict[str, float] = {}
        self._previous_prices: dict[str, float] = {}
        self._active_apps: list[tuple[int, int, str, str]] = []
        self._last_discovery: datetime.datetime | None = None
        self._price_snapshots: dict[str, dict[str, float]] = {}
        self._float_cache: dict[str, float] = {}
        self._last_cycle_stats: dict[str, Any] = {}
        self._stop = threading.Event()

    def stop(self) -> None:
        """Signal running executor thread to stop sleeping."""
        self._stop.set()

    @property
    def accounts(self) -> list[tuple[str, str]]:
        return _parse_steam_ids(self._cfg.get(CONF_STEAM_IDS, ""))

    @property
    def min_item_value(self) -> float:
        return float(self._cfg.get(CONF_MIN_ITEM_VALUE, DEFAULT_MIN_VALUE))

    @property
    def include_trading_cards(self) -> bool:
        return bool(self._cfg.get(CONF_INCLUDE_TRADING_CARDS, False))

    @property
    def fetch_floats(self) -> bool:
        return bool(self._cfg.get(CONF_FETCH_FLOATS, False))

    @property
    def last_cycle_stats(self) -> dict[str, Any]:
        return dict(self._last_cycle_stats)

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _async_load_store(self) -> None:
        data = await self._store.async_load() or {}
        self._entity_pictures = data.get("entity_pictures", {})
        self._current_prices = data.get("current_prices", {})
        self._previous_prices = data.get("previous_prices", {})
        raw_apps = data.get("active_apps", [])
        self._active_apps = [tuple(a) for a in raw_apps]
        last_disc = data.get("last_discovery")
        self._last_discovery = (
            datetime.datetime.fromisoformat(last_disc) if last_disc else None
        )
        self._price_snapshots = data.get("price_snapshots", {})
        self._float_cache = data.get("float_cache", {})

    async def _async_save_store(
        self,
        new_prices: dict[str, float],
        active_apps: list[tuple[int, int, str, str]],
        price_snapshots: dict[str, dict[str, float]],
        float_cache: dict[str, float],
    ) -> None:
        self._previous_prices = {**self._current_prices}
        self._current_prices = {**new_prices}
        self._active_apps = active_apps
        self._last_discovery = datetime.datetime.now()
        self._price_snapshots = price_snapshots
        self._float_cache = float_cache
        await self._store.async_save(
            {
                "entity_pictures": self._entity_pictures,
                "current_prices": self._current_prices,
                "previous_prices": self._previous_prices,
                "active_apps": [list(a) for a in active_apps],
                "last_discovery": self._last_discovery.isoformat(),
                "price_snapshots": price_snapshots,
                "float_cache": float_cache,
            }
        )

    # ── Core update ───────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        self._stop.clear()
        await self._async_load_store()
        try:
            return await self.hass.async_add_executor_job(self._sync_cycle)
        except steam_inventory.InventoryPrivateError as err:
            raise UpdateFailed(f"Steam inventory private: {err}") from err
        except steam_inventory.InventoryFetchError as err:
            # Transient IP ban — keep showing last known values instead of error state
            _LOGGER.warning("Inventory fetch failed: %s — reusing stale data", err)
            if self.data is not None:
                return self.data
            raise UpdateFailed(f"Steam inventory fetch error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Cycle failed: {err}") from err

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _needs_discovery(self) -> bool:
        if not self._active_apps:
            return True
        if self._last_discovery is None:
            return True
        return (datetime.datetime.now() - self._last_discovery) > _DISCOVERY_INTERVAL

    def _discover_active_apps(
        self, http: httpx.Client
    ) -> list[tuple[int, int, str, str]]:
        """Probe known app IDs to find which ones have non-empty inventories."""
        include_cards = self.include_trading_cards
        candidates = [
            app for app in KNOWN_MARKETABLE_APPS
            if app[0] != 753 or include_cards
        ]
        active: dict[str, tuple[int, int, str, str]] = {}

        for steam_id, _ in self.accounts:
            for appid, contextid, slug, game_name in candidates:
                if slug in active:
                    continue
                count = steam_inventory.check_inventory_count(
                    http, steam_id, appid, contextid, stop=self._stop
                )
                if count > 0:
                    active[slug] = (appid, contextid, slug, game_name)
                    _LOGGER.info(
                        "Discovery: found %s (appid=%d, %d items)",
                        game_name, appid, count,
                    )
                time.sleep(0.5)

        result = list(active.values())
        _LOGGER.info("Discovery complete: %d active games", len(result))
        return result

    # ── CSGOFloat ─────────────────────────────────────────────────────────────

    def _fetch_floats_for_game(
        self, http: httpx.Client, items: list[dict], appid: int
    ) -> dict[str, float]:
        """Fetch float values for CS2 skins (appid=730) with inspect links."""
        if appid != 730 or not self.fetch_floats:
            return {}
        from .api.csgofloat import fetch_floats
        floats = fetch_floats(http, items, cached=self._float_cache, stop=self._stop)
        # Update persistent cache (asset_id → float)
        for item in items:
            name = item.get("market_hash_name", "")
            asset_id = item.get("asset_id", name)
            if name in floats:
                self._float_cache[asset_id] = floats[name]
        return floats

    # ── Price thresholds ──────────────────────────────────────────────────────

    def _check_price_alerts(
        self,
        prices: dict[str, float],
        targets: dict[str, dict],
    ) -> None:
        """Fire HA events when item prices cross configured thresholds."""
        for name, thresholds in targets.items():
            price = prices.get(name)
            if price is None:
                continue
            high = thresholds.get("high")
            low = thresholds.get("low")
            if high is not None and price >= high:
                self.hass.loop.call_soon_threadsafe(
                    self.hass.bus.async_fire,
                    "steam_price_alert",
                    {
                        "market_hash_name": name,
                        "current_price": price,
                        "threshold_type": "high",
                        "threshold_value": high,
                    },
                )
                _LOGGER.info("Price alert HIGH: %s = %.2f >= %.2f", name, price, high)
            if low is not None and price <= low:
                self.hass.loop.call_soon_threadsafe(
                    self.hass.bus.async_fire,
                    "steam_price_alert",
                    {
                        "market_hash_name": name,
                        "current_price": price,
                        "threshold_type": "low",
                        "threshold_value": low,
                    },
                )
                _LOGGER.info("Price alert LOW: %s = %.2f <= %.2f", name, price, low)

    # ── Main cycle ────────────────────────────────────────────────────────────

    def _sync_cycle(self) -> dict[str, Any]:
        """Synchronous cycle — runs in executor thread."""
        cycle_start = time.monotonic()
        cfg = self._cfg
        cap = int(cfg.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
        strict_ratio = float(cfg.get(CONF_STRICT_MISSING_RATIO, DEFAULT_STRICT_RATIO))
        min_val = self.min_item_value
        config_dir = self.hass.config.config_dir

        buy_prices = _load_json_prices(config_dir, "cs2_buy_prices.json")
        reference_prices = _load_json_prices(config_dir, "cs2_reference_prices.json")
        tracked_extras = set(buy_prices) | set(reference_prices)

        watchlist = _load_json_list(config_dir, WATCHLIST_FILE)
        price_targets = _load_json_targets(config_dir, TARGETS_FILE)

        # Compute 24h / 7d reference dates
        today = datetime.date.today().isoformat()
        date_24h = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        date_7d = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        prices_24h = self._price_snapshots.get(date_24h, {})
        prices_7d = self._price_snapshots.get(date_7d, {})

        with httpx.Client() as http:
            # ── Discovery ─────────────────────────────────────────────────────
            active_apps = list(self._active_apps)
            if self._needs_discovery():
                active_apps = self._discover_active_apps(http)

            if not active_apps:
                return _empty_result()

            previous_prices = dict(self._previous_prices)
            limits = RateLimits.coordinator()
            per_game_data: dict[str, dict] = {}
            all_items_flat: list[dict] = []
            all_fresh_prices: dict[str, float] = {}
            total_stale = 0
            total_missing = 0
            float_cache = dict(self._float_cache)

            for appid, contextid, slug, game_name in active_apps:
                if self._stop.is_set():
                    break

                # ── Fetch inventories for this game ────────────────────────────
                merged: dict[str, dict] = {}
                accounts_ok = 0
                for steam_id, account_name in self.accounts:
                    try:
                        raw = steam_inventory.fetch_inventory(
                            http, steam_id, app_id=appid, context_id=contextid,
                            stop=self._stop,
                        )
                        accounts_ok += 1
                    except steam_inventory.InventoryFetchError as err:
                        _LOGGER.warning("Inventory fetch failed for %s: %s — skipping", account_name, err)
                        continue
                    trackable = [
                        item for item in raw
                        if item.get("marketable")
                        or item["market_hash_name"] in tracked_extras
                    ]
                    for item in trackable:
                        name = item["market_hash_name"]
                        if name not in merged:
                            merged[name] = {**item}
                        else:
                            merged[name]["_qty"] = merged[name].get("_qty", 1) + 1
                        pic = item.get("entity_picture")
                        if pic:
                            self._entity_pictures[name] = pic

                if accounts_ok == 0 and self.accounts:
                    # All accounts failed — propagate so coordinator can return stale data
                    raise steam_inventory.InventoryFetchError(
                        f"All {len(self.accounts)} accounts failed inventory fetch for {game_name}"
                    )

                inventory = list(merged.values())
                if cap > 0:
                    inventory.sort(
                        key=lambda i: max(
                            buy_prices.get(i["market_hash_name"], 0.0),
                            reference_prices.get(i["market_hash_name"], 0.0),
                        ),
                        reverse=True,
                    )
                unique_names = [i["market_hash_name"] for i in inventory]
                names_to_fetch = unique_names[:cap] if cap > 0 else unique_names

                # ── Fetch floats (CS2 only, opt-in) ───────────────────────────
                floats = self._fetch_floats_for_game(
                    http,
                    [i for i in inventory if i["market_hash_name"] in names_to_fetch],
                    appid,
                )

                # ── Fetch prices ───────────────────────────────────────────────
                prices = steam_market.fetch_prices(
                    http, names_to_fetch,
                    limits=limits, stop=self._stop, app_id=appid,
                )

                stale_used = []
                for name in [n for n in names_to_fetch if n not in prices]:
                    if name in previous_prices:
                        prices[name] = previous_prices[name]
                        stale_used.append(name)
                still_missing = [n for n in names_to_fetch if n not in prices]
                total_stale += len(stale_used)
                total_missing += len(still_missing)

                ratio = len(still_missing) / max(len(names_to_fetch), 1)
                if ratio > strict_ratio:
                    _LOGGER.warning(
                        "%s: %d/%d prices missing (%.0f%%>strict %.0f%%), using stale",
                        game_name, len(still_missing), len(names_to_fetch),
                        ratio * 100, strict_ratio * 100,
                    )

                fresh_prices = {k: v for k, v in prices.items() if k not in stale_used}
                all_fresh_prices.update(fresh_prices)

                # ── Compute per-game metrics ───────────────────────────────────
                items_data = compute_item_metrics(
                    inventory=[i for i in inventory if i["market_hash_name"] in names_to_fetch],
                    prices=prices,
                    floats=floats,
                    previous_prices=previous_prices,
                    buy_prices=buy_prices,
                    reference_prices=reference_prices,
                    prices_24h=prices_24h,
                    prices_7d=prices_7d,
                )

                for item in items_data:
                    if not item.get("entity_picture"):
                        item["entity_picture"] = self._entity_pictures.get(item["name"])
                    item["game_slug"] = slug
                    item["game_name"] = game_name

                if min_val > 0:
                    items_data = [
                        i for i in items_data
                        if (i.get("current_price") or 0) >= min_val
                        or (i.get("buy_price") or 0) >= min_val
                    ]

                if not items_data:
                    _LOGGER.info(
                        "Skipping %s — no items above min_value=%.2f EUR",
                        game_name, min_val,
                    )
                    continue

                prev_total = self._previous_total(previous_prices, items_data)
                game_metrics = compute_global_metrics(items_data, previous_total=prev_total)

                per_game_data[slug] = {
                    "appid": appid,
                    "name": game_name,
                    "slug": slug,
                    "items": items_data,
                    "metrics": game_metrics,
                }
                all_items_flat.extend(items_data)

            # ── Watchlist prices ───────────────────────────────────────────────
            watchlist_prices: dict[str, float] = {}
            watchlist_names = [
                w["market_hash_name"] for w in watchlist
                if "market_hash_name" in w
                and w["market_hash_name"] not in all_fresh_prices
            ]
            if watchlist_names and not self._stop.is_set():
                watchlist_prices = steam_market.fetch_prices(
                    http, watchlist_names,
                    limits=limits, stop=self._stop, app_id=730,
                )
                all_fresh_prices.update(watchlist_prices)

            # ── Price threshold alerts ─────────────────────────────────────────
            if price_targets:
                combined_prices = {**all_fresh_prices}
                self._check_price_alerts(combined_prices, price_targets)

            # ── Global metrics ─────────────────────────────────────────────────
            prev_global = self._previous_total(previous_prices, all_items_flat)
            global_metrics = compute_global_metrics(
                all_items_flat, previous_total=prev_global
            )

            # ── Price snapshot for today ───────────────────────────────────────
            snapshots = {**self._price_snapshots, today: dict(all_fresh_prices)}
            cutoff = (
                datetime.date.today() - datetime.timedelta(days=_SNAPSHOT_KEEP_DAYS)
            ).isoformat()
            snapshots = {k: v for k, v in snapshots.items() if k >= cutoff}

        cycle_duration = round(time.monotonic() - cycle_start, 1)

        self._last_cycle_stats = {
            "total_value": global_metrics["total_value"],
            "items_count": global_metrics["items_count"],
            "active_games": len(per_game_data),
            "stale_count": total_stale,
            "missing_count": total_missing,
            "cycle_duration_s": cycle_duration,
            "last_update": datetime.datetime.now().isoformat(),
        }

        # Persist state
        self.hass.loop.call_soon_threadsafe(
            lambda apps=active_apps, prices=all_fresh_prices, snaps=snapshots, fc=float_cache:
                self.hass.async_create_task(
                    self._async_save_store(prices, apps, snaps, fc)
                )
        )

        _LOGGER.info(
            "Steam cycle: total=%.2f EUR, games=%d, items=%d, stale=%d, missing=%d, %.1fs",
            global_metrics["total_value"],
            len(per_game_data),
            global_metrics["items_count"],
            total_stale,
            total_missing,
            cycle_duration,
        )

        return {
            "global": global_metrics,
            "per_game": per_game_data,
            "items": all_items_flat,
            "per_account": {},
            "active_apps": active_apps,
            "stale_count": total_stale,
            "missing_count": total_missing,
            "watchlist": [
                {
                    "market_hash_name": w["market_hash_name"],
                    "appid": w.get("appid", 730),
                    "current_price": watchlist_prices.get(w["market_hash_name"]),
                    "target_price": w.get("target_price"),
                    "note": w.get("note", ""),
                    "slug": make_slug(w["market_hash_name"]),
                }
                for w in watchlist
                if "market_hash_name" in w
            ],
        }

    @staticmethod
    def _previous_total(previous_prices: dict, items_data: list[dict]) -> float | None:
        if not previous_prices:
            return None
        present = {i["name"] for i in items_data}
        qty_map = {i["name"]: i.get("quantity", 1) for i in items_data}
        total = sum(
            price * qty_map[name]
            for name, price in previous_prices.items()
            if name in present
        )
        return round(total, 2) if total else None


def _empty_result() -> dict[str, Any]:
    return {
        "global": compute_global_metrics([], previous_total=None),
        "per_game": {},
        "items": [],
        "per_account": {},
        "active_apps": [],
        "stale_count": 0,
        "missing_count": 0,
        "watchlist": [],
    }
