"""Tests for steam_history.interpolate_gaps and _decode_cookie."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from custom_components.cs2.api.steam_history import interpolate_gaps, _decode_cookie


class TestInterpolateGaps:
    def test_no_gaps(self):
        hist = {"2024-01-01": 10.0, "2024-01-02": 20.0, "2024-01-03": 30.0}
        result = interpolate_gaps(hist)
        assert result == hist

    def test_single_gap(self):
        hist = {"2024-01-01": 10.0, "2024-01-03": 30.0}
        result = interpolate_gaps(hist)
        assert "2024-01-02" in result
        assert result["2024-01-02"] == 20.0  # linear interpolation

    def test_multiple_gaps(self):
        hist = {"2024-01-01": 0.0, "2024-01-05": 40.0}
        result = interpolate_gaps(hist)
        # 4 gaps → step 10 per day
        assert result["2024-01-02"] == pytest.approx(10.0, abs=0.01)
        assert result["2024-01-03"] == pytest.approx(20.0, abs=0.01)
        assert result["2024-01-04"] == pytest.approx(30.0, abs=0.01)

    def test_single_entry_returned_unchanged(self):
        hist = {"2024-01-01": 99.0}
        assert interpolate_gaps(hist) == hist

    def test_empty_returned_unchanged(self):
        assert interpolate_gaps({}) == {}

    def test_endpoints_preserved(self):
        hist = {"2024-01-01": 5.0, "2024-01-10": 50.0}
        result = interpolate_gaps(hist)
        assert result["2024-01-01"] == 5.0
        assert result["2024-01-10"] == 50.0

    def test_all_dates_present(self):
        hist = {"2024-01-01": 1.0, "2024-01-04": 4.0}
        result = interpolate_gaps(hist)
        assert set(result.keys()) == {
            "2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"
        }

    def test_rounding_to_4dp(self):
        hist = {"2024-01-01": 0.0, "2024-01-04": 1.0}
        result = interpolate_gaps(hist)
        # interpolated at 1/3 and 2/3
        val = result.get("2024-01-02")
        assert val is not None
        assert str(val)[::-1].find(".") <= 4  # max 4 decimal places


    def test_sparse_multiple_segments(self):
        # Three known points with gaps between each — bisect must find correct bracket
        hist = {"2024-01-01": 10.0, "2024-01-06": 20.0, "2024-01-11": 10.0}
        result = interpolate_gaps(hist)
        # Mid-point of first segment: day 3 → 14.0
        assert result["2024-01-03"] == pytest.approx(14.0, abs=0.01)
        # Mid-point of second segment: day 8 → 16.0
        assert result["2024-01-08"] == pytest.approx(16.0, abs=0.01)
        # All 11 days must be present
        assert len(result) == 11

    def test_large_sparse_correctness(self):
        # 100-day range with linear price 0→100: each day i → price i.0
        from datetime import date, timedelta
        d0 = date(2024, 1, 1)
        d100 = d0 + timedelta(days=100)
        hist = {d0.isoformat(): 0.0, d100.isoformat(): 100.0}
        result = interpolate_gaps(hist)
        assert len(result) == 101
        for i in range(101):
            ds = (d0 + timedelta(days=i)).isoformat()
            assert result[ds] == pytest.approx(float(i), abs=0.01)


class TestDecodeCookie:
    def test_plain_value(self):
        assert _decode_cookie("abc123") == "abc123"

    def test_url_encoded_decoded(self):
        # %7C is |
        decoded = _decode_cookie("abc%7Cxyz")
        assert decoded == "abc|xyz"

    def test_crlf_rejected(self):
        with pytest.raises(ValueError):
            _decode_cookie("abc\r\ninjected")

    def test_newline_rejected(self):
        with pytest.raises(ValueError):
            _decode_cookie("value\ninjected")

    def test_semicolon_rejected(self):
        with pytest.raises(ValueError):
            _decode_cookie("value;other=x")

    def test_comma_rejected(self):
        with pytest.raises(ValueError):
            _decode_cookie("value,other")

    def test_space_rejected(self):
        with pytest.raises(ValueError):
            _decode_cookie("has space")

    def test_nul_rejected(self):
        with pytest.raises(ValueError):
            _decode_cookie("val\x00ue")
