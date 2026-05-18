"""Steam Market pricehistory client — requires authenticated session cookie."""
from __future__ import annotations

import logging
import time
import urllib.parse
from datetime import date, datetime, timedelta

import httpx

from ..const import HEADERS, STEAM_HISTORY_URL

_LOGGER = logging.getLogger(__name__)

_CURRENCY_DIVISOR = 100.0  # Steam returns prices in cents for EUR (currency=3)


def _decode_cookie(raw: str) -> str:
    """URL-decode cookie value (browsers copy it URL-encoded)."""
    decoded = urllib.parse.unquote(raw)
    if any(c in decoded for c in ("\r", "\n", "\x00")):
        raise ValueError("Invalid cookie value: CRLF/NUL characters forbidden")
    return decoded


def fetch_item_history(
    http: httpx.Client,
    market_hash_name: str,
    cookie: str,
) -> dict[str, float]:
    """Fetch daily max price for one item.

    Returns {iso_date: price_eur} — dates with no transaction are absent.
    Steam returns hourly candles; we take the daily high.
    """
    url = STEAM_HISTORY_URL.format(name=urllib.parse.quote(market_hash_name))
    headers = {
        **HEADERS,
        "Cookie": f"steamLoginSecure={_decode_cookie(cookie)}",
        "Referer": "https://steamcommunity.com/market/",
    }
    try:
        resp = http.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as err:
        # Log only status code — full httpx exception repr may include Cookie header
        _LOGGER.warning("pricehistory fetch failed for %s: HTTP %d", market_hash_name, err.response.status_code)
        return {}
    except Exception as err:
        _LOGGER.warning("pricehistory fetch failed for %s: %s", market_hash_name, type(err).__name__)
        return {}

    if not data.get("success"):
        _LOGGER.debug("pricehistory: success=false for %s", market_hash_name)
        return {}

    daily: dict[str, float] = {}
    for entry in data.get("prices", []):
        # entry = ["Nov 01 2021 01: +0", "12.34", "5"]
        try:
            raw_date, raw_price, _ = entry
            dt = datetime.strptime(raw_date[:11].strip(), "%b %d %Y")
            ds = dt.date().isoformat()
            price = float(raw_price)
            if ds not in daily or price > daily[ds]:
                daily[ds] = price
        except Exception:
            continue

    return daily


def interpolate_gaps(history: dict[str, float]) -> dict[str, float]:
    """Linear interpolation for dates missing between first and last known price."""
    if len(history) < 2:
        return dict(history)

    dates = sorted(history)
    d0 = date.fromisoformat(dates[0])
    d1 = date.fromisoformat(dates[-1])
    filled: dict[str, float] = {}
    cur = d0
    while cur <= d1:
        ds = cur.isoformat()
        if ds in history:
            filled[ds] = history[ds]
        else:
            prev = next((d for d in reversed(dates) if d < ds), None)
            nxt = next((d for d in dates if d > ds), None)
            if prev and nxt:
                p0, p1 = history[prev], history[nxt]
                dp = date.fromisoformat(prev)
                dn = date.fromisoformat(nxt)
                frac = (cur - dp).days / (dn - dp).days
                filled[ds] = round(p0 + (p1 - p0) * frac, 4)
        cur += timedelta(days=1)
    return filled
