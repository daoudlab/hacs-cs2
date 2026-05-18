"""Steam Inventory — Home Assistant custom integration."""
from __future__ import annotations

import logging
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    DOMAIN,
    CONF_IMPORT_START_DATE,
    CONF_STEAM_COOKIE,
    CONF_MIN_ITEM_VALUE,
    DEFAULT_MIN_VALUE,
    SERVICE_RUN_IMPORT,
    SERVICE_GENERATE_DASHBOARDS,
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

    # Check if a pending import was queued from the config flow (cookie transport).
    # The config flow stores it under flow_id (not entry_id), so pop the first item.
    pending_store = hass.data.get("cs2_pending_import", {})
    pending = pending_store.pop(next(iter(pending_store), None), None)
    if pending and pending.get("cookie"):
        hass.async_create_task(
            _run_import(hass, coordinator, pending["cookie"], pending.get("start_date"))
        )

    # Register services (once per domain)
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

    if not hass.services.has_service(DOMAIN, SERVICE_GENERATE_DASHBOARDS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_DASHBOARDS,
            _handle_generate_dashboards,
            schema=vol.Schema({}),
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        coordinator.stop()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_RUN_IMPORT)
        hass.services.async_remove(DOMAIN, SERVICE_GENERATE_DASHBOARDS)
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _handle_run_import(call: ServiceCall) -> None:
    hass = call.hass
    cookie = call.data[CONF_STEAM_COOKIE].strip()
    start_date = call.data.get(CONF_IMPORT_START_DATE, "").strip() or None
    min_value = float(call.data.get(CONF_MIN_ITEM_VALUE, DEFAULT_MIN_VALUE))

    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        _LOGGER.error("cs2.run_import: no active Steam Inventory entry found")
        return

    coordinator = coordinators[0]
    if not coordinator.data:
        _LOGGER.warning("cs2.run_import: no data yet — wait for first scan cycle")
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


async def _handle_generate_dashboards(call: ServiceCall) -> None:
    hass = call.hass
    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators or not coordinators[0].data:
        _LOGGER.warning("cs2.generate_dashboards: no data yet")
        return

    coordinator = coordinators[0]
    await hass.async_add_executor_job(
        _write_dashboards, hass.config.config_dir, coordinator.data
    )


def _write_dashboards(config_dir: str, data: dict) -> None:
    from .dashboard import generate_dashboards
    import os

    out_dir = os.path.join(config_dir, "steam_dashboards")
    os.makedirs(out_dir, exist_ok=True)
    files = generate_dashboards(data, out_dir)
    _LOGGER.info(
        "cs2.generate_dashboards: wrote %d files to %s: %s",
        len(files), out_dir, ", ".join(files),
    )
