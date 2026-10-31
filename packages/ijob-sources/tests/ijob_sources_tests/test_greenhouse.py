import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_core.countries import CountryCode
from ijob_core.model import ExternalId, SourceKind, SourceRef, parse_source_key
from ijob_core.money import Money
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import IdentityMismatchError, ParseError, SourceNotFoundError
from ijob_sources.greenhouse import GreenhouseBoard, parse_jobs
from ijob_sources.polite_http import CachedResponse, ConditionalCache, PoliteClient

_FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse" / "tailscale_jobs.json"
_BODY = _FIXTURE.read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_BODY)["jobs"]
_FIRST, _SECOND = _JOBS
_PAY_BODY = (
    Path(__file__).parent / "fixtures" / "greenhouse" / "tailscale_jobs_pay.json"
).read_bytes()
_PAY_FIRST, _PAY_SECOND = json.loads(_PAY_BODY)["jobs"]
_KEY = parse_source_key("tailscale")
_PAY_URL = (
    "https://boards-api.greenhouse.io/v1/boards/tailscale/jobs"
    "?content=true&pay_transparency=true"
)
_LABEL = f"id {_SECOND['id']}"


def _body(*jobs: object) -> bytes:
    return json.dumps({"jobs": list(jobs)}).encode()


def _without(job: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: value for name, value in job.items() if name not in names}


def test_parse_fixture_maps_first_job() -> None:
    draft = parse_jobs(_BODY, _KEY).drafts[0]

    assert draft.source == SourceRef(SourceKind.GREENHOUSE, _KEY)
    assert draft.external_id == ExternalId("4721252005")
    assert draft.title == "Analytics Engineer, Data"
    assert draft.company == "Tailscale"
    assert draft.location == "Remote (Canada)"
    assert draft.url == "https://job-boards.greenhouse.io/tailscale/jobs/4721252005"
    assert draft.posted_at == datetime(
        2026, 8, 4, 14, 21, 41, tzinfo=timezone(timedelta(hours=-4))
    )
    assert json.loads(draft.raw) == _FIRST


def test_parse_fixture_returns_two_drafts_none_rejected() -> None:
    batch = parse_jobs(_BODY, _KEY)

    assert len(batch.drafts) == 2
    assert batch.rejected == 0


@pytest.mark.parametrize("company_name", [None, ""])
def test_company_falls_back_to_board_key(company_name: str | None) -> None:
    job = {**_FIRST, "company_name": company_name}
    if company_name is None:
        job = _without(_FIRST, "company_name")

    assert parse_jobs(_body(job), _KEY).drafts[0].company == "tailscale"


def test_absent_location_and_first_published_are_none() -> None:
    job = _without(_FIRST, "location", "first_published")

    draft = parse_jobs(_body(job), _KEY).drafts[0]

    assert draft.location is None
    assert draft.posted_at is None


