import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_core.countries import CountryCode
from ijob_core.model import (
    ExternalId,
    RemoteScope,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.money import Money, Period
from ijob_sources.ashby import AshbyBoard, parse_ashby
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import IdentityMismatchError, ParseError, SourceNotFoundError
from ijob_sources.polite_http import CachedResponse, ConditionalCache, PoliteClient

_FIXTURE = Path(__file__).parent / "fixtures" / "ashby" / "ramp_job_board.json"
_BODY = _FIXTURE.read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_BODY)["jobs"]
_FIRST, _MONTHLY, _THIRD = _JOBS
_KEY = parse_source_key("ramp")
_COMPANY = "Ramp"
_URL = "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
_LABEL = f"id {_MONTHLY['id']!r}"


def _body(*jobs: object) -> bytes:
    return json.dumps({"jobs": list(jobs)}).encode()


def _without(job: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: value for name, value in job.items() if name not in names}


def _parse(*jobs: object) -> SourceBatch:
    return parse_ashby(_body(*jobs), _KEY, _COMPANY)


def _with_components(*components: object) -> dict[str, Any]:
    return {**_FIRST, "compensation": {"summaryComponents": list(components)}}


def _salary(**overrides: object) -> dict[str, Any]:
    component: dict[str, Any] = {
        "compensationType": "Salary",
        "interval": "1 YEAR",
        "currencyCode": "USD",
        "minValue": 100,
        "maxValue": 200,
    }
    return {**component, **overrides}


