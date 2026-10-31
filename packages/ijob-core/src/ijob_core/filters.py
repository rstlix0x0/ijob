"""Deterministic filters that decide which postings to show.

`FilterSpec` holds the criteria and `keep` applies them to one posting. A posting
is dropped only when the data decides: a rule whose input the posting lacks keeps
the posting.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from ijob_core.countries import CountryCode
from ijob_core.model import Posting, RemoteScope
from ijob_core.money import Money, Period

_COUNTRY_BOUND_SCOPES: Final = frozenset({RemoteScope.NONE, RemoteScope.COUNTRY})
_MONTHS_PER_PERIOD: Final = {Period.MONTH: 1, Period.YEAR: 12}


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """The criteria a posting must meet to be kept; every field defaults to "no filter".

    Attributes:
        include: Patterns for wanted roles, searched in the title. Empty means
            any role is wanted.
        exclude: Patterns for unwanted roles, searched in the title.
        countries: The countries to keep; empty means any.
        remote: The remote scopes to keep; empty means any.
        since: The oldest a posting's publication date may be, measured back
            from the time of the check; `None` means any age.
        companies: Company names to keep, each matched as a case-insensitive
            substring; empty means any company.
        min_salary: The least pay to keep; `None` means any pay.
        require_salary: Whether a posting that states no salary is dropped.
    """

    include: tuple[re.Pattern[str], ...] = ()
    exclude: tuple[re.Pattern[str], ...] = ()
    countries: frozenset[CountryCode] = field(default_factory=frozenset)
    remote: frozenset[RemoteScope] = field(default_factory=frozenset)
    since: timedelta | None = None
    companies: tuple[str, ...] = ()
    min_salary: Money | None = None
    require_salary: bool = False


def keep(posting: Posting, spec: FilterSpec, *, now: datetime) -> bool:
    """Decide whether a posting passes every filter in a spec.

    Args:
        posting: The posting to check.
        spec: The criteria to apply.
        now: The time `spec.since` is measured back from; timezone-aware.

    Returns:
        `False` if some rule's data shows the posting does not match, else `True`.
    """
    return (
        _role_passes(posting, spec)
        and _country_passes(posting, spec)
        and _remote_passes(posting, spec)
        and _since_passes(posting, spec, now)
        and _company_passes(posting, spec)
        and _salary_passes(posting, spec)
    )


def _matches_any(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    """Tell whether any pattern is found in the text."""
    return any(pattern.search(text) for pattern in patterns)


def _role_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Apply the role rule: exclusions, then inclusions judged by title and hint."""
    if _matches_any(spec.exclude, posting.title):
        return False
    if not spec.include or _matches_any(spec.include, posting.title):
        return True
    if posting.role_hint is None:
        return True
    return _matches_any(spec.include, posting.role_hint)


def _country_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Apply the country rule to a posting tied to one country."""
    if not spec.countries or posting.country is None:
        return True
    if posting.remote not in _COUNTRY_BOUND_SCOPES:
        return True
    return posting.country in spec.countries


def _remote_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Apply the remote-scope rule."""
    if not spec.remote or posting.remote is None:
        return True
    return posting.remote in spec.remote


def _since_passes(posting: Posting, spec: FilterSpec, now: datetime) -> bool:
    """Apply the age rule."""
    if spec.since is None or posting.posted_at is None:
        return True
    return posting.posted_at >= now - spec.since


def _company_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Apply the company rule."""
    if not spec.companies:
        return True
    company = posting.company.casefold()
    return any(name.casefold() in company for name in spec.companies)


def _amount(money: Money) -> Decimal:
    """Pick the figure a salary is judged on: its max, else its min."""
    return next(bound for bound in (money.max, money.min) if bound is not None)


def _months_per_period(period: Period | None) -> int | None:
    """Count the months a period spans, or `None` for a period that cannot convert."""
    return _MONTHS_PER_PERIOD.get(period) if period is not None else None


def _reaches(salary: Money, threshold: Money) -> bool | None:
    """Compare a salary with a threshold.

    Returns:
        Whether the salary reaches the threshold, or `None` when the two differ
        in currency or in a period that cannot be converted.
    """
    if salary.currency != threshold.currency:
        return None
    if salary.period is None or threshold.period is None:
        return None
    if salary.period == threshold.period:
        return _amount(salary) >= _amount(threshold)
    salary_months = _months_per_period(salary.period)
    threshold_months = _months_per_period(threshold.period)
    if salary_months is None or threshold_months is None:
        return None
    return _amount(salary) * threshold_months >= _amount(threshold) * salary_months


def _salary_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Apply the salary rules: the threshold on comparable pay, then the requirement."""
    if spec.require_salary and not posting.salaries:
        return False
    if spec.min_salary is None:
        return True
    verdicts = [_reaches(salary, spec.min_salary) for salary in posting.salaries]
    comparable = [verdict for verdict in verdicts if verdict is not None]
    return not comparable or any(comparable)
