from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.money import CurrencyMismatchError, Money


def test_from_decimal_rounds_half_even_to_micros() -> None:
    assert Money.from_decimal("1.2345675", "CAD").micros == 1_234_568
    assert Money.from_decimal("1.2345665", "CAD").micros == 1_234_566


def test_arithmetic_and_comparison_require_same_currency() -> None:
    a, b = Money(5_000_000, "CAD"), Money(2_000_000, "CAD")
    assert (a + b).micros == 7_000_000
    assert (a - b).micros == 3_000_000
    assert a > b and b < a and a >= a and b <= a
    with pytest.raises(CurrencyMismatchError):
        a + Money(1, "USD")
    with pytest.raises(CurrencyMismatchError):
        a < Money(1, "USD")  # noqa: B015


@pytest.mark.parametrize("bad", ["cad", "CA", "CADD", "12A", ""])
def test_rejects_invalid_currency(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid currency"):
        Money(1, bad)


def test_rejects_non_int_micros() -> None:
    with pytest.raises(TypeError):
        Money(1.5, "CAD")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True, "CAD")


def test_scaled_uses_banker_rounding() -> None:
    assert Money(5, "CAD").scaled("0.5").micros == 2
    assert Money(7, "CAD").scaled("0.5").micros == 4


@given(
    st.integers(min_value=-(10**15), max_value=10**15),
    st.integers(min_value=-(10**15), max_value=10**15),
)
def test_addition_is_commutative_and_exact(x: int, y: int) -> None:
    a, b = Money(x, "USD"), Money(y, "USD")
    assert a + b == b + a
    assert (a + b).micros == x + y


@given(st.decimals(min_value=Decimal("-1000000"), max_value=Decimal("1000000"), places=6))
def test_decimal_round_trip_is_lossless_at_six_places(value: Decimal) -> None:
    assert Money.from_decimal(value, "CAD").to_decimal() == value
