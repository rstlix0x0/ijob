import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.model import RemoteScope
from ijob_core.money import Money, Period
from ijob_sources.jsonld import (
    JobPostingFields,
    job_postings,
    ld_json_blocks,
    read_job_posting,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "jsonld"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _page(*blocks: object) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(block)}</script>'
        for block in blocks
    )
    return f"<html><head>{scripts}</head><body></body></html>"


def _read(html_text: str) -> JobPostingFields:
    (posting,) = job_postings(html_text)
    return read_job_posting(posting)


def _posting(**fields: object) -> dict[str, object]:
    return {"@type": "JobPosting", "title": "Engineer", **fields}


def _location(address: object) -> dict[str, Any]:
    return {"@type": "Place", "address": address}


def test_teamtailor_page() -> None:
    fields = _read(_fixture("teamtailor.html"))

    assert fields.title == "Solution Architect (Bilingual French/English)"
    assert fields.company == "Teamtailor"
    assert fields.location == "Stockholm"
    assert fields.country == "SE"
    assert fields.identifier == "8636263"
    assert fields.posted_at == datetime(
        2026, 10, 29, 13, 25, 57, tzinfo=timezone(timedelta(hours=1))
    )


def test_jazzhr_page_picks_jobposting_block() -> None:
    html_text = _fixture("jazzhr.html")

    assert len(job_postings(html_text)) == 1
    fields = _read(html_text)

    assert fields.title == "Open Source Software Release Engineer"
    assert fields.company == "Eclipse Foundation, Inc."
    assert fields.location == "Brussels"
    assert fields.salaries == (
        Money(Decimal(55000), Decimal(80000), "EUR", Period.YEAR),
    )
    assert fields.remote is RemoteScope.COUNTRY
    assert fields.posted_at == datetime(2026, 10, 7, tzinfo=UTC)
    assert fields.url is not None
    assert fields.url.startswith("https://eclipsefoundation.applytojob.com/")


def test_lever_page_minimal() -> None:
    fields = _read(_fixture("lever.html"))

    assert fields.company == "Epoch AI"
    assert fields.location == "Remote"
    assert fields.country is None
    assert fields.remote is None
    assert fields.salaries == ()
    assert fields.identifier is None
    assert fields.url is None


def test_country_only_address() -> None:
    posting = _posting(jobLocation=_location({"addressCountry": "Canada"}))

    fields = _read(_page(posting))

    assert fields.location == "Canada"
    assert fields.country == "CA"


def test_remote_address_country_names_no_country() -> None:
    posting = _posting(jobLocation=_location({"addressCountry": "Remote"}))

    fields = _read(_page(posting))

    assert fields.location == "Remote"
    assert fields.country is None


def test_country_given_as_object() -> None:
    address = {"addressCountry": {"@type": "Country", "name": "Japan"}}

    fields = _read(_page(_posting(jobLocation=_location(address))))

    assert fields.country == "JP"


def test_string_address_and_naive_date() -> None:
    posting = _posting(
        jobLocation=_location("Helsinki, Finland"),
        datePosted="2026-10-30T18:37:12.691765",
    )

    fields = _read(_page(posting))

    assert fields.location == "Helsinki, Finland"
    assert fields.country == "FI"
    assert fields.posted_at is None


def test_locality_beats_country_for_location() -> None:
    address = {"addressLocality": "Tokyo", "addressCountry": "JP"}

    fields = _read(_page(_posting(jobLocation=_location(address))))

    assert fields.location == "Tokyo"
    assert fields.country == "JP"


def test_first_country_that_parses_wins() -> None:
    places = [
        _location({"addressCountry": "Nowhere"}),
        _location({"addressCountry": "FR"}),
        _location({"addressCountry": "DE"}),
    ]

    fields = _read(_page(_posting(jobLocation=places)))

    assert fields.country == "FR"


def test_no_location_leaves_fields_empty() -> None:
    fields = _read(_page(_posting()))

    assert fields.location is None
    assert fields.country is None


