"""CS2 Inventory sensor entities."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SENSOR_TOTAL_ID, SENSOR_ITEM_PREFIX, SENSOR_ACCOUNT_PREFIX
from .coordinator import CS2Coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CS2Coordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [CS2TotalSensor(coordinator)]

    data = coordinator.data or {}
    for item in data.get("items", []):
        entities.append(CS2ItemSensor(coordinator, item["slug"], item["name"]))

    for account_name in data.get("per_account", {}):
        entities.append(CS2AccountSensor(coordinator, account_name))

    async_add_entities(entities)

    # Dynamic add: new items or accounts that appear after initial setup
    def _handle_coordinator_update() -> None:
        known_item_ids = {e.entity_id for e in entities if isinstance(e, CS2ItemSensor)}
        known_acct_ids = {e.entity_id for e in entities if isinstance(e, CS2AccountSensor)}
        new: list[SensorEntity] = []

        _data = coordinator.data or {}
        for item in _data.get("items", []):
            eid = f"{SENSOR_ITEM_PREFIX}{item['slug']}"
            if eid not in known_item_ids:
                e = CS2ItemSensor(coordinator, item["slug"], item["name"])
                entities.append(e)
                known_item_ids.add(eid)
                new.append(e)

        for account_name in _data.get("per_account", {}):
            eid = f"{SENSOR_ACCOUNT_PREFIX}{account_name.lower()}"
            if eid not in known_acct_ids:
                e = CS2AccountSensor(coordinator, account_name)
                entities.append(e)
                known_acct_ids.add(eid)
                new.append(e)

        if new:
            async_add_entities(new)

    coordinator.async_add_listener(_handle_coordinator_update)


# ── Base ──────────────────────────────────────────────────────────────────────

class _CS2Base(CoordinatorEntity[CS2Coordinator], SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:steam"

    def __init__(self, coordinator: CS2Coordinator) -> None:
        super().__init__(coordinator)

    @property
    def _last_updated(self) -> str:
        return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


# ── Total sensor ──────────────────────────────────────────────────────────────

class CS2TotalSensor(_CS2Base):
    """Global portfolio total — sensor.cs2_inventory_total."""

    _attr_unique_id = "cs2_inventory_total"
    _attr_name = "CS2 Inventory Total"

    def __init__(self, coordinator: CS2Coordinator) -> None:
        super().__init__(coordinator)
        self.entity_id = SENSOR_TOTAL_ID

    @property
    def native_value(self) -> float | None:
        g = (self.coordinator.data or {}).get("global", {})
        v = g.get("total_value")
        return round(v, 2) if v is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        g = (self.coordinator.data or {}).get("global", {})
        return {
            "friendly_name": "CS2 Inventory Total",
            "total_net": g.get("total_net"),
            "profit_brut": g.get("profit_brut"),
            "profit_net": g.get("profit_net"),
            "roi_global": g.get("roi_global"),
            "delta": g.get("delta"),
            "previous_total": g.get("previous_total"),
            "items_count": g.get("items_count"),
            "items_total_qty": g.get("items_total_qty"),
            "items_with_price": g.get("items_with_price"),
            "best_performer_name": g.get("best_performer_name"),
            "best_performer_roi": g.get("best_performer_roi"),
            "worst_performer_name": g.get("worst_performer_name"),
            "worst_performer_roi": g.get("worst_performer_roi"),
            "last_updated_time": self._last_updated,
            "last_updated_sheet": datetime.now().strftime("%y%m%d_%H%M"),
        }


# ── Per-item sensor ───────────────────────────────────────────────────────────

class CS2ItemSensor(_CS2Base):
    """One sensor per unique inventory item — sensor.cs2_item_{slug}."""

    def __init__(self, coordinator: CS2Coordinator, slug: str, market_name: str) -> None:
        super().__init__(coordinator)
        self._slug = slug
        self._market_name = market_name
        self._attr_unique_id = f"cs2_item_{slug}"
        self._attr_name = market_name
        self.entity_id = f"{SENSOR_ITEM_PREFIX}{slug}"

    def _item(self) -> dict:
        for item in (self.coordinator.data or {}).get("items", []):
            if item["slug"] == self._slug:
                return item
        return {}

    @property
    def native_value(self) -> float | None:
        v = self._item().get("current_price")
        return v

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item()
        return {
            "friendly_name": self._market_name,
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
            "entity_picture": item.get("entity_picture"),
            "last_updated_time": self._last_updated,
        }

    @property
    def available(self) -> bool:
        return bool(self._item())


# ── Per-account sensor ────────────────────────────────────────────────────────

class CS2AccountSensor(_CS2Base):
    """Per-account total — sensor.cs2_inventory_total_{account}."""

    def __init__(self, coordinator: CS2Coordinator, account_name: str) -> None:
        super().__init__(coordinator)
        self._account = account_name
        self._attr_unique_id = f"cs2_inventory_total_{account_name.lower()}"
        self._attr_name = f"CS2 Inventory — {account_name}"
        self.entity_id = f"{SENSOR_ACCOUNT_PREFIX}{account_name.lower()}"

    def _metrics(self) -> dict:
        return (self.coordinator.data or {}).get("per_account", {}).get(self._account, {})

    @property
    def native_value(self) -> float | None:
        v = self._metrics().get("total_value")
        return round(v, 2) if v is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._metrics()
        return {
            "friendly_name": f"CS2 Inventory — {self._account}",
            "total_net": m.get("total_net"),
            "profit_brut": m.get("profit_brut"),
            "roi_global": m.get("roi_global"),
            "delta": m.get("delta"),
            "items_count": m.get("items_count"),
            "items_total_qty": m.get("items_total_qty"),
            "items_with_price": m.get("items_with_price"),
            "last_updated_time": self._last_updated,
        }

    @property
    def available(self) -> bool:
        return bool(self._metrics())
