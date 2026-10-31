import asyncio
import json
from collections.abc import Callable
from importlib.resources import files
from typing import Any

import httpx
import pytest
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_sources.collect import Source, SourceBatch
from ijob_sources.detect import Resolution, resolve_url, verify_new_source
from ijob_sources.errors import (
    IdentityMismatchError,
    ParseError,
    RobotsDisallowedError,
    UnsupportedUrlError,
)
from ijob_sources.greenhouse import GreenhouseBoard
from ijob_sources.lever import LeverSite
from ijob_sources.manual import manual_key
from ijob_sources.polite_http import PoliteClient

_PAGE_URL = "https://careers.example.test/jobs/release-engineer"
_FEED_URL = "https://feed.example.test/rss.xml"

type _Handler = Callable[[httpx.Request], httpx.Response]


def _fixture(package: str, name: str) -> bytes:
    return (files(f"ijob_sources_tests.fixtures.{package}") / name).read_bytes()


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Server:
    """Answers robots.txt, hands every other request to a handler, and counts them."""

    def __init__(
        self, handler: _Handler, robots: str = "User-agent: *\nAllow: /\n"
    ) -> None:
        self._handler = handler
        self._robots = robots
        self.page_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self._robots)
        self.page_requests.append(request)
        return self._handler(request)


def _client(server: _Server) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(server),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )


def _serving(body: bytes, status: int = 200) -> _Handler:
    return lambda _request: httpx.Response(status, content=body)


def _resolve(url: str, server: _Server, company: str = "Acme") -> Resolution:
    async def run() -> Resolution:
        async with _client(server) as client:
            return await resolve_url(url, company=company, client=client)

    return asyncio.run(run())


def _verify(source: Source, server: _Server) -> SourceBatch:
    async def run() -> SourceBatch:
        async with _client(server) as client:
            return await verify_new_source(source, client)

    return asyncio.run(run())


def _ref(kind: SourceKind, key: str) -> SourceRef:
    return SourceRef(kind, parse_source_key(key))


def _posting(title: str) -> dict[str, Any]:
    return {
        "@type": "JobPosting",
        "title": title,
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
    }


def _page(*blocks: dict[str, Any]) -> bytes:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(block)}</script>'
        for block in blocks
    )
    return f"<html><head>{scripts}</head><body></body></html>".encode()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://boards.greenhouse.io/stripe/jobs/12345",
            _ref(SourceKind.GREENHOUSE, "stripe"),
        ),
        (
            "https://job-boards.greenhouse.io/tailscale?gh_src=x",
            _ref(SourceKind.GREENHOUSE, "tailscale"),
        ),
        (
            "http://jobs.lever.co/spotify/abc-123",
            _ref(SourceKind.LEVER, "spotify"),
        ),
        (
            "https://jobs.ashbyhq.com/mistral.ai",
            _ref(SourceKind.ASHBY, "mistral.ai"),
        ),
        (
            "https://apply.workable.com/huggingface/",
            _ref(SourceKind.WORKABLE, "huggingface"),
        ),
        (
            "https://jobs.gem.com/retool#top",
            _ref(SourceKind.GEM, "retool"),
        ),
    ],
)
def test_hiring_system_urls(url: str, expected: SourceRef) -> None:
    server = _Server(_serving(b""))

    resolution = _resolve(url, server)

    assert resolution == Resolution(ref=expected, target_url=None, batch=None)
    assert server.page_requests == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Jobs.Lever.co/spotify", _ref(SourceKind.LEVER, "spotify")),
        (
            "HTTPS://BOARDS.GREENHOUSE.IO/Stripe/jobs/1",
            _ref(SourceKind.GREENHOUSE, "Stripe"),
        ),
        (
            "https://Apply.Workable.com/HuggingFace/",
            _ref(SourceKind.WORKABLE, "HuggingFace"),
        ),
    ],
)
def test_hiring_system_host_is_case_insensitive_and_the_token_case_is_kept(
    url: str, expected: SourceRef
) -> None:
    server = _Server(_serving(b""))

    assert _resolve(url, server).ref == expected
    assert server.page_requests == []


def test_greenhouse_embed_is_not_a_board() -> None:
    url = "https://boards.greenhouse.io/embed/job_board?for=stripe"
    server = _Server(_serving(b"<html><body></body></html>"))

    with pytest.raises(UnsupportedUrlError) as caught:
        _resolve(url, server)

    assert caught.value.url == url
    assert [str(request.url) for request in server.page_requests] == [url]


@pytest.mark.parametrize("token", ["j", "api", "J"])
def test_workable_job_link_not_an_account(token: str) -> None:
    url = f"https://apply.workable.com/{token}/81B46579FE"
    server = _Server(_serving(_fixture("jsonld", "jazzhr.html")))

    resolution = _resolve(url, server, company="Eclipse Foundation")

    assert resolution.ref == SourceRef(SourceKind.JOBPOSTING, manual_key(url))
    assert [str(request.url) for request in server.page_requests] == [url]


def test_hiring_system_token_that_is_not_a_source_key_is_unsupported() -> None:
    server = _Server(_serving(b""))

    with pytest.raises(UnsupportedUrlError) as caught:
        _resolve("https://jobs.lever.co/bad%20token", server)

    assert caught.value.url == "https://jobs.lever.co/bad%20token"
    assert server.page_requests == []


