from decimal import Decimal

import pytest

from scraping.parsers import PriceParseError, parse_price


@pytest.mark.parametrize("text,expected,currency", [
    ("1.234,56 €", Decimal("1234.56"), "EUR"),      # EU thousands + decimal
    ("0,17 €", Decimal("0.17"), "EUR"),
    ("0,02 €", Decimal("0.02"), "EUR"),
    ("£1.999,99", Decimal("1999.99"), "GBP"),        # mixed separators, legacy case
    ("£0.02", Decimal("0.02"), "GBP"),
    ("£0,17", Decimal("0.17"), "GBP"),
    ("0,317 €", Decimal("0.32"), "EUR"),             # 3 decimals, integer part 0
    ("1.234 €", Decimal("1234.00"), "EUR"),          # bare thousands
    ("1,234.56", Decimal("1234.56"), "EUR"),         # US format, no symbol
    ("40.000,00 €", Decimal("40000.00"), "EUR"),     # the Digimon outlier's format
    ("2,99 €", Decimal("2.99"), "EUR"),
    ("15 €", Decimal("15.00"), "EUR"),               # no separator at all
])
def test_parse_price(text, expected, currency):
    value, code = parse_price(text)
    assert value == expected
    assert code == currency


@pytest.mark.parametrize("bad", ["", "   ", "N/A", "—", "€"])
def test_parse_price_rejects_garbage(bad):
    with pytest.raises(PriceParseError):
        parse_price(bad)
