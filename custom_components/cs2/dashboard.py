"""Generate Lovelace dashboard YAML files for the Steam Inventory integration."""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import yaml

from .const import SENSOR_TOTAL_ID, SENSOR_GAME_PREFIX, SENSOR_ITEM_PREFIX, SENSOR_SYNC_ID, SENSOR_WATCHLIST_PREFIX

_LOGGER = logging.getLogger(__name__)


def generate_dashboards(data: dict[str, Any], out_dir: str) -> list[str]:
    """Write dashboard YAML files to `out_dir`. Returns list of written filenames."""
    files = []

    global_path = os.path.join(out_dir, "steam_global.yaml")
    with open(global_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(_global_dashboard(data), f, allow_unicode=True, sort_keys=False)
    files.append("steam_global.yaml")

    for slug, game in data.get("per_game", {}).items():
        if not re.match(r'^[a-z0-9_]+$', slug):
            _LOGGER.warning("Skipping dashboard for unsafe slug: %r", slug)
            continue
        filename = f"steam_{slug}.yaml"
        game_path = os.path.join(out_dir, filename)
        with open(game_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(_game_dashboard(slug, game), f, allow_unicode=True, sort_keys=False)
        files.append(filename)

    watchlist = data.get("watchlist", [])
    if watchlist:
        wl_path = os.path.join(out_dir, "steam_watchlist.yaml")
        with open(wl_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(_watchlist_dashboard(watchlist), f, allow_unicode=True, sort_keys=False)
        files.append("steam_watchlist.yaml")

    return files


# ── Global dashboard ────────────────────────────────────────────────────────────

def _global_dashboard(data: dict) -> dict:
    per_game = data.get("per_game", {})
    g = data.get("global", {})
    watchlist = data.get("watchlist", [])

    game_glance = [
        {
            "entity": f"{SENSOR_GAME_PREFIX}{slug}_total",
            "name": game["name"],
        }
        for slug, game in per_game.items()
    ]

    all_game_entities = [f"{SENSOR_GAME_PREFIX}{slug}_total" for slug in per_game]

    cards_overview: list[dict] = [
        {
            "type": "entity",
            "entity": SENSOR_TOTAL_ID,
            "name": "Portfolio Steam",
            "icon": "mdi:currency-eur",
        },
    ]

    if game_glance:
        cards_overview.append({
            "type": "glance",
            "title": "Par jeu",
            "entities": game_glance,
            "show_state": True,
            "show_name": True,
        })

    cards_overview += [
        {
            "type": "history-graph",
            "title": "7 derniers jours",
            "entities": [{"entity": SENSOR_TOTAL_ID, "name": "Portfolio total"}],
            "hours_to_show": 168,
            "refresh_interval": 0,
        },
        {
            "type": "statistics-graph",
            "title": "Historique long terme",
            "entities": [SENSOR_TOTAL_ID],
            "stat_types": ["state"],
            "period": {"calendar": {"period": "month"}},
        },
        {
            "type": "entities",
            "title": "Synchronisation",
            "entities": [
                {
                    "entity": SENSOR_SYNC_ID,
                    "name": "Statut sync",
                    "icon": "mdi:sync",
                },
            ],
            "show_header_toggle": False,
        },
    ]

    views = [
        {
            "title": "Portfolio",
            "icon": "mdi:steam",
            "cards": cards_overview,
        }
    ]

    # Performance view — multi-game LTS comparison
    if all_game_entities:
        perf_entities = [SENSOR_TOTAL_ID] + all_game_entities[:4]
        views.append({
            "title": "Performance",
            "icon": "mdi:chart-line",
            "cards": [
                {
                    "type": "statistics-graph",
                    "title": "Comparaison jeux (mensuel)",
                    "entities": perf_entities,
                    "stat_types": ["state"],
                    "period": {"calendar": {"period": "month"}},
                },
                {
                    "type": "history-graph",
                    "title": "Comparaison 7j",
                    "entities": [
                        {"entity": e, "name": per_game.get(e.replace(SENSOR_GAME_PREFIX, "").replace("_total", ""), {}).get("name", e)}
                        for e in all_game_entities[:4]
                    ] + [{"entity": SENSOR_TOTAL_ID, "name": "Total"}],
                    "hours_to_show": 168,
                },
            ],
        })

    # Watchlist view
    if watchlist:
        wl_entities = [
            {
                "entity": f"{SENSOR_WATCHLIST_PREFIX}{w['slug']}",
                "name": w["market_hash_name"][:40],
            }
            for w in watchlist
            if w.get("slug")
        ]
        views.append({
            "title": "Watchlist",
            "icon": "mdi:eye",
            "cards": [
                {
                    "type": "entities",
                    "title": f"Watchlist ({len(watchlist)} items)",
                    "entities": wl_entities,
                    "show_header_toggle": False,
                },
            ],
        })

    return {"title": "Steam Inventory", "views": views}


# ── Per-game dashboard ──────────────────────────────────────────────────────────

def _game_dashboard(slug: str, game: dict) -> dict:
    game_name = game.get("name", slug)
    items = game.get("items", [])
    metrics = game.get("metrics", {})
    total_entity = f"{SENSOR_GAME_PREFIX}{slug}_total"

    # Sort by current value desc
    sorted_items = sorted(items, key=lambda i: (i.get("current_price") or 0) * (i.get("quantity") or 1), reverse=True)

    # Items with buy price (for P&L view)
    pl_items = [i for i in sorted_items if i.get("buy_price")]

    def _item_entity(item: dict, *, secondary: str | None = None) -> dict:
        entity = f"{SENSOR_ITEM_PREFIX}{item['slug']}"
        entry: dict = {"entity": entity, "name": item["name"][:40]}
        if secondary:
            entry["secondary_info"] = secondary
        return entry

    def _glance_item(item: dict) -> dict:
        return {
            "entity": f"{SENSOR_ITEM_PREFIX}{item['slug']}",
            "name": item["name"][:30],
        }

    # Top 10 for glance (entity_picture shows as icon in glance)
    top_glance = [_glance_item(i) for i in sorted_items[:12]]

    # All items entity list
    all_entity_list = [_item_entity(i) for i in sorted_items]

    cards_portfolio: list[dict] = [
        {
            "type": "entity",
            "entity": total_entity,
            "name": f"Total {game_name}",
            "icon": "mdi:currency-eur",
        },
    ]

    if top_glance:
        cards_portfolio.append({
            "type": "glance",
            "title": f"Top items",
            "entities": top_glance,
            "show_state": True,
            "show_name": True,
            "show_icon": True,
            "columns": 5,
        })

    if all_entity_list:
        cards_portfolio.append({
            "type": "entities",
            "title": f"Items ({len(items)})",
            "entities": all_entity_list,
            "show_header_toggle": False,
        })

    cards_portfolio += [
        {
            "type": "history-graph",
            "title": f"Historique {game_name} (7j)",
            "entities": [{"entity": total_entity, "name": game_name}],
            "hours_to_show": 168,
            "refresh_interval": 0,
        },
        {
            "type": "statistics-graph",
            "title": f"Statistiques LTS — {game_name}",
            "entities": [total_entity],
            "stat_types": ["state"],
            "period": {"calendar": {"period": "month"}},
        },
    ]

    views = [
        {
            "title": game_name,
            "icon": "mdi:steam",
            "cards": cards_portfolio,
        }
    ]

    # P&L view — items with buy price
    if pl_items:
        pl_entity_list: list[dict] = []
        for item in pl_items:
            entity_id = f"{SENSOR_ITEM_PREFIX}{item['slug']}"
            pl_entity_list.append({"entity": entity_id, "name": item["name"][:40]})
            if item.get("roi") is not None:
                pl_entity_list.append({
                    "entity": entity_id,
                    "name": "ROI",
                    "type": "attribute",
                    "attribute": "roi",
                    "suffix": "%",
                })
            if item.get("buy_price") is not None:
                pl_entity_list.append({
                    "entity": entity_id,
                    "name": "Prix achat",
                    "type": "attribute",
                    "attribute": "buy_price",
                    "suffix": " €",
                })
            pl_entity_list.append({"type": "divider"})

        views.append({
            "title": "P&L",
            "icon": "mdi:chart-bar",
            "cards": [
                {
                    "type": "entities",
                    "title": f"P&L par item ({len(pl_items)} avec prix achat)",
                    "entities": pl_entity_list,
                    "show_header_toggle": False,
                },
            ],
        })

    # Float view for CS2 (slug == "cs2")
    if slug == "cs2":
        float_items = [i for i in sorted_items if i.get("float_value") is not None]
        if float_items:
            float_list = []
            for item in float_items[:20]:
                entity_id = f"{SENSOR_ITEM_PREFIX}{item['slug']}"
                float_list.append({"entity": entity_id, "name": item["name"][:40]})
                float_list.append({
                    "entity": entity_id,
                    "name": "Float",
                    "type": "attribute",
                    "attribute": "float_value",
                })
                float_list.append({"type": "divider"})

            views.append({
                "title": "Floats",
                "icon": "mdi:decimal",
                "cards": [
                    {
                        "type": "entities",
                        "title": f"Float values CS2 ({len(float_items)} items)",
                        "entities": float_list,
                        "show_header_toggle": False,
                    },
                ],
            })

    return {"title": f"Steam — {game_name}", "views": views}


# ── Watchlist dashboard ─────────────────────────────────────────────────────────

def _watchlist_dashboard(watchlist: list[dict]) -> dict:
    below_target = [w for w in watchlist if w.get("current_price") is not None and w.get("target_price") is not None and w["current_price"] <= w["target_price"]]
    above_target = [w for w in watchlist if w not in below_target]

    def _wl_entities(items: list[dict]) -> list[dict]:
        result = []
        for w in items:
            slug = w.get("slug", "")
            if not slug:
                continue
            entity_id = f"{SENSOR_WATCHLIST_PREFIX}{slug}"
            result.append({"entity": entity_id, "name": w["market_hash_name"][:40]})
            if w.get("target_price"):
                result.append({
                    "entity": entity_id,
                    "name": "Cible",
                    "type": "attribute",
                    "attribute": "target_price",
                    "suffix": " €",
                })
            if w.get("note"):
                result.append({
                    "entity": entity_id,
                    "name": "Note",
                    "type": "attribute",
                    "attribute": "note",
                })
            result.append({"type": "divider"})
        return result

    cards = []
    if below_target:
        cards.append({
            "type": "entities",
            "title": f"🎯 Sous la cible ({len(below_target)})",
            "entities": _wl_entities(below_target),
            "show_header_toggle": False,
        })
    if above_target:
        cards.append({
            "type": "entities",
            "title": f"En attente ({len(above_target)})",
            "entities": _wl_entities(above_target),
            "show_header_toggle": False,
        })

    return {
        "title": "Steam Watchlist",
        "views": [
            {
                "title": "Watchlist",
                "icon": "mdi:eye",
                "cards": cards or [{"type": "entity", "entity": SENSOR_SYNC_ID}],
            }
        ],
    }
