"""Steam inventory client — public profile, no cookie required.

Uses the generic Steam inventory API (game-agnostic). Items with
``marketable: 1`` in the Steam API response are tracked — this is the
authoritative flag for items tradeable on the Steam Community Market.

Note: inventory requests use urllib (not httpx) — httpx's TLS fingerprint
triggers Steam's bot-detection on the inventory endpoint, while urllib passes.
"""
from __future__ import annotations

import json as _json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any

from ..const import (
    HEADERS,
    INVENTORY_PAGE_DELAY,
    STEAM_INVENTORY_URL,
)

_DEFAULT_APP_ID = 730   # CS2 — callers always pass app_id explicitly
_DEFAULT_CONTEXT_ID = 2

_LOGGER = logging.getLogger(__name__)


class InventoryPrivateError(Exception):
    """Steam inventory is set to private — must be public for the tracker."""


class InventoryFetchError(Exception):
    """Transient or hard failure fetching the inventory."""


class InventoryBannedError(InventoryFetchError):
    """Steam returned 401 — IP soft-ban, cooldown required before retry."""


class InventoryRateLimitedError(InventoryFetchError):
    """Steam returned 429 on inventory after retries — short cooldown required."""


class _Resp:
    """Minimal response wrapper around urllib so callers stay unchanged."""
    def __init__(self, status_code: int, body: bytes = b"") -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return _json.loads(self._body)

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")


def _get(url: str, timeout: int = 30) -> _Resp:
    """urllib GET with browser-like headers — avoids httpx TLS fingerprint block."""
    # Enforce https — URLs are built from constant Steam templates; this guards
    # against SSRF / file:// if a template were ever parameterised by host.
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url[:32]}")
    req = urllib.request.Request(url, headers=HEADERS)  # nosec B310 - https enforced above
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            return _Resp(r.status, r.read())
    except urllib.error.HTTPError as e:
        return _Resp(e.code, b"")
    except Exception as err:
        raise InventoryFetchError(f"HTTP error: {err}") from err


def check_inventory_count(
    steam_id: str,
    app_id: int,
    context_id: int,
    stop=None,
) -> int:
    """Return total_inventory_count for a game, 0 on error/private/empty, -1 on 429."""
    url = STEAM_INVENTORY_URL.format(
        steam_id=steam_id, appid=app_id, contextid=context_id
    ) + "&count=1"
    try:
        resp = _get(url, timeout=10)
        if resp.status_code == 403:
            return 0  # private
        if resp.status_code == 429:
            _LOGGER.debug("Rate limited during discovery for appid=%d", app_id)
            if stop:
                stop.wait(5)
            else:
                time.sleep(5)
            return -1  # sentinel: caller must abort discovery and preserve cached apps
        if resp.status_code != 200:
            return 0
        data = resp.json()
        return int(data.get("total_inventory_count", 0))
    except Exception:
        return 0


def fetch_inventory(
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
            resp = _get(url, timeout=30)
        except InventoryFetchError:
            raise

        if resp.status_code == 403:
            raise InventoryPrivateError(
                f"Steam inventory {steam_id} is private (HTTP 403)"
            )
        if resp.status_code == 429:
            page_429_count += 1
            if page_429_count >= 3:
                raise InventoryRateLimitedError(
                    f"Steam inventory 429 after 3 attempts for {steam_id} ({len(items)} items fetched)"
                )
            # Exponential backoff: 15s first, 30s second, then raise
            backoff = 15 * page_429_count
            _LOGGER.warning(
                "Rate limited on inventory, waiting %ds (attempt %d/3)",
                backoff, page_429_count,
            )
            if stop:
                stop.wait(backoff)
            else:
                time.sleep(backoff)
            continue
        if resp.status_code == 401:
            # Temporary IP soft-ban — coordinator will apply cooldown and fall back to stale data
            _LOGGER.warning("Inventory 401 for %s (IP rate-limited?) — %d items fetched so far", steam_id, len(items))
            raise InventoryBannedError(f"Steam inventory 401 (IP banned?) after {len(items)} items")
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
            if icon_raw and re.match(r'^[A-Za-z0-9_\-/+=]+$', icon_raw):
                entity_picture = f"https://community.steamstatic.com/economy/image/{icon_raw}"
            else:
                entity_picture = None

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


