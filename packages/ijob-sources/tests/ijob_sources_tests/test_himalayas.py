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
    RemoteScope,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.money import Money, Period
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError
from ijob_sources.himalayas import HimalayasBoard, parse_himalayas
from ijob_sources.polite_http import CachedResponse, PoliteClient

_KEY = parse_source_key("all")
_FIRST_URL = "https://himalayas.app/jobs/api?limit=20"


def _fixture(name: str) -> bytes:
    resource = files("ijob_sources_tests.fixtures.himalayas") / name
    return resource.read_bytes()


_PAGE1 = _fixture("page1.json")
_PAGE2 = _fixture("page2.json")
_JOBS1: list[dict[str, Any]] = json.loads(_PAGE1)["jobs"]
_JOBS2: list[dict[str, Any]] = json.loads(_PAGE2)["jobs"]
_SRI_LANKA, _ANNUAL_US, _WORLDWIDE = _JOBS1
_HOURLY, _MONTHLY, _NO_AMOUNT = _JOBS2
_CURSOR = json.loads(_PAGE1)["nextCursor"]


def _body(*jobs: object, cursor: str | None = None) -> bytes:
    return json.dumps({"jobs": list(jobs), "nextCursor": cursor}).encode()


def _only(job: dict[str, Any]) -> SightingDraft:
    return parse_himalayas(_body(job), _KEY).batch.drafts[0]


def test_parse_maps_fields() -> None:
    page = parse_himalayas(_PAGE1, _KEY)
    draft = page.batch.drafts[0]

    assert draft.source == SourceRef(SourceKind.HIMALAYAS, _KEY)
    assert draft.external_id == ExternalId(_SRI_LANKA["guid"])
    assert draft.title == "AI Developer (Python/ Django)"
    assert draft.company == "Dijital Team Pty Ltd"
    assert draft.location == "Sri Lanka"
    assert draft.url == _SRI_LANKA["applicationLink"]
    assert draft.posted_at == datetime.fromtimestamp(1793413500, tz=UTC)
    assert draft.country == "LK"
    assert draft.remote is RemoteScope.COUNTRY
    assert json.loads(draft.raw) == _SRI_LANKA
    assert len(page.batch.drafts) == 3
    assert page.batch.rejected == 0
    assert page.next_cursor == _CURSOR


def test_worldwide_and_region() -> None:
    worldwide = _only(_WORLDWIDE)
    region = _only({**_SRI_LANKA, "locationRestrictions": ["Germany", "France"]})

    assert worldwide.remote is RemoteScope.WORLDWIDE
    assert worldwide.location is None
    assert worldwide.country is None
    assert region.remote is RemoteScope.REGION
    assert region.location == "Germany, France"
    assert region.country is None


def test_single_restriction_that_is_no_country_is_a_region() -> None:
    draft = _only({**_SRI_LANKA, "locationRestrictions": ["Europe"]})

    assert draft.remote is RemoteScope.REGION
    assert draft.country is None
    assert draft.location == "Europe"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        pytest.param(
            _ANNUAL_US,
            Money(Decimal(104550), Decimal(161950), "USD", Period.YEAR),
            id="annual",
        ),
        pytest.param(
            _MONTHLY,
            Money(Decimal(40000), Decimal(40000), "ZAR", Period.MONTH),
            id="monthly",
        ),
        pytest.param(
            _HOURLY,
            Money(Decimal(20), Decimal(20), "EUR", Period.HOUR),
            id="hourly",
        ),
    ],
)
def test_salary_periods(job: dict[str, Any], expected: Money) -> None:
    assert _only(job).salaries == (expected,)


def test_fractional_amount_is_exact() -> None:
    job = {**_ANNUAL_US, "maxSalary": 376843.95}

    assert _only(job).salaries[0].max == Decimal("376843.95")


def test_unknown_period_maps_to_none() -> None:
    job = {**_ANNUAL_US, "salaryPeriod": "weekly"}

    assert _only(job).salaries == (
        Money(Decimal(104550), Decimal(161950), "USD", None),
    )


def test_period_without_amount_has_no_salary() -> None:
    page = parse_himalayas(_body(_SRI_LANKA, _NO_AMOUNT), _KEY)

    assert [draft.salaries for draft in page.batch.drafts] == [(), ()]
    assert page.batch.rejected == 0


def test_amount_without_currency_has_no_salary() -> None:
    job = {**_ANNUAL_US, "currency": None}

    assert _only(job).salaries == ()


def test_single_positive_bound_is_kept() -> None:
    job = {**_ANNUAL_US, "minSalary": 0}

    assert _only(job).salaries == (Money(None, Decimal(161950), "USD", Period.YEAR),)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"guid": None}, id="no-guid"),
        pytest.param({"title": ""}, id="empty-title"),
        pytest.param({"companyName": 4}, id="non-text-company"),
        pytest.param({"applicationLink": "ftp://x.test/1"}, id="non-http-url"),
        pytest.param({"locationRestrictions": "Germany"}, id="restrictions-not-list"),
        pytest.param({"locationRestrictions": [3]}, id="restriction-not-text"),
        pytest.param({"pubDate": "yesterday"}, id="unreadable-date"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    overrides: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    bad = {**_ANNUAL_US, **overrides}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.himalayas"):
        batch = parse_himalayas(_body(_SRI_LANKA, bad), _KEY).batch

    assert len(batch.drafts) == 1
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "invalid" in warnings[0].getMessage()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"minSalary": "a lot"}, id="non-number-bound"),
        pytest.param({"minSalary": float("nan")}, id="non-finite-bound"),
        pytest.param({"minSalary": 9, "maxSalary": 1}, id="inverted"),
        pytest.param({"currency": "usd"}, id="bad-currency"),
        pytest.param({"currency": ""}, id="empty-currency"),
        pytest.param({"currency": 5}, id="non-text-currency"),
    ],
)
def test_unbuildable_salary_is_dropped_and_the_job_kept(
    overrides: dict[str, Any],
) -> None:
    page = parse_himalayas(_body({**_ANNUAL_US, **overrides}), _KEY)

    assert len(page.batch.drafts) == 1
    assert page.batch.rejected == 0
    assert page.batch.drafts[0].salaries == ()


