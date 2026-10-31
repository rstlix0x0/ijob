import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ijob_core.countries import CountryCode
from ijob_core.filters import FilterSpec, keep
from ijob_core.model import Posting, PostingId, RemoteScope
from ijob_core.money import Money, Period

_NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
_DE = CountryCode("DE")
_SG = CountryCode("SG")
_ENGINEER = re.compile(r"\bengineer", re.IGNORECASE)
_ACCOUNT_EXEC = re.compile(r"\baccount executive\b", re.IGNORECASE)


def _posting(  # noqa: PLR0913  # builder mirrors the optional fields of Posting
    *,
    title: str = "Backend Engineer",
    company: str = "Acme",
    posted_at: datetime | None = None,
    salaries: tuple[Money, ...] = (),
    country: CountryCode | None = None,
    remote: RemoteScope | None = None,
    role_hint: str | None = None,
) -> Posting:
    return Posting(
        id=PostingId(1),
        title=title,
        company=company,
        location=None,
        url="https://example.com/job/1",
        posted_at=posted_at,
        first_seen=_NOW,
        last_seen=_NOW,
        salaries=salaries,
        country=country,
        remote=remote,
        role_hint=role_hint,
    )


def _money(amount: str, currency: str, period: Period | None) -> Money:
    return Money(None, Decimal(amount), currency, period)


def test_empty_spec_keeps_everything() -> None:
    assert keep(_posting(), FilterSpec(), now=_NOW)


def test_role_exclude_drops() -> None:
    spec = FilterSpec(exclude=(_ACCOUNT_EXEC,))
    posting = _posting(title="Account Executive - Commercial")
    assert not keep(posting, spec, now=_NOW)


def test_role_exclude_keeps_other_titles() -> None:
    spec = FilterSpec(exclude=(_ACCOUNT_EXEC,))
    assert keep(_posting(title="Backend Engineer"), spec, now=_NOW)


def test_role_include_match_keeps() -> None:
    spec = FilterSpec(include=(_ENGINEER,))
    assert keep(_posting(title="Backend Engineer", role_hint="Finance"), spec, now=_NOW)


def test_role_hint_matching_include_keeps() -> None:
    spec = FilterSpec(include=(_ENGINEER,))
    posting = _posting(title="Platform Lead", role_hint="Software Engineering")
    assert keep(posting, spec, now=_NOW)


def test_role_no_include_match_without_hint_passes() -> None:
    spec = FilterSpec(include=(_ENGINEER,))
    assert keep(_posting(title="Platform Lead", role_hint=None), spec, now=_NOW)


def test_role_hint_without_match_drops() -> None:
    spec = FilterSpec(include=(_ENGINEER,))
    posting = _posting(title="Platform Lead", role_hint="Finance")
    assert not keep(posting, spec, now=_NOW)


def test_empty_include_never_drops() -> None:
    spec = FilterSpec(include=())
    posting = _posting(title="Platform Lead", role_hint="Finance")
    assert keep(posting, spec, now=_NOW)


def test_country_drops_known_onsite() -> None:
    spec = FilterSpec(countries=frozenset({_SG}))
    posting = _posting(country=_DE, remote=RemoteScope.NONE)
    assert not keep(posting, spec, now=_NOW)


def test_country_drops_known_country_scoped_remote() -> None:
    spec = FilterSpec(countries=frozenset({_SG}))
    posting = _posting(country=_DE, remote=RemoteScope.COUNTRY)
    assert not keep(posting, spec, now=_NOW)


def test_country_in_set_keeps() -> None:
    spec = FilterSpec(countries=frozenset({_SG, _DE}))
    posting = _posting(country=_DE, remote=RemoteScope.NONE)
    assert keep(posting, spec, now=_NOW)


@pytest.mark.parametrize(
    ("country", "remote"),
    [
        (None, RemoteScope.NONE),
        (_DE, None),
        (_DE, RemoteScope.REGION),
        (_DE, RemoteScope.WORLDWIDE),
    ],
)
def test_country_passes_unknown_or_wide(
    country: CountryCode | None, remote: RemoteScope | None
) -> None:
    spec = FilterSpec(countries=frozenset({_SG}))
    assert keep(_posting(country=country, remote=remote), spec, now=_NOW)


def test_remote_filter() -> None:
    spec = FilterSpec(remote=frozenset({RemoteScope.WORLDWIDE}))
    assert not keep(_posting(remote=RemoteScope.NONE), spec, now=_NOW)
    assert keep(_posting(remote=RemoteScope.WORLDWIDE), spec, now=_NOW)
    assert keep(_posting(remote=None), spec, now=_NOW)


def test_since() -> None:
    spec = FilterSpec(since=timedelta(days=7))
    old = _posting(posted_at=_NOW - timedelta(days=10))
    fresh = _posting(posted_at=_NOW - timedelta(days=3))
    assert not keep(old, spec, now=_NOW)
    assert keep(fresh, spec, now=_NOW)
    assert keep(_posting(posted_at=None), spec, now=_NOW)


