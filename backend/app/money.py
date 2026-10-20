from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
HUNDRED = Decimal(100)
THOUSAND = Decimal(1000)


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_to_cents(value: Decimal) -> int:
    return int((money(value) * HUNDRED).to_integral_value(rounding=ROUND_HALF_UP))


def cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / HUNDRED).quantize(CENT)


def cents_to_str(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def decimal_to_milli(value: Decimal) -> int:
    return int((value * THOUSAND).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def milli_to_decimal(milli: int) -> Decimal:
    return Decimal(milli) / THOUSAND


def milli_to_str(milli: int) -> str:
    sign = "-" if milli < 0 else ""
    whole, frac = divmod(abs(milli), 1000)
    if frac == 0:
        return f"{sign}{whole}"
    text = f"{sign}{whole}.{frac:03d}".rstrip("0")
    return text


def parse_decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if stripped == "" or stripped == "-1":
            return None
        return Decimal(stripped)
    number = Decimal(str(value))
    if number == Decimal("-1"):
        return None
    return number


def percent_to_bps(rate: Decimal) -> int:
    normalized = rate
    if Decimal("0") < rate < Decimal("1"):
        normalized = rate * HUNDRED
    return int((normalized * HUNDRED).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def bps_to_str(bps: int) -> str:
    whole, frac = divmod(bps, 100)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:02d}".rstrip("0")
