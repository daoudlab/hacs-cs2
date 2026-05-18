"""Config flow for CS2/Steam Inventory."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

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
    CONF_IMPORT_START_DATE,
    CONF_STEAM_COOKIE,
    STEAM_INVENTORY_URL,
)

_LOGGER = logging.getLogger(__name__)

_STEAM_ID_RE = re.compile(r"^\d{17}$")


def _test_steam_connection(steam_id: str) -> str:
    """Quick probe of Steam inventory — returns a status string."""
    import httpx
    from .const import HEADERS
    url = STEAM_INVENTORY_URL.format(steam_id=steam_id, appid=730, contextid=2) + "&count=1"
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            count = resp.json().get("total_inventory_count", 0)
            return f"✅ Connecté — {count} items CS2 détectés"
        if resp.status_code == 403:
            return "⚠️ Inventaire privé — rendez-le public pour le tracking"
        if resp.status_code == 429:
            return "⚠️ Rate-limited par Steam — l'intégration fonctionnera quand même"
        return f"⚠️ HTTP {resp.status_code} — vérifiez le Steam ID"
    except Exception:
        return "⚠️ Steam inaccessible — l'intégration fonctionnera quand le réseau sera disponible"


def _parse_steam_ids(raw: str) -> list[tuple[str, str]]:
    """Parse 'steamid:name,steamid:name' or plain comma-separated IDs."""
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


def _validate_steam_ids(raw: str) -> list[tuple[str, str]]:
    accounts = _parse_steam_ids(raw)
    if not accounts:
        raise vol.Invalid("At least one Steam ID required")
    for sid, _ in accounts:
        if not _STEAM_ID_RE.match(sid):
            raise vol.Invalid(f"Invalid Steam ID (must be 17 digits): {sid}")
    return accounts


STEP_ACCOUNTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STEAM_IDS): str,
    }
)

STEP_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            int, vol.Range(min=5, max=1440)
        ),
        vol.Optional(CONF_STRICT_MISSING_RATIO, default=DEFAULT_STRICT_RATIO): vol.All(
            float, vol.Range(min=0.0, max=1.0)
        ),
        vol.Optional(CONF_MIN_ITEM_VALUE, default=DEFAULT_MIN_VALUE): vol.All(
            float, vol.Range(min=0.0)
        ),
        vol.Optional(CONF_MAX_ITEMS, default=DEFAULT_MAX_ITEMS): vol.All(
            int, vol.Range(min=0)
        ),
        vol.Optional(CONF_INCLUDE_TRADING_CARDS, default=False): bool,
        vol.Optional(CONF_FETCH_FLOATS, default=False): bool,
    }
)


class CS2ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Steam Inventory."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {
            "example": "76561190000000001:main,76561190000000002:alt",
            "connection_status": "",
        }

        if user_input is not None:
            try:
                accounts = _validate_steam_ids(user_input[CONF_STEAM_IDS])
                self._data[CONF_STEAM_IDS] = user_input[CONF_STEAM_IDS]

                # Best-effort Steam connection test — never blocks the flow
                status = await self.hass.async_add_executor_job(
                    _test_steam_connection, accounts[0][0]
                )
                _LOGGER.info("Steam connection test: %s", status)
                return await self.async_step_settings()

            except vol.Invalid:
                errors["base"] = "invalid_steam_ids"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_ACCOUNTS_SCHEMA,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_settings(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_import()

        return self.async_show_form(
            step_id="settings",
            data_schema=STEP_SETTINGS_SCHEMA,
            description_placeholders={
                "scan_tip": "60",
                "ratio_tip": "0.3",
                "min_tip": "0.5",
                "max_tip": "200",
            },
        )

    async def async_step_import(self, user_input: dict | None = None) -> FlowResult:
        """Step 3 — optional historical import (cookie + start date)."""
        if user_input is not None:
            cookie = (user_input.get(CONF_STEAM_COOKIE) or "").strip()
            start_date = (user_input.get(CONF_IMPORT_START_DATE) or "").strip()

            self._data[CONF_IMPORT_START_DATE] = start_date
            # Embed flow_id so async_setup_entry can match the pending import correctly
            self._data["_setup_flow_id"] = self.flow_id

            accounts = _parse_steam_ids(self._data[CONF_STEAM_IDS])
            title = " + ".join(name for _, name in accounts)

            if cookie:
                self.hass.data.setdefault("cs2_pending_import", {})[
                    self.flow_id
                ] = {"cookie": cookie, "start_date": start_date}

            return self.async_create_entry(title=title, data=self._data)

        schema = vol.Schema(
            {
                vol.Optional(CONF_IMPORT_START_DATE, default=""): str,
                vol.Optional(CONF_STEAM_COOKIE, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="import",
            data_schema=schema,
            description_placeholders={
                "note": "Laissez le cookie vide pour ignorer l'import historique."
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "CS2OptionsFlow":
        return CS2OptionsFlow(config_entry)


class CS2OptionsFlow(config_entries.OptionsFlow):
    """Handle options flow (re-configure after setup)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _validate_steam_ids(user_input[CONF_STEAM_IDS])
                return self.async_create_entry(title="", data=user_input)
            except vol.Invalid:
                errors["base"] = "invalid_steam_ids"

        current = {**self._config_entry.data, **self._config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_STEAM_IDS, default=current.get(CONF_STEAM_IDS, "")
                ): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(int, vol.Range(min=5, max=1440)),
                vol.Optional(
                    CONF_STRICT_MISSING_RATIO,
                    default=current.get(CONF_STRICT_MISSING_RATIO, DEFAULT_STRICT_RATIO),
                ): vol.All(float, vol.Range(min=0.0, max=1.0)),
                vol.Optional(
                    CONF_MIN_ITEM_VALUE,
                    default=current.get(CONF_MIN_ITEM_VALUE, DEFAULT_MIN_VALUE),
                ): vol.All(float, vol.Range(min=0.0)),
                vol.Optional(
                    CONF_MAX_ITEMS,
                    default=current.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS),
                ): vol.All(int, vol.Range(min=0)),
                vol.Optional(
                    CONF_INCLUDE_TRADING_CARDS,
                    default=current.get(CONF_INCLUDE_TRADING_CARDS, False),
                ): bool,
                vol.Optional(
                    CONF_FETCH_FLOATS,
                    default=current.get(CONF_FETCH_FLOATS, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
