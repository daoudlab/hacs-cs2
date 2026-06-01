"""Tests for dashboard.py — generate_dashboards and helper structure."""
import sys
import os
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.cs2.dashboard import generate_dashboards


# ── Minimal coordinator-shaped data fixture ──────────────────────────────────

def _item(name, slug, price=10.0, qty=1, roi=None, buy=None):
    return {
        "name": name,
        "slug": slug,
        "game_slug": "cs2",
        "game_name": "Counter-Strike 2",
        "appid": 730,
        "current_price": price,
        "buy_price": buy,
        "quantity": qty,
        "roi": roi,
        "delta_yesterday": 0.5,
        "delta_since_crash": None,
        "delta_from_start": None,
        "before_crash": None,
        "rarity_color": "#eb4b4b",
        "float_value": None,
        "entity_picture": f"https://cdn.steam/img/{slug}.png",
    }


def _make_data(items=None, watchlist=None):
    items = items or [
        _item("AK-47 | Redline (Field-Tested)", "ak_47_redline_field_tested", price=10.0, qty=2, roi=25.0, buy=8.0),
        _item("AWP | Asiimov (Field-Tested)", "awp_asiimov_field_tested", price=130.0, qty=1),
    ]
    watchlist = watchlist or []
    wl_by_slug = {w["slug"]: w for w in watchlist}
    items_by_slug = {f"cs2__{i['slug']}": i for i in items}
    return {
        "global": {
            "total_value": sum(i["current_price"] * i["quantity"] for i in items),
            "total_net": 140.0,
            "total_buy": 100.0,
            "profit_brut": 50.0,
            "profit_net": 40.0,
            "roi_global": 40.0,
            "delta": 5.0,
            "items_count": len(items),
            "items_total_qty": sum(i["quantity"] for i in items),
            "items_with_price": len(items),
            "best_performer_name": items[0]["name"],
            "best_performer_roi": items[0]["roi"],
            "worst_performer_name": None,
            "worst_performer_roi": None,
        },
        "per_game": {
            "cs2": {
                "name": "Counter-Strike 2",
                "appid": 730,
                "metrics": {
                    "total_value": 150.0,
                    "total_net": 140.0,
                    "profit_brut": 50.0,
                    "roi_global": 40.0,
                    "delta": 5.0,
                    "items_count": len(items),
                    "items_total_qty": sum(i["quantity"] for i in items),
                    "items_with_price": len(items),
                    "best_performer_name": items[0]["name"],
                    "best_performer_roi": items[0]["roi"],
                    "worst_performer_name": None,
                    "worst_performer_roi": None,
                },
                "items": items,
            }
        },
        "items": items,
        "items_by_slug": items_by_slug,
        "active_apps": [(730, 2, "cs2", "Counter-Strike 2")],
        "stale_count": 0,
        "missing_count": 0,
        "watchlist": watchlist,
        "watchlist_by_slug": wl_by_slug,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestGenerateDashboards:
    def test_creates_global_file(self, tmp_path):
        files = generate_dashboards(_make_data(), str(tmp_path))
        assert "steam_global.yaml" in files
        assert (tmp_path / "steam_global.yaml").exists()

    def test_creates_game_file(self, tmp_path):
        files = generate_dashboards(_make_data(), str(tmp_path))
        assert "steam_cs2.yaml" in files
        assert (tmp_path / "steam_cs2.yaml").exists()

    def test_no_watchlist_file_when_empty(self, tmp_path):
        files = generate_dashboards(_make_data(watchlist=[]), str(tmp_path))
        assert "steam_watchlist.yaml" not in files

    def test_creates_watchlist_file_when_non_empty(self, tmp_path):
        wl = [{"market_hash_name": "M4A4 | Howl", "slug": "m4a4_howl",
               "appid": 730, "current_price": 1200.0, "target_price": 900.0, "note": ""}]
        files = generate_dashboards(_make_data(watchlist=wl), str(tmp_path))
        assert "steam_watchlist.yaml" in files

    def test_global_yaml_is_valid(self, tmp_path):
        generate_dashboards(_make_data(), str(tmp_path))
        content = (tmp_path / "steam_global.yaml").read_text()
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict)
        assert "views" in parsed

    def test_game_yaml_is_valid(self, tmp_path):
        generate_dashboards(_make_data(), str(tmp_path))
        content = (tmp_path / "steam_cs2.yaml").read_text()
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict)
        assert "views" in parsed

    def test_watchlist_yaml_is_valid(self, tmp_path):
        wl = [{"market_hash_name": "AWP | Dragon Lore", "slug": "awp_dragon_lore",
               "appid": 730, "current_price": 2000.0, "target_price": None, "note": ""}]
        generate_dashboards(_make_data(watchlist=wl), str(tmp_path))
        content = (tmp_path / "steam_watchlist.yaml").read_text()
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict)

    def test_unsafe_slug_skipped(self, tmp_path):
        data = _make_data()
        data["per_game"]["../evil"] = data["per_game"].pop("cs2")
        files = generate_dashboards(data, str(tmp_path))
        # Only global should be written — unsafe slug skipped
        assert not any("evil" in f for f in files)
        assert "steam_global.yaml" in files

    def test_no_game_data_only_global(self, tmp_path):
        data = _make_data()
        data["per_game"] = {}
        files = generate_dashboards(data, str(tmp_path))
        assert files == ["steam_global.yaml"]

    def test_yaml_uses_block_style(self, tmp_path):
        generate_dashboards(_make_data(), str(tmp_path))
        content = (tmp_path / "steam_global.yaml").read_text()
        # default_flow_style=False: no inline {} or [] sequences on nested dicts/lists
        # The root-level dict keys must not be on a single line like {views: [...]}
        assert not content.startswith("{")

    def test_global_contains_sensor_reference(self, tmp_path):
        generate_dashboards(_make_data(), str(tmp_path))
        content = (tmp_path / "steam_global.yaml").read_text()
        assert "sensor.steam_inventory_total" in content

    def test_game_contains_item_entity(self, tmp_path):
        generate_dashboards(_make_data(), str(tmp_path))
        content = (tmp_path / "steam_cs2.yaml").read_text()
        assert "ak_47_redline_field_tested" in content

    def test_multiple_games_produce_multiple_files(self, tmp_path):
        data = _make_data()
        data["per_game"]["dota2"] = {
            "name": "Dota 2",
            "appid": 570,
            "metrics": data["per_game"]["cs2"]["metrics"],
            "items": [],
        }
        files = generate_dashboards(data, str(tmp_path))
        assert "steam_cs2.yaml" in files
        assert "steam_dota2.yaml" in files