def test_unhashable_period_maps_to_none() -> None:
    job = {**_ANNUAL_US, "salaryPeriod": ["annual"]}

    assert _only(job).salaries == (
        Money(Decimal(104550), Decimal(161950), "USD", None),
    )


def test_job_that_is_not_an_object_is_rejected() -> None:
    batch = parse_himalayas(_body("a job", _SRI_LANKA), _KEY).batch

    assert len(batch.drafts) == 1
    assert batch.rejected == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html></html>", id="not-json"),
        pytest.param(b"[]", id="top-level-list"),
        pytest.param(b'{"jobs": {}}', id="jobs-not-list"),
        pytest.param(b'{"nextCursor": null}', id="no-jobs"),
        pytest.param(b'{"jobs": [], "nextCursor": 7}', id="cursor-not-text"),
        pytest.param(b"[" * 100_000, id="too-deep"),
    ],
)
def test_unreadable_body_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_himalayas(body, _KEY)


@pytest.mark.parametrize("cursor", [None, ""])
def test_no_cursor_means_last_page(cursor: str | None) -> None:
    assert parse_himalayas(_body(cursor=cursor), _KEY).next_cursor is None


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


def _client(handler: _Handler, cache: _DictCache | None = None) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        cache=cache,
    )


def _fetch(
    handler: _Handler, cache: _DictCache | None = None
) -> tuple[SourceBatch, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        requests.append(request)
        return handler(request)

    async def run() -> SourceBatch:
        async with _client(recording, cache) as client:
            return await HimalayasBoard(_KEY).fetch(client)

    return asyncio.run(run()), requests


def _two_pages(request: httpx.Request) -> httpx.Response:
    if "cursor" in request.url.params:
        return httpx.Response(200, content=_PAGE2)
    return httpx.Response(200, content=_PAGE1)


def test_paging_follows_cursor_and_stops() -> None:
    batch, requests = _fetch(_two_pages)

    assert len(requests) == 2
    assert str(requests[0].url) == _FIRST_URL
    assert requests[1].url.params["cursor"] == _CURSOR
    assert requests[1].url.params["limit"] == "20"
    assert [draft.title for draft in batch.drafts] == [
        job["title"] for job in [*_JOBS1, *_JOBS2]
    ]
    assert batch.rejected == 0


def test_paging_capped_at_five() -> None:
    def always_more(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_body(_SRI_LANKA, cursor="more"))

    batch, requests = _fetch(always_more)

    assert len(requests) == 5
    assert len(batch.drafts) == 5


def test_cursor_is_percent_encoded() -> None:
    def odd_cursor(request: httpx.Request) -> httpx.Response:
        if "cursor" in request.url.params:
            return httpx.Response(200, content=_body())
        return httpx.Response(200, content=_body(cursor="a+b/c=&d"))

    _, requests = _fetch(odd_cursor)

    assert requests[1].url.params["cursor"] == "a+b/c=&d"
    assert "&d" not in requests[1].url.query.decode().split("cursor=")[1]


def test_rejections_are_summed_across_pages() -> None:
    def pages(request: httpx.Request) -> httpx.Response:
        if "cursor" in request.url.params:
            return httpx.Response(200, content=_body({"title": ""}))
        return httpx.Response(200, content=_body({"title": ""}, cursor="x"))

    batch, _ = _fetch(pages)

    assert batch.drafts == []
    assert batch.rejected == 2


def test_only_the_first_page_is_conditional() -> None:
    second_url = f"{_FIRST_URL}&cursor={_CURSOR}"
    cache = _DictCache(
        {
            _FIRST_URL: CachedResponse(None, "Sat, 31 Oct 2026 00:00:00 GMT", _PAGE1),
            second_url: CachedResponse(None, "Sat, 31 Oct 2026 00:00:00 GMT", _PAGE2),
        }
    )

    _, requests = _fetch(_two_pages, cache)

    assert requests[0].headers["If-Modified-Since"] == "Sat, 31 Oct 2026 00:00:00 GMT"
    assert "If-Modified-Since" not in requests[1].headers


def test_not_modified_first_page_is_read_from_the_cache() -> None:
    cache = _DictCache(
        {_FIRST_URL: CachedResponse(None, "Sat, 31 Oct 2026 00:00:00 GMT", _PAGE2)}
    )

    batch, requests = _fetch(lambda _: httpx.Response(304), cache)

    assert len(requests) == 1
    assert len(batch.drafts) == 3


def test_himalayas_board_satisfies_source_protocol() -> None:
    board = HimalayasBoard(_KEY)

    async def run() -> CollectResult:
        async with _client(_robots_then(_two_pages)) as client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.HIMALAYAS, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 6


def _robots_then(handler: _Handler) -> _Handler:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return handler(request)

    return wrapped