@pytest.mark.parametrize(
    ("bad_job", "label"),
    [
        pytest.param({**_SECOND, "title": ""}, _LABEL, id="empty-title"),
        pytest.param(
            {**_SECOND, "absolute_url": "ftp://example.test/1"},
            _LABEL,
            id="non-http-url",
        ),
        pytest.param(_without(_SECOND, "id"), "index 1", id="missing-id"),
        pytest.param(_without(_SECOND, "title"), _LABEL, id="no-title"),
        pytest.param(_without(_SECOND, "absolute_url"), _LABEL, id="no-url"),
        pytest.param("not a job", "index 1", id="string-job"),
        pytest.param({**_SECOND, "title": 5}, _LABEL, id="non-text-title"),
        pytest.param(
            {**_SECOND, "absolute_url": ["x"]},
            _LABEL,
            id="non-text-url",
        ),
        pytest.param({**_SECOND, "id": ["x"]}, "index 1", id="list-id"),
        pytest.param({**_SECOND, "id": True}, "index 1", id="bool-id"),
        pytest.param({**_SECOND, "company_name": 3}, _LABEL, id="company"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    bad_job: object, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ijob_sources.greenhouse"):
        batch = parse_jobs(_body(_FIRST, bad_job), _KEY)

    assert [draft.external_id for draft in batch.drafts] == ["4721252005"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()
    assert "invalid" in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"location": "Remote"}, "location", id="string-location"),
        pytest.param({"location": {"name": 7}}, "location", id="non-text-location"),
        pytest.param(
            {"location": {"name": "\ud800"}}, "location", id="surrogate-location"
        ),
        pytest.param(
            {"first_published": "yesterday"}, "posted_at", id="unreadable-date"
        ),
        pytest.param({"first_published": 20260804}, "posted_at", id="non-text-date"),
        pytest.param(
            {"first_published": "2026-08-04T14:21:41"}, "posted_at", id="naive-date"
        ),
        pytest.param(
            {"departments": [{"name": "\ud800"}]}, "role_hint", id="surrogate-hint"
        ),
        pytest.param({"departments": [{"name": 7}]}, "role_hint", id="non-text-hint"),
    ],
)
def test_unreadable_optional_field_is_dropped_and_job_kept(
    overrides: dict[str, Any], field: str
) -> None:
    batch = parse_jobs(_body({**_PAY_FIRST, **overrides}), _KEY)

    assert batch.rejected == 0
    assert getattr(batch.drafts[0], field) is None


def test_null_fields_are_treated_as_absent() -> None:
    job = {**_FIRST, "company_name": None, "location": None, "first_published": None}
    draft = parse_jobs(_body(job), _KEY).drafts[0]

    assert draft.company == "tailscale"
    assert draft.location is None
    assert draft.posted_at is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "\ud800"},
        {"title": "\ud800"},
        {"company_name": "\ud800"},
        {"absolute_url": "https://x.test/\ud800"},
    ],
)
def test_text_that_is_not_utf8_encodable_is_rejected(overrides: dict[str, Any]) -> None:
    batch = parse_jobs(_body({**_FIRST, **overrides}, _SECOND), _KEY)

    assert [draft.external_id for draft in batch.drafts] == [str(_SECOND["id"])]
    assert batch.rejected == 1


def test_job_that_cannot_be_re_encoded_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(_FIRST)

    def overflow(_: object) -> str:
        raise RecursionError

    monkeypatch.setattr(json, "dumps", overflow)
    batch = parse_jobs(body, _KEY)

    assert batch.drafts == []
    assert batch.rejected == 1


def test_job_id_is_logged_without_control_characters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = {**_FIRST, "id": "a\nFAKE LOG", "title": ""}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.greenhouse"):
        parse_jobs(_body(job), _KEY)

    assert "\n" not in caplog.records[0].getMessage()


def test_deeply_nested_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_jobs(b"[" * 100_000, _KEY)
    assert isinstance(caught.value.__cause__, RecursionError)


def test_non_json_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_jobs(b"<html>not json</html>", _KEY)

    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "body",
    [b'{"meta": {"total": 0}}', b'{"jobs": {"a": 1}}', b"[]", b"null"],
    ids=["no-jobs", "jobs-not-list", "top-level-list", "null"],
)
def test_missing_jobs_list_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_jobs(body, _KEY)


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
    board: GreenhouseBoard,
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
            return httpx.Response(200, text="User-agent: *\nDisallow: /embed/\n")
        if requests is not None:
            requests.append(request)
        return board_response

    return handler


def test_fetch_requests_board_url_with_content() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, content=_BODY), requests)

    batch = _fetch(GreenhouseBoard(_KEY), handler)

    assert [str(request.url) for request in requests] == [_PAY_URL]
    assert batch == parse_jobs(_BODY, _KEY)


def test_fetch_unknown_board_raises_source_not_found() -> None:
    not_found = httpx.Response(404, text='{"status":404,"error":"Job not found"}')

    with pytest.raises(SourceNotFoundError):
        _fetch(
            GreenhouseBoard(parse_source_key("zz-no-such-board")), _serving(not_found)
        )


