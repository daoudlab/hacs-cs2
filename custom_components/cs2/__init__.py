"""Steam Inventory — Home Assistant custom integration."""
from __future__ import annotations

import asyncio
import logging
import time as _time
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

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
    SERVICE_WATCHLIST_ADD,
    SERVICE_WATCHLIST_REMOVE,
    WATCHLIST_FILE,
    RECOMMENDED_FRONTEND_CARDS,
)
from .coordinator import CS2Coordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]

_BUY_PRICE_WRITE_LOCK = asyncio.Lock()
_WATCHLIST_WRITE_LOCK = asyncio.Lock()


_ENTITY_ID_MIGRATIONS: dict[str, str] = {
    # pre-has_entity_name (Round ≤4) → post-has_entity_name (Round 5+)
    "sensor.steam_sync_status": "sensor.steam_inventory_sync_status",
    "sensor.steam_cs2_total": "sensor.steam_inventory_cs2_total",
    "sensor.steam_dota2_total": "sensor.steam_inventory_dota2_total",
    "sensor.steam_tf2_total": "sensor.steam_inventory_tf2_total",
    "sensor.steam_rust_total": "sensor.steam_inventory_rust_total",
    "sensor.steam_inventory_total_total": "sensor.steam_inventory_total",
    # Round 5: HA name-slugification mismatch — "Dota 2 Total" → "dota_2_total" (underscore
    # before digit) but slug is "dota2".  Explicit entity_id from Round 6+ uses the slug.
    "sensor.steam_inventory_dota_2_total": "sensor.steam_inventory_dota2_total",
    "sensor.steam_inventory_payday_2_total": "sensor.steam_inventory_payday2_total",
    "sensor.steam_inventory_dont_starve_together_total": "sensor.steam_inventory_dst_total",
    "sensor.steam_inventory_killing_floor_2_total": "sensor.steam_inventory_kf2_total",
    "sensor.steam_inventory_primal_carnage_extinction_total": "sensor.steam_inventory_primal_carnage_total",
    "sensor.steam_inventory_naraka_bladepoint_total": "sensor.steam_inventory_naraka_total",
    "sensor.steam_inventory_golf_with_your_friends_total": "sensor.steam_inventory_golf_friends_total",
}


