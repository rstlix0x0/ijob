"""Salary amounts for job postings.

`Money` holds a pay range or a single pay bound in one currency. It validates
its fields when built, so code that receives a `Money` can trust it.
`money_text` writes one as text. `reaches` says whether one pay reaches another,
whatever their periods and currencies.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from ijob_core.errors import InvalidValueError

_CURRENCY_PATTERN: Final = re.compile(r"[A-Z]{3}")
_HOURS_PER_YEAR: Final = Decimal(2080)
_MONTHS_PER_YEAR: Final = Decimal(12)
_YEARLY_FROM_USD: Final = Decimal(20000)
"""A stated amount with no period at or above this many US dollars is yearly."""

UNITS_PER_USD: Final = {
    "USD": Decimal(1),
    "EUR": Decimal("0.92"),
    "GBP": Decimal("0.79"),
    "CHF": Decimal("0.88"),
    "CAD": Decimal("1.37"),
    "AUD": Decimal("1.52"),
    "NZD": Decimal("1.65"),
    "SGD": Decimal("1.33"),
    "HKD": Decimal("7.8"),
    "JPY": Decimal(150),
    "KRW": Decimal(1350),
    "CNY": Decimal("7.2"),
    "TWD": Decimal(32),
    "INR": Decimal(84),
    "IDR": Decimal(16000),
    "MYR": Decimal("4.5"),
    "THB": Decimal(34),
    "VND": Decimal(25000),
    "PHP": Decimal(57),
    "PKR": Decimal(280),
    "BDT": Decimal(115),
    "AED": Decimal("3.67"),
    "SAR": Decimal("3.75"),
    "ILS": Decimal("3.7"),
    "TRY": Decimal(33),
    "PLN": Decimal("3.9"),
    "CZK": Decimal(23),
    "HUF": Decimal(360),
    "RON": Decimal("4.6"),
    "SEK": Decimal("10.5"),
    "NOK": Decimal("10.7"),
    "DKK": Decimal("6.9"),
    "BRL": Decimal("5.5"),
    "MXN": Decimal(18),
    "ARS": Decimal(900),
    "COP": Decimal(4000),
    "CLP": Decimal(950),
    "ZAR": Decimal(18),
    "NGN": Decimal(1500),
    "EGP": Decimal(48),
    "UAH": Decimal(41),
}
"""Rough, fixed rates, for telling whether a salary is in range: never for money."""


class Period(StrEnum):
    """The span of time a salary amount is paid for."""

    HOUR = "hour"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True, slots=True)
class Money:
    """A salary range or single bound, in one currency.

    Attributes:
        min: The lower bound, when the source gives one.
        max: The upper bound, when the source gives one.
        currency: The three-letter uppercase currency code, e.g. `USD`.
        period: The span each amount is paid for, or `None` when the source
            states no period.
    """

    min: Decimal | None
    max: Decimal | None
    currency: str
    period: Period | None

    def __post_init__(self) -> None:
        """Reject an amount that cannot describe a salary.

        Raises:
            InvalidValueError: If both bounds are `None`, a bound is negative
                or not finite, `min` is above `max`, or `currency` is not
                three uppercase ASCII letters.
        """
        if self.min is None and self.max is None:
            raise InvalidValueError("money", "has neither a min nor a max")
        for name, bound in (("min", self.min), ("max", self.max)):
            if bound is None:
                continue
            if not bound.is_finite():
                raise InvalidValueError(name, f"{bound} is not a finite number")
            if bound < 0:
                raise InvalidValueError(name, f"{bound} is negative")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise InvalidValueError("min", f"{self.min} is above max {self.max}")
        if _CURRENCY_PATTERN.fullmatch(self.currency) is None:
            reason = f"{self.currency!r} is not three uppercase letters"
            raise InvalidValueError("currency", reason)


def money_text(money: Money) -> str:
    """Write a salary range as `120000-150000 USD/year`.

    A range with one bound reads `from 120000 USD` or `up to 150000 USD`; the
    `/period` is left off when the source states none.
    """
    match money.min, money.max:
        case low, None:
            amount = f"from {low:f}"
        case None, high:
            amount = f"up to {high:f}"
        case low, high:
            amount = f"{low:f}-{high:f}"
    period = f"/{money.period}" if money.period is not None else ""
    return f"{amount} {money.currency}{period}"


def _amount(money: Money) -> Decimal:
    """Pick the figure a salary is judged on: its max, else its min."""
    return next(bound for bound in (money.max, money.min) if bound is not None)


def _yearly(money: Money, period: Period | None) -> Decimal:
    """Scale an amount to a year, given the period it is paid for."""
    match period:
        case Period.HOUR:
            return _amount(money) * _HOURS_PER_YEAR
        case Period.MONTH:
            return _amount(money) * _MONTHS_PER_YEAR
        case _:
            return _amount(money)


def _period_of(money: Money) -> Period | None:
    """Give a salary's period, working out a likely one when it states none.

    Most sources state no period. A large amount is a year's pay, and a small one
    a month's.
    """
    if money.period is not None:
        return money.period
    rate = UNITS_PER_USD.get(money.currency)
    if rate is None or _amount(money) / rate >= _YEARLY_FROM_USD:
        return Period.YEAR
    return Period.MONTH


def reaches(salary: Money, threshold: Money) -> bool:
    """Say whether a salary reaches a threshold.

    Different periods compare as a year's pay. Different currencies compare
    through rough, fixed rates. A currency with no rate compares only with itself.

    Args:
        salary: The pay a job states.
        threshold: The least pay wanted.

    Returns:
        Whether the salary reaches it; `False` when the two cannot be compared.

    Examples:
        >>> usd = Money(None, Decimal(120000), "USD", Period.YEAR)
        >>> reaches(usd, Money(Decimal(6000), None, "SGD", Period.MONTH))
        True
        >>> reaches(usd, Money(Decimal(20000), None, "USD", Period.MONTH))
        False
    """
    pay = _yearly(salary, _period_of(salary))
    wanted = _yearly(threshold, _period_of(threshold))
    if salary.currency == threshold.currency:
        return pay >= wanted
    pay_rate = UNITS_PER_USD.get(salary.currency)
    wanted_rate = UNITS_PER_USD.get(threshold.currency)
    if pay_rate is None or wanted_rate is None:
        return False
    return pay * wanted_rate >= wanted * pay_rate
