import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any

import httpx
import pytest
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.money import Money, Period
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError
from ijob_sources.jobicy import JobicyBoard, parse_jobicy
from ijob_sources.polite_http import CachedResponse, PoliteClient

_KEY = parse_source_key("engineering")
_FIRST_URL = "https://jobicy.com/api/v2/remote-jobs?count=50&industry=engineering"


def _fixture(name: str) -> bytes:
    resource = files("ijob_sources_tests.fixtures.jobicy") / name
    return resource.read_bytes()


_PAGE1 = _fixture("page1.json")
_PAGE2 = _fixture("page2.json")
_JOBS1: list[dict[str, Any]] = json.loads(_PAGE1)["jobs"]
_JOBS2: list[dict[str, Any]] = json.loads(_PAGE2)["jobs"]
_TEAMSNAP, _TWO_COUNTRIES, _HOURLY = _JOBS1
_CURSOR = json.loads(_PAGE1)["nextCursor"]


def _body(*jobs: object, cursor: str | None = None, has_more: bool = False) -> bytes:
    return json.dumps(
        {"jobs": list(jobs), "nextCursor": cursor, "hasMore": has_more}
    ).encode()


def _only(job: dict[str, Any]) -> SightingDraft:
    return parse_jobicy(_body(job), _KEY).batch.drafts[0]


def test_parse_maps_fields() -> None:
    page = parse_jobicy(_PAGE1, _KEY)
    draft = page.batch.drafts[0]

    assert draft.source == SourceRef(SourceKind.JOBICY, _KEY)
    assert draft.external_id == ExternalId("158129")
    assert draft.title == "Senior Fullstack Engineer (BE-leaning)"
    assert draft.company == "TeamSnap"
    assert draft.location == "USA"
    assert draft.url == (
        "https://jobicy.com/jobs/158129-senior-fullstack-engineer-be-leaning"
    )
    assert draft.posted_at == datetime(2026, 10, 30, 18, 7, 40, tzinfo=UTC)
    assert draft.country == "US"
    assert draft.remote is None
    assert draft.salaries == (Money(Decimal(150000), None, "USD", Period.YEAR),)
    assert json.loads(draft.raw) == _TEAMSNAP
    assert len(page.batch.drafts) == 3
    assert page.batch.rejected == 0
    assert page.next_cursor == _CURSOR


def test_multi_country_geo_takes_last_part() -> None:
    draft = _only(_TWO_COUNTRIES)

    assert draft.location == "Canada,  USA"
    assert draft.country == "US"
    assert draft.remote is None


def test_geo_that_names_no_country_gives_no_country() -> None:
    draft = _only({**_TEAMSNAP, "jobGeo": "Anywhere"})

    assert draft.country is None


def test_absent_geo_gives_no_location() -> None:
    job = {k: v for k, v in _TEAMSNAP.items() if k != "jobGeo"}

    draft = _only(job)

    assert draft.location is None
    assert draft.country is None


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        pytest.param(
            _TWO_COUNTRIES,
            Money(Decimal(175000), Decimal(220000), "USD", Period.YEAR),
            id="yearly",
        ),
        pytest.param(
            _HOURLY,
            Money(Decimal(40), Decimal(50), "USD", Period.HOUR),
            id="hourly",
        ),
        pytest.param(
            {**_TWO_COUNTRIES, "salaryPeriod": "weekly"},
            Money(Decimal(175000), Decimal(220000), "USD", None),
            id="unmapped-period",
        ),
    ],
)
def test_salary_periods(job: dict[str, Any], expected: Money) -> None:
    assert _only(job).salaries == (expected,)


def test_no_salary_fields_give_no_salary() -> None:
    names = ("salaryMin", "salaryMax", "salaryCurrency", "salaryPeriod")
    job = {k: v for k, v in _TEAMSNAP.items() if k not in names}

    assert _only(job).salaries == ()


def test_amount_without_currency_has_no_salary() -> None:
    assert _only({**_TEAMSNAP, "salaryCurrency": None}).salaries == ()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"id": None}, id="no-id"),
        pytest.param({"id": True}, id="bool-id"),
        pytest.param({"id": "158129"}, id="text-id"),
        pytest.param({"jobTitle": ""}, id="empty-title"),
        pytest.param({"companyName": None}, id="no-company"),
        pytest.param({"url": "ftp://x.test/1"}, id="non-http-url"),
        pytest.param({"jobGeo": 4}, id="non-text-geo"),
        pytest.param({"pubDate": "yesterday"}, id="unreadable-date"),
        pytest.param({"pubDate": "2026-10-30T18:07:40"}, id="naive-date"),
        pytest.param({"pubDate": 4}, id="non-text-date"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    overrides: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    bad = {**_TEAMSNAP, **overrides}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.jobicy"):
        batch = parse_jobicy(_body(_TWO_COUNTRIES, bad), _KEY).batch

    assert [draft.external_id for draft in batch.drafts] == ["158069"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "invalid" in warnings[0].getMessage()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"salaryMin": "a lot"}, id="non-number-bound"),
        pytest.param({"salaryMin": float("nan")}, id="non-finite-bound"),
        pytest.param({"salaryMin": 9, "salaryMax": 1}, id="inverted"),
        pytest.param({"salaryCurrency": "usd"}, id="bad-currency"),
        pytest.param({"salaryCurrency": ""}, id="empty-currency"),
        pytest.param({"salaryCurrency": 5}, id="non-text-currency"),
    ],
)
def test_unbuildable_salary_is_dropped_and_the_job_kept(
    overrides: dict[str, Any],
) -> None:
    page = parse_jobicy(_body({**_TEAMSNAP, **overrides}), _KEY)

    assert [draft.external_id for draft in page.batch.drafts] == ["158129"]
    assert page.batch.rejected == 0
    assert page.batch.drafts[0].salaries == ()