def test_greenhouse_board_satisfies_source_protocol() -> None:
    board = GreenhouseBoard(_KEY)
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

    assert board.ref == SourceRef(SourceKind.GREENHOUSE, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 2


def test_pay_ranges_become_money() -> None:
    draft = parse_jobs(_PAY_BODY, _KEY).drafts[0]

    assert draft.salaries == (Money(Decimal(203680), Decimal(254600), "CAD", None),)


def test_each_pay_range_becomes_one_money() -> None:
    ranges = [
        {"min_cents": 10000, "max_cents": 20000, "currency_type": "USD"},
        {"min_cents": 30000, "max_cents": 40000, "currency_type": "GBP"},
    ]
    job = {**_PAY_FIRST, "pay_input_ranges": ranges}

    salaries = parse_jobs(_body(job), _KEY).drafts[0].salaries

    assert salaries == (
        Money(Decimal(100), Decimal(200), "USD", None),
        Money(Decimal(300), Decimal(400), "GBP", None),
    )


def test_pay_range_with_one_positive_bound_keeps_that_bound() -> None:
    ranges = [{"min_cents": 0, "max_cents": 20000, "currency_type": "USD"}]
    job = {**_PAY_FIRST, "pay_input_ranges": ranges}

    salaries = parse_jobs(_body(job), _KEY).drafts[0].salaries

    assert salaries == (Money(None, Decimal(200), "USD", None),)


@pytest.mark.parametrize(
    "pay_range",
    [
        pytest.param({"currency_type": "USD"}, id="no-bounds"),
        pytest.param(
            {"min_cents": 0, "max_cents": 0, "currency_type": "USD"}, id="zero-bounds"
        ),
        pytest.param({"min_cents": 100, "max_cents": 200}, id="no-currency"),
        pytest.param(
            {"min_cents": 100, "max_cents": 200, "currency_type": "usd"},
            id="lower-case-currency",
        ),
        pytest.param(
            {"min_cents": 300, "max_cents": 200, "currency_type": "USD"},
            id="min-above-max",
        ),
        pytest.param(
            {"min_cents": "100", "max_cents": None, "currency_type": "USD"},
            id="non-integer-bound",
        ),
        pytest.param("not a range", id="not-an-object"),
    ],
)
def test_unusable_pay_range_is_skipped_and_job_kept(pay_range: object) -> None:
    job = {**_PAY_FIRST, "pay_input_ranges": [pay_range]}

    batch = parse_jobs(_body(job), _KEY)

    assert batch.rejected == 0
    assert batch.drafts[0].salaries == ()


def test_job_without_pay_has_no_salary() -> None:
    job = _without(_PAY_FIRST, "pay_input_ranges")

    batch = parse_jobs(_body(job), _KEY)

    assert batch.drafts[0].salaries == ()
    assert batch.rejected == 0


def test_pay_ranges_that_are_not_a_list_are_ignored() -> None:
    job = {**_PAY_FIRST, "pay_input_ranges": {"min_cents": 1}}

    assert parse_jobs(_body(job), _KEY).drafts[0].salaries == ()


def test_country_from_first_parsable_office() -> None:
    assert parse_jobs(_PAY_BODY, _KEY).drafts[0].country == CountryCode("CA")


def test_country_skips_offices_that_name_no_country() -> None:
    offices = [
        {"location": "Remote"},
        {"location": None},
        "not an office",
        {"location": "Germany"},
        {"location": "Canada"},
    ]
    job = {**_PAY_FIRST, "offices": offices}

    assert parse_jobs(_body(job), _KEY).drafts[0].country == CountryCode("DE")


@pytest.mark.parametrize("offices", [None, [], "Canada"], ids=["null", "empty", "text"])
def test_country_is_none_without_usable_offices(offices: object) -> None:
    job = {**_PAY_FIRST, "offices": offices}

    assert parse_jobs(_body(job), _KEY).drafts[0].country is None


def test_job_without_offices_has_no_country() -> None:
    job = _without(_PAY_FIRST, "offices")

    assert parse_jobs(_body(job), _KEY).drafts[0].country is None


def test_role_hint_from_department() -> None:
    assert parse_jobs(_PAY_BODY, _KEY).drafts[0].role_hint == "Data"


def test_role_hint_takes_first_named_department() -> None:
    departments = [{"name": ""}, {"id": 1}, {"name": "Platform"}, {"name": "Data"}]
    job = {**_PAY_FIRST, "departments": departments}

    assert parse_jobs(_body(job), _KEY).drafts[0].role_hint == "Platform"


@pytest.mark.parametrize(
    "departments", [None, [], "Data"], ids=["null", "empty", "text"]
)
def test_role_hint_is_none_without_usable_departments(departments: object) -> None:
    job = {**_PAY_FIRST, "departments": departments}

    assert parse_jobs(_body(job), _KEY).drafts[0].role_hint is None


def test_fetch_requests_pay_url_conditionally() -> None:
    requests: list[httpx.Request] = []
    cache = _DictCache({_PAY_URL: CachedResponse('"v1"', None, _PAY_BODY)})
    handler = _serving(httpx.Response(304), requests)

    batch = _fetch(GreenhouseBoard(_KEY), handler, cache)

    assert [str(request.url) for request in requests] == [_PAY_URL]
    assert _PAY_URL.endswith("content=true&pay_transparency=true")
    assert requests[0].headers["if-none-match"] == '"v1"'
    assert batch == parse_jobs(_PAY_BODY, _KEY)


def test_identity_checked_when_company_given() -> None:
    handler = _serving(httpx.Response(200, content=_PAY_BODY))

    batch = _fetch(GreenhouseBoard(_KEY, company="Tailscale"), handler)

    assert len(batch.drafts) == 2
    with pytest.raises(IdentityMismatchError) as caught:
        _fetch(GreenhouseBoard(_KEY, company="Ramp"), handler)
    assert caught.value.ref == SourceRef(SourceKind.GREENHOUSE, _KEY)
    assert "Ramp" in caught.value.reason


def test_identity_falls_back_to_content_when_company_name_is_absent() -> None:
    jobs = [_without(job, "company_name") for job in (_PAY_FIRST, _PAY_SECOND)]
    handler = _serving(httpx.Response(200, content=_body(*jobs)))

    batch = _fetch(GreenhouseBoard(_KEY, company="Tailscale"), handler)

    assert len(batch.drafts) == 2


def test_identity_is_not_checked_without_a_company() -> None:
    handler = _serving(httpx.Response(200, content=_PAY_BODY))

    batch = _fetch(GreenhouseBoard(_KEY), handler)

    assert len(batch.drafts) == 2


def test_identity_reads_names_from_a_job_rejected_for_another_reason() -> None:
    rejected = {**_PAY_FIRST, "title": ""}
    handler = _serving(httpx.Response(200, content=_body(rejected)))

    batch = _fetch(GreenhouseBoard(_KEY, company="Tailscale"), handler)

    assert (batch.drafts, batch.rejected) == ([], 1)


def test_identity_fails_when_every_listed_job_is_rejected_and_none_names_it() -> None:
    rejected = {**_PAY_FIRST, "title": ""}
    handler = _serving(httpx.Response(200, content=_body(rejected)))

    with pytest.raises(IdentityMismatchError):
        _fetch(GreenhouseBoard(_KEY, company="Ramp"), handler)


def test_identity_rejects_a_board_name_that_merely_contains_the_company() -> None:
    handler = _serving(httpx.Response(200, content=_PAY_BODY))

    with pytest.raises(IdentityMismatchError):
        _fetch(GreenhouseBoard(_KEY, company="Scale"), handler)
