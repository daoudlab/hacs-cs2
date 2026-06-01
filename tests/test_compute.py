"""Tests for compute.py — portfolio metrics calculation."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.cs2.compute import compute_item_metrics, compute_global_metrics


def _make_item(name: str, marketable: bool = True) -> dict:
    return {
        "market_hash_name": name,
        "marketable": marketable,
        "name_color": None,
        "inspect_link": None,
        "entity_picture": None,
        "asset_id": name[:8],
        "classid": "1",
        "instanceid": "0",
        "is_skin": marketable,
    }


class TestComputeItemMetrics:
    def test_single_item_with_price(self):
        items = [_make_item("AWP | Redline (FT)")]
        result = compute_item_metrics(
            inventory=items,
            prices={"AWP | Redline (FT)": 25.0},
            floats={},
            previous_prices={},
            buy_prices={},
            reference_prices={},
        )
        assert len(result) == 1
        assert result[0]["current_price"] == 25.0
        assert result[0]["quantity"] == 1
        assert result[0]["roi"] is None  # no buy price

    def test_quantity_counted_for_duplicates(self):
        items = [_make_item("AK-47 | Slate")] * 3
        result = compute_item_metrics(
            inventory=items,
            prices={"AK-47 | Slate": 10.0},
            floats={},
            previous_prices={},
            buy_prices={},
            reference_prices={},
        )
        assert len(result) == 1
        assert result[0]["quantity"] == 3

    def test_roi_computed_when_buy_price_set(self):
        items = [_make_item("USP-S | Kill Confirmed")]
        result = compute_item_metrics(
            inventory=items,
            prices={"USP-S | Kill Confirmed": 30.0},
            floats={},
            previous_prices={},
            buy_prices={"USP-S | Kill Confirmed": 20.0},
            reference_prices={},
        )
        assert result[0]["roi"] == 50.0  # (30-20)/20 * 100

    def test_roi_none_when_buy_below_threshold(self):
        items = [_make_item("Sticker | Katowice")]
        result = compute_item_metrics(
            inventory=items,
            prices={"Sticker | Katowice": 5.0},
            floats={},
            previous_prices={},
            buy_prices={"Sticker | Katowice": 0.05},  # below 0.10 threshold
            reference_prices={},
        )
        assert result[0]["roi"] is None

    def test_delta_24h_and_7d(self):
        items = [_make_item("M4A4 | Howl")]
        result = compute_item_metrics(
            inventory=items,
            prices={"M4A4 | Howl": 1000.0},
            floats={},
            previous_prices={},
            buy_prices={},
            reference_prices={},
            prices_24h={"M4A4 | Howl": 950.0},
            prices_7d={"M4A4 | Howl": 900.0},
        )
        assert result[0]["delta_24h"] == 50.0
        assert result[0]["delta_7d"] == 100.0

    def test_no_price_gives_none(self):
        items = [_make_item("Rare Item")]
        result = compute_item_metrics(
            inventory=items,
            prices={},
            floats={},
            previous_prices={},
            buy_prices={},
            reference_prices={},
        )
        assert result[0]["current_price"] is None
        assert result[0]["roi"] is None

    def test_slug_generated(self):
        items = [_make_item("AWP | Dragon Lore (FN)")]
        result = compute_item_metrics(
            inventory=items,
            prices={},
            floats={},
            previous_prices={},
            buy_prices={},
            reference_prices={},
        )
        assert result[0]["slug"] == "awp_dragon_lore_fn"

    def test_float_attached(self):
        items = [_make_item("AK-47 | Fire Serpent")]
        result = compute_item_metrics(
            inventory=items,
            prices={"AK-47 | Fire Serpent": 500.0},
            floats={"AK-47 | Fire Serpent": 0.123456},
            previous_prices={},
            buy_prices={},
            reference_prices={},
        )
        assert result[0]["float_value"] == 0.123456


class TestComputeGlobalMetrics:
    def _items(self):
        return [
            {
                "name": "A",
                "current_price": 100.0,
                "buy_price": 80.0,
                "quantity": 1,
                "roi": 25.0,
            },
            {
                "name": "B",
                "current_price": 50.0,
                "buy_price": None,
                "quantity": 2,
                "roi": None,
            },
        ]

    def test_total_value(self):
        m = compute_global_metrics(self._items(), previous_total=None)
        # A: 100*1 + B: 50*2 = 200
        assert m["total_value"] == 200.0

    def test_items_count(self):
        m = compute_global_metrics(self._items(), previous_total=None)
        assert m["items_count"] == 2

    def test_items_total_qty(self):
        m = compute_global_metrics(self._items(), previous_total=None)
        assert m["items_total_qty"] == 3

    def test_profit_only_items_with_buy_price(self):
        m = compute_global_metrics(self._items(), previous_total=None)
        # Only A contributes: profit_brut = 100 - 80 = 20
        assert m["profit_brut"] == 20.0

    def test_delta_with_previous_total(self):
        m = compute_global_metrics(self._items(), previous_total=180.0)
        assert m["delta"] == 20.0

    def test_delta_none_without_previous(self):
        m = compute_global_metrics(self._items(), previous_total=None)
        assert m["delta"] is None

    def test_best_performer(self):
        m = compute_global_metrics(self._items(), previous_total=None)
        assert m["best_performer_name"] == "A"
        assert m["best_performer_roi"] == 25.0

    def test_worst_performer_none_when_one_item_has_roi(self):
        m = compute_global_metrics(self._items(), previous_total=None)
        # Only one item with ROI — worst_performer is None to avoid showing same item twice
        assert m["worst_performer_name"] is None
        assert m["worst_performer_roi"] is None

    def test_empty_inventory(self):
        m = compute_global_metrics([], previous_total=None)
        assert m["total_value"] == 0.0
        assert m["items_count"] == 0
        assert m["profit_brut"] is None
        assert m["best_performer_name"] is None
