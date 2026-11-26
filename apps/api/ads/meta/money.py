"""Conversion between our integer micros and Meta's minor currency units.

Meta expresses budgets in the smallest unit of the account currency (cents for USD, whole units for
zero-decimal currencies). Everything inside the platform uses micros.
"""

MICROS = 1_000_000
ZERO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "PYG", "XAF", "XOF", "RWF"})


def _factor(currency: str) -> int:
    """Micros per Meta minor unit."""
    return MICROS if currency.upper() in ZERO_DECIMAL else MICROS // 100


def to_minor(micros: int, currency: str) -> int:
    return micros // _factor(currency)


def from_minor(minor: int | str, currency: str) -> int:
    return int(minor) * _factor(currency)
