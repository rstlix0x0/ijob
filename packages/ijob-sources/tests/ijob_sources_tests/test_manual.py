import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_core.model import (
    ExternalId,
    RemoteScope,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.money import Money, Period
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import (
    HttpStatusError,
    IdentityMismatchError,
    ParseError,
    RobotsDisallowedError,
)
from ijob_sources.jsonld import job_postings
from ijob_sources.manual import (
    FeedUrl,
    JobPostingPage,
    manual_key,
    parse_feed,
    parse_jobposting_page,
)
from ijob_sources.polite_http import CachedResponse, ConditionalCache, PoliteClient

_FIXTURES = Path(__file__).parent / "fixtures"
_JAZZHR = (_FIXTURES / "jsonld" / "jazzhr.html").read_bytes()
_GOLANG = (_FIXTURES / "rss" / "golang.xml").read_bytes()

_PAGE_URL = "https://careers.example.test/jobs/release-engineer"
_FEED_URL = "https://feed.example.test/rss.xml"
_KEY = parse_source_key("0123456789abcdef")
_JAZZHR_URL = (
    "https://eclipsefoundation.applytojob.com/apply/gdWcbFq2mr/"
    "Open-Source-Software-Release-Engineer"
)
_GOLANG_FIRST = (
    "https://www.golangprojects.com/"
    "golang-go-job-gkd-Remote-Senior-Software-Engineer-London-Sotheby-s-remotework.html"
)


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _MemoryCache:
    """A conditional cache that keeps responses in a dict."""

    def __init__(self) -> None:
        self.entries: dict[str, CachedResponse] = {}

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


def _page(*postings: dict[str, Any]) -> bytes:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        for posting in postings
    )
    return f"<html><head>{scripts}</head><body></body></html>".encode()


def _posting(**fields: object) -> dict[str, Any]:
    return {
        "@type": "JobPosting",
        "title": "Rust Engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"},
        **fields,
    }


def _feed(*items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>t</title>{''.join(items)}</channel></rss>"
    ).encode()


def _serving(
    response: httpx.Response, requests: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        if requests is not None:
            requests.append(request)
        return response

    return handler


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    cache: ConditionalCache | None = None,
) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        cache=cache,
    )


def _fetch(
    source: JobPostingPage | FeedUrl,
    handler: Callable[[httpx.Request], httpx.Response],
) -> SourceBatch:
    async def run() -> SourceBatch:
        async with _client(handler) as client:
            return await source.fetch(client)

    return asyncio.run(run())


def _parse(
    body: bytes, company: str = "Eclipse Foundation", target_url: str = _PAGE_URL
) -> SourceBatch:
    return parse_jobposting_page(body, _KEY, target_url=target_url, company=company)


def test_manual_key_is_16_hex_and_stable() -> None:
    key = manual_key(_PAGE_URL)

    assert re.fullmatch(r"[0-9a-f]{16}", key)
    assert manual_key(_PAGE_URL) == key
    assert manual_key(_FEED_URL) != key


def test_manual_key_is_the_sha256_prefix_of_the_utf8_url() -> None:
    url = "https://example.test/café"

    assert manual_key(url) == hashlib.sha256(url.encode()).hexdigest()[:16]


def test_jobposting_page_maps_fields() -> None:
    batch = _parse(_JAZZHR)

    assert batch.rejected == 0
    (draft,) = batch.drafts
    assert draft.source == SourceRef(SourceKind.JOBPOSTING, _KEY)
    assert draft.title == "Open Source Software Release Engineer"
    assert draft.company == "Eclipse Foundation, Inc."
    assert draft.location == "Brussels"
    assert draft.url == _JAZZHR_URL
    assert draft.posted_at == datetime(2026, 10, 7, tzinfo=UTC)
    assert draft.salaries == (
        Money(Decimal(55000), Decimal(80000), "EUR", Period.YEAR),
    )
    assert draft.remote is RemoteScope.COUNTRY
    assert json.loads(draft.raw) == job_postings(_JAZZHR.decode())[0]


def test_external_id_and_url_fall_back_to_the_target_url() -> None:
    (draft,) = _parse(_page(_posting()), company="Acme").drafts

    assert draft.external_id == ExternalId(_PAGE_URL)
    assert draft.url == _PAGE_URL


def test_identifier_and_url_of_the_posting_win() -> None:
    body = _page(
        _posting(
            identifier={"@type": "PropertyValue", "value": "R-42"},
            url="https://careers.example.test/jobs/42",
        )
    )

    (draft,) = _parse(body, company="Acme").drafts

    assert draft.external_id == ExternalId("R-42")
    assert draft.url == "https://careers.example.test/jobs/42"


def test_jobposting_identity() -> None:
    assert len(_parse(_JAZZHR, company="Eclipse Foundation").drafts) == 1

    with pytest.raises(IdentityMismatchError) as caught:
        _parse(_JAZZHR, company="Acme")

    assert caught.value.ref == SourceRef(SourceKind.JOBPOSTING, _KEY)


