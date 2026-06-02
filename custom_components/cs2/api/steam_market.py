"""Steam Market priceoverview client with EU-style price normalization
and rate limiting identical to the HACS integration.

Returns a dict {market_hash_name: price_eur}. Items where Steam responds
``success=false``, returns no price, or rate-limits us beyond MAX_RETRIES
are simply absent from the dict — the caller (strict-mode) decides whether
the missing ratio is acceptable.
"""
from __future__ import annotations

import concurrent.futures
import logging
import random
import re
import threading
import time
import urllib.parse

from dataclasses import dataclass

import httpx

from ..const import (
    HEADERS,
    MAX_BACKOFF,
    MAX_RETRIES,
    PAUSE_SECONDS,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    REQUESTS_BEFORE_PAUSE,
    RETRY_BACKOFF_BASE,
    STEAM_MARKET_PRICE_URL,
)
from ..const import DEFAULT_CURRENCY

_DEFAULT_APP_ID = 730  # CS2 — callers always pass app_id explicitly
_PARALLEL_WORKERS = 3   # Steam tolerates ~3 concurrent requests per IP
_BATCH_PAUSE_EVERY = 7  # batches of 3 → ~21 requests per pause window


class _CycleAbort(Exception):
    """Raised by _fetch_one when abort_on_429=True and a 429 is received.

    Propagates through ThreadPoolExecutor.fut.result() to fetch_prices_parallel
    which catches it and returns the prices collected so far (circuit-breaker).
    """


@dataclass(frozen=True)
class RateLimits:
    """Tunable rate-limit knobs for a single fetch pass.

    Defaults mirror the values HACS/skins.ps1 used. The ``patient``
    profile is what we use for the manual --patient one-shot.
    """
    request_delay_min: float = REQUEST_DELAY_MIN
    request_delay_max: float = REQUEST_DELAY_MAX
    requests_before_pause: int = REQUESTS_BEFORE_PAUSE
    pause_seconds: float = PAUSE_SECONDS
    max_retries: int = MAX_RETRIES
    retry_backoff_base: int = RETRY_BACKOFF_BASE
    max_backoff: int = MAX_BACKOFF
    parallel_workers: int = _PARALLEL_WORKERS
    abort_on_429: bool = False  # circuit-breaker: abort the whole pass on first 429

    @classmethod
    def coordinator(cls) -> "RateLimits":
        """Conservative pacing (4.5-6s) stays under the ~20 req/min Steam limit.
        Circuit-breaker on first 429: abort the pass and return prices so far.
        The coordinator retries still-missing items after a 5-min cooldown.
        """
        return cls(
            request_delay_min=4.5,
            request_delay_max=6.0,
            requests_before_pause=15,
            pause_seconds=30,
            max_retries=1,
            retry_backoff_base=1,
            max_backoff=0,
            parallel_workers=1,
            abort_on_429=True,
        )

    @classmethod
    def coordinator_retry(cls) -> "RateLimits":
        """Used for the second pass on still-missing items after the 5-min cooldown.
        Slower and more patient since we're trying to recover from a rate limit.
        """
        return cls(
            request_delay_min=4.5,
            request_delay_max=6.0,
            requests_before_pause=10,
            pause_seconds=30,
            max_retries=3,
            retry_backoff_base=2,
            max_backoff=120,
            parallel_workers=1,
        )

_LOGGER = logging.getLogger(__name__)

MARKET_HEADERS = {**HEADERS, "Referer": "https://steamcommunity.com/market/"}


def _sleep(seconds: float, stop: threading.Event | None) -> bool:
    """Sleep for `seconds`, waking early if `stop` is set. Returns True if stopped."""
    if stop:
        return stop.wait(seconds)
    time.sleep(seconds)
    return False

_CURRENCY_STRIP = re.compile(r"[€$£¥\s ]")
_NON_NUMERIC = re.compile(r"[^0-9.,]")


def fetch_prices(
    client: httpx.Client,
    names: list[str],
    *,
    on_progress=None,
    limits: RateLimits | None = None,
    stop: threading.Event | None = None,
    app_id: int = _DEFAULT_APP_ID,
    currency: int = DEFAULT_CURRENCY,
) -> tuple[dict[str, float], bool]:
    """Sequentially fetch Steam Market prices with rate limiting.

    Returns (prices, circuit_broken) where circuit_broken=True means a 429
    cut the pass short — the caller should apply a cross-cycle backoff.
    """
    rl = limits or RateLimits()
    results: dict[str, float] = {}
    request_count = 0
    total = len(names)
    circuit_broken = False

    for idx, name in enumerate(names, 1):
        if stop and stop.is_set():
            break
        try:
            price = _fetch_one(client, name, rl, stop=stop, app_id=app_id, currency=currency)
        except _CycleAbort:
            _LOGGER.warning(
                "Circuit-breaker at item %d/%d — returning %d prices collected so far",
                idx, total, len(results),
            )
            circuit_broken = True
            break
        if price is not None:
            results[name] = price
        if on_progress:
            on_progress(idx, total, name, price)

        request_count += 1
        if stop and stop.is_set():
            break
        if request_count % rl.requests_before_pause == 0 and idx < total:
            _LOGGER.info(
                "Pausing %ds after %d requests (rate limit prevention)",
                rl.pause_seconds, request_count,
            )
            _sleep(rl.pause_seconds, stop)
        elif idx < total:
            _sleep(random.uniform(rl.request_delay_min, rl.request_delay_max), stop)

    _LOGGER.info("Fetched %d/%d prices from Steam Market", len(results), total)
    return results, circuit_broken


