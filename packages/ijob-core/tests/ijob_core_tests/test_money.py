import dataclasses
from decimal import Decimal

import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.money import Money, Period, money_text


def test_money_accepts_range_and_single_bound() -> None:
    ranged = Money(Decimal(1), Decimal(2), "USD", Period.YEAR)
    single = Money(None, Decimal(5), "EUR", None)
    assert (ranged.min, ranged.max, ranged.currency, ranged.period) == (
        Decimal(1),
        Decimal(2),
        "USD",
        Period.YEAR,
    )
    assert (single.min, single.max, single.period) == (None, Decimal(5), None)


def test_money_accepts_equal_and_zero_bounds() -> None:
    assert Money(Decimal(0), Decimal(0), "JPY", Period.HOUR).max == Decimal(0)
    assert Money(Decimal(7), Decimal(7), "JPY", Period.MONTH).min == Decimal(7)


def test_money_rejects_no_bound() -> None:
    with pytest.raises(InvalidValueError):
        Money(None, None, "USD", Period.YEAR)


def test_money_rejects_negative_bound() -> None:
    with pytest.raises(InvalidValueError):
        Money(Decimal(-1), None, "USD", None)
    with pytest.raises(InvalidValueError):
        Money(None, Decimal(-1), "USD", None)


def test_money_rejects_min_above_max() -> None:
    with pytest.raises(InvalidValueError):
        Money(Decimal(3), Decimal(2), "USD", None)


@pytest.mark.parametrize("currency", ["usd", "US", "USDX", "", "US1", "USD\n"])
def test_money_rejects_bad_currency(currency: str) -> None:
    with pytest.raises(InvalidValueError):
        Money(Decimal(1), None, currency, None)


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity")])
def test_money_rejects_non_finite_bound(bad: Decimal) -> None:
    with pytest.raises(InvalidValueError):
        Money(bad, None, "USD", None)
    with pytest.raises(InvalidValueError):
        Money(None, bad, "USD", None)


def test_period_values() -> None:
    assert [period.value for period in Period] == ["hour", "month", "year"]


def test_money_is_frozen_and_hashable() -> None:
    money = Money(Decimal(1), None, "USD", None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        money.currency = "EUR"  # type: ignore[misc]
    assert hash(money) == hash(Money(Decimal(1), None, "USD", None))


@pytest.mark.parametrize(
    ("money", "expected"),
    [
        (
            Money(Decimal(120000), Decimal(150000), "USD", Period.YEAR),
            "120000-150000 USD/year",
        ),
        (Money(Decimal(120000), None, "USD", None), "from 120000 USD"),
        (Money(None, Decimal(150000), "EUR", Period.MONTH), "up to 150000 EUR/month"),
        (Money(None, Decimal(150000), "USD", None), "up to 150000 USD"),
    ],
)
def test_money_text_moved(money: Money, expected: str) -> None:
    assert money_text(money) == expected
