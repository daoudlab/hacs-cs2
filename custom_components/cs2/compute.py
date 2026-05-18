"""Per-item and portfolio metrics, ported from the HACS coordinator.

Output schema is deliberately identical to the HACS sensors so that the
existing 363 KB custom dashboard keeps working with no edits.
"""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

from .const import STEAM_TAX
from .slugify import make_slug


def compute_item_metrics(
    inventory: list[dict],
    prices: dict[str, float],
    floats: dict[str, float],
    previous_prices: dict[str, float],
    buy_prices: dict[str, float],
    reference_prices: dict[str, float],
    prices_24h: dict[str, float] | None = None,
    prices_7d: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Compute per-item metrics keyed by market_hash_name.

    ``previous_prices`` is keyed by market_hash_name (not slug) — that's the
    only behavioural divergence from the HACS version (which used a
    composite slug when an account_slug was set). Here we always operate
    on a single Steam account.
    """
    results: list[dict[str, Any]] = []

    # The Steam inventory legitimately contains duplicate market_hash_names
    # (the user can own several copies of the same skin). We collapse those
    # into one HA sensor per unique name — keeping the first occurrence's
    # metadata (colour, inspect link) — and record the count under
    # ``quantity`` so the portfolio total can multiply price × qty.
    counts: dict[str, int] = {}
    first_seen: dict[str, dict] = {}
    order: list[str] = []
    for item in inventory:
        name = item["market_hash_name"]
        counts[name] = counts.get(name, 0) + 1
        if name not in first_seen:
            first_seen[name] = item
            order.append(name)

    for name in order:
        item = first_seen[name]
        qty = counts[name]
        current = prices.get(name)
        buy = buy_prices.get(name, 0.0)
        ref = reference_prices.get(name, 0.0)
        prev = previous_prices.get(name)

        if 0 < buy < 0.50:
            _LOGGER.warning("Low buy_price %.2f for %s — ROI may be misleading", buy, name)
        roi = (
            round(((current - buy) / buy) * 100, 2)
            if buy >= 0.10 and current is not None
            else None
        )
        delta_yesterday = (
            round(current - prev, 2)
            if prev is not None and current is not None
            else None
        )
        delta_since_crash = (
            round(current - ref, 2) if ref > 0 and current is not None else None
        )
        delta_from_start = (
            round(current - buy, 2) if buy > 0 and current is not None else None
        )
        p24 = (prices_24h or {}).get(name)
        p7d = (prices_7d or {}).get(name)
        delta_24h = round(current - p24, 2) if current is not None and p24 is not None else None
        delta_7d = round(current - p7d, 2) if current is not None and p7d is not None else None

        results.append(
            {
                "name": name,
                "slug": make_slug(name),
                "quantity": qty,
                "current_price": round(current, 2) if current is not None else None,
                "buy_price": round(buy, 2) if buy else None,
                "before_crash": round(ref, 2) if ref else None,
                "delta_yesterday": delta_yesterday,
                "delta_24h": delta_24h,
                "delta_7d": delta_7d,
                "delta_since_crash": delta_since_crash,
                "delta_from_start": delta_from_start,
                "roi": roi,
                "rarity_color": item.get("name_color"),
                "float_value": floats.get(name),
                "entity_picture": item.get("entity_picture"),
            }
        )

    return results


def compute_global_metrics(
    items_data: list[dict],
    previous_total: float | None,
) -> dict[str, Any]:
    """Portfolio aggregates — schema identical to HACS sensor.cs2_inventory_total."""
    total_value = sum(
        item["current_price"] * item.get("quantity", 1)
        for item in items_data
        if item["current_price"] is not None
    )
    # P&L: only sum items where we know the cost basis, so value and buy are comparable
    pl_items = [i for i in items_data if i.get("buy_price") and i["buy_price"] > 0]
    total_buy = sum(i["buy_price"] * i.get("quantity", 1) for i in pl_items)
    total_value_pl = sum(
        i["current_price"] * i.get("quantity", 1)
        for i in pl_items
        if i["current_price"] is not None
    )

    profit_brut = round(total_value_pl - total_buy, 2) if total_buy > 0 else None
    profit_net = round(profit_brut * STEAM_TAX, 2) if profit_brut is not None else None
    total_net = round(total_value * STEAM_TAX, 2)
    roi_global = (
        round((profit_brut / total_buy) * 100, 2)
        if profit_brut is not None and total_buy > 0
        else None
    )
    delta = (
        round(total_value - previous_total, 2)
        if previous_total and previous_total > 0
        else None
    )

    items_with_roi = [item for item in items_data if item["roi"] is not None]
    best = max(items_with_roi, key=lambda x: x["roi"], default=None)
    worst = min(items_with_roi, key=lambda x: x["roi"], default=None)

    return {
        "total_value": round(total_value, 2),
        "total_net": total_net,
        "total_buy": round(total_buy, 2) if total_buy else None,
        "profit_brut": profit_brut,
        "profit_net": profit_net,
        "roi_global": roi_global,
        "delta": delta,
        "items_count": len(items_data),
        "items_total_qty": sum(item.get("quantity", 1) for item in items_data),
        "items_with_price": sum(
            1 for item in items_data if item["current_price"] is not None
        ),
        "best_performer_name": best["name"] if best else None,
        "best_performer_roi": best["roi"] if best else None,
        "worst_performer_name": worst["name"] if worst else None,
        "worst_performer_roi": worst["roi"] if worst else None,
    }
