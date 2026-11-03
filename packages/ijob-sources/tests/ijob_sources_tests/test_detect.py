import asyncio
import json
from collections.abc import AsyncIterator, Callable
from importlib.resources import files
from typing import Any, override

import httpx
import pytest
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_sources.collect import Source, SourceBatch
from ijob_sources.detect import (
    BOARD_HOSTS,
    Resolution,
    board_host,
    board_ref,
    resolve_url,
    verify_new_source,
)
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    IdentityMismatchError,
    ParseError,
    RobotsDisallowedError,
    SourceNotFoundError,
    UnrecognizedPageError,
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


def test_board_hosts_match_board_ref() -> None:
    assert len(BOARD_HOSTS) == 6
    for host, kind in BOARD_HOSTS.items():
        assert board_ref(f"https://{host}/acme/jobs/1") == _ref(kind, "acme")


def test_board_ref_is_none_off_the_boards() -> None:
    assert board_ref("https://example.com/acme") is None
    assert board_ref("https://jobs.lever.co.evil.com/acme") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://jobsxlever.co/acme",
        "https://jobs-lever.co/acme",
        "https://job_boards.greenhouse.io/acme",
        "https://job-boardsxgreenhouse.io/acme",
        "https://boards.greenhouse.io.evil.test/acme",
        "https://job\u017f.lever.co/acme",
        "https://x.jobs.lever.co/acme",
    ],
)
def test_a_look_alike_host_is_no_board(url: str) -> None:
    assert board_ref(url) is None
    assert board_host(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co./acme",
        "https://u@jobs.lever.co/acme",
        "https://u:pw@JOBS.lever.co:443/acme",
        "https://jobs.lever.co:443/acme",
    ],
)
def test_the_host_decides_not_its_spelling(url: str) -> None:
    assert board_ref(url) == _ref(SourceKind.LEVER, "acme")
    assert board_host(url) == "jobs.lever.co"


def test_board_host_names_a_board_host_without_an_account() -> None:
    assert board_host("https://jobs.lever.co/") == "jobs.lever.co"
    assert board_host("https://boards.greenhouse.io/embed/job_board?for=x") == (
        "boards.greenhouse.io"
    )
    assert board_ref("https://jobs.lever.co/") is None
    assert board_host("ftp://jobs.lever.co/acme") is None
    assert board_host("not a url") is None
    assert board_host("https://example.com/") is None


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


def test_unrecognized_page_is_distinct() -> None:
    server = _Server(_serving(b"<html><body><p>Hello</p></body></html>"))

    with pytest.raises(UnrecognizedPageError) as caught:
        _resolve(_PAGE_URL, server)

    assert isinstance(caught.value, UnsupportedUrlError)
    assert caught.value.url == _PAGE_URL
    assert str(caught.value) == (
        f"cannot use {_PAGE_URL}: no hiring system, job posting or feed found"
    )


def test_empty_feed_is_not_unrecognized() -> None:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b"</channel></rss>"
    )
    server = _Server(_serving(body))

    with pytest.raises(UnsupportedUrlError) as caught:
        _resolve(_FEED_URL, server)

    assert not isinstance(caught.value, UnrecognizedPageError)


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


def _redirecting(
    moves: dict[str, str], body: bytes, content_type: str = "text/html"
) -> _Handler:
    """Redirect each URL in `moves` to its target; any other URL answers `body`."""

    def handler(request: httpx.Request) -> httpx.Response:
        target = moves.get(str(request.url))
        if target is not None:
            return httpx.Response(301, headers={"Location": target})
        return httpx.Response(200, content=body, headers={"Content-Type": content_type})

    return handler


