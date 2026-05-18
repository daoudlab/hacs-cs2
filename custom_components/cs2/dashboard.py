"""Generate Lovelace dashboard YAML files for the Steam Inventory integration."""
from __future__ import annotations

import os
from typing import Any

import yaml

from .const import SENSOR_TOTAL_ID, SENSOR_GAME_PREFIX, SENSOR_ITEM_PREFIX


def generate_dashboards(data: dict[str, Any], out_dir: str) -> list[str]:
    """Write dashboard YAML files to `out_dir`. Returns list of written filenames."""
    files = []

    global_path = os.path.join(out_dir, "steam_global.yaml")
    with open(global_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(_global_dashboard(data), f, allow_unicode=True, sort_keys=False)
    files.append("steam_global.yaml")

    for slug, game in data.get("per_game", {}).items():
        filename = f"steam_{slug}.yaml"
        game_path = os.path.join(out_dir, filename)
        with open(game_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(_game_dashboard(slug, game), f, allow_unicode=True, sort_keys=False)
        files.append(filename)

    return files


def _global_dashboard(data: dict) -> dict:
    per_game = data.get("per_game", {})
    glance_entities = [
        {"entity": f"{SENSOR_GAME_PREFIX}{slug}_total", "name": game["name"]}
        for slug, game in per_game.items()
    ]
    return {
        "title": "Steam Inventory",
        "views": [
            {
                "title": "Global",
                "icon": "mdi:steam",
                "cards": [
                    {
                        "type": "entity",
                        "entity": SENSOR_TOTAL_ID,
                        "name": "Total Portfolio",
                        "icon": "mdi:currency-eur",
                    },
                    {
                        "type": "glance",
                        "title": "Par jeu",
                        "entities": glance_entities or [],
                    },
                    {
                        "type": "history-graph",
                        "title": "Historique total",
                        "entities": [{"entity": SENSOR_TOTAL_ID, "name": "Valeur totale"}],
                        "hours_to_show": 168,
                        "refresh_interval": 0,
                    },
                    {
                        "type": "statistics-graph",
                        "title": "Statistiques (LTS)",
                        "entities": [SENSOR_TOTAL_ID],
                        "stat_types": ["state"],
                        "period": {"calendar": {"period": "month"}},
                    },
                ],
            }
        ],
    }


def _game_dashboard(slug: str, game: dict) -> dict:
    game_name = game.get("name", slug)
    items = game.get("items", [])
    total_entity = f"{SENSOR_GAME_PREFIX}{slug}_total"

    item_entities = [
        {
            "entity": f"{SENSOR_ITEM_PREFIX}{slug}_{item['slug']}",
            "name": item["name"][:40],
        }
        for item in sorted(items, key=lambda i: i.get("current_price") or 0, reverse=True)
    ]

    return {
        "title": f"Steam — {game_name}",
        "views": [
            {
                "title": game_name,
                "icon": "mdi:steam",
                "cards": [
                    {
                        "type": "entity",
                        "entity": total_entity,
                        "name": f"Total {game_name}",
                        "icon": "mdi:currency-eur",
                    },
                    {
                        "type": "entities",
                        "title": f"Items ({len(items)})",
                        "entities": item_entities or [],
                        "show_header_toggle": False,
                    },
                    {
                        "type": "history-graph",
                        "title": f"Historique {game_name}",
                        "entities": [{"entity": total_entity}],
                        "hours_to_show": 168,
                    },
                ],
            }
        ],
    }