def _migrate_entity_registry(hass: HomeAssistant) -> None:
    """Rename stale pre-migration entity IDs to the new has_entity_name format."""
    from homeassistant.helpers import entity_registry as er
    ent_reg = er.async_get(hass)
    for old_id, new_id in _ENTITY_ID_MIGRATIONS.items():
        entry = ent_reg.async_get(old_id)
        if entry and not ent_reg.async_get(new_id):
            ent_reg.async_update_entity(old_id, new_entity_id=new_id)
            _LOGGER.info("Entity registry migration: %s → %s", old_id, new_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _migrate_entity_registry(hass)

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
        try:
            hass.async_create_background_task(
                _run_import(hass, coordinator, pending["cookie"], pending.get("start_date"), coordinator.min_item_value, stop=coordinator._stop),
                name="cs2_run_import_setup",
            )
        except Exception:
            coordinator._import_running = False
            raise

    # Register services (once per domain)
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_IMPORT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_IMPORT,
            _handle_run_import,
            schema=vol.Schema(
                {
                    vol.Required(CONF_STEAM_COOKIE): str,
                    # Accept None: the HA UI sends empty optional fields as null,
                    # which would fail a bare str/float validator. Handler defaults them.
                    vol.Optional(CONF_IMPORT_START_DATE, default=""): vol.Any(None, str),
                    vol.Optional(CONF_MIN_ITEM_VALUE, default=DEFAULT_MIN_VALUE): vol.Any(
                        None, vol.Coerce(float)
                    ),
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

    if not hass.services.has_service(DOMAIN, SERVICE_WATCHLIST_ADD):
        hass.services.async_register(
            DOMAIN,
            SERVICE_WATCHLIST_ADD,
            _handle_watchlist_add,
            schema=vol.Schema(
                {
                    vol.Required("market_hash_name"): str,
                    vol.Optional("target_price"): vol.Any(
                        vol.All(vol.Coerce(float), vol.Range(min=0, max=1_000_000)),
                        None,
                    ),
                    vol.Optional("note", default=""): str,
                    vol.Optional("appid", default=730): vol.All(
                        vol.Coerce(int), vol.Range(min=1)
                    ),
                }
            ),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_WATCHLIST_REMOVE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_WATCHLIST_REMOVE,
            _handle_watchlist_remove,
            schema=vol.Schema({vol.Required("market_hash_name"): str}),
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
                    SERVICE_FORCE_REFRESH, SERVICE_SET_BUY_PRICE,
                    SERVICE_WATCHLIST_ADD, SERVICE_WATCHLIST_REMOVE):
            hass.services.async_remove(DOMAIN, svc)
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _handle_run_import(call: ServiceCall) -> None:
    hass = call.hass

    # Require admin — this service accepts a Steam session cookie
    # Block automation/system calls with no user context (user_id=None)
    if not call.context.user_id:
        raise HomeAssistantError("cs2.run_import: user context required (cannot call from automation without user)")
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        raise HomeAssistantError("cs2.run_import: admin access required")

    cookie = call.data[CONF_STEAM_COOKIE].strip().strip('"').strip("'")
    # Tolerate stray surrounding quotes from the UI (YAML/form quirks).
    start_date = (str(call.data.get(CONF_IMPORT_START_DATE) or "")
                  .strip().strip('"').strip("'").strip()) or None
    if start_date:
        try:
            from datetime import date as _date
            _date.fromisoformat(start_date)
        except ValueError:
            raise HomeAssistantError(f"cs2.run_import: invalid start_date format (expected YYYY-MM-DD): {start_date}")
    _mv = call.data.get(CONF_MIN_ITEM_VALUE)
    min_value = float(_mv) if _mv is not None else DEFAULT_MIN_VALUE

    coordinators = [v for v in hass.data.get(DOMAIN, {}).values() if isinstance(v, CS2Coordinator)]
    if not coordinators:
        raise HomeAssistantError("cs2.run_import: no active Steam Inventory entry found")
    coordinator = coordinators[0]

    # If no explicit start_date, derive from configured history_days retention
    if not start_date:
        from datetime import date as _date, timedelta as _timedelta
        start_date = (_date.today() - _timedelta(days=coordinator.history_days)).isoformat()
        _LOGGER.info("cs2.run_import: no start_date — using %s (history_days=%d)", start_date, coordinator.history_days)

    if coordinator._import_running:
        raise HomeAssistantError("cs2.run_import: import already in progress")
    if not coordinator.data:
        raise HomeAssistantError("cs2.run_import: no data yet — wait for first scan cycle")

    _LOGGER.info(
        "cs2.run_import: scheduling import (coordinators=%d, picked id=%s, data_ready=%s)",
        len(coordinators), id(coordinator), bool(coordinator.data),
    )
    coordinator._import_running = True
    try:
        # Long-running fire-and-forget work: use a background task so HA keeps a
        # strong reference and the coroutine is actually scheduled to run. A plain
        # async_create_task created from a service handler can fail to execute.
        hass.async_create_background_task(
            _run_import(hass, coordinator, cookie, start_date, min_value, stop=coordinator._stop),
            name="cs2_run_import",
        )
    except Exception:
        coordinator._import_running = False
        raise
    _LOGGER.info("cs2.run_import: background task scheduled")


async def _run_import(
    hass: HomeAssistant,
    coordinator: CS2Coordinator,
    cookie: str,
    start_date: str | None,
    min_value: float = DEFAULT_MIN_VALUE,
    stop=None,
) -> None:
    _LOGGER.info("cs2 import: coroutine top (coordinator id=%s)", id(coordinator))
    start = _time.monotonic()
    try:
        _LOGGER.info(
            "cs2 import: task entered (start_date=%s, min_value=%s, data_ready=%s)",
            start_date, min_value, bool(coordinator.data),
        )
        from .importer import async_run_import

        # Wait for first coordinator refresh to complete (covers first-install race)
        deadline = _time.monotonic() + 300  # max 5 minutes
        while not coordinator.data:
            if _time.monotonic() > deadline or (stop and stop.is_set()):
                _LOGGER.warning("cs2 import: timed out waiting for coordinator data — skipping")
                coordinator._import_progress = {
                    "running": False,
                    "error": "timed out waiting for coordinator data",
                    "elapsed_s": int(_time.monotonic() - start),
                }
                return
            await asyncio.sleep(10)

        items = (coordinator.data or {}).get("items", [])
        if not items:
            _LOGGER.warning("cs2 import: no items in coordinator data, skipping")
            coordinator._import_progress = {
                "running": False,
                "error": "no items in coordinator data",
                "elapsed_s": int(_time.monotonic() - start),
            }
            return

        coordinator._import_progress = {
            "running": True, "fetched": 0, "total": len(items), "skipped": 0, "elapsed_s": 0
        }

        def _progress_cb(fetched: int, total: int, skipped: int) -> None:
            progress = {
                "running": True,
                "fetched": fetched,
                "total": total,
                "skipped": skipped,
                "elapsed_s": int(_time.monotonic() - start),
            }
            hass.loop.call_soon_threadsafe(
                setattr, coordinator, "_import_progress", progress
            )

        result = await async_run_import(
            hass, items, cookie, start_date, min_value, stop=stop, progress_cb=_progress_cb,
            currency=coordinator.currency, unit=coordinator.currency_code,
        )
        _LOGGER.info(
            "cs2 import finished: fetched=%d skipped=%d global_days=%d game_days=%d per_item_series=%d",
            result.get("fetched", 0),
            result.get("skipped", 0),
            len(result.get("daily_totals", {})),
            sum(len(v) for v in result.get("per_game_totals", {}).values()),
            len(result.get("per_item_histories", {})),
        )
        coordinator._import_progress = {
            "running": False,
            "fetched": result.get("fetched", 0),
            "total": len(items),
            "skipped": result.get("skipped", 0),
            "elapsed_s": int(_time.monotonic() - start),
        }
    except Exception as err:
        _LOGGER.exception("cs2 import failed")
        coordinator._import_progress = {
            "running": False,
            "error": str(err),
            "elapsed_s": int(_time.monotonic() - start),
        }
    finally:
        coordinator._import_running = False


async def _handle_generate_dashboards(call: ServiceCall) -> None:
    hass = call.hass
    if not call.context.user_id:
        raise HomeAssistantError("cs2.generate_dashboards: user context required")
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        raise HomeAssistantError("cs2.generate_dashboards: admin access required")
    coordinators = [v for v in hass.data.get(DOMAIN, {}).values() if isinstance(v, CS2Coordinator)]
    if not coordinators or not coordinators[0].data:
        raise HomeAssistantError("cs2.generate_dashboards: no data yet — wait for first scan cycle")

    coordinator = coordinators[0]
    await hass.async_add_executor_job(
        _write_dashboards, hass.config.config_dir, coordinator.data
    )
    await _notify_frontend_cards(hass)


async def _notify_frontend_cards(hass: HomeAssistant) -> None:
    """Surface the HACS frontend cards needed for the enhanced dashboards.

    An HA integration cannot install HACS frontend plugins itself, so we list
    them in a persistent_notification with their HACS repos.
    """
    lines = [
        f"- **{label}** — `{repo}`" for label, repo in RECOMMENDED_FRONTEND_CARDS
    ]
    message = (
        "Les dashboards générés utilisent des cartes natives HA (aucun module "
        "requis). Pour le rendu enrichi (vignettes skins, graphiques), installez "
        "ces cartes via **HACS → Frontend** :\n\n" + "\n".join(lines)
    )
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": "CS2 — cartes Lovelace recommandées",
            "message": message,
            "notification_id": "cs2_frontend_cards",
        },
        blocking=False,
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
        raise HomeAssistantError("cs2.force_refresh: user context required")
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        raise HomeAssistantError("cs2.force_refresh: admin access required")
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, CS2Coordinator):
            await coordinator.async_request_refresh()
    _LOGGER.info("cs2.force_refresh: triggered")


async def _handle_set_buy_price(call) -> None:
    """Write or delete a buy price in cs2_buy_prices.json."""
    import json
    from pathlib import Path
    hass = call.hass
    if not call.context.user_id:
        raise HomeAssistantError("cs2.set_buy_price: user context required")
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        raise HomeAssistantError("cs2.set_buy_price: admin access required")
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
        # Atomic write: write to .tmp then replace to prevent data loss on concurrent calls
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp_path.replace(path)

    async with _BUY_PRICE_WRITE_LOCK:
        await hass.async_add_executor_job(_write)
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, CS2Coordinator):
            await coordinator.async_request_refresh()


