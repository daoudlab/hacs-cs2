"""Steam inventory client — public profile, no cookie required.

Ported from the HACS integration (aiohttp+async → httpx+sync) since the
systemd service is a oneshot and doesn't benefit from asyncio.
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

_LOGGER = logging.getLogger(__name__)

# Item types that can appear on the Steam Market with an active listing.
# Collectibles (medals/coins), Tools, and WeaponCase are never tradeable
# as individual price sensors and are excluded.
_MARKETABLE_TYPES = {
    "CSGO_Type_Pistol",
    "CSGO_Type_SMG",
    "CSGO_Type_Rifle",
    "CSGO_Type_SniperRifle",
    "CSGO_Type_Shotgun",
    "CSGO_Type_Machinegun",
    "CSGO_Type_Knife",
    "CSGO_Type_Gloves",
    "CSGO_Type_Equipment",   # Zeus x27 and similar
    "CSGO_Type_MusicKit",    # StatTrak / standard music kits
    "Type_CustomPlayer",     # Agent skins
    # CSGO_Type_Spray handled separately below — only sealed graffiti are listed
}


class InventoryPrivateError(Exception):
    """Steam inventory is set to private — must be public for the tracker."""


class InventoryFetchError(Exception):
    """Transient or hard failure fetching the inventory."""


def fetch_inventory(client: httpx.Client, steam_id: str) -> list[dict[str, Any]]:
    """Return all CS2 items from the public Steam inventory of `steam_id`.

    Each item:
      market_hash_name, name_color (rarity hex), inspect_link, asset_id,
      classid, instanceid, is_skin
    """
    items: list[dict[str, Any]] = []
    last_assetid: str | None = None

    while True:
        url = STEAM_INVENTORY_URL.format(steam_id=steam_id)
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
            _LOGGER.warning("Rate limited on inventory, waiting 30s")
            time.sleep(30)
            continue
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

            inspect_link: str | None = None
            for action in desc.get("actions", []):
                link = action.get("link", "")
                if "csgo_econ_action_preview" in link or "csgo" in link.lower():
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

            item_type = next(
                (t.get("internal_name", "") for t in desc.get("tags", [])
                 if t.get("category") == "Type"),
                "",
            )
            # Applied graffiti ("Graffiti | ...") have no active Steam Market
            # listings — only sealed ones ("Sealed Graffiti | ...") do.
            is_skin = item_type in _MARKETABLE_TYPES or (
                item_type == "CSGO_Type_Spray" and name.startswith("Sealed Graffiti |")
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
                    "is_skin": is_skin,
                }
            )

        if not data.get("more_items"):
            break

        last_assetid = data.get("last_assetid")
        _LOGGER.debug("Inventory pagination: next after %s", last_assetid)
        time.sleep(INVENTORY_PAGE_DELAY)

    _LOGGER.info("Fetched %d items from inventory %s", len(items), steam_id)
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
