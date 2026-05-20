"""Background historical import job — pricehistory → HA recorder statistics."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from homeassistant.core import HomeAssistant
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.util import dt as dt_util

from .api.steam_history import fetch_item_history, interpolate_gaps
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_IMPORT_DELAY = 2.5  # seconds between item fetches (polite rate-limit)


async def async_run_import(
    hass: HomeAssistant,
    items: list[dict[str, Any]],
    cookie: str,
    start_date: str | None,
    min_value: float,
    stop=None,
) -> dict[str, Any]:
    """Orchestrate the historical import — runs in executor for HTTP, async for recorder."""
    _LOGGER.info(
        "CS2 import: starting for %d items, start_date=%s, min_value=%.2f",
        len(items),
        start_date or "all",
        min_value,
    )

    result = await hass.async_add_executor_job(
        _sync_fetch_histories,
        items,
        cookie,
        start_date,
        min_value,
        stop,
    )

    if result["daily_totals"] and not (stop and stop.is_set()):
        await _inject_statistics(hass, result["daily_totals"])
    elif stop and stop.is_set():
        _LOGGER.info("CS2 import: stopped early — skipping statistic injection to avoid partial data")

    _LOGGER.info(
        "CS2 import complete: %d days, %d items fetched, %d skipped",
        len(result["daily_totals"]),
        result["fetched"],
        result["skipped"],
    )
    return result


def _sync_fetch_histories(
    items: list[dict[str, Any]],
    cookie: str,
    start_date: str | None,
    min_value: float,
    stop=None,
) -> dict[str, Any]:
    """Synchronous: fetch pricehistory for all items, aggregate daily totals."""
    daily_totals: dict[str, float] = {}
    fetched = 0
    skipped = 0

    with httpx.Client() as http:
        for item in items:
            if stop and stop.is_set():
                _LOGGER.info("CS2 import: interrupted by stop signal after %d items", fetched)
                break

            name = item.get("name", item.get("market_hash_name", ""))
            qty = item.get("quantity", 1)

            if min_value > 0 and (item.get("current_price") or 0) < min_value:
                skipped += 1
                continue

            history = fetch_item_history(http, name, cookie)
            if not history:
                skipped += 1
                continue

            filled = interpolate_gaps(history)

            for ds, price in filled.items():
                if start_date and ds < start_date:
                    continue
                daily_totals[ds] = daily_totals.get(ds, 0.0) + price * qty

            fetched += 1
            if stop:
                if stop.wait(_IMPORT_DELAY):
                    _LOGGER.info("CS2 import: interrupted by stop signal after %d items", fetched)
                    break
            else:
                time.sleep(_IMPORT_DELAY)

    # Round all totals
    daily_totals = {ds: round(v, 2) for ds, v in daily_totals.items()}
    return {"daily_totals": daily_totals, "fetched": fetched, "skipped": skipped}


async def _inject_statistics(
    hass: HomeAssistant,
    daily_totals: dict[str, float],
) -> None:
    """Inject daily totals into HA recorder as external statistics (idempotent)."""
    statistic_id = f"{DOMAIN}:portfolio_total"
    unit = "EUR"

    # Idempotence: find the last already-recorded date and skip older points
    cutoff_date: str | None = None
    try:
        last = await get_last_statistics(hass, 1, statistic_id, False, {"mean"})
        if last and statistic_id in last and last[statistic_id]:
            last_start = last[statistic_id][0].get("start")
            if last_start is not None:
                if hasattr(last_start, "date"):
                    cutoff_date = last_start.date().isoformat()
                else:
                    cutoff_date = str(last_start)[:10]
                _LOGGER.info(
                    "CS2 import: last recorded stat = %s — skipping older dates",
                    cutoff_date,
                )
    except Exception as err:
        _LOGGER.warning("CS2 import: could not read last statistics, will inject all: %s", err)

    metadata = StatisticMetaData(
        has_mean=True,
        has_sum=False,
        name="CS2 Portfolio Total",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=unit,
    )

    statistics: list[StatisticData] = []
    for ds in sorted(daily_totals):
        if cutoff_date and ds <= cutoff_date:
            continue  # already imported
        try:
            dt = datetime.fromisoformat(ds).replace(
                hour=12, minute=0, tzinfo=timezone.utc
            )
        except ValueError:
            continue
        statistics.append(
            StatisticData(
                start=dt,
                state=daily_totals[ds],
                mean=daily_totals[ds],
            )
        )

    if statistics:
        async_add_external_statistics(hass, metadata, statistics)
        _LOGGER.info("CS2 import: injected %d new stat points (skipped=%d already recorded)",
                     len(statistics), len(daily_totals) - len(statistics))
    else:
        _LOGGER.info("CS2 import: no new stat points to inject (all already recorded up to %s)", cutoff_date)