def test_parse_ashby_maps_fields() -> None:
    draft = parse_ashby(_BODY, _KEY, _COMPANY).drafts[0]

    assert _FIRST["title"] == " Security Engineer, Cloud"
    assert draft.source == SourceRef(SourceKind.ASHBY, _KEY)
    assert draft.external_id == ExternalId("34413f8d-26bf-4bbc-8ade-eb309a0e2245")
    assert draft.title == "Security Engineer, Cloud"
    assert draft.company == "Ramp"
    assert draft.location == "New York, NY (HQ)"
    assert draft.url == (
        "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    )
    assert draft.country == CountryCode("US")
    assert draft.remote is RemoteScope.NONE
    assert draft.posted_at == datetime(2026, 4, 7, 17, 12, 35, 753000, tzinfo=UTC)
    assert draft.role_hint == "Engineering"
    assert draft.salaries == (
        Money(Decimal(211400), Decimal(290600), "USD", Period.YEAR),
    )
    assert json.loads(draft.raw) == _FIRST


def test_parse_fixture_returns_three_drafts_none_rejected() -> None:
    batch = parse_ashby(_BODY, _KEY, _COMPANY)

    assert len(batch.drafts) == 3
    assert batch.rejected == 0


def test_monthly_salary() -> None:
    assert _MONTHLY["title"] == "Software Engineering Intern, Android"
    draft = _parse(_MONTHLY).drafts[0]

    assert draft.salaries == (
        Money(Decimal(12500), Decimal(12500), "USD", Period.MONTH),
    )


def test_yearly_salary_next_to_equity() -> None:
    assert _parse(_THIRD).drafts[0].salaries == (
        Money(Decimal(143200), Decimal(284000), "USD", Period.YEAR),
    )


def test_unmapped_interval_has_no_period() -> None:
    job = _with_components(_salary(interval="NONE"))

    batch = _parse(job)

    assert batch.rejected == 0
    assert batch.drafts[0].salaries == (Money(Decimal(100), Decimal(200), "USD", None),)


def test_job_without_compensation_has_no_salary() -> None:
    unpaid = _without(_FIRST, "compensation")

    assert _parse(unpaid).drafts[0].salaries == ()


def test_non_salary_components_ignored() -> None:
    equity = {
        "compensationType": "EquityPercentage",
        "interval": "NONE",
        "currencyCode": None,
        "minValue": 0.1,
        "maxValue": 0.5,
    }
    bonus = _salary(compensationType="Bonus")

    assert _parse(_with_components(equity, bonus)).drafts[0].salaries == ()


def test_each_salary_component_becomes_one_money() -> None:
    job = _with_components(
        _salary(),
        _salary(interval="1 MONTH", currencyCode="GBP", minValue=5.5, maxValue=None),
    )

    assert _parse(job).drafts[0].salaries == (
        Money(Decimal(100), Decimal(200), "USD", Period.YEAR),
        Money(Decimal("5.5"), None, "GBP", Period.MONTH),
    )


@pytest.mark.parametrize(
    "component",
    [
        pytest.param(_salary(minValue=None, maxValue=None), id="no-bounds"),
        pytest.param(_salary(minValue=0, maxValue=0), id="zero-bounds"),
        pytest.param(_salary(currencyCode=None), id="no-currency"),
        pytest.param(_salary(currencyCode="usd"), id="lower-case-currency"),
        pytest.param(_salary(minValue=300, maxValue=200), id="min-above-max"),
        pytest.param(_salary(minValue="100", maxValue=True), id="non-numeric-bounds"),
        pytest.param(_salary(minValue=float("nan"), maxValue=None), id="nan"),
        pytest.param("not a component", id="not-an-object"),
    ],
)
def test_unusable_salary_component_is_skipped_and_job_kept(component: object) -> None:
    batch = _parse(_with_components(component))

    assert batch.rejected == 0
    assert batch.drafts[0].salaries == ()


@pytest.mark.parametrize(
    "compensation",
    [None, [], "text", {"summaryComponents": None}, {"summaryComponents": "x"}],
    ids=["null", "list", "text", "null-components", "text-components"],
)
def test_unusable_compensation_gives_no_salary(compensation: object) -> None:
    job = {**_FIRST, "compensation": compensation}

    batch = _parse(job)

    assert batch.rejected == 0
    assert batch.drafts[0].salaries == ()


@pytest.mark.parametrize(
    ("workplace_type", "remote"),
    [("OnSite", RemoteScope.NONE), ("Hybrid", RemoteScope.NONE), ("Remote", None)],
)
def test_workplace_type_sets_remote_scope(
    workplace_type: str, remote: RemoteScope | None
) -> None:
    job = {**_FIRST, "workplaceType": workplace_type}

    assert _parse(job).drafts[0].remote is remote


def test_remote_workplace_is_unknown_scope() -> None:
    job = {**_FIRST, "workplaceType": "Remote", "isRemote": True}

    assert _parse(job).drafts[0].remote is None


@pytest.mark.parametrize("workplace_type", [None, "", "Elsewhere", 3, ["Hybrid"], {}])
def test_other_workplace_type_is_unknown_scope(workplace_type: object) -> None:
    job = {**_FIRST, "workplaceType": workplace_type, "isRemote": False}

    assert _parse(job).drafts[0].remote is None


def test_absent_workplace_type_is_unknown_scope() -> None:
    assert _parse(_without(_FIRST, "workplaceType")).drafts[0].remote is None


def test_absent_optional_fields_are_none() -> None:
    job = _without(_FIRST, "location", "address", "publishedAt", "department")

    draft = _parse(job).drafts[0]

    assert draft.location is None
    assert draft.country is None
    assert draft.posted_at is None
    assert draft.role_hint is None


def test_null_optional_fields_are_treated_as_absent() -> None:
    job = {
        **_FIRST,
        "location": None,
        "address": None,
        "publishedAt": None,
        "department": None,
    }

    draft = _parse(job).drafts[0]

    assert (draft.location, draft.country) == (None, None)
    assert (draft.posted_at, draft.role_hint) == (None, None)


@pytest.mark.parametrize(
    "address",
    [
        {"postalAddress": None},
        {"postalAddress": {}},
        {"postalAddress": {"addressCountry": None}},
        {"postalAddress": {"addressCountry": 7}},
        {"postalAddress": {"addressCountry": "Atlantis"}},
        {"postalAddress": "USA"},
        "USA",
    ],
)
def test_address_without_a_country_has_none(address: object) -> None:
    job = {**_FIRST, "address": address}

    batch = _parse(job)

    assert batch.rejected == 0
    assert batch.drafts[0].country is None


def test_empty_department_is_no_hint() -> None:
    assert _parse({**_FIRST, "department": ""}).drafts[0].role_hint is None


def test_empty_job_list_is_empty_batch() -> None:
    assert parse_ashby(b'{"jobs": []}', _KEY, _COMPANY) == SourceBatch(
        drafts=[], rejected=0
    )


@pytest.mark.parametrize(
    "body",
    [b"[]", b"null", b'{"jobs": {"a": 1}}', b'{"apiVersion": "1"}', b"7"],
    ids=["array", "null", "jobs-not-list", "no-jobs", "number"],
)
def test_missing_jobs_list_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_ashby(body, _KEY, _COMPANY)


def test_non_json_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_ashby(b"Not Found", _KEY, _COMPANY)

    assert isinstance(caught.value.__cause__, ValueError)


def test_deeply_nested_body_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_ashby(b"[" * 100_000, _KEY, _COMPANY)


@pytest.mark.parametrize(
    ("bad_job", "label"),
    [
        pytest.param({**_MONTHLY, "title": ""}, _LABEL, id="empty-title"),
        pytest.param({**_MONTHLY, "title": "   "}, _LABEL, id="blank-title"),
        pytest.param({**_MONTHLY, "title": 5}, _LABEL, id="non-text-title"),
        pytest.param({**_MONTHLY, "title": "\ud800"}, _LABEL, id="surrogate-title"),
        pytest.param(_without(_MONTHLY, "title"), _LABEL, id="no-title"),
        pytest.param(
            {**_MONTHLY, "jobUrl": "ftp://example.test/1"}, _LABEL, id="non-http-url"
        ),
        pytest.param(_without(_MONTHLY, "jobUrl"), _LABEL, id="no-url"),
        pytest.param({**_MONTHLY, "jobUrl": ["x"]}, _LABEL, id="non-text-url"),
        pytest.param(_without(_MONTHLY, "id"), "index 1", id="missing-id"),
        pytest.param({**_MONTHLY, "id": 7}, "index 1", id="integer-id"),
        pytest.param({**_MONTHLY, "id": ""}, "id ''", id="empty-id"),
        pytest.param({**_MONTHLY, "id": "\ud800"}, "id '\\ud800'", id="surrogate-id"),
        pytest.param("not a job", "index 1", id="string-job"),
    ],
)
def test_invalid_job_rejected_others_kept(
    bad_job: object, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ijob_sources.ashby"):
        batch = _parse(_FIRST, bad_job)

    assert [draft.external_id for draft in batch.drafts] == [_FIRST["id"]]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()
    assert "invalid" in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"location": ["NY"]}, "location", id="non-text-location"),
        pytest.param({"location": "\ud800"}, "location", id="surrogate-location"),
        pytest.param({"publishedAt": "yesterday"}, "posted_at", id="unreadable-date"),
        pytest.param({"publishedAt": 20260804}, "posted_at", id="non-text-date"),
        pytest.param(
            {"publishedAt": "2026-08-04T14:21:41"}, "posted_at", id="naive-date"
        ),
        pytest.param({"department": 3}, "role_hint", id="non-text-department"),
        pytest.param({"department": "\ud800"}, "role_hint", id="surrogate-department"),
    ],
)
def test_unreadable_optional_field_is_dropped_and_job_kept(
    overrides: dict[str, Any], field: str
) -> None:
    batch = _parse({**_MONTHLY, **overrides})

    assert batch.rejected == 0
    assert getattr(batch.drafts[0], field) is None


