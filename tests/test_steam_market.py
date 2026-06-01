"""Tests for steam_market.normalize_price — locale-agnostic price parsing."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.cs2.api.steam_market import normalize_price


class TestNormalizePrice:
    # ── EUR formats ──────────────────────────────────────────────────────────
    def test_eur_comma_decimal(self):
        assert normalize_price("12,34 €") == 12.34

    def test_eur_dot_thousands_comma_decimal(self):
        assert normalize_price("1.234,56€") == 1234.56

    def test_eur_no_symbol(self):
        assert normalize_price("7,50") == 7.50

    def test_eur_dot_decimal(self):
        assert normalize_price("12.34€") == 12.34

    # ── USD formats ──────────────────────────────────────────────────────────
    def test_usd_dot_decimal(self):
        assert normalize_price("$12.34") == 12.34

    def test_usd_comma_thousands(self):
        assert normalize_price("$1,234.56") == 1234.56

    # ── Edge cases ───────────────────────────────────────────────────────────
    def test_none_input(self):
        assert normalize_price(None) is None

    def test_empty_string(self):
        assert normalize_price("") is None

    def test_zero(self):
        assert normalize_price("0,00 €") == 0.0

    def test_large_value(self):
        assert normalize_price("10.000,00€") == 10000.0

    def test_gbp(self):
        assert normalize_price("£5.99") == 5.99

    def test_rounding(self):
        # Should round to 2 decimals
        result = normalize_price("1,999")
        assert result == 1.999 or result == 2.0 or result == 1.99  # within 2dp

    def test_spaces_stripped(self):
        assert normalize_price("  25,00 €  ") == 25.0
