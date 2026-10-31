import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_core.countries import CountryCode
from ijob_core.model import ExternalId, SourceKind, SourceRef, parse_source_key
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError, SourceNotFoundError
from ijob_sources.letsgetrusty import LetsGetRusty, parse_lgr_page
from ijob_sources.polite_http import CachedResponse, PoliteClient

_FIXTURES = Path(__file__).parent / "fixtures" / "letsgetrusty"
_PAGE1 = (_FIXTURES / "page1.html").read_text(encoding="utf-8")
_PAGE2 = (_FIXTURES / "page2.html").read_text(encoding="utf-8")
_KEY = parse_source_key("all")
_PAGE1_URL = "https://jobs.letsgetrusty.com/?page=1"


def _job(job_id: str = "recA", **overrides: object) -> dict[str, Any]:
    job: dict[str, Any] = {
        "@type": "JobPosting",
        "title": "Rust Engineer",
        "description": "Trimmed description.",
        "datePosted": "2026-10-29",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressCountry": "Germany"},
        },
        "url": f"https://jobs.letsgetrusty.com?job={job_id}",
    }
    job.update(overrides)
    return job


def _page(*items: object, total: int | None = 194) -> str:
    elements = [
        {"@type": "ListItem", "position": n, "item": item}
        for n, item in enumerate(items, start=1)
    ]
    block = {"@type": "ItemList", "itemListElement": elements}
    count = "" if total is None else f"<p>Showing 1-10 of {total} jobs</p>"
    return (
        f'<script type="application/ld+json">{json.dumps(block)}</script>'
        f"<body>{count}</body>"
    )


def _with_block(block: str) -> str:
    return (
        "<p>Showing 1-10 of 194 jobs</p>"
        f'<script type="application/ld+json">{block}</script>'
    )


def _numbered_site(
    total: int, requests: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        requests.append(request)
        number = request.url.params["page"]
        return httpx.Response(
            200, content=_page(_job(f"rec{number}"), total=total).encode()
        )

    return handler


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _DictCache:
    def __init__(self, entries: dict[str, CachedResponse]) -> None:
        self.entries = entries

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    cache: _DictCache | None = None,
) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        cache=cache,
    )


def _fetch(
    handler: Callable[[httpx.Request], httpx.Response],
    cache: _DictCache | None = None,
) -> SourceBatch:
    async def run() -> SourceBatch:
        async with _client(handler, cache) as client:
            return await LetsGetRusty(_KEY).fetch(client)

    return asyncio.run(run())


def _fixture_site(
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if requests is not None:
            requests.append(request)
        page = _PAGE1 if request.url.params["page"] == "1" else _PAGE2
        return httpx.Response(200, content=page.encode())

    return handler


def test_ref_names_the_source() -> None:
    assert LetsGetRusty(_KEY).ref == SourceRef(SourceKind.LETSGETRUSTY, _KEY)


def test_parse_maps_fields() -> None:
    first = _fetch(_fixture_site()).drafts[0]

    assert first.source == SourceRef(SourceKind.LETSGETRUSTY, _KEY)
    assert first.external_id == ExternalId("rec3oDYFsU2vezquE")
    assert first.title == "Backend Software Engineer"
    assert first.company == "Quantum Technology Recruiting Inc."
    assert first.location == "Canada"
    assert first.country == CountryCode("CA")
    assert first.url == "https://jobs.letsgetrusty.com?job=rec3oDYFsU2vezquE"
    assert first.posted_at == datetime(2026, 10, 29, tzinfo=UTC)
    assert first.remote is None
    assert json.loads(first.raw)["employmentType"] == "FULL_TIME"


def test_company_name_is_stripped() -> None:
    second = _fetch(_fixture_site()).drafts[1]

    assert second.company == "Carv"


def test_remote_address_country() -> None:
    drafts = {draft.external_id: draft for draft in _fetch(_fixture_site()).drafts}
    wasmer = drafts[ExternalId("recVXEeY7ed0JwwSt")]

    assert wasmer.country is None
    assert wasmer.location == "Remote"
    assert wasmer.remote is None


def test_both_pages_read_and_featured_block_ignored() -> None:
    batch = _fetch(_fixture_site())

    assert [draft.external_id for draft in batch.drafts] == [
        "rec3oDYFsU2vezquE",
        "recJQ3Th4Y34OUnrO",
        "recVXEeY7ed0JwwSt",
        "recsCLJ0sZ2KOlSFz",
        "reclETV6klPiCMIId",
        "rec0jaHdTkEhGgz5T",
    ]
    assert batch.rejected == 0
    assert batch.still_listed == ()


def test_job_repeated_across_pages_is_kept_once() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, content=_page(_job("recSame"), total=11).encode())

    batch = _fetch(handler)

    assert [draft.external_id for draft in batch.drafts] == ["recSame"]
    assert batch.rejected == 0