def test_since_keeps_exact_boundary() -> None:
    spec = FilterSpec(since=timedelta(days=7))
    boundary = _posting(posted_at=_NOW - timedelta(days=7))
    assert keep(boundary, spec, now=_NOW)


def test_company_substring() -> None:
    spec = FilterSpec(companies=("stripe",))
    assert keep(_posting(company="Stripe, Inc."), spec, now=_NOW)
    assert not keep(_posting(company="Ramp"), spec, now=_NOW)


def test_company_any_entry_matches() -> None:
    spec = FilterSpec(companies=("stripe", "RAMP"))
    assert keep(_posting(company="Ramp"), spec, now=_NOW)


def test_salary_comparable_below_drops() -> None:
    spec = FilterSpec(min_salary=_money("6000", "SGD", Period.MONTH))
    posting = _posting(salaries=(_money("5000", "SGD", Period.MONTH),))
    assert not keep(posting, spec, now=_NOW)


def test_salary_comparable_reaching_keeps() -> None:
    spec = FilterSpec(min_salary=_money("6000", "SGD", Period.MONTH))
    posting = _posting(salaries=(_money("6000", "SGD", Period.MONTH),))
    assert keep(posting, spec, now=_NOW)


def test_salary_any_comparable_reaching_keeps() -> None:
    spec = FilterSpec(min_salary=_money("6000", "SGD", Period.MONTH))
    posting = _posting(
        salaries=(
            _money("5000", "SGD", Period.MONTH),
            _money("7000", "SGD", Period.MONTH),
        )
    )
    assert keep(posting, spec, now=_NOW)


def test_salary_judged_on_min_when_no_max() -> None:
    spec = FilterSpec(min_salary=_money("6000", "SGD", Period.MONTH))
    low = Money(Decimal(5000), None, "SGD", Period.MONTH)
    assert not keep(_posting(salaries=(low,)), spec, now=_NOW)


def test_salary_judged_on_max_over_min() -> None:
    spec = FilterSpec(min_salary=_money("6000", "SGD", Period.MONTH))
    ranged = Money(Decimal(4000), Decimal(7000), "SGD", Period.MONTH)
    assert keep(_posting(salaries=(ranged,)), spec, now=_NOW)


def test_salary_month_year_convert() -> None:
    spec = FilterSpec(min_salary=_money("72000", "SGD", Period.YEAR))
    assert keep(
        _posting(salaries=(_money("6500", "SGD", Period.MONTH),)), spec, now=_NOW
    )
    assert not keep(
        _posting(salaries=(_money("5500", "SGD", Period.MONTH),)), spec, now=_NOW
    )


def test_salary_year_against_month_threshold() -> None:
    spec = FilterSpec(min_salary=_money("6000", "SGD", Period.MONTH))
    assert keep(
        _posting(salaries=(_money("72000", "SGD", Period.YEAR),)), spec, now=_NOW
    )
    assert not keep(
        _posting(salaries=(_money("70000", "SGD", Period.YEAR),)), spec, now=_NOW
    )


@pytest.mark.parametrize(
    "salary",
    [
        _money("1", "USD", Period.MONTH),
        _money("1", "SGD", Period.HOUR),
        _money("1", "SGD", None),
    ],
)
def test_salary_incomparable_passes(salary: Money) -> None:
    spec = FilterSpec(min_salary=_money("6000", "SGD", Period.MONTH))
    assert keep(_posting(salaries=(salary,)), spec, now=_NOW)


def test_salary_hour_threshold_compares_hour_only() -> None:
    spec = FilterSpec(min_salary=_money("50", "SGD", Period.HOUR))
    assert not keep(
        _posting(salaries=(_money("40", "SGD", Period.HOUR),)), spec, now=_NOW
    )
    assert keep(_posting(salaries=(_money("1", "SGD", Period.MONTH),)), spec, now=_NOW)


def test_min_salary_without_salaries_passes() -> None:
    spec = FilterSpec(min_salary=_money("6000", "SGD", Period.MONTH))
    assert keep(_posting(), spec, now=_NOW)


def test_require_salary() -> None:
    assert not keep(_posting(), FilterSpec(require_salary=True), now=_NOW)
    assert keep(_posting(), FilterSpec(require_salary=False), now=_NOW)
    salaried = _posting(salaries=(_money("1", "USD", None),))
    assert keep(salaried, FilterSpec(require_salary=True), now=_NOW)


def test_rules_combine_with_and() -> None:
    spec = FilterSpec(include=(_ENGINEER,), companies=("acme",))
    assert keep(_posting(company="Acme"), spec, now=_NOW)
    assert not keep(_posting(company="Other"), spec, now=_NOW)