def _fetch_one(
    client: httpx.Client,
    name: str,
    rl: RateLimits,
    stop: threading.Event | None = None,
    app_id: int = _DEFAULT_APP_ID,
    currency: int = DEFAULT_CURRENCY,
) -> float | None:
    encoded = urllib.parse.quote(name)
    url = STEAM_MARKET_PRICE_URL.format(name=encoded, appid=app_id, currency=currency)

    for attempt in range(rl.max_retries):
        if stop and stop.is_set():
            return None
        try:
            resp = client.get(url, headers=MARKET_HEADERS, timeout=30)
        except httpx.HTTPError as err:
            _LOGGER.warning("HTTP error (attempt %d) for %s: %s", attempt + 1, name, type(err).__name__)
            if attempt < rl.max_retries - 1:
                _sleep(rl.retry_backoff_base**attempt, stop)
            continue

        if resp.status_code == 429:
            if rl.abort_on_429:
                _LOGGER.warning(
                    "Rate limited for %s — circuit-breaker, aborting fetch pass",
                    name,
                )
                raise _CycleAbort()
            retry_after = int(resp.headers.get("Retry-After", 0))
            if retry_after > 0:
                backoff = min(rl.max_backoff, int(retry_after * 1.2))
            else:
                backoff = min(
                    rl.max_backoff,
                    30 + random.randint(0, 15) * (rl.retry_backoff_base**attempt),
                )
            _LOGGER.warning(
                "Rate limited for %s (attempt %d) — sleeping %ds",
                name, attempt + 1, backoff,
            )
            _sleep(backoff, stop)
            continue

        if resp.status_code != 200:
            _LOGGER.debug("HTTP %d for %s", resp.status_code, name)
            return None

        try:
            data = resp.json()
        except Exception:
            _LOGGER.debug("Non-JSON response for %s: %s", name, resp.text[:100])
            return None

        if not data.get("success"):
            return None

        raw = data.get("lowest_price") or data.get("median_price")
        price = normalize_price(raw)
        if price is not None:
            _LOGGER.debug("Price for %s: %.2f EUR", name, price)
        return price

    _LOGGER.warning("Giving up on %s after %d attempt(s)", name, rl.max_retries)
    return None


def fetch_prices_parallel(
    client: httpx.Client,
    names: list[str],
    *,
    on_progress=None,
    limits: RateLimits | None = None,
    stop: threading.Event | None = None,
    app_id: int = _DEFAULT_APP_ID,
    currency: int = DEFAULT_CURRENCY,
) -> tuple[dict[str, float], bool]:
    """Fetch prices with bounded parallelism (MAX_WORKERS=3).

    httpx.Client is thread-safe — the connection pool is shared across workers.
    Inter-batch pause every _BATCH_PAUSE_EVERY batches (~21 requests) to avoid
    triggering Steam's rate limiter at the IP level.

    Returns (prices, circuit_broken) where circuit_broken=True means a 429
    cut the pass short — the caller should apply a cross-cycle backoff.
    """
    rl = limits or RateLimits()
    workers = rl.parallel_workers
    results: dict[str, float] = {}
    total = len(names)
    batches = [names[i: i + workers] for i in range(0, total, workers)]
    completed = 0
    circuit_broken = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for batch_idx, batch in enumerate(batches):
            if stop and stop.is_set():
                break

            futures = {
                executor.submit(_fetch_one, client, name, rl, stop=stop, app_id=app_id, currency=currency): name
                for name in batch
            }
            for fut in concurrent.futures.as_completed(futures):
                name = futures[fut]
                completed += 1
                try:
                    price = fut.result()
                except _CycleAbort:
                    _LOGGER.warning(
                        "Circuit-breaker at item %d/%d — returning %d prices collected so far",
                        completed, total, len(results),
                    )
                    circuit_broken = True
                    return results, circuit_broken
                except Exception as exc:
                    _LOGGER.warning("fetch_one raised for %s: %s", name, type(exc).__name__)
                    price = None
                if price is not None:
                    results[name] = price
                if on_progress:
                    on_progress(completed, total, name, price)

            if stop and stop.is_set():
                break

            is_last = batch_idx == len(batches) - 1
            if not is_last:
                if (batch_idx + 1) % _BATCH_PAUSE_EVERY == 0:
                    _LOGGER.info(
                        "Pausing %ds after %d batches (%d requests)",
                        rl.pause_seconds, batch_idx + 1, completed,
                    )
                    _sleep(rl.pause_seconds, stop)
                else:
                    _sleep(random.uniform(rl.request_delay_min, rl.request_delay_max), stop)

    _LOGGER.info("Fetched %d/%d prices from Steam Market (parallel)", len(results), total)
    return results, circuit_broken


def normalize_price(raw: str | None) -> float | None:
    """Parse '12,34 €', '1.234,56€', '$12.34', '12,345.67' → float EUR.

    Identical logic to PowerShell Normalize-Price in skins.ps1, kept so the
    numbers match the historical Excel snapshots when we replay the dataset.
    """
    if not raw:
        return None
    cleaned = _CURRENCY_STRIP.sub("", raw)
    cleaned = _NON_NUMERIC.sub("", cleaned)
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        last_comma = cleaned.rfind(",")
        last_dot = cleaned.rfind(".")
        if last_comma > last_dot:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None
