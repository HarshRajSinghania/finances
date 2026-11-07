"""
Money, exactly.

Binary floats cannot hold most decimal amounts (1.05 × 0.70 is $0.7349999…),
so the engine never computes with them. Amounts are ``Decimal`` quantized to the
currency's ISO 4217 minor unit, compared as integer minor units, and written to
JSON as strings so no reader can turn them back into floats.

The parsers underneath predate this module and hand over floats already rounded
to cents; ``amount()`` goes through ``str`` so 0.1 + 0.2 arrives as 0.30, not as
the binary expansion of 0.30000000000000004.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

# ISO 4217 minor units that are not 2. Everything else is 2.
EXPONENTS = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0,
    "KRW": 0, "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}

ROUNDING = ROUND_HALF_EVEN          # recorded in every report
ZERO = Decimal(0)


def exponent(currency: str) -> int:
    return EXPONENTS.get((currency or "").upper(), 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-exponent(currency))


def amount(value, currency: str = "") -> Decimal:
    """Any number a source hands over, as an exact Decimal at the currency's precision."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, bool):
        raise TypeError("a boolean is not an amount")
    elif isinstance(value, (int, float)):
        d = Decimal(str(value))
    else:
        text = str(value).strip().replace(",", "")
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"not an amount: {value!r}") from None
    return d.quantize(quantum(currency), rounding=ROUNDING)


def minor(value: Decimal, currency: str) -> int:
    """Integer minor units: what amounts are compared and summed in."""
    return int(value.scaleb(exponent(currency)).to_integral_value(rounding=ROUNDING))


def from_minor(units: int, currency: str) -> Decimal:
    return amount(Decimal(units).scaleb(-exponent(currency)), currency)


def text(value: Decimal | None) -> str | None:
    """The JSON form: a plain decimal string, never exponent notation."""
    return None if value is None else format(value, "f")


def show(value: Decimal, currency: str = "") -> str:
    """For people: thousands separators and the currency code."""
    places = exponent(currency)
    out = f"{value:,.{places}f}"
    return f"{out} {currency}".strip() if currency else out
