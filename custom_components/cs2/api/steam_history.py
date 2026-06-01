"""Steam Market pricehistory client — requires authenticated session cookie."""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse
import bisect
from datetime import date, datetime, timedelta

import httpx

from ..const import HEADERS, STEAM_HISTORY_URL

_LOGGER = logging.getLogger(__name__)

_CURRENCY_DIVISOR = 100.0  # Steam returns prices in cents for EUR (currency=3)


def _decode_cookie(raw: str) -> str:
    """URL-decode cookie value (browsers copy it URL-encoded)."""
    decoded = urllib.parse.unquote(raw)
    # Block CRLF/NUL and cookie injection chars (; , space) that could fragment the header
    if any(c in decoded for c in ("\r", "\n", "\x00", ";", ",", " ", "\t")):
        raise ValueError("Invalid cookie value: forbidden characters")
    return decoded


def fetch_item_history(
    http: httpx.Client,
    market_hash_name: str,
    cookie: str,
    stop: threading.Event | None = None,
    app_id: int = 730,
) -> dict[str, float]:
    """Fetch daily max price for one item.

    Returns {iso_date: price_eur} — dates with no transaction are absent.
    Steam returns hourly candles; we take the daily high.
    Retries once on 429 (30s wait) before giving up.
    """
    url = STEAM_HISTORY_URL.format(appid=app_id, name=urllib.parse.quote(market_hash_name))
    headers = {
        **HEADERS,
        "Cookie": f"steamLoginSecure={_decode_cookie(cookie)}",
        "Referer": "https://steamcommunity.com/market/",
    }
    data: dict | None = None
    for attempt in range(2):
        try:
            resp = http.get(url, headers=headers, timeout=30)
        except httpx.HTTPError as err:
            _LOGGER.warning("pricehistory fetch failed for %s: %s", market_hash_name, type(err).__name__)
            return {}

        if resp.status_code == 429:
            if attempt >= 1:
                _LOGGER.warning("pricehistory 429 for %s — giving up after 2 attempts", market_hash_name)
                return {}
            backoff = 30
            _LOGGER.warning("pricehistory 429 for %s — retrying in %ds", market_hash_name, backoff)
            if stop:
                if stop.wait(backoff):
                    return {}
            else:
                time.sleep(backoff)
            continue

        if resp.status_code != 200:
            # Log only status code — full repr may include Cookie header via httpx
            _LOGGER.warning("pricehistory fetch failed for %s: HTTP %d", market_hash_name, resp.status_code)
            return {}

        try:
            data = resp.json()
        except Exception as err:
            _LOGGER.warning("pricehistory fetch failed for %s: %s", market_hash_name, type(err).__name__)
            return {}
        break

    if data is None:
        return {}

    if not data.get("success"):
        _LOGGER.debug("pricehistory: success=false for %s", market_hash_name)
        return {}

    daily: dict[str, float] = {}
    for entry in data.get("prices", []):
        # entry = ["Nov 01 2021 01: +0", "12.34", "5"]
        try:
            raw_date, raw_price, _ = entry
            # Steam always uses English abbreviated month names (e.g. "Nov 01 2021 01: +0")
            # Trim to first 11 chars → "Nov 01 2021", try short then long month names
            date_str = raw_date[:11].strip()
            dt = None
            for fmt in ("%b %d %Y", "%B %d %Y", "%b  %d %Y"):
                try:
                    dt = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                _LOGGER.debug("Unrecognised Steam date format: %r — skipping entry", raw_date)
                continue
            ds = dt.date().isoformat()
            price = float(raw_price)
            if ds not in daily or price > daily[ds]:
                daily[ds] = price
        except Exception:
            continue

    return daily


def interpolate_gaps(history: dict[str, float]) -> dict[str, float]:
    """Linear interpolation for dates missing between first and last known price.

    O(days × log n) via bisect — was O(days × n) with linear scans.
    """
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
            # bisect_left returns the insertion point for ds in the sorted list.
            # dates[idx-1] is the last known date before ds (predecessor).
            # dates[idx]   is the first known date after ds (successor).
            idx = bisect.bisect_left(dates, ds)
            prev = dates[idx - 1] if idx > 0 else None
            nxt = dates[idx] if idx < len(dates) else None
            if prev and nxt:
                p0, p1 = history[prev], history[nxt]
                dp = date.fromisoformat(prev)
                dn = date.fromisoformat(nxt)
                frac = (cur - dp).days / (dn - dp).days
                filled[ds] = round(p0 + (p1 - p0) * frac, 4)
        cur += timedelta(days=1)
    return filled
