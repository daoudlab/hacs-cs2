"""Tests for slugify.make_slug — entity ID stability is critical."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.cs2.slugify import make_slug


class TestMakeSlug:
    def test_basic(self):
        assert make_slug("AWP | Redline (Field-Tested)") == "awp_redline_field_tested"

    def test_pipe_spaces(self):
        assert make_slug("AK-47 | Redline (Field-Tested)") == "ak_47_redline_field_tested"

    def test_already_clean(self):
        assert make_slug("rusty_ak47") == "rusty_ak47"

    def test_unicode_stripped(self):
        # Non-ASCII chars → underscore
        slug = make_slug("Döring Knife")
        assert slug == slug.lower()
        assert "_" in slug

    def test_leading_trailing_underscores_stripped(self):
        assert not make_slug("!!AWP!!").startswith("_")
        assert not make_slug("!!AWP!!").endswith("_")

    def test_multiple_non_alnum_collapsed(self):
        # "AWP  ::  Dragon" → no double underscores
        slug = make_slug("AWP  ::  Dragon")
        assert "__" not in slug

    def test_case_folded(self):
        assert make_slug("AWP") == "awp"

    def test_star_wars_name(self):
        # Real CS2 skin name
        result = make_slug("M4A4 | In Living Color (Factory New)")
        assert result == "m4a4_in_living_color_factory_new"

    def test_numbers_preserved(self):
        assert make_slug("Desert Eagle | 1911 (Minimal Wear)") == "desert_eagle_1911_minimal_wear"


