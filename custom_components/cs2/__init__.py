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
    CONF_STEAM_IDS,
    DEFAULT_MIN_VALUE,
    SERVICE_RUN_IMPORT,
    SERVICE_GENERATE_DASHBOARDS,
    SERVICE_FORCE_REFRESH,
    SERVICE_SET_BUY_PRICE,
)
from .coordinator import CS2Coordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = CS2Coordinator(hass, entry)

    # Pop the pending import cookie keyed by steam_ids (set during config flow step_import)
    steam_ids = entry.data.get(CONF_STEAM_IDS, "")
    pending = hass.data.get(DOMAIN, {}).get("pending_imports", {}).pop(steam_ids, None)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Fetch data in background — Steam API takes 2+ min, don't block config flow
    hass.async_create_task(coordinator.async_request_refresh())

    if pending and pending.get("cookie"):
        coordinator._import_running = True
        hass.async_create_task(
            _run_import(hass, coordinator, pending["cookie"], pending.get("start_date"), coordinator.min_item_value, stop=coordinator._stop)
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

    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_REFRESH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FORCE_REFRESH,
            _handle_force_refresh,
            schema=vol.Schema({}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_BUY_PRICE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_BUY_PRICE,
            _handle_set_buy_price,
            schema=vol.Schema(
                {
                    vol.Required("market_hash_name"): str,
                    vol.Optional("price", default=0.0): vol.Any(
                        vol.All(vol.Coerce(float), vol.Range(min=0, max=1_000_000)),
                        None,
                    ),
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
    if not hass.data.get(DOMAIN):
        for svc in (SERVICE_RUN_IMPORT, SERVICE_GENERATE_DASHBOARDS,
                    SERVICE_FORCE_REFRESH, SERVICE_SET_BUY_PRICE):
            hass.services.async_remove(DOMAIN, svc)
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _handle_run_import(call: ServiceCall) -> None:
    hass = call.hass

    # Require admin — this service accepts a Steam session cookie
    # Block automation/system calls with no user context (user_id=None)
    if not call.context.user_id:
        _LOGGER.error("cs2.run_import: user context required (cannot call from automation without user)")
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        _LOGGER.error("cs2.run_import: admin access required")
        return

    cookie = call.data[CONF_STEAM_COOKIE].strip()
    start_date = call.data.get(CONF_IMPORT_START_DATE, "").strip() or None
    if start_date:
        try:
            from datetime import date as _date
            _date.fromisoformat(start_date)
        except ValueError:
            _LOGGER.error("cs2.run_import: invalid start_date format (expected YYYY-MM-DD): %s", start_date)
            return
    min_value = float(call.data.get(CONF_MIN_ITEM_VALUE, DEFAULT_MIN_VALUE))

    coordinators = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        _LOGGER.error("cs2.run_import: no active Steam Inventory entry found")
        return
    coordinator = coordinators[0]

    # If no explicit start_date, derive from configured history_days retention
    if not start_date:
        from datetime import date as _date, timedelta as _timedelta
        start_date = (_date.today() - _timedelta(days=coordinator.history_days)).isoformat()
        _LOGGER.info("cs2.run_import: no start_date — using %s (history_days=%d)", start_date, coordinator.history_days)

    if coordinator._import_running:
        _LOGGER.warning("cs2.run_import: import already in progress — ignoring duplicate call")
        return
    if not coordinator.data:
        _LOGGER.warning("cs2.run_import: no data yet — wait for first scan cycle")
        return

    coordinator._import_running = True
    hass.async_create_task(
        _run_import(hass, coordinator, cookie, start_date, min_value, stop=coordinator._stop)
    )


async def _run_import(
    hass: HomeAssistant,
    coordinator: CS2Coordinator,
    cookie: str,
    start_date: str | None,
    min_value: float = DEFAULT_MIN_VALUE,
    stop=None,
) -> None:
    from .importer import async_run_import

    items = (coordinator.data or {}).get("items", [])
    if not items:
        _LOGGER.warning("cs2 import: no items in coordinator data, skipping")
        coordinator._import_running = False
        return

    try:
        result = await async_run_import(hass, items, cookie, start_date, min_value, stop=stop)
        _LOGGER.info("cs2 import finished: %s", result)
    except Exception as err:
        _LOGGER.error("cs2 import failed: %s", err)
    finally:
        coordinator._import_running = False


async def _handle_generate_dashboards(call: ServiceCall) -> None:
    hass = call.hass
    if not call.context.user_id:
        _LOGGER.error("cs2.generate_dashboards: user context required"); return
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        _LOGGER.error("cs2.generate_dashboards: admin access required"); return
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


async def _handle_force_refresh(call) -> None:
    """Trigger an immediate coordinator refresh."""
    hass = call.hass
    if not call.context.user_id:
        _LOGGER.error("cs2.force_refresh: user context required"); return
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        _LOGGER.error("cs2.force_refresh: admin access required"); return
    for coordinator in hass.data.get(DOMAIN, {}).values():
        await coordinator.async_request_refresh()
    _LOGGER.info("cs2.force_refresh: triggered")


async def _handle_set_buy_price(call) -> None:
    """Write or delete a buy price in cs2_buy_prices.json."""
    import json
    from pathlib import Path
    hass = call.hass
    if not call.context.user_id:
        _LOGGER.error("cs2.set_buy_price: user context required"); return
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        _LOGGER.error("cs2.set_buy_price: admin access required"); return
    name = call.data["market_hash_name"].strip()
    price = call.data.get("price")
    config_dir = hass.config.config_dir
    path = Path(config_dir) / "cs2_buy_prices.json"

    def _write() -> None:
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except Exception as err:
                _LOGGER.warning("cs2_buy_prices.json unreadable, starting fresh: %s", err)
        if price is None or price == 0:
            data.pop(name, None)
            _LOGGER.info("cs2.set_buy_price: removed %s", name)
        else:
            data[name] = round(float(price), 2)
            _LOGGER.info("cs2.set_buy_price: set %s = %.2f EUR", name, float(price))
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    await hass.async_add_executor_job(_write)
    for coordinator in hass.data.get(DOMAIN, {}).values():
        await coordinator.async_request_refresh()