def test_jobposting_page_detected() -> None:
    server = _Server(_serving(_fixture("jsonld", "jazzhr.html")))

    resolution = _resolve(_PAGE_URL, server, company="Eclipse Foundation")

    assert resolution.ref == SourceRef(SourceKind.JOBPOSTING, manual_key(_PAGE_URL))
    assert resolution.target_url == _PAGE_URL
    assert resolution.batch is not None
    assert len(resolution.batch.drafts) == 1
    assert len(server.page_requests) == 1


def test_jobposting_identity_checked_without_a_second_fetch() -> None:
    server = _Server(_serving(_fixture("jsonld", "jazzhr.html")))

    with pytest.raises(IdentityMismatchError) as caught:
        _resolve(_PAGE_URL, server, company="Acme")

    assert caught.value.ref == SourceRef(SourceKind.JOBPOSTING, manual_key(_PAGE_URL))
    assert len(server.page_requests) == 1


def test_listing_page_with_several_postings_not_a_jobposting() -> None:
    server = _Server(
        _serving(_page(_posting("Rust Engineer"), _posting("Go Engineer")))
    )

    with pytest.raises(UnsupportedUrlError) as caught:
        _resolve(_PAGE_URL, server)

    assert caught.value.reason == "no hiring system, job posting or feed found"
    assert len(server.page_requests) == 1


def test_feed_detected() -> None:
    server = _Server(_serving(_fixture("rss", "golang.xml")))

    resolution = _resolve(_FEED_URL, server, company="Golang Projects")

    assert resolution.ref == SourceRef(SourceKind.FEED, manual_key(_FEED_URL))
    assert resolution.target_url == _FEED_URL
    assert resolution.batch is not None
    assert resolution.batch.drafts
    assert {draft.company for draft in resolution.batch.drafts} == {"Golang Projects"}
    assert len(server.page_requests) == 1


def test_feed_without_items_is_refused() -> None:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b"</channel></rss>"
    )
    server = _Server(_serving(body))

    with pytest.raises(UnsupportedUrlError) as caught:
        _resolve(_FEED_URL, server)

    assert caught.value.url == _FEED_URL
    assert caught.value.reason == "no jobs; cannot verify the company"
    assert len(server.page_requests) == 1


def test_feed_keeps_the_extras_of_its_items() -> None:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><item><title>A</title>'
        b"<link>https://x.test/1</link><category>go</category></item></channel></rss>"
    )

    resolution = _resolve(_FEED_URL, _Server(_serving(body)))

    assert resolution.batch is not None
    assert json.loads(resolution.batch.drafts[0].raw)["extras"] == {"category": "go"}


def test_rejected_job_page_naming_another_company_is_a_mismatch() -> None:
    posting = {**_posting("Eng"), "datePosted": "not a date"}
    posting["hiringOrganization"] = {"@type": "Organization", "name": "Other Corp"}
    server = _Server(_serving(_page(posting)))

    with pytest.raises(IdentityMismatchError):
        _resolve(_PAGE_URL, server, company="Ramp")

    assert len(server.page_requests) == 1


def test_unsupported_rejected() -> None:
    server = _Server(_serving(b"<html><body><p>Hello</p></body></html>"))

    with pytest.raises(UnsupportedUrlError) as caught:
        _resolve(_PAGE_URL, server)

    assert caught.value.url == _PAGE_URL
    assert caught.value.reason == "no hiring system, job posting or feed found"


def test_robots_applies() -> None:
    server = _Server(
        _serving(_fixture("jsonld", "jazzhr.html")),
        robots="User-agent: *\nDisallow: /jobs/\n",
    )

    with pytest.raises(RobotsDisallowedError):
        _resolve(_PAGE_URL, server)

    assert server.page_requests == []


def test_verify_returns_the_batch() -> None:
    body = _fixture("greenhouse", "tailscale_jobs_pay.json")
    source = GreenhouseBoard(parse_source_key("tailscale"), company="Tailscale")
    server = _Server(_serving(body))

    batch = _verify(source, server)

    assert len(batch.drafts) == 2
    assert len(server.page_requests) == 1


def test_verify_refuses_empty_board() -> None:
    source = LeverSite(parse_source_key("ghost"), company="Ghost")
    server = _Server(_serving(b"[]"))

    with pytest.raises(UnsupportedUrlError) as caught:
        _verify(source, server)

    assert caught.value.url == "lever:ghost"
    assert caught.value.reason == "no jobs; cannot verify the company"


def test_verify_identity_mismatch_propagates() -> None:
    body = _fixture("greenhouse", "tailscale_jobs_pay.json")
    source = GreenhouseBoard(parse_source_key("tailscale"), company="Ramp")
    server = _Server(_serving(body))

    with pytest.raises(IdentityMismatchError):
        _verify(source, server)


def test_verify_parse_error_propagates() -> None:
    source = LeverSite(parse_source_key("ghost"), company="Ghost")
    server = _Server(_serving(b"not json"))

    with pytest.raises(ParseError):
        _verify(source, server)
