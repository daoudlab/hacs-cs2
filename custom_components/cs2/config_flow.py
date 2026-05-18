"""Config flow for CS2 Inventory."""
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
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STRICT_RATIO,
    DEFAULT_MIN_VALUE,
    DEFAULT_MAX_ITEMS,
)

_LOGGER = logging.getLogger(__name__)

_STEAM_ID_RE = re.compile(r"^\d{17}$")


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
    }
)


class CS2ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CS2 Inventory."""

    VERSION = 1
    _data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                accounts = _validate_steam_ids(user_input[CONF_STEAM_IDS])
                self._data[CONF_STEAM_IDS] = user_input[CONF_STEAM_IDS]
                return await self.async_step_settings()
            except vol.Invalid as err:
                errors["base"] = "invalid_steam_ids"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_ACCOUNTS_SCHEMA,
            errors=errors,
            description_placeholders={
                "example": "76561190000000001:main,76561190000000002:alt"
            },
        )

    async def async_step_settings(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            accounts = _parse_steam_ids(self._data[CONF_STEAM_IDS])
            title = " + ".join(name for _, name in accounts)
            return self.async_create_entry(title=title, data=self._data)

        return self.async_show_form(
            step_id="settings",
            data_schema=STEP_SETTINGS_SCHEMA,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> CS2OptionsFlow:
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
                vol.Required(CONF_STEAM_IDS, default=current.get(CONF_STEAM_IDS, "")): str,
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
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