@pytest.mark.parametrize(
    ("posting", "expected"),
    [
        pytest.param(
            {"jobLocationType": "TELECOMMUTE"}, None, id="telecommute-no-countries"
        ),
        pytest.param(
            {
                "jobLocationType": "TELECOMMUTE",
                "applicantLocationRequirements": [
                    {"@type": "Country", "name": "US"},
                    {"@type": "Country", "name": "CA"},
                ],
            },
            None,
            id="telecommute-many-countries",
        ),
        pytest.param(
            {
                "jobLocationType": "TELECOMMUTE",
                "applicantLocationRequirements": [{"@type": "Country", "name": "US"}],
            },
            RemoteScope.COUNTRY,
            id="telecommute-one-country-in-list",
        ),
        pytest.param(
            {
                "jobLocationType": "TELECOMMUTE",
                "applicantLocationRequirements": {
                    "@type": "AdministrativeArea",
                    "name": "Europe",
                },
            },
            None,
            id="telecommute-region",
        ),
        pytest.param(
            {"applicantLocationRequirements": {"@type": "Country", "name": "US"}},
            None,
            id="not-telecommute",
        ),
    ],
)
def test_remote_scope(posting: dict[str, Any], expected: RemoteScope | None) -> None:
    assert _read(_page(_posting(**posting))).remote is expected


@pytest.mark.parametrize(
    ("unit", "period"),
    [
        ("YEAR", Period.YEAR),
        ("MONTH", Period.MONTH),
        ("HOUR", Period.HOUR),
        ("WEEK", None),
    ],
)
def test_salary_period(unit: str, period: Period | None) -> None:
    salary = {
        "currency": "USD",
        "value": {"unitText": unit, "minValue": 10, "maxValue": 20},
    }

    fields = _read(_page(_posting(baseSalary=salary)))

    assert fields.salaries == (Money(Decimal(10), Decimal(20), "USD", period),)


def test_salary_with_one_positive_bound() -> None:
    salary = {
        "currency": "JPY",
        "value": {"unitText": "YEAR", "minValue": 8000000, "maxValue": 0},
    }

    fields = _read(_page(_posting(baseSalary=salary)))

    assert fields.salaries == (Money(Decimal(8000000), None, "JPY", Period.YEAR),)


@pytest.mark.parametrize(
    "salary",
    [
        pytest.param({"value": {"minValue": 10}}, id="no-currency"),
        pytest.param({"currency": "USD", "value": {}}, id="no-bounds"),
        pytest.param(
            {"currency": "USD", "value": {"minValue": 0, "maxValue": 0}}, id="zero"
        ),
        pytest.param({"currency": "USD"}, id="no-value"),
        pytest.param("competitive", id="not-an-object"),
    ],
)
def test_salary_absent_when_not_stated(salary: object) -> None:
    assert _read(_page(_posting(baseSalary=salary))).salaries == ()


def test_inverted_salary_range_is_invalid() -> None:
    salary = {"currency": "USD", "value": {"minValue": 20, "maxValue": 10}}

    with pytest.raises(InvalidValueError):
        _read(_page(_posting(baseSalary=salary)))


@pytest.mark.parametrize(
    "bound",
    [
        pytest.param(float("nan"), id="json-nan"),
        pytest.param(float("inf"), id="json-infinity"),
        pytest.param(float("-inf"), id="json-negative-infinity"),
        pytest.param("NaN", id="text-nan"),
        pytest.param("sNaN", id="text-snan"),
        pytest.param("Infinity", id="text-infinity"),
        pytest.param("-Infinity", id="text-negative-infinity"),
    ],
)
@pytest.mark.parametrize("name", ["minValue", "maxValue"])
def test_non_finite_salary_bound_is_invalid(bound: object, name: str) -> None:
    salary = {"currency": "USD", "value": {name: bound}}

    with pytest.raises(InvalidValueError):
        _read(_page(_posting(baseSalary=salary)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("2026-10-07", datetime(2026, 10, 7, tzinfo=UTC), id="date"),
        pytest.param(
            "2026-10-07T08:30:00Z",
            datetime(2026, 10, 7, 8, 30, tzinfo=UTC),
            id="zulu",
        ),
        pytest.param("2026-10-07T08:30:00", None, id="naive"),
    ],
)
def test_posted_at_shapes(value: str, expected: datetime | None) -> None:
    assert _read(_page(_posting(datePosted=value))).posted_at == expected


