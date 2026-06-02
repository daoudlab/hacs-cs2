"""Generate Lovelace dashboard YAML files for the Steam Inventory integration."""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import yaml

from .const import DOMAIN, SENSOR_TOTAL_ID, SENSOR_GAME_PREFIX, SENSOR_ITEM_PREFIX, SENSOR_SYNC_ID, SENSOR_WATCHLIST_PREFIX

_LOGGER = logging.getLogger(__name__)


def generate_dashboards(data: dict[str, Any], out_dir: str) -> list[str]:
    """Write dashboard YAML files to `out_dir`. Returns list of written filenames."""
    files = []

    global_path = os.path.join(out_dir, "steam_global.yaml")
    with open(global_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(_global_dashboard(data), f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    files.append("steam_global.yaml")

    for slug, game in data.get("per_game", {}).items():
        if not re.match(r'^[a-z0-9_]+$', slug):
            _LOGGER.warning("Skipping dashboard for unsafe slug: %r", slug)
            continue
        filename = f"steam_{slug}.yaml"
        game_path = os.path.join(out_dir, filename)
        with open(game_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(_game_dashboard(slug, game), f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        files.append(filename)

    watchlist = data.get("watchlist", [])
    if watchlist:
        wl_path = os.path.join(out_dir, "steam_watchlist.yaml")
        with open(wl_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(_watchlist_dashboard(watchlist), f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        files.append("steam_watchlist.yaml")

    return files


# ── Shared helpers ───────────────────────────────────────────────────────────────

def _attr_row(entity: str, name: str, attribute: str, suffix: str = "") -> dict:
    """Entities-card row that shows a single sensor attribute."""
    row: dict = {
        "entity": entity,
        "name": name,
        "type": "attribute",
        "attribute": attribute,
    }
    if suffix:
        row["suffix"] = suffix
    return row


def _divider() -> dict:
    return {"type": "divider"}


def _button(name: str, icon: str, service: str, service_data: dict | None = None) -> dict:
    """Button card that calls a HA service."""
    card: dict = {
        "type": "button",
        "name": name,
        "icon": icon,
        "tap_action": {
            "action": "call-service",
            "service": service,
            "service_data": service_data or {},
        },
    }
    return card


def _conditional(condition_entity: str, condition_state: str, card: dict) -> dict:
    """Conditional card — shown only when entity is in given state."""
    return {
        "type": "conditional",
        "conditions": [
            {"condition": "state", "entity": condition_entity, "state": condition_state}
        ],
        "card": card,
    }


# ── Global dashboard ────────────────────────────────────────────────────────────

def _global_dashboard(data: dict) -> dict:
    per_game = data.get("per_game", {})
    watchlist = data.get("watchlist", [])

    game_glance = [
        {
            "entity": f"{SENSOR_GAME_PREFIX}{slug}_total",
            "name": game["name"],
        }
        for slug, game in per_game.items()
    ]

    all_game_entities = [f"{SENSOR_GAME_PREFIX}{slug}_total" for slug in per_game]

    # ── Overview tab ──────────────────────────────────────────────────────────
    cards_overview: list[dict] = [
        # Big total value
        {
            "type": "entity",
            "entity": SENSOR_TOTAL_ID,
            "name": "Portfolio Steam",
            "icon": "mdi:currency-eur",
        },
        # Key financial metrics
        {
            "type": "entities",
            "title": "Métriques clés",
            "entities": [
                _attr_row(SENSOR_TOTAL_ID, "Valeur nette (après tax 15%)", "total_net", " €"),
                _attr_row(SENSOR_TOTAL_ID, "Profit brut", "profit_brut", " €"),
                _attr_row(SENSOR_TOTAL_ID, "Profit net", "profit_net", " €"),
                _attr_row(SENSOR_TOTAL_ID, "ROI global", "roi_global", " %"),
                _attr_row(SENSOR_TOTAL_ID, "Variation (cycle)", "delta", " €"),
                _divider(),
                _attr_row(SENSOR_TOTAL_ID, "Nb items", "items_count", ""),
                _attr_row(SENSOR_TOTAL_ID, "Jeux actifs", "active_games_count", ""),
                _divider(),
                _attr_row(SENSOR_TOTAL_ID, "Meilleur performer", "best_performer_name", ""),
                _attr_row(SENSOR_TOTAL_ID, "ROI meilleur", "best_performer_roi", " %"),
                _attr_row(SENSOR_TOTAL_ID, "Pire performer", "worst_performer_name", ""),
                _attr_row(SENSOR_TOTAL_ID, "ROI pire", "worst_performer_roi", " %"),
            ],
            "show_header_toggle": False,
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
            "entities": [f"{DOMAIN}:portfolio_total"],
            "stat_types": ["mean"],
            "period": "month",
            "days_to_show": 5000,
        },
    ]

    views = [
        {
            "title": "Portfolio",
            "icon": "mdi:steam",
            "cards": cards_overview,
        }
    ]

    # ── Performance tab ───────────────────────────────────────────────────────
    if all_game_entities:
        perf_stat_ids = [f"{DOMAIN}:portfolio_total"] + [
            f"{DOMAIN}:{slug}_total" for slug in list(per_game)[:4]
        ]
        views.append({
            "title": "Performance",
            "icon": "mdi:chart-line",
            "cards": [
                {
                    "type": "statistics-graph",
                    "title": "Comparaison jeux (mensuel)",
                    "entities": perf_stat_ids,
                    "stat_types": ["mean"],
                    "period": "month",
                    "days_to_show": 5000,
                },
                {
                    "type": "history-graph",
                    "title": "Comparaison 7j",
                    "entities": [
                        {
                            "entity": e,
                            "name": per_game.get(
                                e.replace(SENSOR_GAME_PREFIX, "").replace("_total", ""), {}
                            ).get("name", e),
                        }
                        for e in all_game_entities[:4]
                    ] + [{"entity": SENSOR_TOTAL_ID, "name": "Total"}],
                    "hours_to_show": 168,
                },
            ],
        })

    # ── Watchlist tab ─────────────────────────────────────────────────────────
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

    # ── Administration tab ────────────────────────────────────────────────────
    admin_cards: list[dict] = [
        # Rate-limit warning — shown only when market is limited
        _conditional(
            SENSOR_SYNC_ID,
            "market_limited",
            {
                "type": "markdown",
                "content": (
                    "## ⚠️ Steam Market rate-limité\n"
                    "Les requêtes de prix sont en pause. "
                    "Vérifiez l'attribut `market_rl_until` du capteur Sync pour l'heure de reprise."
                ),
            },
        ),
        _conditional(
            SENSOR_SYNC_ID,
            "rate_limited",
            {
                "type": "markdown",
                "content": (
                    "## ⛔ Compte Steam suspendu (IP ban)\n"
                    "Un ou plusieurs comptes sont en cooldown 1h. "
                    "Vérifiez l'attribut `banned_accounts` du capteur Sync."
                ),
            },
        ),
        # Sync status entity card
        {
            "type": "entity",
            "entity": SENSOR_SYNC_ID,
            "name": "Statut synchronisation",
            "icon": "mdi:sync",
        },
        # Sync details
        {
            "type": "entities",
            "title": "Détails synchronisation",
            "entities": [
                _attr_row(SENSOR_SYNC_ID, "Dernière mise à jour", "last_update", ""),
                _attr_row(SENSOR_SYNC_ID, "Durée du cycle", "cycle_duration_s", " s"),
                _attr_row(SENSOR_SYNC_ID, "Items suivis", "items_count", ""),
                _attr_row(SENSOR_SYNC_ID, "Jeux actifs", "active_games", ""),
                _attr_row(SENSOR_SYNC_ID, "Prix manquants", "missing_count", ""),
                _attr_row(SENSOR_SYNC_ID, "Prix obsolètes", "stale_count", ""),
                _divider(),
                _attr_row(SENSOR_SYNC_ID, "Comptes bannis (IP)", "banned_accounts", ""),
                _attr_row(SENSOR_SYNC_ID, "Market RL actif", "market_rl_active", ""),
                _attr_row(SENSOR_SYNC_ID, "Market RL consécutifs", "market_rl_consecutive", ""),
            ],
            "show_header_toggle": False,
        },
        # Action buttons
        {
            "type": "entities",
            "title": "Actions",
            "entities": [{"type": "divider"}],
            "show_header_toggle": False,
        },
        {
            "type": "horizontal-stack",
            "cards": [
                _button("Actualiser", "mdi:refresh", f"{DOMAIN}.force_refresh"),
                _button("Regen dashboards", "mdi:view-dashboard-edit", f"{DOMAIN}.generate_dashboards"),
            ],
        },
        # Configuration reminder
        {
            "type": "markdown",
            "content": (
                "### Configuration\n"
                "Les réglages (intervalle de scan, seuil valeur, floats, jours d'historique) "
                "sont dans **Paramètres → Intégrations → Steam Inventory → Configurer**.\n\n"
                "### Services disponibles\n"
                "- `cs2.set_buy_price` — prix d'achat d'un item (`market_hash_name`, `price`)\n"
                "- `cs2.watchlist_add` — ajouter un item à surveiller "
                "(`market_hash_name`, `target_price`, `note`, `appid`)\n"
                "- `cs2.watchlist_remove` — retirer de la watchlist (`market_hash_name`)\n"
                "- `cs2.run_import` — importer l'historique complet dans HA statistics "
                "(cookie Steam requis)\n\n"
                "### Blueprint d'alerte de prix\n"
                "Fichier `blueprints/automation/cs2_price_alert.yaml` dans le dépôt. "
                "Importer via **Paramètres → Automatisations → Blueprints → Importer un blueprint**."
            ),
        },
        # Import progress (shows only when import is running or recently finished)
        _conditional(
            SENSOR_SYNC_ID,
            "ok",
            {
                "type": "entities",
                "title": "Progression import (dernier)",
                "entities": [
                    _attr_row(SENSOR_SYNC_ID, "Import en cours", "import_running", ""),
                    _attr_row(SENSOR_SYNC_ID, "Items traités / total", "import_progress", ""),
                ],
                "show_header_toggle": False,
            },
        ),
    ]

    views.append({
        "title": "Administration",
        "icon": "mdi:cog",
        "cards": admin_cards,
    })

    return {"title": "Steam Inventory", "views": views}


# ── Per-game dashboard ──────────────────────────────────────────────────────────

def _game_dashboard(slug: str, game: dict) -> dict:
    game_name = game.get("name", slug)
    items = game.get("items", [])
    metrics = game.get("metrics", {})
    total_entity = f"{SENSOR_GAME_PREFIX}{slug}_total"

    # Sort by current value desc (price × qty)
    sorted_items = sorted(
        items,
        key=lambda i: (i.get("current_price") or 0) * (i.get("quantity") or 1),
        reverse=True,
    )

    # Items with buy price (for P&L view)
    pl_items = [i for i in sorted_items if i.get("buy_price")]

    def _item_name(item: dict, max_len: int = 38) -> str:
        """Item display name with ×N suffix when quantity > 1."""
        qty = item.get("quantity", 1)
        name = item["name"][:max_len]
        return f"{name} ×{qty}" if qty > 1 else name

    def _item_entity(item: dict) -> dict:
        return {
            "entity": f"{SENSOR_ITEM_PREFIX}{item['slug']}",
            "name": _item_name(item),
        }

    def _glance_item(item: dict) -> dict:
        return {
            "entity": f"{SENSOR_ITEM_PREFIX}{item['slug']}",
            "name": _item_name(item, max_len=28),
        }

    top_glance = [_glance_item(i) for i in sorted_items[:12]]
    all_entity_list = [_item_entity(i) for i in sorted_items]

    # ── Portfolio tab ─────────────────────────────────────────────────────────
    cards_portfolio: list[dict] = [
        {
            "type": "entity",
            "entity": total_entity,
            "name": f"Total {game_name}",
            "icon": "mdi:currency-eur",
        },
        # Game-level metrics
        {
            "type": "entities",
            "title": "Métriques",
            "entities": [
                _attr_row(total_entity, "Valeur nette (après tax)", "total_net", " €"),
                _attr_row(total_entity, "Profit brut", "profit_brut", " €"),
                _attr_row(total_entity, "ROI global", "roi_global", " %"),
                _attr_row(total_entity, "Variation (cycle)", "delta", " €"),
                _divider(),
                _attr_row(total_entity, "Nb items", "items_count", ""),
                _attr_row(total_entity, "Quantité totale", "items_total_qty", ""),
                _attr_row(total_entity, "Items avec prix", "items_with_price", ""),
                _divider(),
                _attr_row(total_entity, "Meilleur performer", "best_performer_name", ""),
                _attr_row(total_entity, "ROI meilleur", "best_performer_roi", " %"),
                _attr_row(total_entity, "Pire performer", "worst_performer_name", ""),
                _attr_row(total_entity, "ROI pire", "worst_performer_roi", " %"),
            ],
            "show_header_toggle": False,
        },
    ]

    if top_glance:
        cards_portfolio.append({
            "type": "glance",
            "title": "Top items",
            "entities": top_glance,
            "show_state": True,
            "show_name": True,
            "show_icon": False,  # False → HA uses entity_picture (Steam item image) when available
            "columns": 5,
        })

    if all_entity_list:
        cards_portfolio.append({
            "type": "entities",
            "title": f"Tous les items ({len(items)})",
            "entities": all_entity_list,
            "show_header_toggle": False,
        })

    # Per-item long-term price history (cs2:item_<slug>), populated by cs2.run_import.
    # All owned items with a price, most valuable first.
    item_stat_ids = [
        f"{DOMAIN}:item_{item['slug']}"
        for item in sorted_items
        if (item.get("current_price") or 0) > 0
    ]

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
            "entities": [f"{DOMAIN}:{slug}_total"],
            "stat_types": ["mean"],
            "period": "month",
            "days_to_show": 5000,
        },
    ]
    # Chunk per-item LTS into several statistics-graph cards so each stays readable.
    _LTS_CHUNK = 10
    total_parts = (len(item_stat_ids) + _LTS_CHUNK - 1) // _LTS_CHUNK
    for idx in range(0, len(item_stat_ids), _LTS_CHUNK):
        part = idx // _LTS_CHUNK + 1
        suffix = f" ({part}/{total_parts})" if total_parts > 1 else ""
        cards_portfolio.append({
            "type": "statistics-graph",
            "title": f"Historique LTS par item{suffix} — disponible après cs2.run_import",
            "entities": item_stat_ids[idx:idx + _LTS_CHUNK],
            "stat_types": ["mean"],
            "period": "month",
            "days_to_show": 5000,
        })

    views = [
        {
            "title": game_name,
            "icon": "mdi:steam",
            "cards": cards_portfolio,
        }
    ]

    # ── P&L tab ───────────────────────────────────────────────────────────────
    if pl_items:
        # Compute P&L totals from items data for summary card
        total_buy = sum(
            (i.get("buy_price") or 0) * (i.get("quantity") or 1) for i in pl_items
        )
        total_val = sum(
            (i.get("current_price") or 0) * (i.get("quantity") or 1)
            for i in pl_items
            if i.get("current_price")
        )
        profit_display = round(total_val - total_buy, 2) if total_buy > 0 else None

        summary_rows: list[dict] = [
            {
                "entity": total_entity,
                "name": f"Total {game_name}",
            },
            _attr_row(total_entity, "Profit brut (items avec PA)", "profit_brut", " €"),
            _attr_row(total_entity, "Profit net (après tax 15%)", "profit_net", " €"),
            _attr_row(total_entity, "ROI global", "roi_global", " %"),
        ]

        pl_entity_list: list[dict] = []
        for item in pl_items:
            entity_id = f"{SENSOR_ITEM_PREFIX}{item['slug']}"
            pl_entity_list.append({
                "entity": entity_id,
                "name": _item_name(item),
            })
            if item.get("roi") is not None:
                pl_entity_list.append(
                    _attr_row(entity_id, "ROI", "roi", " %")
                )
            if item.get("buy_price") is not None:
                pl_entity_list.append(
                    _attr_row(entity_id, "Prix achat (PA)", "buy_price", " €")
                )
            if item.get("delta_from_start") is not None:
                pl_entity_list.append(
                    _attr_row(entity_id, "Gain/perte vs PA", "delta_from_start", " €")
                )
            pl_entity_list.append(_divider())

        views.append({
            "title": "P&L",
            "icon": "mdi:chart-bar",
            "cards": [
                {
                    "type": "entities",
                    "title": "Résumé P&L",
                    "entities": summary_rows,
                    "show_header_toggle": False,
                },
                {
                    "type": "entities",
                    "title": f"Détail par item ({len(pl_items)} avec prix d'achat)",
                    "entities": pl_entity_list,
                    "show_header_toggle": False,
                },
            ],
        })

    # ── Floats tab (CS2 only) ─────────────────────────────────────────────────
    if slug == "cs2":
        float_items = [i for i in sorted_items if i.get("float_value") is not None]
        if float_items:
            float_list = []
            for item in float_items[:20]:
                entity_id = f"{SENSOR_ITEM_PREFIX}{item['slug']}"
                float_list.append({"entity": entity_id, "name": _item_name(item)})
                float_list.append(_attr_row(entity_id, "Float", "float_value", ""))
                float_list.append(_divider())

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
    below_target = [
        w for w in watchlist
        if w.get("current_price") is not None
        and w.get("target_price") is not None
        and w["current_price"] <= w["target_price"]
    ]
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
                result.append(_attr_row(entity_id, "Prix cible", "target_price", " €"))
            if w.get("note"):
                result.append(_attr_row(entity_id, "Note", "note", ""))
            result.append(_divider())
        return result

    cards: list[dict] = []

    # Alert card when at least one item is below target
    if below_target:
        below_glance = [
            {
                "entity": f"{SENSOR_WATCHLIST_PREFIX}{w['slug']}",
                "name": w["market_hash_name"][:28],
            }
            for w in below_target
            if w.get("slug")
        ]
        cards.append({
            "type": "markdown",
            "content": f"## 🎯 {len(below_target)} item(s) sous la cible !",
        })
        if below_glance:
            cards.append({
                "type": "glance",
                "title": "Sous la cible — acheter ?",
                "entities": below_glance,
                "show_state": True,
                "show_name": True,
            })
        cards.append({
            "type": "entities",
            "title": f"Détail — sous la cible ({len(below_target)})",
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
