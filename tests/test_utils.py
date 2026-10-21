"""Offline tests for French-number parsing and period arithmetic."""

import datetime as dt

import pytest

from madfinance.utils import (
    days_between,
    parse_fr_bool,
    parse_fr_number,
    period_to_range,
)

TODAY = dt.date(2026, 10, 21)


class TestParseFrNumber:
    @pytest.mark.parametrize("text,expected", [
        ("80 490 370 000,00", 80490370000.0),
        ("34 921 384", 34921384.0),
        ("1\u00a0234", 1234.0),
        ("155,2", 155.2),
        ("3,01%", 3.01),
        ("-9 832 500", -9832500.0),
        ("-1", -1.0),
        ("0", 0.0),
        (42, 42.0),
        (3.5, 3.5),
        # dot handling: 3-digit dotted groups are thousands, anything else
        # is a decimal (the old regex silently truncated at every dot)
        ("3.5", 3.5),
        ("1.234,5", 1234.5),
        ("-1.234,5", -1234.5),
        ("1.234.567", 1234567.0),
        ("1.234", 1234.0),
        ("1234.50", 1234.5),
    ])
    def test_values(self, text, expected):
        assert parse_fr_number(text) == expected

    @pytest.mark.parametrize("value", [
        None, True, False, "", "-", "–", "N/A", "Oui", "Aucun", "n/d",
    ])
    def test_non_values(self, value):
        assert parse_fr_number(value) is None


class TestParseFrBool:
    @pytest.mark.parametrize("text,expected", [
        ("Oui", True), ("yes", True), ("true", True), ("1", True),
        ("Non", False), ("no", False), ("false", False), ("0", False),
        ("peut-être", None), ("", None), (None, None),
    ])
    def test_values(self, text, expected):
        assert parse_fr_bool(text) is expected


class TestPeriodToRange:
    def test_period_1y(self):
        start, end = period_to_range("1y", today=TODAY)
        assert end == TODAY
        assert days_between(start, end) == 365

    def test_period_ytd(self):
        start, end = period_to_range("ytd", today=TODAY)
        assert start == dt.date(TODAY.year, 1, 1)
        assert end == TODAY

    def test_period_max(self):
        start, end = period_to_range("max", today=TODAY)
        assert start == dt.date(2000, 1, 1)
        assert end == TODAY

    def test_default_period_is_1y(self):
        assert period_to_range(today=TODAY) == period_to_range("1y", today=TODAY)

    def test_explicit_start_wins(self):
        start, end = period_to_range(period="5y", start="2024-01-15", today=TODAY)
        assert start == dt.date(2024, 1, 15)
        assert end == TODAY                      # end defaults to today

    def test_explicit_start_and_end(self):
        start, end = period_to_range(start="2025-01-01", end="2025-12-31")
        assert (start, end) == (dt.date(2025, 1, 1), dt.date(2025, 12, 31))

    def test_unknown_period_raises(self):
        with pytest.raises(ValueError, match="Unknown period"):
            period_to_range("1h", today=TODAY)

    def test_start_after_end_raises(self):
        with pytest.raises(ValueError, match="after end"):
            period_to_range(start="2026-01-01", end="2025-01-01")


def test_days_between():
    assert days_between(dt.date(2025, 1, 1), dt.date(2025, 1, 11)) == 10
    assert days_between(dt.date(2025, 1, 11), dt.date(2025, 1, 1)) == -10
