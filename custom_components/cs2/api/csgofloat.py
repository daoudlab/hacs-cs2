"""CSGOFloat client — best-effort float values. Returns {} when blocked."""
from __future__ import annotations

import logging
import time
import urllib.parse

import httpx

from ..const import CSGOFLOAT_API_URL, CSGOFLOAT_HEALTHCHECK, CSGOFLOAT_TIMEOUT

_LOGGER = logging.getLogger(__name__)


def fetch_floats(
    client: httpx.Client,
    items: list[dict],
    cached: dict[str, float] | None = None,
    stop=None,
) -> dict[str, float]:
    """Fetch float for items having an inspect_link. Silent on failure.

    ``cached`` maps asset_id → float_value; items already cached are skipped.
    ``stop`` is a threading.Event for interruptible sleep between requests.
    """
    if not _api_available(client):
        _LOGGER.info("CSGOFloat unavailable — skipping float values")
        return {}

    cached = cached or {}
    results: dict[str, float] = {}
    for item in items:
        if stop and stop.is_set():
            break
        inspect_link = item.get("inspect_link")
        if not inspect_link:
            continue
        name = item.get("market_hash_name", "")
        asset_id = item.get("asset_id", name)
        if asset_id in cached:
            results[name] = cached[asset_id]
            continue
        val = _fetch_one(client, inspect_link, name)
        if val is not None:
            results[name] = val
        if stop:
            stop.wait(1.5)
        else:
            time.sleep(1.5)

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
