"""Background historical import — pricehistory → HA recorder statistics."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
from homeassistant.core import HomeAssistant
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)

from .api.steam_history import fetch_item_history, interpolate_gaps
from .const import DOMAIN
from .slugify import make_slug

_LOGGER = logging.getLogger(__name__)

_IMPORT_DELAY = 5.0        # seconds between item fetches — Steam pricehistory
                          # rate-limits at ~1 req/4s; 2.5s triggered 429 → IP ban
                          # after 1-2 items, killing the import. 5s stays safe.
_TOP_ITEMS_LIMIT = 30      # max per-item stat series injected (by descending current value)


async def async_run_import(
    hass: HomeAssistant,
    items: list[dict[str, Any]],
    cookie: str,
    start_date: str | None,
    min_value: float,
    stop=None,
    progress_cb: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """Orchestrate historical import — executor for HTTP, async for recorder."""
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
        progress_cb,
    )

    if result["daily_totals"] and not (stop and stop.is_set()):
        # Select top N items by current value for per-item stat injection
        top_items = sorted(
            [i for i in items if (i.get("current_price") or 0) > 0],
            key=lambda i: (i.get("current_price") or 0) * (i.get("quantity", 1) or 1),
            reverse=True,
        )[:_TOP_ITEMS_LIMIT]
        top_names = {i["name"] for i in top_items}
        top_histories = {
            name: hist
            for name, hist in result["per_item_histories"].items()
            if name in top_names
        }
        await _inject_statistics(
            hass,
            result["daily_totals"],
            result["per_game_totals"],
            top_histories,
        )
    elif stop and stop.is_set():
        _LOGGER.info(
            "CS2 import: stopped early — skipping injection to avoid partial data"
        )

    _LOGGER.info(
        "CS2 import complete: %d global days, %d per-game entries, %d items fetched, "
        "%d skipped, %d per-item series",
        len(result["daily_totals"]),
        sum(len(v) for v in result["per_game_totals"].values()),
        result["fetched"],
        result["skipped"],
        len(result["per_item_histories"]),
    )
    return result


def _sync_fetch_histories(
    items: list[dict[str, Any]],
    cookie: str,
    start_date: str | None,
    min_value: float,
    stop=None,
    progress_cb: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """Synchronous: fetch pricehistory for all items, aggregate daily totals."""
    daily_totals: dict[str, float] = {}
    per_game_totals: dict[str, dict[str, float]] = {}
    per_item_histories: dict[str, dict[str, float]] = {}
    fetched = 0
    skipped = 0
    auth_failures = 0   # consecutive empty results on actually-attempted items

    # Pre-compute total for progress reporting (exclude items below min_value)
    total_to_fetch = sum(
        1 for item in items
        if not (min_value > 0 and (item.get("current_price") or 0) < min_value)
    )

    with httpx.Client() as http:
        for item in items:
            if stop and stop.is_set():
                _LOGGER.info(
                    "CS2 import: interrupted by stop signal after %d items", fetched
                )
                break

            name = item.get("name", item.get("market_hash_name", ""))
            qty = item.get("quantity", 1)
            game_slug = item.get("game_slug", "")
            appid = item.get("appid", 730)

            if min_value > 0 and (item.get("current_price") or 0) < min_value:
                skipped += 1
                continue

            history = fetch_item_history(http, name, cookie, stop=stop, app_id=appid)
            if not history:
                auth_failures += 1
                if auth_failures >= 10 and fetched == 0:
                    _LOGGER.warning(
                        "CS2 import: %d consecutive empty results with 0 successes — "
                        "Steam cookie may be expired or invalid",
                        auth_failures,
                    )
                skipped += 1
                continue
            auth_failures = 0  # reset on any successful fetch

            filled = interpolate_gaps(history)

            item_daily: dict[str, float] = {}
            for ds, price in filled.items():
                if start_date and ds < start_date:
                    continue
                value = price * qty
                daily_totals[ds] = daily_totals.get(ds, 0.0) + value
                if game_slug:
                    game_days = per_game_totals.setdefault(game_slug, {})
                    game_days[ds] = game_days.get(ds, 0.0) + value
                # Per-item: store raw price (not ×qty) so chart shows unit price trend
                item_daily[ds] = price

            if item_daily:
                per_item_histories[name] = item_daily

            fetched += 1
            if progress_cb:
                progress_cb(fetched, total_to_fetch, skipped)

            if stop:
                if stop.wait(_IMPORT_DELAY):
                    _LOGGER.info(
                        "CS2 import: interrupted by stop signal after %d items", fetched
                    )
                    break
            else:
                time.sleep(_IMPORT_DELAY)

    daily_totals = {ds: round(v, 2) for ds, v in daily_totals.items()}
    per_game_totals = {
        slug: {ds: round(v, 2) for ds, v in days.items()}
        for slug, days in per_game_totals.items()
    }
    per_item_histories = {
        name: {ds: round(p, 4) for ds, p in days.items()}
        for name, days in per_item_histories.items()
    }
    return {
        "daily_totals": daily_totals,
        "per_game_totals": per_game_totals,
        "per_item_histories": per_item_histories,
        "fetched": fetched,
        "skipped": skipped,
    }


async def _inject_statistics(
    hass: HomeAssistant,
    daily_totals: dict[str, float],
    per_game_totals: dict[str, dict[str, float]],
    per_item_histories: dict[str, dict[str, float]] | None = None,
) -> None:
    """Inject daily totals into HA recorder as external statistics (idempotent)."""
    # Global portfolio
    await _inject_one_statistic(
        hass,
        statistic_id=f"{DOMAIN}:portfolio_total",
        name="Steam Portfolio Total",
        daily_totals=daily_totals,
    )
    # Per-game breakdown
    for game_slug, game_days in per_game_totals.items():
        await _inject_one_statistic(
            hass,
            statistic_id=f"{DOMAIN}:{game_slug}_total",
            name=f"Steam Portfolio — {game_slug.upper()}",
            daily_totals=game_days,
        )
    # Per-item top N (unit price, not portfolio value)
    if per_item_histories:
        for market_hash_name, item_daily in per_item_histories.items():
            slug = make_slug(market_hash_name)
            await _inject_one_statistic(
                hass,
                statistic_id=f"{DOMAIN}:item_{slug}",
                name=f"Steam Item — {market_hash_name[:60]}",
                daily_totals=item_daily,
            )
        _LOGGER.info(
            "CS2 import: injected per-item stats for %d items", len(per_item_histories)
        )


async def _inject_one_statistic(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    daily_totals: dict[str, float],
) -> None:
    """Inject a single statistic series, skipping dates already recorded."""
    cutoff_date: str | None = None
    try:
        last = await hass.async_add_executor_job(
            get_last_statistics, hass, 1, statistic_id, False, {"mean"}
        )
        if last and statistic_id in last and last[statistic_id]:
            last_start = last[statistic_id][0].get("start")
            if last_start is not None:
                if hasattr(last_start, "date"):
                    cutoff_date = last_start.date().isoformat()
                else:
                    cutoff_date = str(last_start)[:10]
                _LOGGER.info(
                    "CS2 import [%s]: last recorded = %s — skipping older dates",
                    statistic_id,
                    cutoff_date,
                )
    except Exception as err:
        _LOGGER.warning(
            "CS2 import [%s]: could not read last statistics, will inject all: %s",
            statistic_id,
            err,
        )

    metadata = StatisticMetaData(
        has_mean=True,
        has_sum=False,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement="EUR",
    )

    statistics: list[StatisticData] = []
    for ds in sorted(daily_totals):
        if cutoff_date and ds <= cutoff_date:
            continue
        try:
            dt = datetime.fromisoformat(ds).replace(hour=12, minute=0, tzinfo=timezone.utc)
        except ValueError:
            continue
        statistics.append(
            StatisticData(start=dt, state=daily_totals[ds], mean=daily_totals[ds])
        )

    if statistics:
        async_add_external_statistics(hass, metadata, statistics)
        _LOGGER.info(
            "CS2 import [%s]: injected %d new stat points (%d already recorded)",
            statistic_id,
            len(statistics),
            len(daily_totals) - len(statistics),
        )
    else:
        _LOGGER.info(
            "CS2 import [%s]: no new points to inject (all recorded up to %s)",
            statistic_id,
            cutoff_date,
        )
