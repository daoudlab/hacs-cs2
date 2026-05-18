"""Generate Lovelace dashboard YAML files for the Steam Inventory integration."""
from __future__ import annotations

import os
from typing import Any

from .const import SENSOR_TOTAL_ID, SENSOR_GAME_PREFIX, SENSOR_ITEM_PREFIX


def generate_dashboards(data: dict[str, Any], out_dir: str) -> list[str]:
    """Write dashboard YAML files to `out_dir`. Returns list of written filenames."""
    files = []

    global_yaml = _global_dashboard(data)
    global_path = os.path.join(out_dir, "steam_global.yaml")
    with open(global_path, "w", encoding="utf-8") as f:
        f.write(global_yaml)
    files.append("steam_global.yaml")

    for slug, game in data.get("per_game", {}).items():
        game_yaml = _game_dashboard(slug, game)
        filename = f"steam_{slug}.yaml"
        game_path = os.path.join(out_dir, filename)
        with open(game_path, "w", encoding="utf-8") as f:
            f.write(game_yaml)
        files.append(filename)

    return files


def _global_dashboard(data: dict) -> str:
    per_game = data.get("per_game", {})
    game_entities = "\n".join(
        f"        - {SENSOR_GAME_PREFIX}{slug}_total"
        for slug in per_game
    )

    # Build a glance card for game totals
    glance_entities = "\n".join(
        f"      - entity: {SENSOR_GAME_PREFIX}{slug}_total\n        name: {game['name']}"
        for slug, game in per_game.items()
    )

    return f"""\
title: Steam Inventory
views:
  - title: Global
    icon: mdi:steam
    cards:
      - type: entity
        entity: {SENSOR_TOTAL_ID}
        name: Total Portfolio
        icon: mdi:currency-eur

      - type: glance
        title: Par jeu
        entities:
{glance_entities or "          []"}

      - type: history-graph
        title: Historique total
        entities:
          - entity: {SENSOR_TOTAL_ID}
            name: Valeur totale
        hours_to_show: 168
        refresh_interval: 0

      - type: statistics-graph
        title: Statistiques (LTS)
        entities:
          - sensor.cs2_portfolio_total
        stat_types:
          - state
        period:
          calendar:
            period: month
"""


def _game_dashboard(slug: str, game: dict) -> str:
    game_name = game.get("name", slug)
    items = game.get("items", [])
    total_entity = f"{SENSOR_GAME_PREFIX}{slug}_total"

    item_rows = "\n".join(
        f"      - entity: {SENSOR_ITEM_PREFIX}{item['slug']}\n"
        f"        name: {item['name'][:40]}"
        for item in sorted(items, key=lambda i: i.get("current_price") or 0, reverse=True)
    )

    return f"""\
title: Steam — {game_name}
views:
  - title: {game_name}
    icon: mdi:steam
    cards:
      - type: entity
        entity: {total_entity}
        name: Total {game_name}
        icon: mdi:currency-eur

      - type: entities
        title: Items ({len(items)})
        entities:
{item_rows or "          []"}
        show_header_toggle: false

      - type: history-graph
        title: Historique {game_name}
        entities:
          - entity: {total_entity}
        hours_to_show: 168
"""