def test_unreadable_posted_at_is_invalid() -> None:
    with pytest.raises(InvalidValueError):
        _read(_page(_posting(datePosted="last week")))


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        pytest.param({"@type": "PropertyValue", "value": 856062}, "856062", id="int"),
        pytest.param({"value": "abc"}, "abc", id="text"),
        pytest.param("job-7", "job-7", id="scalar"),
        pytest.param(42, "42", id="scalar-int"),
        pytest.param({"name": "no value"}, None, id="no-value"),
        pytest.param(True, None, id="bool"),
    ],
)
def test_identifier(identifier: object, expected: str | None) -> None:
    assert _read(_page(_posting(identifier=identifier))).identifier == expected


def test_non_text_title_is_invalid() -> None:
    with pytest.raises(InvalidValueError):
        _read(_page(_posting(title=7)))


def test_non_object_organization_is_invalid() -> None:
    with pytest.raises(InvalidValueError):
        _read(_page(_posting(hiringOrganization="Acme")))


def test_lone_surrogate_text_is_invalid() -> None:
    page = (
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "\\ud800"}</script>'
    )

    with pytest.raises(InvalidValueError):
        _read(page)


def test_invalid_json_block_skipped() -> None:
    good = json.dumps(_posting(title="Kept"))
    page = (
        '<script type="application/ld+json">{not json</script>'
        f'<script type="application/ld+json">{good}</script>'
    )

    assert [p["title"] for p in job_postings(page)] == ["Kept"]


def test_other_scripts_and_types_ignored() -> None:
    page = '<script type="text/javascript">{"@type": "JobPosting"}</script>' + _page(
        {"@type": "Organization"}, _posting(title="Kept")
    )

    assert [p["title"] for p in job_postings(page)] == ["Kept"]


def test_lists_and_graph_are_walked() -> None:
    graph = {"@graph": [{"@type": "Organization"}, _posting(title="In graph")]}
    page = _page([_posting(title="In list")], graph)

    titles = [p["title"] for p in job_postings(page)]

    assert titles == ["In list", "In graph"]


def test_type_may_be_a_list() -> None:
    page = _page({"@type": ["Thing", "JobPosting"], "title": "Multi"})

    assert [p["title"] for p in job_postings(page)] == ["Multi"]


def test_script_type_is_case_insensitive() -> None:
    body = json.dumps(_posting(title="Kept"))
    page = f'<SCRIPT TYPE="Application/LD+JSON">{body}</SCRIPT>'

    assert [p["title"] for p in job_postings(page)] == ["Kept"]


def test_page_without_blocks_has_no_postings() -> None:
    assert job_postings("<html><body>nothing</body></html>") == []


def test_deeply_nested_block_is_skipped() -> None:
    page = f'<script type="application/ld+json">{"[" * 100_000}</script>' + _page(
        _posting(title="Kept")
    )

    assert [p["title"] for p in job_postings(page)] == ["Kept"]


def test_ld_json_blocks_returns_parsed_blocks_in_page_order() -> None:
    page = (
        '<script type="application/ld+json">{not json</script>'
        '<script type="text/javascript">{"a": 1}</script>'
        + _page({"@type": "ItemList"}, [1, 2])
    )

    assert ld_json_blocks(page) == [{"@type": "ItemList"}, [1, 2]]


def test_ld_json_blocks_matches_script_type_case_insensitively() -> None:
    page = '<SCRIPT TYPE=" Application/LD+JSON ">{"a": 1}</SCRIPT>'

    assert ld_json_blocks(page) == [{"a": 1}]


def test_ld_json_blocks_skips_a_deeply_nested_block() -> None:
    page = f'<script type="application/ld+json">{"[" * 100_000}</script>' + _page(
        {"a": 1}
    )

    assert ld_json_blocks(page) == [{"a": 1}]


def test_ld_json_blocks_of_a_page_without_scripts_is_empty() -> None:
    assert ld_json_blocks("<html><body>nothing</body></html>") == []


def test_job_postings_does_not_enter_item_list_elements() -> None:
    page = _page(
        {"@type": "ItemList", "itemListElement": [{"item": _posting(title="Listed")}]}
    )

    assert job_postings(page) == []
