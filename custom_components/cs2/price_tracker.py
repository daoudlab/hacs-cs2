"""Rolling price fetcher — picks the stalest items each cycle.

Extracted from coordinator.py so the fetch-and-timestamp logic is isolated
and independently testable without spinning up a full HA coordinator.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from .api import steam_market
from .api.steam_market import RateLimits
from .const import DEFAULT_CURRENCY, DEFAULT_FETCH_CHUNK_SIZE

_LOGGER = logging.getLogger(__name__)


class RollingPriceFetcher:
    """Manages per-item fetch timestamps and picks the stalest items each cycle.

    Items that have never been fetched have an implicit timestamp of 0 and
    always go first, ensuring the entire inventory is covered over time.
    Each ``fetch()`` call selects at most ``chunk_size`` items, fetches their
    prices, and records the attempt timestamp regardless of success, so items
    are rotated out after each attempt.
    """

    def __init__(self, chunk_size: int = DEFAULT_FETCH_CHUNK_SIZE) -> None:
        self._chunk_size = chunk_size
        self.timestamps: dict[str, float] = {}  # market_hash_name → epoch of last attempt

    def load_timestamps(self, timestamps: dict[str, float]) -> None:
        """Restore timestamps from a persisted store (called on coordinator load)."""
        self.timestamps = dict(timestamps)

    def prune(self, active_names: set[str]) -> None:
        """Drop timestamps for items no longer in any active inventory or watchlist.

        Prevents unbounded growth on large inventories where items rotate out
        over time.  Call once per cycle after the game loop with the union of
        all active item names and watchlist names.
        """
        self.timestamps = {k: v for k, v in self.timestamps.items() if k in active_names}

    def fetch(
        self,
        http: httpx.Client,
        names_to_fetch: list[str],
        limits: RateLimits,
        stop: threading.Event | None,
        app_id: int,
        currency: int = DEFAULT_CURRENCY,
    ) -> tuple[dict[str, float], bool]:
        """Fetch prices for the ``chunk_size`` stalest items from ``names_to_fetch``.

        Updates internal timestamps for every attempted item (even those that
        returned None) so they rotate out of the priority queue.

        Returns (prices, circuit_broken) where circuit_broken=True means a 429
        cut the pass short and the coordinator should apply a cross-cycle backoff.
        """
        if not names_to_fetch:
            return {}, False

        sorted_by_age = sorted(
            names_to_fetch,
            key=lambda n: self.timestamps.get(n, 0.0),
        )
        chunk = sorted_by_age[: self._chunk_size]
        attempted: list[str] = []

        def _on_progress(idx: int, total: int, name: str, price: float | None) -> None:
            attempted.append(name)

        fresh_prices, circuit_broken = steam_market.fetch_prices_parallel(
            http,
            chunk,
            on_progress=_on_progress,
            limits=limits,
            stop=stop,
            app_id=app_id,
            currency=currency,
        )

        now = time.time()
        for name in attempted:
            self.timestamps[name] = now

        return fresh_prices, circuit_broken
