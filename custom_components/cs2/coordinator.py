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
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STRICT_RATIO,
    DEFAULT_MIN_VALUE,
    DEFAULT_MAX_ITEMS,
    KNOWN_MARKETABLE_APPS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .slugify import make_slug

_LOGGER = logging.getLogger(__name__)

# Re-run discovery after this interval
_DISCOVERY_INTERVAL = datetime.timedelta(days=7)


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

    async def _async_save_store(
        self,
        new_prices: dict[str, float],
        active_apps: list[tuple[int, int, str, str]],
    ) -> None:
        self._previous_prices = {**self._current_prices}
        self._current_prices = {**new_prices}
        self._active_apps = active_apps
        self._last_discovery = datetime.datetime.now()
        await self._store.async_save(
            {
                "entity_pictures": self._entity_pictures,
                "current_prices": self._current_prices,
                "previous_prices": self._previous_prices,
                "active_apps": [list(a) for a in active_apps],
                "last_discovery": self._last_discovery.isoformat(),
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
                time.sleep(0.5)  # gentle pacing

        result = list(active.values())
        _LOGGER.info("Discovery complete: %d active games", len(result))
        return result

    # ── Main cycle ────────────────────────────────────────────────────────────

    def _sync_cycle(self) -> dict[str, Any]:
        """Synchronous cycle — runs in executor thread."""
        cfg = self._cfg
        cap = int(cfg.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
        strict_ratio = float(cfg.get(CONF_STRICT_MISSING_RATIO, DEFAULT_STRICT_RATIO))
        min_val = self.min_item_value
        config_dir = self.hass.config.config_dir

        buy_prices = _load_json_prices(config_dir, "cs2_buy_prices.json")
        reference_prices = _load_json_prices(config_dir, "cs2_reference_prices.json")
        tracked_extras = set(buy_prices) | set(reference_prices)

        with httpx.Client() as http:
            # ── Discovery ─────────────────────────────────────────────────────
            active_apps = list(self._active_apps)
            if self._needs_discovery():
                active_apps = self._discover_active_apps(http)

            if not active_apps:
                return _empty_result()

            previous_prices = dict(self._previous_prices)
            limits = RateLimits()
            per_game_data: dict[str, dict] = {}
            all_items_flat: list[dict] = []
            all_fresh_prices: dict[str, float] = {}
            total_stale = 0
            total_missing = 0

            for appid, contextid, slug, game_name in active_apps:
                if self._stop.is_set():
                    break

                # ── Fetch inventories for this game ────────────────────────────
                merged: dict[str, dict] = {}
                for steam_id, account_name in self.accounts:
                    raw = steam_inventory.fetch_inventory(
                        http, steam_id, app_id=appid, context_id=contextid,
                        stop=self._stop,
                    )
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

                inventory = list(merged.values())
                # Sort by known price descending before capping so max_items
                # keeps the most valuable items, not random inventory order
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
                    floats={},
                    previous_prices=previous_prices,
                    buy_prices=buy_prices,
                    reference_prices=reference_prices,
                )

                for item in items_data:
                    if not item.get("entity_picture"):
                        item["entity_picture"] = self._entity_pictures.get(item["name"])
                    item["game_slug"] = slug
                    item["game_name"] = game_name

                # Apply min_value filter
                if min_val > 0:
                    items_data = [
                        i for i in items_data
                        if (i.get("current_price") or 0) >= min_val
                        or (i.get("buy_price") or 0) >= min_val
                    ]

                # Skip game if no items pass the threshold
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

            # ── Global metrics (flat list of all games' items) ─────────────────
            prev_global = self._previous_total(previous_prices, all_items_flat)
            global_metrics = compute_global_metrics(
                all_items_flat, previous_total=prev_global
            )

        # Persist state
        self.hass.loop.call_soon_threadsafe(
            lambda apps=active_apps, prices=all_fresh_prices:
                self.hass.async_create_task(self._async_save_store(prices, apps))
        )

        _LOGGER.info(
            "Steam cycle: total=%.2f EUR, games=%d, items=%d, stale=%d, missing=%d",
            global_metrics["total_value"],
            len(per_game_data),
            global_metrics["items_count"],
            total_stale,
            total_missing,
        )

        return {
            "global": global_metrics,
            "per_game": per_game_data,
            "items": all_items_flat,
            "per_account": {},
            "active_apps": active_apps,
            "stale_count": total_stale,
            "missing_count": total_missing,
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
    }
