"""CS2 Inventory — Home Assistant custom integration."""
from __future__ import annotations

import logging
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    CONF_IMPORT_START_DATE,
    CONF_STEAM_COOKIE,
    CONF_MIN_ITEM_VALUE,
    DEFAULT_MIN_VALUE,
    SERVICE_RUN_IMPORT,
)
from .coordinator import CS2Coordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = CS2Coordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Fetch data in background — Steam API takes 2+ min, don't block config flow
    hass.async_create_task(coordinator.async_request_refresh())

    # Check if a pending import was queued from the config flow (cookie transport)
    pending = hass.data.get("cs2_pending_import", {}).pop(
        entry.entry_id, None
    ) or hass.data.get("cs2_pending_import", {}).pop(
        # flow_id used as key during config flow, entry_id after
        next(
            (k for k in hass.data.get("cs2_pending_import", {})),
            None,
        ),
        None,
    )
    if pending and pending.get("cookie"):
        hass.async_create_task(
            _run_import(hass, coordinator, pending["cookie"], pending.get("start_date"))
        )

    # Register service (once per domain, not per entry)
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_IMPORT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_IMPORT,
            _handle_run_import,
            schema=vol.Schema(
                {
                    vol.Required(CONF_STEAM_COOKIE): str,
                    vol.Optional(CONF_IMPORT_START_DATE, default=""): str,
                    vol.Optional(CONF_MIN_ITEM_VALUE, default=DEFAULT_MIN_VALUE): float,
                }
            ),
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        coordinator.stop()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    # Remove service if no more entries
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_RUN_IMPORT)
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _handle_run_import(call: ServiceCall) -> None:
    """Handle cs2.run_import service call."""
    hass = call.hass
    cookie = call.data[CONF_STEAM_COOKIE].strip()
    start_date = call.data.get(CONF_IMPORT_START_DATE, "").strip() or None
    min_value = float(call.data.get(CONF_MIN_ITEM_VALUE, DEFAULT_MIN_VALUE))

    # Use first loaded coordinator
    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        _LOGGER.error("cs2.run_import: no active CS2 entry found")
        return

    coordinator = coordinators[0]
    if not coordinator.data:
        _LOGGER.warning("cs2.run_import: coordinator has no data yet — run after first cycle")
        return

    hass.async_create_task(
        _run_import(hass, coordinator, cookie, start_date, min_value)
    )


async def _run_import(
    hass: HomeAssistant,
    coordinator: CS2Coordinator,
    cookie: str,
    start_date: str | None,
    min_value: float = DEFAULT_MIN_VALUE,
) -> None:
    """Launch the background historical import."""
    from .importer import async_run_import

    items = (coordinator.data or {}).get("items", [])
    if not items:
        _LOGGER.warning("cs2 import: no items in coordinator data, skipping")
        return

    try:
        result = await async_run_import(hass, items, cookie, start_date, min_value)
        _LOGGER.info("cs2 import finished: %s", result)
    except Exception as err:
        _LOGGER.error("cs2 import failed: %s", err)