def test_page_without_jobposting_is_parse_error() -> None:
    with pytest.raises(ParseError):
        _parse(b"<html><body>no data</body></html>")


def test_page_with_several_jobpostings_is_parse_error() -> None:
    with pytest.raises(ParseError):
        _parse(_page(_posting(), _posting(title="Other")), company="Acme")


def test_unreadable_posting_is_rejected_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _page(_posting(datePosted="not a date"))

    with caplog.at_level(logging.WARNING, logger="ijob_sources.manual"):
        batch = _parse(body, company="Acme")

    assert batch == SourceBatch(drafts=[], rejected=1)
    assert _PAGE_URL in caplog.text


def test_posting_without_a_title_is_rejected() -> None:
    posting = {"@type": "JobPosting", "hiringOrganization": {"name": "Acme"}}

    assert _parse(_page(posting), company="Acme") == SourceBatch(drafts=[], rejected=1)


def test_posting_without_a_company_proves_nothing_and_is_a_mismatch() -> None:
    posting = {"@type": "JobPosting", "title": "No company"}

    with pytest.raises(IdentityMismatchError):
        _parse(_page(posting), company="Acme")


def test_rejected_posting_naming_another_company_is_a_mismatch() -> None:
    body = _page(
        _posting(hiringOrganization={"name": "Other Corp"}, datePosted="not a date")
    )

    with pytest.raises(IdentityMismatchError) as caught:
        _parse(body, company="Ramp")

    assert caught.value.ref == SourceRef(SourceKind.JOBPOSTING, _KEY)


def test_rejected_posting_naming_the_company_is_still_rejected() -> None:
    body = _page(_posting(datePosted="not a date"))

    assert _parse(body, company="Acme Corp") == SourceBatch(drafts=[], rejected=1)


def test_parse_decodes_utf8() -> None:
    body = (
        b'<script type="application/ld+json">'
        + json.dumps(_posting(title="Ingénieur"), ensure_ascii=False).encode()
        + b"</script>"
    )

    (draft,) = _parse(body, company="Acme").drafts

    assert draft.title == "Ingénieur"


def test_fetch_makes_one_plain_get_of_the_target() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, content=_JAZZHR), requests)
    page = JobPostingPage(_KEY, target_url=_PAGE_URL, company="Eclipse Foundation")

    batch = _fetch(page, handler)

    assert [str(request.url) for request in requests] == [_PAGE_URL]
    assert batch == _parse(_JAZZHR)


def test_fetch_checks_identity() -> None:
    handler = _serving(httpx.Response(200, content=_JAZZHR))
    page = JobPostingPage(_KEY, target_url=_PAGE_URL, company="Acme")

    with pytest.raises(IdentityMismatchError):
        _fetch(page, handler)


def test_gone_page_is_http_status_error() -> None:
    handler = _serving(httpx.Response(410))
    page = JobPostingPage(_KEY, target_url=_PAGE_URL, company="Acme")

    with pytest.raises(HttpStatusError) as caught:
        _fetch(page, handler)

    assert caught.value.status == 410


def test_robots_disallowed_page_is_not_requested() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, content=_JAZZHR), requests)
    page = JobPostingPage(
        _KEY,
        target_url="https://careers.example.test/private/job",
        company="Eclipse Foundation",
    )

    with pytest.raises(RobotsDisallowedError):
        _fetch(page, handler)

    assert requests == []


def test_jobposting_page_is_never_conditional() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(200, content=_JAZZHR, headers={"etag": '"v1"'})

    page = JobPostingPage(_KEY, target_url=_PAGE_URL, company="Eclipse Foundation")

    async def run() -> None:
        async with _client(handler, _MemoryCache()) as client:
            await page.fetch(client)
            await page.fetch(client)

    asyncio.run(run())

    assert seen == [None, None]


def test_jobposting_page_ref_and_protocol() -> None:
    page = JobPostingPage(_KEY, target_url=_PAGE_URL, company="Eclipse Foundation")
    handler = _serving(httpx.Response(200, content=_JAZZHR))

    async def run() -> CollectResult:
        async with _client(handler) as client:
            return await collect([page], client)

    result = asyncio.run(run())

    assert page.ref == SourceRef(SourceKind.JOBPOSTING, _KEY)
    assert str(page.ref) == f"jobposting:{_KEY}"
    assert result.failures == []
    assert len(result.batches[page.ref].drafts) == 1


def test_feed_url_maps_items() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, content=_GOLANG), requests)
    feed = FeedUrl(_KEY, target_url=_FEED_URL, company="Golang Projects")

    batch = _fetch(feed, handler)

    assert [str(request.url) for request in requests] == [_FEED_URL]
    assert batch.rejected == 0
    assert len(batch.drafts) == 3
    first = batch.drafts[0]
    assert first.source == SourceRef(SourceKind.FEED, _KEY)
    assert first.external_id == ExternalId(_GOLANG_FIRST)
    assert first.url == _GOLANG_FIRST
    assert (
        first.title
        == "Senior Software Engineer\xa0@\xa0Sotheby\N{RIGHT SINGLE QUOTATION MARK}s"
    )
    assert first.company == "Golang Projects"
    assert first.posted_at is None
    assert json.loads(first.raw)["link"] == _GOLANG_FIRST
    assert {draft.company for draft in batch.drafts} == {"Golang Projects"}