@pytest.mark.parametrize(("total", "pages"), [(19, 2), (10, 1), (11, 2), (250, 20)])
def test_page_count_from_total(total: int, pages: int) -> None:
    requests: list[httpx.Request] = []

    batch = _fetch(_numbered_site(total, requests))

    assert [request.url.params["page"] for request in requests] == [
        str(number) for number in range(1, pages + 1)
    ]
    assert len(batch.drafts) == pages


def test_no_job_param_requested() -> None:
    requests: list[httpx.Request] = []

    _fetch(_numbered_site(35, requests))

    assert requests
    assert all("job" not in request.url.params for request in requests)
    assert all("job=" not in str(request.url) for request in requests)
    assert all(request.url.host == "jobs.letsgetrusty.com" for request in requests)


def test_first_page_conditional_and_later_pages_plain() -> None:
    cache = _DictCache(
        {
            _PAGE1_URL: CachedResponse('"a"', None, _PAGE1.encode()),
            "https://jobs.letsgetrusty.com/?page=2": CachedResponse(
                '"b"', None, _PAGE2.encode()
            ),
        }
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        requests.append(request)
        if request.url.params["page"] == "1":
            return httpx.Response(304)
        return httpx.Response(200, content=_PAGE2.encode())

    batch = _fetch(handler, cache)

    assert str(requests[0].url) == _PAGE1_URL
    assert requests[0].headers["if-none-match"] == '"a"'
    assert "if-none-match" not in requests[1].headers
    assert len(batch.drafts) == 6


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        pytest.param({"title": ""}, "recBad", id="empty-title"),
        pytest.param({"title": 5}, "recBad", id="non-text-title"),
        pytest.param(
            {"hiringOrganization": {"@type": "Organization"}}, "recBad", id="no-company"
        ),
        pytest.param({"datePosted": "yesterday"}, "recBad", id="bad-date"),
        pytest.param({"@type": "Organization"}, "recBad", id="not-a-job-posting"),
        pytest.param({"url": "https://jobs.letsgetrusty.com"}, "index 1", id="no-job"),
        pytest.param({"url": 7}, "index 1", id="non-text-url"),
        pytest.param({"url": "ftp://x.test/?job=recBad"}, "recBad", id="non-http-url"),
    ],
)
def test_bad_job_is_rejected_and_others_kept(
    overrides: dict[str, object], label: str, caplog: pytest.LogCaptureFixture
) -> None:
    bad = _job("recBad", **overrides)
    page = _page(_job("recGood"), bad)

    with caplog.at_level(logging.WARNING, logger="ijob_sources.letsgetrusty"):
        batch = parse_lgr_page(page, _KEY)

    assert [draft.external_id for draft in batch.drafts] == ["recGood"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()


def test_list_element_that_is_not_an_object_is_rejected() -> None:
    block = {"@type": "ItemList", "itemListElement": ["x", {"item": "y"}, {}]}
    page = f'<script type="application/ld+json">{json.dumps(block)}</script>'

    batch = parse_lgr_page(page, _KEY)

    assert batch.drafts == []
    assert batch.rejected == 3


def test_item_list_script_type_is_case_insensitive() -> None:
    block = {"@type": "ItemList", "itemListElement": [{"item": _job("recUp")}]}
    page = f'<script type="Application/LD+JSON">{json.dumps(block)}</script>'

    batch = parse_lgr_page(page, _KEY)

    assert [draft.external_id for draft in batch.drafts] == ["recUp"]


def test_parse_lgr_page_needs_no_total() -> None:
    assert len(parse_lgr_page(_page(_job(), total=None), _KEY).drafts) == 1


@pytest.mark.parametrize(
    "page",
    [
        _page(_job(), total=None),
        "<p>Showing 1-10 of 194 jobs</p>",
        _with_block('{"@type": "WebSite"}'),
        _with_block('{"@type": "ItemList", "itemListElement": {}}'),
        _with_block("not json"),
        "",
    ],
    ids=[
        "no-total",
        "no-block",
        "no-item-list",
        "elements-not-a-list",
        "block-not-json",
        "empty",
    ],
)
def test_first_page_without_total_or_item_list_is_parse_error(page: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, content=page.encode())

    with pytest.raises(ParseError):
        _fetch(handler)


def test_later_page_without_item_list_is_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.params["page"] == "1":
            return httpx.Response(200, content=_PAGE1.encode())
        return httpx.Response(200, content=b"<html></html>")

    with pytest.raises(ParseError):
        _fetch(handler)


def test_missing_page_is_source_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(404)

    with pytest.raises(SourceNotFoundError):
        _fetch(handler)


def test_collect_runs_the_source() -> None:
    async def run() -> CollectResult:
        async with _client(_fixture_site()) as client:
            return await collect([LetsGetRusty(_KEY)], client)

    result = asyncio.run(run())

    assert result.failures == []
    batch = result.batches[SourceRef(SourceKind.LETSGETRUSTY, _KEY)]
    assert len(batch.drafts) == 6