def test_job_id_is_logged_without_control_characters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = {**_FIRST, "id": "a\nFAKE LOG", "title": ""}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.ashby"):
        _parse(job)

    assert "\n" not in caplog.records[0].getMessage()


def test_job_that_cannot_be_re_encoded_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(_FIRST)

    def overflow(_: object) -> str:
        raise RecursionError

    monkeypatch.setattr(json, "dumps", overflow)
    batch = parse_ashby(body, _KEY, _COMPANY)

    assert batch.drafts == []
    assert batch.rejected == 1


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _DictCache:
    """A `ConditionalCache` that keeps responses in a dict."""

    def __init__(self, entries: dict[str, CachedResponse] | None = None) -> None:
        self.entries = entries or {}

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


def _fetch(
    board: AshbyBoard,
    handler: Callable[[httpx.Request], httpx.Response],
    cache: ConditionalCache | None = None,
) -> SourceBatch:
    fake_time = _FakeTime()

    async def run() -> SourceBatch:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
            cache=cache,
        )
        async with client:
            return await board.fetch(client)

    return asyncio.run(run())


def _serving(
    board_response: httpx.Response, requests: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(401, text="Unauthorized")
        if requests is not None:
            requests.append(request)
        return board_response

    return handler


def _serve_body(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return _serving(httpx.Response(200, content=body))


def test_fetch_requests_board_url_with_compensation_conditionally() -> None:
    requests: list[httpx.Request] = []
    cache = _DictCache({_URL: CachedResponse('"v1"', None, _BODY)})

    batch = _fetch(
        AshbyBoard(_KEY, company=_COMPANY),
        _serving(httpx.Response(304), requests),
        cache,
    )

    assert [str(request.url) for request in requests] == [_URL]
    assert requests[0].headers["if-none-match"] == '"v1"'
    assert batch == parse_ashby(_BODY, _KEY, _COMPANY)


def test_fetch_unknown_board_raises_source_not_found() -> None:
    not_found = httpx.Response(404, text="Not Found")
    board = AshbyBoard(parse_source_key("zz-none"), company=_COMPANY)

    with pytest.raises(SourceNotFoundError):
        _fetch(board, _serving(not_found))


def test_fetch_runs_identity() -> None:
    handler = _serving(httpx.Response(200, content=_BODY))

    batch = _fetch(AshbyBoard(_KEY, company="Ramp"), handler)

    assert len(batch.drafts) == 3
    with pytest.raises(IdentityMismatchError) as caught:
        _fetch(AshbyBoard(_KEY, company="Mercury"), handler)
    assert caught.value.ref == SourceRef(SourceKind.ASHBY, _KEY)
    assert "Mercury" in caught.value.reason


def test_identity_reads_the_text_of_a_job_rejected_for_another_reason() -> None:
    other = {**_FIRST, "title": "", "descriptionPlain": "About Mercury"}
    handler = _serve_body(_body(other))

    batch = _fetch(AshbyBoard(_KEY, company="Mercury"), handler)

    assert (batch.drafts, batch.rejected) == ([], 1)


def test_identity_fails_when_every_listed_job_is_rejected_and_none_names_it() -> None:
    other = {**_FIRST, "title": "", "descriptionPlain": "About Ramp"}

    with pytest.raises(IdentityMismatchError):
        _fetch(AshbyBoard(_KEY, company="Mercury"), _serve_body(_body(other)))


def test_ashby_board_satisfies_source_protocol() -> None:
    board = AshbyBoard(_KEY, company=_COMPANY)
    fake_time = _FakeTime()
    handler = _serving(httpx.Response(200, content=_BODY))

    async def run() -> CollectResult:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
        async with client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.ASHBY, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 3
