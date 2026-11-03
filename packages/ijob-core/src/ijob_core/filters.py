"""Deterministic filters that decide which postings to show.

`FilterSpec` holds the criteria and `keep` applies them to one posting. A filter
shows only the postings it can confirm match, because a job search that returns
jobs from everywhere is no filter at all. Most sources leave fields empty, so what
a field does not say is read from the location text where it can be: `Tokyo` is in
Japan, and `Remote - US` is remote within a country.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from ijob_core.countries import CountryCode, countries_in
from ijob_core.model import Posting, RemoteScope
from ijob_core.money import Money, reaches

_WORLDWIDE_WORDS: Final = re.compile(
    r"\b(?:worldwide|anywhere|global|globally|international)\b", re.IGNORECASE
)
_NOT_A_PLACE: Final = re.compile(
    r"\s*(?:full[- ]?time|part[- ]?time|n/?a|location|tbd|tba|contract|permanent)\s*",
    re.IGNORECASE,
)
_REMOTE_WORDS: Final = re.compile(
    r"\b(?:remote|distributed|work from home|wfh)\b", re.IGNORECASE
)
_REGION_WORDS: Final = re.compile(
    r"\b(?:emea|apac|latam|amer|americas|europe|european union|eu|north america|"
    r"south america|asia|africa|middle east|oceania)\b",
    re.IGNORECASE,
)


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
    if posting.role_hint is None:  # no title match and nothing else to go on
        return False
    return _matches_any(spec.include, posting.role_hint)


def remote_scope(posting: Posting) -> RemoteScope | None:
    """Work out where a job may be done from, reading its location if need be.

    Args:
        posting: The posting.

    Returns:
        The scope its source states, or else the one its location text implies:
        `Remote - US` is within a country, `Remote - EMEA` within a region, and a
        bare `Remote` counts as worldwide since it names no limit. A location with
        no remote word is on site. `None` when it has no location to read.
    """
    if posting.remote is not None:
        return posting.remote
    text = posting.location
    if not text or _NOT_A_PLACE.fullmatch(text):
        return None
    if _WORLDWIDE_WORDS.search(text):
        return RemoteScope.WORLDWIDE
    if not _REMOTE_WORDS.search(text):
        return RemoteScope.NONE
    return _reach_of_remote(posting, text)


def _reach_of_remote(posting: Posting, text: str) -> RemoteScope:
    """Say how far a remote job reaches, from what its location names."""
    if posting.country is not None or countries_in(text):
        return RemoteScope.COUNTRY
    if _REGION_WORDS.search(text):
        return RemoteScope.REGION
    return RemoteScope.WORLDWIDE


def _country_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Keep a job in a wanted country, or one stated to be open to the whole world.

    The location the user reads decides when it names a country, since a source's
    country field sometimes disagrees with it. The field decides otherwise.
    """
    if not spec.countries:
        return True
    text = posting.location or ""
    if posting.remote is RemoteScope.WORLDWIDE or _WORLDWIDE_WORDS.search(text):
        return True
    named = countries_in(text)
    if named:
        return bool(named & spec.countries)
    return posting.country in spec.countries


def _remote_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Keep a job whose remote scope is known to be a wanted one."""
    return not spec.remote or remote_scope(posting) in spec.remote


def _since_passes(posting: Posting, spec: FilterSpec, now: datetime) -> bool:
    """Keep a job posted recently, or first seen recently when it states no date."""
    if spec.since is None:
        return True
    return (posting.posted_at or posting.first_seen) >= now - spec.since


def _company_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Apply the company rule."""
    if not spec.companies:
        return True
    company = posting.company.casefold()
    return any(name.casefold() in company for name in spec.companies)


def _salary_passes(posting: Posting, spec: FilterSpec) -> bool:
    """Keep a job that states enough pay, whatever its currency and period."""
    if spec.require_salary and not posting.salaries:
        return False
    if spec.min_salary is None:
        return True
    return any(reaches(salary, spec.min_salary) for salary in posting.salaries)
