"""Price text parsing.

Replaces the legacy convert_currency_string(): no more fake GBP×1.16
conversion — the real currency code is returned and stored on the snapshot.

Cardmarket renders locale-formatted prices, so both "1.234,56 €" and
"£1,234.56" (and mixed oddities the legacy notebook documented, like
"£1.999,99") must parse.
"""

import re
from decimal import Decimal, InvalidOperation

CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "$": "USD"}

_CLEAN = re.compile(r"[^\d.,]")


class PriceParseError(ValueError):
    pass


def parse_price(text: str) -> tuple[Decimal, str]:
    """Parse a price string into (Decimal, currency code).

    Separator rules:
    - Both '.' and ',' present → the RIGHTMOST one is the decimal separator.
    - One separator with 1-2 trailing digits → decimal separator.
    - One separator with 3 trailing digits → thousands separator, UNLESS the
      integer part is exactly '0' ("0.317" is 0.317, never 317).
    """
    if not text or not text.strip():
        raise PriceParseError("empty price text")

    currency = "EUR"
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            currency = code
            break

    cleaned = _CLEAN.sub("", text.replace("\xa0", ""))
    if not cleaned:
        raise PriceParseError(f"no digits in price text: {text!r}")

    has_dot, has_comma = "." in cleaned, "," in cleaned
    if has_dot and has_comma:
        decimal_sep = "." if cleaned.rindex(".") > cleaned.rindex(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        head, tail = cleaned.rsplit(sep, 1)
        if len(tail) == 3 and head.replace(sep, "") != "0" and head != "":
            cleaned = cleaned.replace(sep, "")  # thousands: 1.234 → 1234
        else:
            cleaned = head.replace(sep, "") + "." + tail

    try:
        value = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise PriceParseError(f"unparseable price text: {text!r}") from exc
    if value < 0:
        raise PriceParseError(f"negative price: {text!r}")
    return value, currency