async def _handle_watchlist_add(call) -> None:
    """Add or update an item in cs2_watchlist.json."""
    import json
    from pathlib import Path
    hass = call.hass
    if not call.context.user_id:
        raise HomeAssistantError("cs2.watchlist_add: user context required")
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        raise HomeAssistantError("cs2.watchlist_add: admin access required")

    name = call.data["market_hash_name"].strip()
    target_price = call.data.get("target_price")
    note = (call.data.get("note") or "").strip()
    appid = int(call.data.get("appid", 730))
    path = Path(hass.config.config_dir) / WATCHLIST_FILE

    def _write() -> None:
        items: list = []
        if path.exists():
            try:
                items = json.loads(path.read_text())
            except Exception as err:
                _LOGGER.warning("watchlist unreadable, starting fresh: %s", err)
        entry: dict = {"market_hash_name": name, "appid": appid}
        if target_price is not None:
            entry["target_price"] = round(float(target_price), 2)
        if note:
            entry["note"] = note
        existing_idx = next(
            (i for i, w in enumerate(items) if w.get("market_hash_name") == name), None
        )
        if existing_idx is not None:
            items[existing_idx] = entry
            _LOGGER.info("cs2.watchlist_add: updated %s", name)
        else:
            items.append(entry)
            _LOGGER.info("cs2.watchlist_add: added %s", name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
        tmp.replace(path)

    async with _WATCHLIST_WRITE_LOCK:
        await hass.async_add_executor_job(_write)
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, CS2Coordinator):
            await coordinator.async_request_refresh()


async def _handle_watchlist_remove(call) -> None:
    """Remove an item from cs2_watchlist.json."""
    import json
    from pathlib import Path
    hass = call.hass
    if not call.context.user_id:
        raise HomeAssistantError("cs2.watchlist_remove: user context required")
    user = await hass.auth.async_get_user(call.context.user_id)
    if not user or not user.is_admin:
        raise HomeAssistantError("cs2.watchlist_remove: admin access required")

    name = call.data["market_hash_name"].strip()
    path = Path(hass.config.config_dir) / WATCHLIST_FILE

    def _write() -> None:
        if not path.exists():
            return
        try:
            items = json.loads(path.read_text())
        except Exception:
            return
        before = len(items)
        items = [w for w in items if w.get("market_hash_name") != name]
        if len(items) < before:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
            tmp.replace(path)
            _LOGGER.info("cs2.watchlist_remove: removed %s", name)
        else:
            _LOGGER.warning("cs2.watchlist_remove: %s not found in watchlist", name)

    async with _WATCHLIST_WRITE_LOCK:
        await hass.async_add_executor_job(_write)
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, CS2Coordinator):
            await coordinator.async_request_refresh()
