"""CSGOFloat client — best-effort float values. Returns {} when blocked."""
from __future__ import annotations

import logging
import time
import urllib.parse

import httpx

from ..const import CSGOFLOAT_API_URL, CSGOFLOAT_HEALTHCHECK, CSGOFLOAT_TIMEOUT

_LOGGER = logging.getLogger(__name__)


def fetch_floats(
    client: httpx.Client, items: list[dict]
) -> dict[str, float]:
    """Fetch float for items having an inspect_link. Silent on failure."""
    if not _api_available(client):
        _LOGGER.info("CSGOFloat unavailable — skipping float values")
        return {}

    results: dict[str, float] = {}
    for item in items:
        inspect_link = item.get("inspect_link")
        if not inspect_link:
            continue
        name = item.get("market_hash_name", "")
        val = _fetch_one(client, inspect_link, name)
        if val is not None:
            results[name] = val
        time.sleep(1)

    _LOGGER.info("Retrieved %d float values from CSGOFloat", len(results))
    return results


def _api_available(client: httpx.Client) -> bool:
    try:
        resp = client.get(CSGOFLOAT_HEALTHCHECK, timeout=5)
        if resp.status_code in (200, 404):
            return True
        if resp.status_code in (403, 429, 503):
            return False
    except Exception:
        pass

    # Probe fallback: dummy inspect URL — if API replies "Bots not allowed",
    # we know Valve has flagged the service. Any other 4xx means it's up.
    try:
        dummy = CSGOFLOAT_API_URL.format(
            inspect_url=urllib.parse.quote(
                "steam://rungame/730/76561202255233023/+csgo_econ_action_preview test",
                safe="",
            )
        )
        resp = client.get(dummy, timeout=5)
        if resp.status_code == 400:
            try:
                data = resp.json()
                if "Bots are temporarily not allowed" in data.get("error", ""):
                    return False
            except Exception:
                pass
            return True
        return resp.status_code not in (403, 503)
    except Exception:
        return False


def _fetch_one(client: httpx.Client, inspect_link: str, name: str) -> float | None:
    try:
        encoded = urllib.parse.quote(inspect_link, safe="")
        url = CSGOFLOAT_API_URL.format(inspect_url=encoded)
        resp = client.get(url, timeout=CSGOFLOAT_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            val = data.get("iteminfo", {}).get("floatvalue")
            if val is not None:
                return round(float(val), 6)
    except Exception as err:
        _LOGGER.debug("CSGOFloat error for %s: %s", name, err)
    return None