def test_feed_url_reads_pub_date() -> None:
    body = _feed(
        "<item><title>Engineer</title><link>https://x.test/1</link>"
        "<pubDate>Tue, 06 Oct 2026 10:00:00 +0000</pubDate></item>"
    )
    feed = FeedUrl(_KEY, target_url=_FEED_URL, company="Acme")

    (draft,) = _fetch(feed, _serving(httpx.Response(200, content=body))).drafts

    assert draft.posted_at == datetime(2026, 10, 6, 10, tzinfo=UTC)


def test_feed_url_sends_a_validator_on_the_second_fetch() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(200, content=_GOLANG, headers={"etag": '"v1"'})

    feed = FeedUrl(_KEY, target_url=_FEED_URL, company="Acme")

    async def run() -> None:
        async with _client(handler, _MemoryCache()) as client:
            await feed.fetch(client)
            await feed.fetch(client)

    asyncio.run(run())

    assert seen == [None, '"v1"']


@pytest.mark.parametrize(
    "item",
    [
        "<item><title>No link</title></item>",
        "<item><link>https://x.test/1</link></item>",
        "<item><title>Bad link</title><link>ftp://x.test/1</link></item>",
    ],
)
def test_feed_url_drops_unusable_items(
    item: str, caplog: pytest.LogCaptureFixture
) -> None:
    body = _feed(item, "<item><title>Good</title><link>https://x.test/2</link></item>")
    feed = FeedUrl(_KEY, target_url=_FEED_URL, company="Acme")

    with caplog.at_level(logging.WARNING, logger="ijob_sources.manual"):
        batch = _fetch(feed, _serving(httpx.Response(200, content=body)))

    assert [draft.title for draft in batch.drafts] == ["Good"]
    assert batch.rejected == 1
    assert caplog.records


def test_feed_url_runs_no_identity_check() -> None:
    feed = FeedUrl(_KEY, target_url=_FEED_URL, company="Nobody In The Feed")

    batch = _fetch(feed, _serving(httpx.Response(200, content=_GOLANG)))

    assert len(batch.drafts) == 3


def test_feed_url_body_that_is_not_rss_is_parse_error() -> None:
    feed = FeedUrl(_KEY, target_url=_FEED_URL, company="Acme")

    with pytest.raises(ParseError):
        _fetch(feed, _serving(httpx.Response(200, content=b"<html></html>")))


def test_feed_url_ref_and_protocol() -> None:
    feed = FeedUrl(_KEY, target_url=_FEED_URL, company="Acme")
    handler = _serving(httpx.Response(200, content=_GOLANG))

    async def run() -> CollectResult:
        async with _client(handler) as client:
            return await collect([feed], client)

    result = asyncio.run(run())

    assert feed.ref == SourceRef(SourceKind.FEED, _KEY)
    assert result.failures == []
    assert len(result.batches[feed.ref].drafts) == 3


def _parse_feed(body: bytes, company: str = "Acme") -> SourceBatch:
    return parse_feed(body, _KEY, target_url=_FEED_URL, company=company)


def test_parse_feed_maps_items() -> None:
    batch = _parse_feed(_GOLANG, company="Golang Projects")

    assert batch.rejected == 0
    assert len(batch.drafts) == 3
    first = batch.drafts[0]
    assert first.source == SourceRef(SourceKind.FEED, _KEY)
    assert first.external_id == ExternalId(_GOLANG_FIRST)
    assert first.company == "Golang Projects"


def test_parse_feed_raw_keeps_the_items_extras() -> None:
    body = _feed(
        "<item><title>Go</title><link>https://x.test/1</link>"
        "<category>go</category></item>"
    )

    (draft,) = _parse_feed(body).drafts

    assert json.loads(draft.raw)["extras"] == {"category": "go"}


def test_parse_feed_drops_unusable_items(caplog: pytest.LogCaptureFixture) -> None:
    body = _feed(
        "<item><title>No link</title></item>",
        "<item><title>Good</title><link>https://x.test/2</link></item>",
    )

    with caplog.at_level(logging.WARNING, logger="ijob_sources.manual"):
        batch = _parse_feed(body)

    assert [draft.title for draft in batch.drafts] == ["Good"]
    assert batch.rejected == 1
    assert _FEED_URL in caplog.text


def test_parse_feed_of_an_empty_channel_is_an_empty_batch() -> None:
    assert _parse_feed(_feed()) == SourceBatch(drafts=[], rejected=0)


def test_parse_feed_body_that_is_not_rss_is_parse_error() -> None:
    with pytest.raises(ParseError):
        _parse_feed(b"<html></html>")


def test_fetch_is_the_feed_parsed() -> None:
    feed = FeedUrl(_KEY, target_url=_FEED_URL, company="Acme")

    batch = _fetch(feed, _serving(httpx.Response(200, content=_GOLANG)))

    assert batch == _parse_feed(_GOLANG)
