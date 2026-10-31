"""Salary amounts for job postings.

`Money` holds a pay range or a single pay bound in one currency. It validates
its fields when built, so code that receives a `Money` can trust it.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from ijob_core.errors import InvalidValueError

_CURRENCY_PATTERN: Final = re.compile(r"[A-Z]{3}")


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