@pytest.mark.parametrize(
    ("given", "final"),
    [
        (
            "https://careers.example.test/careers",
            "https://careers.example.test/careers/",
        ),
        (
            "http://careers.example.test/careers/",
            "https://careers.example.test/careers/",
        ),
    ],
    ids=["trailing-slash", "https-upgrade"],
)
def test_jobposting_redirect_is_followed_and_the_final_address_kept(
    given: str, final: str
) -> None:
    server = _Server(_redirecting({given: final}, _fixture("jsonld", "jazzhr.html")))

    resolution = _resolve(given, server, company="Eclipse Foundation")

    assert resolution.ref == SourceRef(SourceKind.JOBPOSTING, manual_key(final))
    assert resolution.target_url == final
    assert resolution.batch is not None
    assert resolution.batch.drafts[0].url != given
    assert [str(request.url) for request in server.page_requests] == [given, final]


def test_feed_redirect_keeps_the_final_address() -> None:
    final = "https://feed.example.test/rss.xml"
    given = "http://feed.example.test/rss"
    server = _Server(
        _redirecting(
            {given: final}, _fixture("rss", "golang.xml"), "application/rss+xml"
        )
    )

    resolution = _resolve(given, server, company="Golang Projects")

    assert resolution.ref == SourceRef(SourceKind.FEED, manual_key(final))
    assert resolution.target_url == final
    assert resolution.batch is not None


def test_redirect_to_a_hiring_board_resolves_to_that_board() -> None:
    given = "https://careers.example.test/join"
    board = "https://job-boards.greenhouse.io/tailscale?gh_src=x"
    server = _Server(_redirecting({given: board}, b"<html></html>"))

    resolution = _resolve(given, server, company="Tailscale")

    assert resolution == Resolution(
        ref=_ref(SourceKind.GREENHOUSE, "tailscale"), target_url=None, batch=None
    )


def test_unrecognized_page_after_a_redirect_keeps_the_url_given_and_the_page() -> None:
    given = "http://careers.example.test/team"
    final = "https://careers.example.test/team/"
    body = b"<html><body><p>Hello</p></body></html>"
    server = _Server(_redirecting({given: final}, body))

    with pytest.raises(UnrecognizedPageError) as caught:
        _resolve(given, server)

    assert caught.value.url == given
    assert str(caught.value) == (
        f"cannot use {given}: no hiring system, job posting or feed found"
    )
    page = caught.value.page
    assert page is not None
    assert page.body == body
    assert page.content_type == "text/html"
    assert page.final_url == final


def test_unrecognized_page_without_a_redirect_keeps_the_page() -> None:
    server = _Server(_redirecting({}, b"<p>Hello</p>", "text/html; charset=utf-8"))

    with pytest.raises(UnrecognizedPageError) as caught:
        _resolve(_PAGE_URL, server)

    page = caught.value.page
    assert page is not None
    assert page.final_url == _PAGE_URL
    assert page.content_type == "text/html; charset=utf-8"


_BOARD_HOP = "https://jobs.lever.co/acme"


def _redirecting_to_a_board_that_answers(given: str, status: int) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == given:
            return httpx.Response(301, headers={"Location": _BOARD_HOP})
        return httpx.Response(status)

    return handler


@pytest.mark.parametrize("status", [403, 404])
def test_redirect_to_a_board_whose_page_cannot_be_read_still_names_the_board(
    status: int,
) -> None:
    given = "https://careers.example.test/join"
    server = _Server(_redirecting_to_a_board_that_answers(given, status))

    resolution = _resolve(given, server, company="Acme")

    assert resolution == Resolution(
        ref=_ref(SourceKind.LEVER, "acme"), target_url=None, batch=None
    )
    assert [str(request.url) for request in server.page_requests] == [given, _BOARD_HOP]


def test_redirect_to_a_board_that_robots_txt_disallows_still_names_the_board() -> None:
    given = "https://careers.example.test/join"
    server = _Server(
        _redirecting({given: _BOARD_HOP}, b""),
        robots="User-agent: *\nDisallow: /acme\n",
    )

    resolution = _resolve(given, server, company="Acme")

    assert resolution.ref == _ref(SourceKind.LEVER, "acme")
    assert [str(request.url) for request in server.page_requests] == [given]


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, HttpStatusError), (404, SourceNotFoundError)],
)
def test_redirect_to_a_page_that_is_no_board_keeps_the_error(
    status: int, error: type[HttpStatusError]
) -> None:
    given = "https://careers.example.test/join"
    hop = "https://careers.example.test/team"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == given:
            return httpx.Response(301, headers={"Location": hop})
        return httpx.Response(status)

    with pytest.raises(error) as caught:
        _resolve(given, _Server(handler))

    assert caught.value.url == hop


