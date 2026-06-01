"""Tests for utils.parse_steam_ids."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.cs2.utils import parse_steam_ids


class TestParseSteamIds:
    def test_single_id_with_name(self):
        result = parse_steam_ids("76561190000000001:main")
        assert result == [("76561190000000001", "main")]

    def test_multiple_ids(self):
        result = parse_steam_ids("76561190000000001:main,76561190000000002:alt")
        assert len(result) == 2
        assert result[0] == ("76561190000000001", "main")
        assert result[1] == ("76561190000000002", "alt")

    def test_id_without_name(self):
        result = parse_steam_ids("76561190000000001")
        assert len(result) == 1
        sid, name = result[0]
        assert sid == "76561190000000001"
        assert "52859" in name  # auto-generated from last 8 digits

    def test_empty_string(self):
        assert parse_steam_ids("") == []

    def test_spaces_stripped(self):
        result = parse_steam_ids(" 76561190000000001 : main , 76561190000000002 : alt ")
        assert result[0] == ("76561190000000001", "main")
        assert result[1] == ("76561190000000002", "alt")

    def test_colon_in_name_preserved(self):
        result = parse_steam_ids("76561190000000001:main:extra")
        # split on first colon only
        assert result[0] == ("76561190000000001", "main:extra")

    def test_trailing_comma_ignored(self):
        result = parse_steam_ids("76561190000000001:main,")
        assert len(result) == 1
