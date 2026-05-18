"""DataUpdateCoordinator for CS2 Inventory."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import steam_inventory, steam_market, csgofloat
from .api.steam_market import RateLimits
from .compute import compute_item_metrics, compute_global_metrics
from .const import (
    DOMAIN,
    CONF_STEAM_IDS,
    CONF_SCAN_INTERVAL,
    CONF_STRICT_MISSING_RATIO,
    CONF_MIN_ITEM_VALUE,
    CONF_MAX_ITEMS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STRICT_RATIO,
    DEFAULT_MIN_VALUE,
    DEFAULT_MAX_ITEMS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .slugify import make_slug

_LOGGER = logging.getLogger(__name__)


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
    """Fetches Steam inventory + prices, computes portfolio metrics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        cfg = {**entry.data, **entry.options}
        interval = cfg.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=interval),
        )
        self._cfg = cfg
        self._store = storage.Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._entity_pictures: dict[str, str] = {}   # name → url (persisted)
        self._current_prices: dict[str, float] = {}
        self._previous_prices: dict[str, float] = {}

    @property
    def accounts(self) -> list[tuple[str, str]]:
        return _parse_steam_ids(self._cfg.get(CONF_STEAM_IDS, ""))

    @property
    def min_item_value(self) -> float:
        return float(self._cfg.get(CONF_MIN_ITEM_VALUE, DEFAULT_MIN_VALUE))

    # ── Persistence (entity_picture + price cache) ────────────────────────────

    async def _async_load_store(self) -> None:
        data = await self._store.async_load() or {}
        self._entity_pictures = data.get("entity_pictures", {})
        self._current_prices = data.get("current_prices", {})
        self._previous_prices = data.get("previous_prices", {})

    async def _async_save_store(self, new_prices: dict[str, float]) -> None:
        # Rotate: current → previous, new → current
        self._previous_prices = {**self._current_prices}
        self._current_prices = {**new_prices}
        await self._store.async_save(
            {
                "entity_pictures": self._entity_pictures,
                "current_prices": self._current_prices,
                "previous_prices": self._previous_prices,
            }
        )

    # ── Core update ───────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        await self._async_load_store()
        try:
            return await self.hass.async_add_executor_job(self._sync_cycle)
        except steam_inventory.InventoryPrivateError as err:
            raise UpdateFailed(f"Steam inventory private: {err}") from err
        except steam_inventory.InventoryFetchError as err:
            raise UpdateFailed(f"Steam inventory fetch error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Cycle failed: {err}") from err

    def _sync_cycle(self) -> dict[str, Any]:
        """Synchronous cycle — runs in executor thread."""
        cfg = self._cfg
        cap = int(cfg.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
        strict_ratio = float(cfg.get(CONF_STRICT_MISSING_RATIO, DEFAULT_STRICT_RATIO))
        config_dir = self.hass.config.config_dir

        buy_prices = _load_json_prices(config_dir, "cs2_buy_prices.json")
        reference_prices = _load_json_prices(config_dir, "cs2_reference_prices.json")
        tracked_extras = set(buy_prices) | set(reference_prices)

        with httpx.Client() as http:
            # ── Fetch inventories ──────────────────────────────────────────────
            per_account: dict[str, list[dict]] = {}
            for steam_id, account_name in self.accounts:
                raw = steam_inventory.fetch_inventory(http, steam_id)
                trackable = [
                    item for item in raw
                    if item.get("is_skin") or item["market_hash_name"] in tracked_extras
                ]
                per_account[account_name] = trackable
                _LOGGER.debug("Account %s: %d raw, %d trackable", account_name, len(raw), len(trackable))

            # ── Merge across accounts ──────────────────────────────────────────
            merged: dict[str, dict] = {}
            for items in per_account.values():
                for item in items:
                    name = item["market_hash_name"]
                    if name not in merged:
                        merged[name] = {**item}
                    else:
                        merged[name]["_qty"] = merged[name].get("_qty", 1) + 1
                    # Update entity_picture cache
                    pic = item.get("entity_picture")
                    if pic:
                        self._entity_pictures[name] = pic

            inventory = list(merged.values())
            unique_names = list(merged.keys())
            names_to_fetch = unique_names[:cap] if cap > 0 and len(unique_names) > cap else unique_names

            # ── Previous prices (fallback + delta) ────────────────────────────
            previous_prices = dict(self._previous_prices)

            # ── Fetch market prices ────────────────────────────────────────────
            limits = RateLimits()
            prices = steam_market.fetch_prices(http, names_to_fetch, limits=limits)
            missing = [n for n in names_to_fetch if n not in prices]

            # Stale fallback
            stale_used = []
            for name in missing:
                if name in previous_prices:
                    prices[name] = previous_prices[name]
                    stale_used.append(name)
            still_missing = [n for n in names_to_fetch if n not in prices]

            ratio = len(still_missing) / max(len(names_to_fetch), 1)
            if ratio > strict_ratio:
                raise UpdateFailed(
                    f"{len(still_missing)}/{len(names_to_fetch)} prices missing "
                    f"({ratio:.0%} > strict {strict_ratio:.0%})"
                )

            # Save fresh prices (not stale) — done async after executor returns
            fresh_prices = {k: v for k, v in prices.items() if k not in stale_used}

            # ── Floats ────────────────────────────────────────────────────────
            floats: dict[str, float] = {}
            # (skipped for now — csgofloat requires per-asset API calls)

            # ── Compute combined metrics ───────────────────────────────────────
            items_data = compute_item_metrics(
                inventory=[i for i in inventory if i["market_hash_name"] in names_to_fetch],
                prices=prices,
                floats=floats,
                previous_prices=previous_prices,
                buy_prices=buy_prices,
                reference_prices=reference_prices,
            )

            # Inject stored entity_picture into items_data
            for item in items_data:
                if not item.get("entity_picture"):
                    item["entity_picture"] = self._entity_pictures.get(item["name"])

            # Apply min_item_value filter for display (keeps data but marks items)
            min_val = self.min_item_value
            if min_val > 0:
                items_data = [
                    i for i in items_data
                    if (i.get("current_price") or 0) >= min_val
                    or (i.get("buy_price") or 0) >= min_val
                ]

            previous_total = self._previous_total(previous_prices, items_data)
            global_metrics = compute_global_metrics(items_data, previous_total=previous_total)

            # ── Per-account metrics ────────────────────────────────────────────
            account_totals: dict[str, dict] = {}
            if len(self.accounts) > 1:
                for account_name, acct_items in per_account.items():
                    acct_data = compute_item_metrics(
                        inventory=[i for i in acct_items if i["market_hash_name"] in names_to_fetch],
                        prices=prices,
                        floats=floats,
                        previous_prices=previous_prices,
                        buy_prices=buy_prices,
                        reference_prices=reference_prices,
                    )
                    acct_prev = self._previous_total(previous_prices, acct_data)
                    account_totals[account_name] = compute_global_metrics(
                        acct_data, previous_total=acct_prev
                    )

        # Save price state (runs back in event loop via executor result)
        self.hass.loop.call_soon_threadsafe(
            lambda: self.hass.async_create_task(
                self._async_save_store(fresh_prices)
            )
        )

        _LOGGER.info(
            "CS2 cycle: total=%.2f EUR, items=%d, stale=%d, missing=%d",
            global_metrics["total_value"],
            global_metrics["items_count"],
            len(stale_used),
            len(still_missing),
        )

        return {
            "items": items_data,
            "global": global_metrics,
            "per_account": account_totals,
            "stale_count": len(stale_used),
            "missing_count": len(still_missing),
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