def test_redirect_to_a_page_that_robots_txt_disallows_keeps_the_error() -> None:
    given = "https://careers.example.test/join"
    server = _Server(
        _redirecting({given: "https://careers.example.test/team"}, b""),
        robots="User-agent: *\nDisallow: /team\n",
    )

    with pytest.raises(RobotsDisallowedError):
        _resolve(given, server)


def test_a_given_page_that_fails_keeps_its_error_even_on_a_known_system() -> None:
    server = _Server(lambda _request: httpx.Response(404))

    with pytest.raises(SourceNotFoundError) as caught:
        _resolve("https://careers.example.test/join", server)

    assert caught.value.url == "https://careers.example.test/join"


@pytest.mark.parametrize(
    "given",
    [
        "https://Feed.Example.test/rss.xml",
        "https://feed.example.test:443/rss.xml",
        "https://feed.example.test/my feed.xml",
        "https://feed.example.test/caf\u00e9.xml",
    ],
)
def test_a_url_without_a_redirect_is_kept_as_given(given: str) -> None:
    server = _Server(_serving(_fixture("rss", "golang.xml")))

    resolution = _resolve(given, server, company="Golang Projects")

    assert resolution.ref == SourceRef(SourceKind.FEED, manual_key(given))
    assert resolution.target_url == given


@pytest.mark.parametrize(
    "url",
    ["foo", "", "https://", "//a.example/x", "ftp://a.example/x", "mailto:a@b.c"],
)
def test_a_url_that_is_not_an_http_address_is_refused_before_any_request(
    url: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with pytest.raises(UnsupportedUrlError) as caught:
        _resolve(url, _Server(handler))

    assert caught.value.url == url
    assert caught.value.reason == "not an http or https address"
    assert requests == []


def test_a_page_over_5_mib_is_refused_after_reading_only_that_much() -> None:
    chunks_read: list[int] = []

    class _Page(httpx.AsyncByteStream):
        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                chunks_read.append(1)
                yield b" " * 1_048_576

    class _Transport(httpx.AsyncBaseTransport):
        @override
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, stream=_Page())

    async def run() -> None:
        fake_time = _FakeTime()
        async with PoliteClient(
            transport=_Transport(), clock=fake_time.clock, sleep=fake_time.sleep
        ) as client:
            await resolve_url(_PAGE_URL, company="Acme", client=client)

    with pytest.raises(UnsupportedUrlError) as caught:
        asyncio.run(run())

    assert caught.value.url == _PAGE_URL
    assert caught.value.reason == "the page is larger than 5 MiB"
    assert len(chunks_read) == 6


@pytest.mark.parametrize("url", ["http://[bad/careers", "http://[::1/careers"])
def test_malformed_url_is_unsupported_and_nothing_is_fetched(url: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with pytest.raises(UnsupportedUrlError) as caught:
        _resolve(url, _Server(handler))

    assert caught.value.url == url
    assert not isinstance(caught.value, UnrecognizedPageError)
    assert requests == []


def test_unreachable_host_is_a_fetch_error_not_a_robots_refusal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        msg = "name or service not known"
        raise httpx.ConnectError(msg)

    async def run() -> None:
        fake_time = _FakeTime()
        async with PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        ) as client:
            await resolve_url(_PAGE_URL, company="Acme", client=client)

    with pytest.raises(FetchError) as caught:
        asyncio.run(run())

    assert caught.value.url == _PAGE_URL
    assert "name or service not known" in caught.value.reason
    assert not isinstance(caught.value, RobotsDisallowedError)
