"""Steam inventory client — public profile, no cookie required.

Uses the generic Steam inventory API (game-agnostic). Items with
``marketable: 1`` in the Steam API response are tracked — this is the
authoritative flag for items tradeable on the Steam Community Market.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from ..const import (
    HEADERS,
    INVENTORY_PAGE_DELAY,
    STEAM_INVENTORY_URL,
    STEAM_PROFILE_XML_URL,
)

_DEFAULT_APP_ID = 730   # CS2 — callers always pass app_id explicitly
_DEFAULT_CONTEXT_ID = 2

_LOGGER = logging.getLogger(__name__)


class InventoryPrivateError(Exception):
    """Steam inventory is set to private — must be public for the tracker."""


class InventoryFetchError(Exception):
    """Transient or hard failure fetching the inventory."""


def check_inventory_count(
    client: httpx.Client,
    steam_id: str,
    app_id: int,
    context_id: int,
    stop=None,
) -> int:
    """Return total_inventory_count for a game, 0 on error / private / empty."""
    url = STEAM_INVENTORY_URL.format(
        steam_id=steam_id, appid=app_id, contextid=context_id
    ) + "&count=1"
    try:
        resp = client.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 403:
            return 0  # private
        if resp.status_code == 429:
            _LOGGER.debug("Rate limited during discovery for appid=%d, skipping", app_id)
            if stop:
                stop.wait(5)
            else:
                time.sleep(5)
            return 0
        if resp.status_code != 200:
            return 0
        data = resp.json()
        return int(data.get("total_inventory_count", 0))
    except Exception:
        return 0


def fetch_inventory(
    client: httpx.Client,
    steam_id: str,
    app_id: int = _DEFAULT_APP_ID,
    context_id: int = _DEFAULT_CONTEXT_ID,
    stop=None,
) -> list[dict[str, Any]]:
    """Return all marketable items from the public Steam inventory of `steam_id`.

    Works for any Steam game — CS2 (730), TF2 (440), Dota 2 (570), etc.

    Each item dict contains:
      market_hash_name, name_color, inspect_link, entity_picture,
      asset_id, classid, instanceid, marketable (bool)
    """
    items: list[dict[str, Any]] = []
    last_assetid: str | None = None
    page_429_count = 0

    while True:
        url = STEAM_INVENTORY_URL.format(
            steam_id=steam_id, appid=app_id, contextid=context_id
        )
        if last_assetid:
            url += f"&start_assetid={last_assetid}"

        try:
            resp = client.get(url, headers=HEADERS, timeout=30)
        except httpx.HTTPError as err:
            raise InventoryFetchError(f"HTTP error: {err}") from err

        if resp.status_code == 403:
            raise InventoryPrivateError(
                f"Steam inventory {steam_id} is private (HTTP 403)"
            )
        if resp.status_code == 429:
            page_429_count += 1
            if page_429_count >= 3:
                _LOGGER.warning("Rate limited on inventory %s — aborting after 3 attempts", steam_id)
                break
            _LOGGER.warning("Rate limited on inventory, waiting 30s (attempt %d/3)", page_429_count)
            if stop:
                stop.wait(30)
            else:
                time.sleep(30)
            continue
        if resp.status_code == 401:
            # Temporary IP soft-ban — let coordinator fall back to stale data
            _LOGGER.warning("Inventory 401 for %s (IP rate-limited?) — %d items fetched so far", steam_id, len(items))
            raise InventoryFetchError(f"Steam inventory 401 (IP banned?) after {len(items)} items")
        if resp.status_code != 200:
            raise InventoryFetchError(
                f"Steam inventory returned HTTP {resp.status_code}"
            )

        data = resp.json()
        if not data or "assets" not in data:
            _LOGGER.warning("Empty inventory response")
            break

        descriptions: dict[str, dict] = {}
        for desc in data.get("descriptions", []):
            key = f"{desc.get('classid')}_{desc.get('instanceid')}"
            descriptions[key] = desc

        for asset in data.get("assets", []):
            key = f"{asset.get('classid')}_{asset.get('instanceid')}"
            desc = descriptions.get(key, {})
            name = desc.get("market_hash_name", "")
            if not name:
                continue

            # Only track items tradeable on the Steam Community Market
            marketable = bool(desc.get("marketable", 0))

            inspect_link: str | None = None
            for action in desc.get("actions", []):
                link = action.get("link", "")
                if link:
                    inspect_link = (
                        link.replace("%owner_steamid%", steam_id)
                        .replace("%assetid%", asset.get("assetid", ""))
                    )
                    break

            raw_color = desc.get("name_color", "")
            name_color = f"#{raw_color}" if raw_color else None

            icon_raw = desc.get("icon_url_large") or desc.get("icon_url", "")
            entity_picture = (
                f"https://community.akamaihd.net/economy/image/{icon_raw}"
                if icon_raw else None
            )

            items.append(
                {
                    "market_hash_name": name,
                    "name_color": name_color,
                    "inspect_link": inspect_link,
                    "entity_picture": entity_picture,
                    "asset_id": asset.get("assetid", ""),
                    "classid": asset.get("classid", ""),
                    "instanceid": asset.get("instanceid", ""),
                    "marketable": marketable,
                    "is_skin": marketable,  # kept for backward-compat with coordinator
                }
            )

        if not data.get("more_items"):
            break

        new_assetid = data.get("last_assetid")
        if not new_assetid or new_assetid == last_assetid:
            _LOGGER.warning("Inventory pagination stalled (assetid=%s) — stopping", new_assetid)
            break
        if len(items) >= 50000:
            _LOGGER.warning("Inventory hard cap reached (50000 items) for %s — stopping", steam_id)
            break
        last_assetid = new_assetid
        _LOGGER.debug("Inventory pagination: next after %s", last_assetid)
        if stop:
            if stop.wait(INVENTORY_PAGE_DELAY):
                _LOGGER.debug("Inventory pagination interrupted by stop signal")
                break
        else:
            time.sleep(INVENTORY_PAGE_DELAY)

    _LOGGER.info(
        "Fetched %d items from inventory %s (appid=%d)", len(items), steam_id, app_id
    )
    return items


def fetch_persona_name(client: httpx.Client, steam_id: str) -> str | None:
    """Public profile XML → display name. Best-effort."""
    url = STEAM_PROFILE_XML_URL.format(steam_id=steam_id)
    try:
        resp = client.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        root = ET.fromstring(resp.text)
        name_el = root.find("steamID")
        if name_el is not None and name_el.text:
            return name_el.text.strip()
    except Exception as err:
        _LOGGER.warning("Persona fetch failed for %s: %s", steam_id, err)
    return None
