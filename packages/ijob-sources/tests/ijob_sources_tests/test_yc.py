import asyncio
import html
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_core.model import ExternalId, SourceKind, SourceRef, parse_source_key
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError, SourceNotFoundError
from ijob_sources.polite_http import CachedResponse, PoliteClient
from ijob_sources.yc import YcJobs, parse_yc_page

_FIXTURES = Path(__file__).parent / "fixtures" / "yc"
_SWE = (_FIXTURES / "software_engineer.html").read_text(encoding="utf-8")
_REMOTE = (_FIXTURES / "remote.html").read_text(encoding="utf-8")
_KEY = parse_source_key("swe")
_SWE_URL = "https://www.ycombinator.com/jobs/role/software-engineer"
_REMOTE_URL = "https://www.ycombinator.com/jobs/role/software-engineer/remote"


def _page(*postings: object) -> str:
    data = {"props": {"jobPostings": list(postings)}}
    return f'<div data-page="{html.escape(json.dumps(data), quote=True)}"></div>'


def _posting(job_id: object = 1, **overrides: object) -> dict[str, Any]:
    posting: dict[str, Any] = {
        "id": job_id,
        "title": "Backend Engineer",
        "url": "/companies/acme/jobs/abc-backend-engineer",
        "location": "Remote",
        "companyName": "Acme",
        "createdAt": "2 days",
    }
    posting.update(overrides)
    return posting


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _site(
    swe: httpx.Response,
    remote: httpx.Response,
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if requests is not None:
            requests.append(request)
        if request.url.path.endswith("/remote"):
            return remote
        return swe

    return handler


def _html(text: str) -> httpx.Response:
    return httpx.Response(200, content=text.encode())


class _DictCache:
    def __init__(self, entries: dict[str, CachedResponse]) -> None:
        self.entries = entries

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


def _fetch(
    handler: Callable[[httpx.Request], httpx.Response],
    cache: _DictCache | None = None,
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
            return await YcJobs(_KEY).fetch(client)

    return asyncio.run(run())


def _fetch_fixtures() -> SourceBatch:
    return _fetch(_site(_html(_SWE), _html(_REMOTE)))


def test_parse_maps_fields() -> None:
    sully = _fetch_fixtures().drafts[0]

    assert sully.source == SourceRef(SourceKind.YC, _KEY)
    assert sully.external_id == ExternalId("100474")
    assert sully.title == "Staff Full Stack Engineer"
    assert sully.company == "Sully"
    assert sully.location == "Mountain View, CA, US"
    assert sully.url == (
        "https://www.ycombinator.com/companies/sully/jobs/DuoVsUf-staff-full-stack-engineer"
    )
    assert sully.posted_at is None
    assert json.loads(sully.raw)["id"] == 100474
    assert json.loads(sully.raw)["salaryRange"] == "$200K - $300K"


def test_pages_merged_by_id() -> None:
    batch = _fetch_fixtures()

    assert [draft.external_id for draft in batch.drafts] == [
        "100474",
        "94311",
        "43777",
        "95525",
    ]
    assert batch.rejected == 0


def test_relative_dates_ignored() -> None:
    assert json.loads(_fetch_fixtures().drafts[0].raw)["createdAt"] == "3 months"
    assert all(draft.posted_at is None for draft in _fetch_fixtures().drafts)


def test_both_pages_requested_conditionally() -> None:
    cache = _DictCache(
        {
            _SWE_URL: CachedResponse('"a"', None, _SWE.encode()),
            _REMOTE_URL: CachedResponse('"b"', None, _REMOTE.encode()),
        }
    )
    requests: list[httpx.Request] = []

    batch = _fetch(_site(httpx.Response(304), httpx.Response(304), requests), cache)

    assert [str(request.url) for request in requests] == [_SWE_URL, _REMOTE_URL]
    assert [request.headers["if-none-match"] for request in requests] == ['"a"', '"b"']
    assert len(batch.drafts) == 4


def test_parse_yc_page_returns_postings() -> None:
    postings = parse_yc_page(_SWE)

    assert [posting["id"] for posting in postings if isinstance(posting, dict)] == [
        100474,
        94311,
        43777,
    ]


@pytest.mark.parametrize(
    "page",
    [
        "<html><body>no data here</body></html>",
        '<div data-page="not json"></div>',
        '<div data-page="[]"></div>',
        '<div data-page="{}"></div>',
        '<div data-page="{&quot;props&quot;: {}}"></div>',
        '<div data-page="{&quot;props&quot;: {&quot;jobPostings&quot;: {}}}"></div>',
        '<div data-page="' + "[" * 100_000 + '"></div>',
    ],
    ids=[
        "no-attribute",
        "not-json",
        "list",
        "no-props",
        "no-postings",
        "postings-not-list",
        "too-deep",
    ],
)
def test_missing_data_page_is_parse_error(page: str) -> None:
    with pytest.raises(ParseError):
        parse_yc_page(page)


def test_a_page_that_cannot_be_read_fails_the_fetch() -> None:
    with pytest.raises(ParseError):
        _fetch(_site(_html(_SWE), _html("<html></html>")))


def test_unknown_page_raises_source_not_found() -> None:
    with pytest.raises(SourceNotFoundError):
        _fetch(_site(httpx.Response(404, text="no"), _html(_REMOTE)))


@pytest.mark.parametrize(
    "bad",
    [
        _posting(2, title=""),
        _posting(2, companyName=None),
        _posting(2, url="no-leading-slash"),
        _posting(2, url=None),
        _posting(2, location=5),
        _posting(job_id=True),
        _posting(job_id=None),
        _posting(job_id="7"),
        "not a posting",
    ],
    ids=[
        "empty-title",
        "no-company",
        "relative-url-without-slash",
        "no-url",
        "non-text-location",
        "bool-id",
        "no-id",
        "text-id",
        "string-posting",
    ],
)
def test_invalid_posting_is_rejected_and_others_kept(
    bad: object, caplog: pytest.LogCaptureFixture
) -> None:
    handler = _site(_html(_page(_posting(1), bad)), _html(_page()))

    with caplog.at_level(logging.WARNING, logger="ijob_sources.yc"):
        batch = _fetch(handler)

    assert [draft.external_id for draft in batch.drafts] == ["1"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_empty_location_is_none() -> None:
    handler = _site(_html(_page(_posting(location=""))), _html(_page()))

    assert _fetch(handler).drafts[0].location is None


def test_text_that_is_not_utf8_encodable_is_rejected() -> None:
    handler = _site(
        _html(_page(_posting(1, title="\ud800"), _posting(2))), _html(_page())
    )

    batch = _fetch(handler)

    assert [draft.external_id for draft in batch.drafts] == ["2"]
    assert batch.rejected == 1


def test_yc_jobs_satisfies_source_protocol() -> None:
    source = YcJobs(_KEY)
    fake_time = _FakeTime()
    handler = _site(_html(_SWE), _html(_REMOTE))

    async def run() -> CollectResult:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
        async with client:
            return await collect([source], client)

    result = asyncio.run(run())

    assert source.ref == SourceRef(SourceKind.YC, _KEY)
    assert result.failures == []
    assert len(result.batches[source.ref].drafts) == 4