def test_unhashable_period_maps_to_none() -> None:
    job = {**_TWO_COUNTRIES, "salaryPeriod": ["yearly"]}

    assert _only(job).salaries == (
        Money(Decimal(175000), Decimal(220000), "USD", None),
    )


def test_job_that_is_not_an_object_is_rejected() -> None:
    batch = parse_jobicy(_body("a job", _TEAMSNAP), _KEY).batch

    assert len(batch.drafts) == 1
    assert batch.rejected == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html></html>", id="not-json"),
        pytest.param(b"[]", id="top-level-list"),
        pytest.param(b'{"jobs": {}}', id="jobs-not-list"),
        pytest.param(b'{"hasMore": false}', id="no-jobs"),
        pytest.param(b'{"jobs": [], "hasMore": "yes"}', id="has-more-not-bool"),
        pytest.param(b'{"jobs": [], "nextCursor": 7}', id="cursor-not-text"),
        pytest.param(b"[" * 100_000, id="too-deep"),
    ],
)
def test_unreadable_body_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_jobicy(body, _KEY)


@pytest.mark.parametrize(
    ("has_more", "cursor"),
    [(False, "c"), (True, None), (True, "")],
    ids=["has-more-false", "no-cursor", "empty-cursor"],
)
def test_no_next_page(has_more: bool, cursor: str | None) -> None:  # noqa: FBT001 - parametrised data
    page = parse_jobicy(_body(cursor=cursor, has_more=has_more), _KEY)

    assert page.next_cursor is None


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _DictCache:
    """A `ConditionalCache` backed by a dict."""

    def __init__(self, entries: dict[str, CachedResponse]) -> None:
        self.entries = dict(entries)

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


type _Handler = Callable[[httpx.Request], httpx.Response]


def _robots_then(handler: _Handler) -> _Handler:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return handler(request)

    return wrapped


def _client(handler: _Handler, cache: _DictCache | None = None) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(_robots_then(handler)),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        cache=cache,
    )


def _fetch(
    handler: _Handler, cache: _DictCache | None = None
) -> tuple[SourceBatch, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async def run() -> SourceBatch:
        async with _client(recording, cache) as client:
            return await JobicyBoard(_KEY).fetch(client)

    return asyncio.run(run()), [r for r in requests if r.url.path != "/robots.txt"]


def _two_pages(request: httpx.Request) -> httpx.Response:
    if "cursor" in request.url.params:
        return httpx.Response(200, content=_PAGE2)
    return httpx.Response(200, content=_PAGE1)


def test_paging_until_has_more_false() -> None:
    batch, requests = _fetch(_two_pages)

    assert len(requests) == 2
    assert str(requests[0].url) == _FIRST_URL
    assert requests[1].url.params["cursor"] == _CURSOR
    assert requests[1].url.params["industry"] == "engineering"
    assert requests[1].url.params["count"] == "50"
    assert [draft.external_id for draft in batch.drafts] == [
        str(job["id"]) for job in [*_JOBS1, *_JOBS2]
    ]
    assert batch.rejected == 0


def test_paging_capped_at_five() -> None:
    def always_more(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_body(_TEAMSNAP, cursor="more", has_more=True)
        )

    batch, requests = _fetch(always_more)

    assert len(requests) == 5
    assert len(batch.drafts) == 5


def test_rejections_are_summed_across_pages() -> None:
    def pages(request: httpx.Request) -> httpx.Response:
        if "cursor" in request.url.params:
            return httpx.Response(200, content=_body({"jobTitle": ""}))
        return httpx.Response(
            200, content=_body({"jobTitle": ""}, cursor="x", has_more=True)
        )

    batch, _ = _fetch(pages)

    assert batch.drafts == []
    assert batch.rejected == 2


def test_only_the_first_page_is_conditional() -> None:
    validator = "Sat, 31 Oct 2026 00:00:00 GMT"
    cache = _DictCache(
        {
            _FIRST_URL: CachedResponse(None, validator, _PAGE1),
            f"{_FIRST_URL}&cursor={_CURSOR}": CachedResponse(None, validator, _PAGE2),
        }
    )

    _, requests = _fetch(_two_pages, cache)

    assert requests[0].headers["If-Modified-Since"] == validator
    assert "If-Modified-Since" not in requests[1].headers


def test_jobicy_board_satisfies_source_protocol() -> None:
    board = JobicyBoard(_KEY)

    async def run() -> CollectResult:
        async with _client(_two_pages) as client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.JOBICY, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 5
