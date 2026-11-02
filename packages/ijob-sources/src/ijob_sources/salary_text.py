"""Read one free-text salary string into a `Money` value.

`parse_salary` is forgiving about layout and strict about meaning: text that
does not state a clear salary gives `None` rather than a guess. Currency
symbols that name no single currency (`$`, `¥`) take the caller's default.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from re import Pattern
from typing import Final

from ijob_core.errors import InvalidValueError
from ijob_core.money import Money, Period

KNOWN_CURRENCIES: Final[frozenset[str]] = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "CAD",
        "AUD",
        "NZD",
        "SGD",
        "HKD",
        "JPY",
        "CNY",
        "INR",
        "IDR",
        "MYR",
        "PHP",
        "THB",
        "VND",
        "KRW",
        "CHF",
        "SEK",
        "NOK",
        "DKK",
        "PLN",
        "CZK",
        "BRL",
        "MXN",
        "ZAR",
    }
)

_SYMBOL_CURRENCIES: Final = {"€": "EUR", "£": "GBP", "₹": "INR"}
_AMBIGUOUS_SYMBOLS: Final = ("$", "¥")
_THOUSANDS_GROUP_DIGITS: Final = 3
_THOUSAND: Final = Decimal(1000)

_LETTER_RUN: Final = re.compile(r"[A-Za-z]+")
_AMOUNT: Final = re.compile(
    r"(?P<head>[0-9]+)(?P<rest>(?:[,. ][0-9]+)*)"
    r"(?P<kilo>[kK](?![A-Za-z]))?(?P<plus>\+)?"
)
_SEPARATED_GROUPS: Final = re.compile(r"([,. ])([0-9]+)")
_LETTERS_BEFORE: Final = re.compile(r"[A-Za-z]+\Z")
_RETIREMENT_PLAN: Final = re.compile(
    r"(?<![$¥€£₹0-9])\b(?:401\(?k\)?|403\(?b\)?|457\(?b\)?)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_UP_TO: Final = re.compile(r"\bup to\b")
_OR_MORE: Final = re.compile(r"\bor more\b")
_LEADING_FROM: Final = re.compile(r"from\b")

_PERIOD_PATTERNS: Final[tuple[tuple[Period, Pattern[str]], ...]] = (
    (
        Period.HOUR,
        re.compile(r"/(?:hr|hour)\b|\bhourly\b|\bper hour\b|\ban hour\b"),
    ),
    (Period.MONTH, re.compile(r"/(?:mo|month)\b|\bmonthly\b|\bper month\b")),
    (
        Period.YEAR,
        re.compile(
            r"/(?:yr|year)\b|\bannually\b|\bper annum\b|\bper year\b|\ba year\b"
        ),
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SalaryDefaults:
    """What a salary text falls back to when it does not say.

    Attributes:
        currency: The currency code that `$` and `¥` stand for, or `None` when
            they stand for none.
        period: The period used when the text names none, or `None` for no
            period.
    """

    currency: str | None
    period: Period | None


def parse_salary(text: str, defaults: SalaryDefaults) -> Money | None:
    """Read a salary string into a `Money`, or `None` when it is not one.

    The currency is the earliest known code written as a whole uppercase
    word, else `€`, `£` or `₹`, else `defaults.currency` when the text holds
    `$` or `¥`. Amounts are digit runs where `,`, `.` or a space followed by
    exactly three digits is a thousands separator, a final `.` followed by any
    other number of digits is the decimal point unless an earlier `.` was a
    thousands separator, and a trailing `k` multiplies by 1,000. Digits joined
    to the letters before them (`H1B`, `Q4`) are not amounts unless those
    letters are a known code (`USD50,000`), and neither are the retirement
    plans `401(k)`, `403(b)` and `457(b)`, with or without parentheses, unless
    a currency symbol comes right before them (`$401k`). Two amounts
    give `min` and `max`; one amount gives `max` after `up to`, and `min` when
    `or more` appears anywhere, a `+` follows the amount (after its `k`, if
    any) or ends the text, or the text starts with `from`; else both. The
    period is the earliest period word, else `defaults.period`.

    Args:
        text: The salary text as a posting writes it.
        defaults: The currency and period to use where the text states none.

    Returns:
        The salary, or `None` when the text names no currency, holds no
        amount, holds three or more amounts, uses an ambiguous separator, or
        describes a range `Money` refuses.
    """
    currency = _currency(text, defaults)
    matches = _amount_matches(text)
    amounts = _amounts(matches)
    if currency is None or amounts is None:
        return None
    plus_after_amount = any(found["plus"] for found in matches)
    lowered = text.lower()
    bounds: tuple[Decimal | None, Decimal | None]
    match amounts:
        case [low, high]:
            bounds = (low, high)
        case [only] if _UP_TO.search(lowered):
            bounds = (None, only)
        case [only] if (
            _OR_MORE.search(lowered)
            or plus_after_amount
            or text.rstrip().endswith("+")
            or _LEADING_FROM.match(lowered.lstrip())
        ):
            bounds = (only, None)
        case [only]:
            bounds = (only, only)
        case _:
            return None
    period = _period(lowered) or defaults.period
    try:
        return Money(min=bounds[0], max=bounds[1], currency=currency, period=period)
    except InvalidValueError:
        return None


def _currency(text: str, defaults: SalaryDefaults) -> str | None:
    for word in _LETTER_RUN.finditer(text):
        if word.group() in KNOWN_CURRENCIES:
            return word.group()
    symbols = [(text.find(s), c) for s, c in _SYMBOL_CURRENCIES.items() if s in text]
    if symbols:
        return min(symbols)[1]
    if any(symbol in text for symbol in _AMBIGUOUS_SYMBOLS):
        return defaults.currency
    return None


def _amount_matches(text: str) -> list[re.Match[str]]:
    """Find the amounts in the text, skipping retirement plans and word digits."""
    blanked = _RETIREMENT_PLAN.sub(lambda plan: " " * len(plan.group()), text)
    return [
        found
        for found in _AMOUNT.finditer(blanked)
        if not _joined_to_a_word(blanked, found.start())
    ]


def _joined_to_a_word(text: str, start: int) -> bool:
    """Return whether letters other than a known code end right at `start`."""
    letters = _LETTERS_BEFORE.search(text, 0, start)
    return letters is not None and letters.group() not in KNOWN_CURRENCIES


def _amounts(matches: list[re.Match[str]]) -> list[Decimal] | None:
    """Read every matched amount, or `None` when one is ambiguous."""
    amounts: list[Decimal] = []
    for found in matches:
        amount = _number(found["head"], found["rest"])
        if amount is None:
            return None
        amounts.append(amount * _THOUSAND if found["kilo"] else amount)
    return amounts


def _number(head: str, rest: str) -> Decimal | None:
    """Read a leading digit run and its separated groups, or `None` if unclear."""
    digits = head
    groups = _SEPARATED_GROUPS.findall(rest)
    dot_groups_thousands = False
    for index, (separator, group) in enumerate(groups):
        if len(group) == _THOUSANDS_GROUP_DIGITS:
            digits += group
            dot_groups_thousands = dot_groups_thousands or separator == "."
        elif separator == "." and index == len(groups) - 1 and not dot_groups_thousands:
            return Decimal(f"{digits}.{group}")
        else:
            return None
    return Decimal(digits)


def _period(lowered: str) -> Period | None:
    """Find the period word that appears earliest in the lowercased text."""
    found = [
        (match.start(), period)
        for period, pattern in _PERIOD_PATTERNS
        if (match := pattern.search(lowered))
    ]
    return min(found)[1] if found else None
