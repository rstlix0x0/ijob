import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
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
from ijob_sources.arbeitnow import ArbeitnowBoard, parse_arbeitnow
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError
from ijob_sources.polite_http import CachedResponse, PoliteClient

_KEY = parse_source_key("all")
_FIRST_URL = "https://www.arbeitnow.com/api/job-board-api"
_NEXT_URL = "https://www.arbeitnow.com/api/job-board-api?page=2"


def _fixture(name: str) -> bytes:
    resource = files("ijob_sources_tests.fixtures.arbeitnow") / name
    return resource.read_bytes()


_PAGE1 = _fixture("page1.json")
_PAGE2 = _fixture("page2.json")
_JOBS1: list[dict[str, Any]] = json.loads(_PAGE1)["data"]
_JOBS2: list[dict[str, Any]] = json.loads(_PAGE2)["data"]
_BRINGLIESEL, _HAMBURG = _JOBS1


def _body(*jobs: object, next_url: str | None = None) -> bytes:
    return json.dumps({"data": list(jobs), "links": {"next": next_url}}).encode()


def _only(job: dict[str, Any]) -> SightingDraft:
    return parse_arbeitnow(_body(job), _KEY).batch.drafts[0]


def test_parse_maps_fields() -> None:
    page = parse_arbeitnow(_PAGE1, _KEY)
    draft = page.batch.drafts[0]

    assert draft.source == SourceRef(SourceKind.ARBEITNOW, _KEY)
    assert draft.external_id == ExternalId("senior-key-account-manager-berlin-422601")
    assert draft.title == "Senior Key Account Manager"
    assert draft.company == "BringLiesel GmbH"
    assert draft.location == "Berlin"
    assert draft.url == _BRINGLIESEL["url"]
    assert draft.posted_at == datetime.fromtimestamp(1793397632, tz=UTC)
    assert draft.remote is None
    assert draft.country is None
    assert draft.salaries == ()
    assert json.loads(draft.raw) == _BRINGLIESEL
    assert len(page.batch.drafts) == 2
    assert page.batch.rejected == 0
    assert page.next_url == _NEXT_URL


@pytest.mark.parametrize(
    ("remote", "expected"),
    [(False, RemoteScope.NONE), (True, None)],
    ids=["on-site", "remote"],
)
def test_remote_flag(remote: bool, expected: RemoteScope | None) -> None:  # noqa: FBT001 - parametrised data
    assert _only({**_HAMBURG, "remote": remote}).remote is expected


def test_absent_remote_flag_is_none() -> None:
    job = {k: v for k, v in _HAMBURG.items() if k != "remote"}

    assert _only(job).remote is None


def test_absent_location_is_none() -> None:
    job = {k: v for k, v in _HAMBURG.items() if k != "location"}

    assert _only(job).location is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"slug": None}, id="no-slug"),
        pytest.param({"slug": 4}, id="non-text-slug"),
        pytest.param({"title": ""}, id="empty-title"),
        pytest.param({"company_name": None}, id="no-company"),
        pytest.param({"url": "ftp://x.test/1"}, id="non-http-url"),
        pytest.param({"location": 4}, id="non-text-location"),
        pytest.param({"remote": "yes"}, id="non-bool-remote"),
        pytest.param({"created_at": "yesterday"}, id="non-number-date"),
        pytest.param({"created_at": 10**30}, id="out-of-range-date"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    overrides: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    bad = {**_HAMBURG, **overrides}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.arbeitnow"):
        batch = parse_arbeitnow(_body(_BRINGLIESEL, bad), _KEY).batch

    assert [draft.external_id for draft in batch.drafts] == [_BRINGLIESEL["slug"]]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "invalid" in warnings[0].getMessage()


def test_job_that_is_not_an_object_is_rejected() -> None:
    batch = parse_arbeitnow(_body("a job", _BRINGLIESEL), _KEY).batch

    assert len(batch.drafts) == 1
    assert batch.rejected == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html></html>", id="not-json"),
        pytest.param(b"[]", id="top-level-list"),
        pytest.param(b'{"data": {}}', id="data-not-list"),
        pytest.param(b'{"links": {}}', id="no-data"),
        pytest.param(b'{"data": [], "links": []}', id="links-not-object"),
        pytest.param(b'{"data": [], "links": {"next": 7}}', id="next-not-text"),
        pytest.param(
            b'{"data": [], "links": {"next": "https://evil.example/api?page=2"}}',
            id="next-on-another-host",
        ),
        pytest.param(
            b'{"data": [], "links": {"next": "http://www.arbeitnow.com/api"}}',
            id="next-not-https",
        ),
        pytest.param(b"[" * 100_000, id="too-deep"),
    ],
)
def test_unreadable_body_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_arbeitnow(body, _KEY)


@pytest.mark.parametrize(
    "body",
    [b'{"data": []}', b'{"data": [], "links": {}}', _body(), _body(next_url="")],
    ids=["no-links", "no-next", "null-next", "empty-next"],
)
def test_no_next_link_means_last_page(body: bytes) -> None:
    assert parse_arbeitnow(body, _KEY).next_url is None


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

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return handler(request)

    return PoliteClient(
        transport=httpx.MockTransport(wrapped),
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
            return await ArbeitnowBoard(_KEY).fetch(client)

    return asyncio.run(run()), requests


def _two_pages(request: httpx.Request) -> httpx.Response:
    if "page" in request.url.params:
        return httpx.Response(200, content=_PAGE2)
    return httpx.Response(200, content=_PAGE1)


def test_paging_follows_links_next() -> None:
    batch, requests = _fetch(_two_pages)

    assert [str(request.url) for request in requests] == [_FIRST_URL, _NEXT_URL]
    assert [draft.external_id for draft in batch.drafts] == [
        job["slug"] for job in [*_JOBS1, *_JOBS2]
    ]
    assert batch.rejected == 0


def test_paging_capped_at_five() -> None:
    def always_more(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_body(_HAMBURG, next_url=_NEXT_URL))

    batch, requests = _fetch(always_more)

    assert len(requests) == 5
    assert len(batch.drafts) == 5


def test_rejections_are_summed_across_pages() -> None:
    def pages(request: httpx.Request) -> httpx.Response:
        if "page" in request.url.params:
            return httpx.Response(200, content=_body({"title": ""}))
        return httpx.Response(200, content=_body({"title": ""}, next_url=_NEXT_URL))

    batch, _ = _fetch(pages)

    assert batch.drafts == []
    assert batch.rejected == 2


def test_only_the_first_page_is_conditional() -> None:
    validator = "Sat, 31 Oct 2026 00:00:00 GMT"
    cache = _DictCache(
        {
            _FIRST_URL: CachedResponse(None, validator, _PAGE1),
            _NEXT_URL: CachedResponse(None, validator, _PAGE2),
        }
    )

    _, requests = _fetch(_two_pages, cache)

    assert requests[0].headers["If-Modified-Since"] == validator
    assert "If-Modified-Since" not in requests[1].headers


def test_arbeitnow_board_satisfies_source_protocol() -> None:
    board = ArbeitnowBoard(_KEY)

    async def run() -> CollectResult:
        async with _client(_two_pages) as client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.ARBEITNOW, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 4
