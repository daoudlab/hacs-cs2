"""Steam Inventory sensor entities (multi-game)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN
from .coordinator import CS2Coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CS2Coordinator = hass.data[DOMAIN][entry.entry_id]

    # Always-present sensors
    entities: list[SensorEntity] = [
        SteamTotalSensor(coordinator),
        SteamSyncSensor(coordinator),
    ]
    known_game_slugs: set[str] = set()
    known_item_slugs: set[str] = set()
    known_watch_slugs: set[str] = set()

    data = coordinator.data or {}
    for slug, game in data.get("per_game", {}).items():
        entities.append(SteamGameSensor(coordinator, slug, game["name"]))
        known_game_slugs.add(slug)

    for item in data.get("items", []):
        item_slug = item["slug"]
        game_slug = item.get("game_slug", "")
        key = f"{game_slug}__{item_slug}"
        if key not in known_item_slugs:
            entities.append(SteamItemSensor(coordinator, item_slug, item["name"], game_slug))
            known_item_slugs.add(key)

    for watch in data.get("watchlist", []):
        w_slug = watch.get("slug", "")
        if w_slug and w_slug not in known_watch_slugs:
            entities.append(SteamWatchlistSensor(coordinator, w_slug, watch["market_hash_name"]))
            known_watch_slugs.add(w_slug)

    async_add_entities(entities)

    # Dynamically add new sensors as coordinator data arrives
    def _handle_coordinator_update() -> None:
        new: list[SensorEntity] = []
        _data = coordinator.data or {}

        for slug, game in _data.get("per_game", {}).items():
            if slug not in known_game_slugs:
                e = SteamGameSensor(coordinator, slug, game["name"])
                known_game_slugs.add(slug)
                new.append(e)

        for item in _data.get("items", []):
            item_slug = item["slug"]
            game_slug = item.get("game_slug", "")
            key = f"{game_slug}__{item_slug}"
            if key not in known_item_slugs:
                e = SteamItemSensor(coordinator, item_slug, item["name"], game_slug)
                known_item_slugs.add(key)
                new.append(e)

        for watch in _data.get("watchlist", []):
            w_slug = watch.get("slug", "")
            if w_slug and w_slug not in known_watch_slugs:
                e = SteamWatchlistSensor(coordinator, w_slug, watch["market_hash_name"])
                known_watch_slugs.add(w_slug)
                new.append(e)

        if new:
            async_add_entities(new)

    # async_add_listener returns an unsubscribe callable — wire it to entry unload
    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


# ── Base ──────────────────────────────────────────────────────────────────────

class _SteamBase(CoordinatorEntity[CS2Coordinator], SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:steam"
    _attr_has_entity_name = True

    def __init__(self, coordinator: CS2Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name="Steam Inventory",
            manufacturer="Valve",
            model="Steam Market",
        )

    @property
    def _last_updated(self) -> str | None:
        return self.coordinator.last_cycle_stats.get("last_update")


# ── Global total sensor ───────────────────────────────────────────────────────

class SteamTotalSensor(_SteamBase):
    """Global portfolio total across all games — sensor.steam_inventory_total."""

    _attr_unique_id = "steam_inventory_total"
    _attr_name = "Total"

    def __init__(self, coordinator: CS2Coordinator) -> None:
        super().__init__(coordinator)

    @property
    def native_value(self) -> float | None:
        g = (self.coordinator.data or {}).get("global", {})
        v = g.get("total_value")
        return round(v, 2) if v is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        g = (self.coordinator.data or {}).get("global", {})
        active = (self.coordinator.data or {}).get("active_apps", [])
        return {
            "total_net": g.get("total_net"),
            "profit_brut": g.get("profit_brut"),
            "profit_net": g.get("profit_net"),
            "roi_global": g.get("roi_global"),
            "delta": g.get("delta"),
            "items_count": g.get("items_count"),
            "items_total_qty": g.get("items_total_qty"),
            "items_with_price": g.get("items_with_price"),
            "best_performer_name": g.get("best_performer_name"),
            "best_performer_roi": g.get("best_performer_roi"),
            "worst_performer_name": g.get("worst_performer_name"),
            "worst_performer_roi": g.get("worst_performer_roi"),
            "active_games": [a[2] for a in active],
            "active_games_count": len(active),
            "last_updated_time": self._last_updated,
        }


# ── Per-game total sensor ──────────────────────────────────────────────────────

class SteamGameSensor(_SteamBase):
    """Per-game portfolio total — sensor.steam_{slug}_total."""

    def __init__(self, coordinator: CS2Coordinator, slug: str, game_name: str) -> None:
        super().__init__(coordinator)
        self._slug = slug
        self._game_name = game_name
        self._attr_unique_id = f"steam_game_{slug}_total"
        self._attr_name = f"{game_name} Total"

    def _game(self) -> dict:
        return (self.coordinator.data or {}).get("per_game", {}).get(self._slug, {})

    @property
    def native_value(self) -> float | None:
        m = self._game().get("metrics", {})
        v = m.get("total_value")
        return round(v, 2) if v is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        game = self._game()
        m = game.get("metrics", {})
        return {
            "game_name": self._game_name,
            "appid": game.get("appid"),
            "total_net": m.get("total_net"),
            "profit_brut": m.get("profit_brut"),
            "roi_global": m.get("roi_global"),
            "delta": m.get("delta"),
            "items_count": m.get("items_count"),
            "items_total_qty": m.get("items_total_qty"),
            "items_with_price": m.get("items_with_price"),
            "best_performer_name": m.get("best_performer_name"),
            "best_performer_roi": m.get("best_performer_roi"),
            "worst_performer_name": m.get("worst_performer_name"),
            "worst_performer_roi": m.get("worst_performer_roi"),
            "last_updated_time": self._last_updated,
        }

    @property
    def available(self) -> bool:
        return bool(self._game())


# ── Per-item sensor ───────────────────────────────────────────────────────────

class SteamItemSensor(_SteamBase):
    """One sensor per unique item — sensor.steam_item_{game_slug}_{slug}."""

    # Spot market price: current reading, not cumulative → MEASUREMENT
    # Remove MONETARY device_class (requires TOTAL state_class) to allow MEASUREMENT
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CS2Coordinator, slug: str, market_name: str, game_slug: str = "") -> None:
        super().__init__(coordinator)
        self._slug = slug
        self._game_slug = game_slug
        self._market_name = market_name
        prefix = f"{game_slug}_" if game_slug else ""
        self._prefix = prefix
        self._attr_unique_id = f"steam_item_{prefix}{slug}"
        self._attr_name = market_name

    def _item(self) -> dict:
        key = f"{self._game_slug}__{self._slug}" if self._game_slug else self._slug
        return (self.coordinator.data or {}).get("items_by_slug", {}).get(key, {})

    @property
    def entity_picture(self) -> str | None:
        return self._item().get("entity_picture")

    @property
    def native_value(self) -> float | None:
        return self._item().get("current_price")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item()
        return {
            "game_name": item.get("game_name"),
            "game_slug": item.get("game_slug"),
            "current_price": item.get("current_price"),
            "buy_price": item.get("buy_price"),
            "before_crash": item.get("before_crash"),
            "delta_yesterday": item.get("delta_yesterday"),
            "delta_since_crash": item.get("delta_since_crash"),
            "delta_from_start": item.get("delta_from_start"),
            "roi": item.get("roi"),
            "rarity_color": item.get("rarity_color"),
            "float_value": item.get("float_value"),
            "quantity": item.get("quantity", 1),
            "last_updated_time": self._last_updated,
        }

    @property
    def available(self) -> bool:
        return bool(self._item())


# ── Sync status sensor ────────────────────────────────────────────────────────

class SteamSyncSensor(_SteamBase):
    """Integration health sensor — sensor.steam_sync_status."""

    _attr_unique_id = "steam_sync_status"
    _attr_name = "Sync Status"
    _attr_icon = "mdi:sync"
    _attr_device_class = None
    _attr_state_class = None
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator: CS2Coordinator) -> None:
        super().__init__(coordinator)

    @property
    def native_value(self) -> str:
        if not self.coordinator.last_update_success:
            return "error"
        if not (self.coordinator.data or {}).get("active_apps"):
            return "no_games"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self.coordinator.last_cycle_stats
        data = self.coordinator.data or {}
        return {
            "last_update": stats.get("last_update"),
            "cycle_duration_s": stats.get("cycle_duration_s"),
            "total_value": stats.get("total_value"),
            "items_count": stats.get("items_count"),
            "active_games": stats.get("active_games"),
            "stale_count": data.get("stale_count"),
            "missing_count": data.get("missing_count"),
            "last_update_success": self.coordinator.last_update_success,
        }


# ── Watchlist sensor ──────────────────────────────────────────────────────────

class SteamWatchlistSensor(_SteamBase):
    """Tracks price of a non-owned item — sensor.steam_watch_{slug}."""

    # Same reasoning as SteamItemSensor — spot price, not cumulative
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CS2Coordinator, slug: str, market_name: str) -> None:
        super().__init__(coordinator)
        self._slug = slug
        self._market_name = market_name
        self._attr_unique_id = f"steam_watch_{slug}"
        self._attr_name = market_name[:50]
        self._attr_icon = "mdi:eye"

    def _watch(self) -> dict:
        return (self.coordinator.data or {}).get("watchlist_by_slug", {}).get(self._slug, {})

    @property
    def native_value(self) -> float | None:
        price = self._watch().get("current_price")
        return round(price, 2) if price is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self._watch()
        price = w.get("current_price")
        target = w.get("target_price")
        return {
            "market_hash_name": self._market_name,
            "appid": w.get("appid", 730),
            "target_price": target,
            "note": w.get("note", ""),
            "below_target": (price is not None and target is not None and price <= target),
            "last_updated_time": self._last_updated,
        }

    @property
    def available(self) -> bool:
        return bool(self._watch())
