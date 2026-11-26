"""Money as integer micros plus an ISO 4217 currency code. Floats are never used."""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Self

MICROS_PER_UNIT = 1_000_000
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CurrencyMismatchError(ValueError):
    """Raised when arithmetic mixes two different currencies."""


def _round_to_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True, slots=True)
class Money:
    micros: int
    currency: str

    def __post_init__(self) -> None:
        if not _CURRENCY_RE.match(self.currency):
            raise ValueError(f"invalid currency code: {self.currency!r}")
        if isinstance(self.micros, bool) or not isinstance(self.micros, int):
            raise TypeError("micros must be an int")

    @classmethod
    def from_decimal(cls, amount: Decimal | str | int, currency: str) -> Self:
        return cls(_round_to_int(Decimal(amount) * MICROS_PER_UNIT), currency)

    def to_decimal(self) -> Decimal:
        return Decimal(self.micros) / MICROS_PER_UNIT

    def _check(self, other: Self) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")

    def __add__(self, other: Self) -> Self:
        self._check(other)
        return type(self)(self.micros + other.micros, self.currency)

    def __sub__(self, other: Self) -> Self:
        self._check(other)
        return type(self)(self.micros - other.micros, self.currency)

    def scaled(self, factor: Decimal | str | int) -> Self:
        return type(self)(_round_to_int(Decimal(self.micros) * Decimal(factor)), self.currency)

    def __lt__(self, other: Self) -> bool:
        self._check(other)
        return self.micros < other.micros

    def __le__(self, other: Self) -> bool:
        self._check(other)
        return self.micros <= other.micros

    def __gt__(self, other: Self) -> bool:
        self._check(other)
        return self.micros > other.micros

    def __ge__(self, other: Self) -> bool:
        self._check(other)
        return self.micros >= other.micros
