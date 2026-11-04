import asyncio
import email.utils
import gzip
import heapq
import importlib.metadata
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import override

import httpx
import pytest
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    SourceNotFoundError,
    SourcesError,
)
from ijob_sources.polite_http import (
    MAX_BODY_BYTES,
    TOO_LARGE_REASON,
    USER_AGENT,
    CachedResponse,
    HostPacing,
    PoliteClient,
)
from ijob_sources.robots import PARSE_LIMIT_BYTES

_NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
_BOARD = "https://boards.example.test/v1/boards/x/jobs"

type _Handler = Callable[[httpx.Request], httpx.Response]


class _FakeTime:
    """A monotonic clock and a sleep that records each wait and advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def _client(handler: _Handler, fake_time: _FakeTime) -> PoliteClient:
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        now=lambda: _NOW,
        sleep=fake_time.sleep,
    )


def _ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="ok")


async def _send(
    client: PoliteClient, url: str, *, follow_redirects: bool = False
) -> httpx.Response:
    async with client:
        return await client._send(url, follow_redirects=follow_redirects)


def test_user_agent_header_sent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200)

    asyncio.run(_send(_client(handler, _FakeTime()), _BOARD))
    version = importlib.metadata.version("ijob-sources")
    expected = f"ijob/{version} (+https://github.com/rstlix0x0/ijob)"
    assert expected == USER_AGENT
    assert seen == [expected]


def test_same_host_requests_are_paced() -> None:
    fake_time = _FakeTime()
    client = _client(_ok, fake_time)

    async def run() -> None:
        async with client:
            await client._send(_BOARD, follow_redirects=False)
            fake_time.now += 0.2
            await client._send(_BOARD, follow_redirects=False)

    asyncio.run(run())
    assert fake_time.waits == [0.8]


def test_different_hosts_are_not_paced() -> None:
    fake_time = _FakeTime()
    client = _client(_ok, fake_time)

    async def run() -> None:
        async with client:
            await client._send(_BOARD, follow_redirects=False)
            await client._send(
                "https://other.example.test/jobs", follow_redirects=False
            )

    asyncio.run(run())
    assert fake_time.waits == []


def test_transport_error_becomes_fetch_error() -> None:
    original = httpx.ConnectError("connection refused")

    def handler(_: httpx.Request) -> httpx.Response:
        raise original

    with pytest.raises(FetchError) as caught:
        asyncio.run(_send(_client(handler, _FakeTime()), _BOARD))
    assert caught.value.__cause__ is original
    assert caught.value.url == _BOARD
    assert "connection refused" in caught.value.reason


def test_too_many_redirects_becomes_fetch_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(301, headers={"Location": _BOARD})

    with pytest.raises(FetchError) as caught:
        asyncio.run(_send(_client(handler, _FakeTime()), _BOARD, follow_redirects=True))
    assert isinstance(caught.value.__cause__, httpx.TooManyRedirects)
    assert len(requests) == 6


def test_decoding_error_becomes_fetch_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=b"not gzip"
        )

    with pytest.raises(FetchError) as caught:
        asyncio.run(_send(_client(handler, _FakeTime()), _BOARD))
    assert isinstance(caught.value.__cause__, httpx.DecodingError)


def test_empty_error_text_falls_back_to_class_name() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    with pytest.raises(FetchError) as caught:
        asyncio.run(_send(_client(handler, _FakeTime()), _BOARD))
    assert caught.value.reason == "ReadTimeout"


def test_redirect_not_followed_by_default() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(301, headers={"Location": "https://elsewhere.test/"})

    response = asyncio.run(_send(_client(handler, _FakeTime()), _BOARD))
    assert response.status_code == 301
    assert len(requests) == 1


class _Site:
    """A transport handler: a robots.txt body and a queue of board replies.

    The last board reply repeats once the queue runs out.
    """

    def __init__(self, *replies: httpx.Response, robots: str = "User-agent: *") -> None:
        self._replies = replies
        self._robots = robots
        self.board_requests = 0
        self.paths: list[str] = []
        self.robots_headers: list[httpx.Headers] = []
        self.board_headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            self.robots_headers.append(request.headers)
            return httpx.Response(200, text=self._robots)
        self.board_headers.append(request.headers)
        reply = self._replies[min(self.board_requests, len(self._replies) - 1)]
        self.board_requests += 1
        return reply


async def _get(client: PoliteClient, url: str = _BOARD) -> httpx.Response:
    async with client:
        return await client.get(url)


def test_get_returns_2xx_response() -> None:
    fake_time = _FakeTime()
    response = asyncio.run(
        _get(_client(_Site(httpx.Response(200, text="body")), fake_time))
    )
    assert (response.status_code, response.text) == (200, "body")
    assert fake_time.waits == [1.0]


def test_get_refuses_robots_disallowed_url() -> None:
    site = _Site(httpx.Response(200), robots="User-agent: *\nDisallow: /")
    with pytest.raises(RobotsDisallowedError) as caught:
        asyncio.run(_get(_client(site, _FakeTime())))
    assert caught.value.url == _BOARD
    assert site.paths == ["/robots.txt"]


def test_get_cannot_read_robots_that_redirects_forever() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": str(request.url)})

    with pytest.raises(FetchError) as caught:
        asyncio.run(_get(_client(handler, _FakeTime())))
    assert caught.value.url == _BOARD
    assert caught.value.reason.startswith("robots.txt could not be read: ")
    assert not isinstance(caught.value, RobotsDisallowedError)


def test_unreachable_host_says_robots_txt_could_not_be_read() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        msg = "name not known"
        raise httpx.ConnectError(msg)

    async def run() -> None:
        async with _client(handler, _FakeTime()) as client:
            for _ in range(2):
                with pytest.raises(FetchError) as caught:
                    await client.get(_BOARD)
                assert caught.value.url == _BOARD
                assert caught.value.reason == (
                    "robots.txt could not be read: name not known"
                )
                assert "disallows" not in str(caught.value)

    asyncio.run(run())
    assert paths == ["/robots.txt", "/robots.txt"]


def test_robots_txt_that_could_not_be_read_is_tried_again() -> None:
    failures = [True]
    site = _Site(httpx.Response(200, text="board"))

    def handler(request: httpx.Request) -> httpx.Response:
        if failures and request.url.path == "/robots.txt":
            failures.pop()
            raise httpx.ConnectError("blip")
        return site(request)

    async def run() -> httpx.Response:
        async with _client(handler, _FakeTime()) as client:
            with pytest.raises(FetchError):
                await client.get(_BOARD)
            return await client.get(_BOARD)

    assert asyncio.run(run()).text == "board"


def test_server_error_for_robots_txt_still_disallows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.path == "/robots.txt" else 200)

    with pytest.raises(RobotsDisallowedError):
        asyncio.run(_get(_client(handler, _FakeTime())))


def test_retry_after_seconds_then_success() -> None:
    fake_time = _FakeTime()
    site = _Site(httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200))
    response = asyncio.run(_get(_client(site, fake_time)))
    assert response.status_code == 200
    assert fake_time.waits == [1.0, 2.0]


def test_retry_after_http_date() -> None:
    fake_time = _FakeTime()
    date = email.utils.format_datetime(_NOW + timedelta(seconds=5), usegmt=True)
    site = _Site(
        httpx.Response(503, headers={"Retry-After": date}), httpx.Response(200)
    )
    response = asyncio.run(_get(_client(site, fake_time)))
    assert response.status_code == 200
    assert fake_time.waits == [1.0, 5.0]


@pytest.mark.parametrize(
    "headers", [{}, {b"Retry-After": b"soon"}, {b"Retry-After": b"\xb2"}]
)
def test_retry_after_missing_or_unparseable_waits_min_interval(
    headers: dict[bytes, bytes],
) -> None:
    fake_time = _FakeTime()
    site = _Site(httpx.Response(429, headers=headers), httpx.Response(200))
    asyncio.run(_get(_client(site, fake_time)))
    assert fake_time.waits == [1.0, 1.0]


def test_retry_after_http_date_without_zone_is_utc() -> None:
    fake_time = _FakeTime()
    site = _Site(
        httpx.Response(429, headers={"Retry-After": "Sat, 31 Oct 2026 12:00:05 -0000"}),
        httpx.Response(200),
    )
    asyncio.run(_get(_client(site, fake_time)))
    assert fake_time.waits == [1.0, 5.0]


def test_retry_after_past_date_waits_zero() -> None:
    fake_time = _FakeTime()
    date = email.utils.format_datetime(_NOW - timedelta(seconds=10), usegmt=True)
    site = _Site(
        httpx.Response(429, headers={"Retry-After": date}), httpx.Response(200)
    )
    asyncio.run(_get(_client(site, fake_time)))
    assert fake_time.waits == [1.0, 0.0, 1.0]


def test_gives_up_after_three_attempts() -> None:
    fake_time = _FakeTime()
    site = _Site(httpx.Response(429, headers={"Retry-After": "2"}))
    with pytest.raises(HttpStatusError) as caught:
        asyncio.run(_get(_client(site, fake_time)))
    assert caught.value.status == 429
    assert site.board_requests == 3
    assert fake_time.waits == [1.0, 2.0, 2.0]


def test_gives_up_on_wait_above_60s() -> None:
    fake_time = _FakeTime()
    site = _Site(httpx.Response(503, headers={"Retry-After": "61"}))
    with pytest.raises(HttpStatusError) as caught:
        asyncio.run(_get(_client(site, fake_time)))
    assert caught.value.status == 503
    assert site.board_requests == 1
    assert fake_time.waits == [1.0]


def test_404_raises_source_not_found() -> None:
    with pytest.raises(SourceNotFoundError):
        asyncio.run(_get(_client(_Site(httpx.Response(404)), _FakeTime())))


@pytest.mark.parametrize("status", [301, 500])
def test_3xx_and_5xx_raise_http_status_error(status: int) -> None:
    fake_time = _FakeTime()
    site = _Site(httpx.Response(status, headers={"Location": "https://x.test/"}))
    with pytest.raises(HttpStatusError) as caught:
        asyncio.run(_get(_client(site, fake_time)))
    assert caught.value.status == status
    assert fake_time.waits == [1.0]


class _DictCache:
    """A `ConditionalCache` backed by a dict."""

    def __init__(self, entries: dict[str, CachedResponse] | None = None) -> None:
        self.entries = dict(entries or {})

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


def _cached_client(
    handler: _Handler, cache: _DictCache, fake_time: _FakeTime
) -> PoliteClient:
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        now=lambda: _NOW,
        sleep=fake_time.sleep,
        cache=cache,
    )


async def _get_conditional(
    client: PoliteClient, url: str = _BOARD, *, conditional: bool = True
) -> httpx.Response:
    async with client:
        return await client.get(url, conditional=conditional)


def test_conditional_sends_both_validators() -> None:
    cache = _DictCache({_BOARD: CachedResponse('W/"x"', "Sat, 01 Aug 2026", b"old")})
    site = _Site(httpx.Response(200, text="new"))
    asyncio.run(_get_conditional(_cached_client(site, cache, _FakeTime())))
    headers = site.board_headers[0]
    assert headers["if-none-match"] == 'W/"x"'
    assert headers["if-modified-since"] == "Sat, 01 Aug 2026"


def test_304_replayed_as_200_from_cache() -> None:
    cache = _DictCache({_BOARD: CachedResponse('"v1"', None, b"cached body")})
    site = _Site(httpx.Response(304))
    response = asyncio.run(_get_conditional(_cached_client(site, cache, _FakeTime())))
    assert (response.status_code, response.content) == (200, b"cached body")
    assert response.request.url == _BOARD


def test_304_without_cache_entry_raises_http_status_error() -> None:
    site = _Site(httpx.Response(304))
    with pytest.raises(HttpStatusError) as caught:
        asyncio.run(_get_conditional(_cached_client(site, _DictCache(), _FakeTime())))
    assert caught.value.status == 304


def test_validated_response_is_stored() -> None:
    cache = _DictCache()
    site = _Site(httpx.Response(200, headers={"ETag": 'W/"x"'}, content=b"body"))
    asyncio.run(_get_conditional(_cached_client(site, cache, _FakeTime())))
    assert cache.entries == {_BOARD: CachedResponse('W/"x"', None, b"body")}

    other = "https://boards.example.test/v1/boards/y/jobs"
    cache = _DictCache()
    site = _Site(
        httpx.Response(200, headers={"Last-Modified": "Sat, 01 Aug 2026"}, content=b"b")
    )
    asyncio.run(_get_conditional(_cached_client(site, cache, _FakeTime()), other))
    assert cache.entries == {other: CachedResponse(None, "Sat, 01 Aug 2026", b"b")}


def test_response_without_validator_not_stored() -> None:
    cache = _DictCache()
    site = _Site(httpx.Response(200, content=b"body"))
    asyncio.run(_get_conditional(_cached_client(site, cache, _FakeTime())))
    assert cache.entries == {}


def test_unconditional_get_ignores_cache() -> None:
    cache = _DictCache({_BOARD: CachedResponse('"v1"', "Sat, 01 Aug 2026", b"old")})
    site = _Site(httpx.Response(200, headers={"ETag": '"v2"'}, content=b"new"))
    asyncio.run(
        _get_conditional(_cached_client(site, cache, _FakeTime()), conditional=False)
    )
    headers = site.board_headers[0]
    assert "if-none-match" not in headers
    assert "if-modified-since" not in headers
    assert cache.entries == {_BOARD: CachedResponse('"v1"', "Sat, 01 Aug 2026", b"old")}


def test_conditional_without_cache_is_unconditional() -> None:
    site = _Site(httpx.Response(200, headers={"ETag": '"v1"'}, content=b"body"))
    fake_time = _FakeTime()
    response = asyncio.run(_get_conditional(_client(site, fake_time)))
    assert (response.status_code, response.content) == (200, b"body")
    headers = site.board_headers[0]
    assert "if-none-match" not in headers
    assert "if-modified-since" not in headers
    assert fake_time.waits == [1.0]


def test_validators_resent_on_retry() -> None:
    cache = _DictCache({_BOARD: CachedResponse('"v1"', None, b"cached body")})
    site = _Site(httpx.Response(503, headers={"Retry-After": "2"}), httpx.Response(304))
    response = asyncio.run(_get_conditional(_cached_client(site, cache, _FakeTime())))
    assert (response.status_code, response.content) == (200, b"cached body")
    assert site.board_requests == 2
    assert [h["if-none-match"] for h in site.board_headers] == ['"v1"', '"v1"']


def test_robots_request_never_conditional() -> None:
    robots_url = "https://boards.example.test/robots.txt"
    cache = _DictCache({robots_url: CachedResponse('"r1"', "Sat, 01 Aug 2026", b"")})
    site = _Site(httpx.Response(200, text="body"))
    asyncio.run(_get_conditional(_cached_client(site, cache, _FakeTime())))
    headers = site.robots_headers[0]
    assert "if-none-match" not in headers
    assert "if-modified-since" not in headers


_WORKABLE = "https://apply.workable.com/api/v1/widget/accounts/x"


async def _get_each(client: PoliteClient, *urls: str) -> None:
    async with client:
        for url in urls:
            await client.get(url)


def _spaced_client(
    site: _Site,
    fake_time: _FakeTime,
    host_intervals: dict[str, float] | None = None,
) -> PoliteClient:
    return PoliteClient(
        transport=httpx.MockTransport(site),
        clock=fake_time.clock,
        now=lambda: _NOW,
        sleep=fake_time.sleep,
        host_intervals=host_intervals,
    )


def test_default_interval_unchanged() -> None:
    fake_time = _FakeTime()
    client = _spaced_client(_Site(httpx.Response(200)), fake_time)
    asyncio.run(_get_each(client, _BOARD, _BOARD))
    assert fake_time.waits == [1.0, 1.0]


def test_crawl_delay_raises_interval() -> None:
    fake_time = _FakeTime()
    site = _Site(httpx.Response(200), robots="User-agent: *\nCrawl-delay: 2")
    asyncio.run(_get_each(_spaced_client(site, fake_time), _BOARD, _BOARD))
    assert fake_time.waits == [2.0, 2.0]


def test_host_override_raises_interval() -> None:
    overrides = {"apply.workable.com": 3.0}
    fake_time = _FakeTime()
    client = _spaced_client(_Site(httpx.Response(200)), fake_time, overrides)
    asyncio.run(_get_each(client, _WORKABLE, _WORKABLE))
    assert fake_time.waits == [3.0, 3.0]

    fake_time = _FakeTime()
    client = _spaced_client(_Site(httpx.Response(200)), fake_time, overrides)
    asyncio.run(_get_each(client, _BOARD, _BOARD))
    assert fake_time.waits == [1.0, 1.0]


def test_strictest_wins() -> None:
    fake_time = _FakeTime()
    site = _Site(httpx.Response(200), robots="User-agent: *\nCrawl-delay: 2")
    client = _spaced_client(site, fake_time, {"apply.workable.com": 3.0})
    asyncio.run(_get_each(client, _WORKABLE, _WORKABLE))
    assert fake_time.waits == [3.0, 3.0]


def test_crawl_delay_above_override_wins() -> None:
    fake_time = _FakeTime()
    site = _Site(httpx.Response(200), robots="User-agent: *\nCrawl-delay: 3")
    client = _spaced_client(site, fake_time, {"apply.workable.com": 2.0})
    asyncio.run(_get_each(client, _WORKABLE, _WORKABLE))
    assert fake_time.waits == [3.0, 3.0]


def test_override_below_min_interval_lowers_it() -> None:
    fake_time = _FakeTime()
    client = _spaced_client(
        _Site(httpx.Response(200)), fake_time, {"apply.workable.com": 0.5}
    )
    asyncio.run(_get_each(client, _WORKABLE, _WORKABLE))
    assert fake_time.waits == [0.5, 0.5]


def test_requests_to_one_host_overlap() -> None:
    both_arrived = asyncio.Event()
    arrived: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        arrived.append(request.url.path)
        if len(arrived) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=30)
        return httpx.Response(200, text="body", request=request)

    async def run() -> list[str]:
        fake_time = _FakeTime()
        async with (
            PoliteClient(
                transport=httpx.MockTransport(handler),
                clock=fake_time.clock,
                now=lambda: _NOW,
                sleep=fake_time.sleep,
            ) as client,
            asyncio.TaskGroup() as group,
        ):
            first = group.create_task(client.get("https://a.example.test/one"))
            second = group.create_task(client.get("https://a.example.test/two"))
        return [first.result().text, second.result().text]

    assert asyncio.run(run()) == ["body", "body"]


def test_host_override_key_is_case_insensitive() -> None:
    fake_time = _FakeTime()
    client = _spaced_client(
        _Site(httpx.Response(200)), fake_time, {"Apply.Workable.com": 3.0}
    )
    asyncio.run(_get_each(client, _WORKABLE, _WORKABLE))
    assert fake_time.waits == [3.0, 3.0]


class _RefusedError(SourcesError):
    """A stub guard's refusal."""


class _Recorder:
    """A request guard that records every URL it sees."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, request: httpx.Request) -> None:
        self.urls.append(str(request.url))


class _Pages:
    """A transport handler serving a fixed table of page replies by URL.

    robots.txt allows everything unless `robots` holds a body for its host.
    """

    def __init__(
        self, pages: dict[str, httpx.Response], robots: dict[str, str] | None = None
    ) -> None:
        self._pages = pages
        self._robots = robots or {}
        self.received: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.received.append(url)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self._robots.get(request.url.host, ""))
        return self._pages[url]


async def _get_following(client: PoliteClient, url: str) -> httpx.Response:
    async with client:
        return await client.get(url, follow_redirects=True)


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


def test_guard_sees_robots_and_page() -> None:
    recorder = _Recorder()
    client = _client(_Site(httpx.Response(200)), _FakeTime())
    client.guard_requests(recorder)
    asyncio.run(_get(client))
    assert recorder.urls == ["https://boards.example.test/robots.txt", _BOARD]


def test_raising_guard_stops_before_sending() -> None:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    async def refuse(_: httpx.Request) -> None:
        raise _RefusedError

    client = _client(handler, _FakeTime())
    client.guard_requests(refuse)
    with pytest.raises(_RefusedError):
        asyncio.run(_get(client))
    assert received == []


def test_follow_redirects_reaches_final_url() -> None:
    pages = _Pages(
        {
            "http://example.com/a": _redirect("/jobs/"),
            "http://example.com/jobs/": httpx.Response(200, text="jobs"),
        }
    )
    response = asyncio.run(
        _get_following(_client(pages, _FakeTime()), "http://example.com/a")
    )
    assert response.text == "jobs"
    assert str(response.url).endswith("/jobs/")


def test_redirect_hop_checks_robots_of_new_origin() -> None:
    pages = _Pages(
        {"http://example.com/a": _redirect("http://other.test/x")},
        robots={"other.test": "User-agent: *\nDisallow: /"},
    )
    with pytest.raises(RobotsDisallowedError) as caught:
        asyncio.run(_get_following(_client(pages, _FakeTime()), "http://example.com/a"))
    assert caught.value.url == "http://other.test/x"
    assert "http://other.test/robots.txt" in pages.received
    assert "http://other.test/x" not in pages.received


def test_guard_refusing_a_hop_stops_it_before_the_transport() -> None:
    pages = _Pages({"http://example.com/a": _redirect("http://10.0.0.1/b")})
    seen: list[str] = []

    async def refuse_private(request: httpx.Request) -> None:
        seen.append(str(request.url))
        if request.url.host == "10.0.0.1":
            raise _RefusedError

    client = _client(pages, _FakeTime())
    client.guard_requests(refuse_private)
    with pytest.raises(_RefusedError):
        asyncio.run(_get_following(client, "http://example.com/a"))
    assert "http://10.0.0.1/robots.txt" in seen
    assert not any("10.0.0.1" in url for url in pages.received)


def test_guard_sees_each_redirect_hop() -> None:
    pages = _Pages(
        {
            "http://example.com/a": _redirect("http://10.0.0.1/b"),
            "http://10.0.0.1/b": httpx.Response(200),
        }
    )
    recorder = _Recorder()
    client = _client(pages, _FakeTime())
    client.guard_requests(recorder)
    asyncio.run(_get_following(client, "http://example.com/a"))
    page_urls = [url for url in recorder.urls if not url.endswith("/robots.txt")]
    assert page_urls == ["http://example.com/a", "http://10.0.0.1/b"]


def test_sixth_redirect_is_fetch_error() -> None:
    pages = _Pages(
        {f"http://example.com/{n}": _redirect(f"/{n + 1}") for n in range(6)}
    )
    with pytest.raises(FetchError) as caught:
        asyncio.run(_get_following(_client(pages, _FakeTime()), "http://example.com/0"))
    assert caught.value.reason == "more than five redirects"
    assert caught.value.url == "http://example.com/5"
    assert "http://example.com/6" not in pages.received


def test_fifth_redirect_is_followed() -> None:
    pages = _Pages(
        {
            **{f"http://example.com/{n}": _redirect(f"/{n + 1}") for n in range(5)},
            "http://example.com/5": httpx.Response(200, text="end"),
        }
    )
    response = asyncio.run(
        _get_following(_client(pages, _FakeTime()), "http://example.com/0")
    )
    assert response.text == "end"


def test_redirect_without_location_is_http_status_error() -> None:
    pages = _Pages({"http://example.com/a": httpx.Response(302)})
    with pytest.raises(HttpStatusError) as caught:
        asyncio.run(_get_following(_client(pages, _FakeTime()), "http://example.com/a"))
    assert caught.value.status == 302


def test_unusable_location_is_fetch_error() -> None:
    pages = _Pages({"http://example.com/a": _redirect("http://example.com:abc/")})
    with pytest.raises(FetchError):
        asyncio.run(_get_following(_client(pages, _FakeTime()), "http://example.com/a"))


@pytest.mark.parametrize("location", ["javascript:alert(1)", "mailto:a@b.c"])
def test_hostless_absolute_location_is_fetch_error(location: str) -> None:
    pages = _Pages({"http://example.com/a": _redirect(location)})
    with pytest.raises(FetchError) as caught:
        asyncio.run(_get_following(_client(pages, _FakeTime()), "http://example.com/a"))
    assert isinstance(caught.value.__cause__, httpx.InvalidURL)


def test_redirect_to_undecodable_host_is_fetch_error() -> None:
    pages = _Pages({"http://example.com/a": _redirect("http://xn--japan-dev.com/x")})
    with pytest.raises(FetchError) as caught:
        asyncio.run(_get_following(_client(pages, _FakeTime()), "http://example.com/a"))
    assert isinstance(caught.value.__cause__, UnicodeError)


def test_undecodable_host_is_a_sources_error() -> None:
    pages = _Pages({})
    with pytest.raises(FetchError):
        asyncio.run(_client(pages, _FakeTime()).get("http://xn--japan-dev.com/x"))


def test_redirect_status_outside_the_followed_set_is_http_status_error() -> None:
    pages = _Pages({"http://example.com/a": _redirect("/b", status=300)})
    with pytest.raises(HttpStatusError) as caught:
        asyncio.run(_get_following(_client(pages, _FakeTime()), "http://example.com/a"))
    assert caught.value.status == 300


def test_redirect_hop_is_paced_and_retried() -> None:
    fake_time = _FakeTime()
    replies = iter(
        [
            _redirect("/b"),
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, text="b"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return next(replies)

    response = asyncio.run(
        _get_following(_client(handler, fake_time), "http://example.com/a")
    )
    assert response.text == "b"
    assert fake_time.waits == [1.0, 1.0, 2.0]


def test_conditional_with_follow_redirects_refused() -> None:
    async def run() -> None:
        async with _client(_ok, _FakeTime()) as client:
            await client.get(_BOARD, conditional=True, follow_redirects=True)

    with pytest.raises(ValueError, match="conditional"):
        asyncio.run(run())


class _RecordingStream(httpx.AsyncByteStream):
    """A response body that counts the chunks read from it and whether it closed."""

    def __init__(
        self, chunks: Sequence[bytes], *, then_raise: Exception | None = None
    ) -> None:
        self._chunks = chunks
        self._then_raise = then_raise
        self.yielded = 0
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.yielded += 1
            yield chunk
        if self._then_raise is not None:
            raise self._then_raise

    @override
    async def aclose(self) -> None:
        self.closed = True


class _StreamingTransport(httpx.AsyncBaseTransport):
    """Serves an empty robots.txt, and every other path from one streamed reply."""

    def __init__(
        self, stream: _RecordingStream, headers: dict[str, str] | None = None
    ) -> None:
        self._stream = stream
        self._headers = headers or {}
        self.requests: list[httpx.Request] = []

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(200, headers=self._headers, stream=self._stream)


def _streaming_client(transport: _StreamingTransport) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=transport, clock=fake_time.clock, sleep=fake_time.sleep
    )


async def _get_limited(
    client: PoliteClient, max_bytes: int | None, url: str = _BOARD
) -> httpx.Response:
    async with client:
        return await client.get(url, max_bytes=max_bytes)


def test_max_bytes_stops_reading_once_the_limit_is_passed() -> None:
    stream = _RecordingStream([b"x" * 50] * 10)
    transport = _StreamingTransport(stream)

    with pytest.raises(ResponseTooLargeError) as caught:
        asyncio.run(_get_limited(_streaming_client(transport), 120))

    assert stream.yielded == 3
    assert stream.closed
    assert caught.value.url == _BOARD
    assert caught.value.limit == 120
    assert _BOARD in str(caught.value)
    assert "120" in str(caught.value)


def test_max_bytes_counts_the_decoded_body() -> None:
    body = b"a" * 100_000
    compressed = gzip.compress(body)
    assert len(compressed) < 1000 < len(body)
    chunks = [compressed[i : i + 8] for i in range(0, len(compressed), 8)]
    stream = _RecordingStream(chunks)
    transport = _StreamingTransport(stream, {"Content-Encoding": "gzip"})

    with pytest.raises(ResponseTooLargeError):
        asyncio.run(_get_limited(_streaming_client(transport), 1000))

    assert stream.yielded < len(chunks)
    assert stream.closed


def test_body_exactly_at_the_limit_is_returned() -> None:
    stream = _RecordingStream([b"x" * 50, b"y" * 50])

    response = asyncio.run(
        _get_limited(_streaming_client(_StreamingTransport(stream)), 100)
    )

    assert response.content == b"x" * 50 + b"y" * 50


def test_body_under_the_limit_round_trips_like_an_unlimited_get() -> None:
    body = "caf\u00e9 jobs".encode()
    compressed = gzip.compress(body)
    headers = {
        "Content-Encoding": "gzip",
        "Content-Length": str(len(compressed)),
        "Content-Type": "text/plain; charset=utf-8",
        "ETag": '"v1"',
    }
    transport = _StreamingTransport(
        _RecordingStream([compressed[:10], compressed[10:]]), headers
    )

    limited = asyncio.run(_get_limited(_streaming_client(transport), 1000))
    plain = asyncio.run(
        _get_limited(
            _streaming_client(
                _StreamingTransport(_RecordingStream([compressed]), headers)
            ),
            None,
        )
    )

    assert limited.content == body
    assert limited.text == "caf\u00e9 jobs"
    assert limited.status_code == 200
    assert limited.url == httpx.URL(_BOARD)
    assert limited.request.url == httpx.URL(_BOARD)
    assert limited.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert limited.headers["ETag"] == '"v1"'
    assert "Content-Encoding" not in limited.headers
    assert limited.headers["Content-Length"] == str(len(body))
    assert limited.content == plain.content
    assert limited.text == plain.text


def test_max_bytes_keeps_the_final_url_of_followed_redirects() -> None:
    pages = _Pages(
        {
            "http://example.com/a": _redirect("/b/"),
            "http://example.com/b/": httpx.Response(200, text="jobs"),
        }
    )

    async def run() -> httpx.Response:
        async with _client(pages, _FakeTime()) as client:
            return await client.get(
                "http://example.com/a", follow_redirects=True, max_bytes=100
            )

    response = asyncio.run(run())
    assert response.text == "jobs"
    assert str(response.url) == "http://example.com/b/"


def test_read_error_while_streaming_is_a_fetch_error() -> None:
    error = httpx.ReadError("connection reset")
    stream = _RecordingStream([b"x" * 10], then_raise=error)

    with pytest.raises(FetchError) as caught:
        asyncio.run(_get_limited(_streaming_client(_StreamingTransport(stream)), 100))

    assert not isinstance(caught.value, ResponseTooLargeError)
    assert caught.value.__cause__ is error
    assert "connection reset" in caught.value.reason
    assert stream.closed


def test_decoding_error_while_streaming_is_a_fetch_error() -> None:
    stream = _RecordingStream([b"not gzip"])
    transport = _StreamingTransport(stream, {"Content-Encoding": "gzip"})

    with pytest.raises(FetchError) as caught:
        asyncio.run(_get_limited(_streaming_client(transport), 100))

    assert isinstance(caught.value.__cause__, httpx.DecodingError)


def test_without_max_bytes_the_whole_body_is_read() -> None:
    stream = _RecordingStream([b"x" * 50] * 10)

    response = asyncio.run(
        _get_limited(_streaming_client(_StreamingTransport(stream)), None)
    )

    assert len(response.content) == 500
    assert stream.yielded == 10


def test_conditional_get_with_max_bytes_still_replays_a_304() -> None:
    cache = _DictCache()
    cache.store(_BOARD, CachedResponse('"v1"', None, b"stored"))
    fake_time = _FakeTime()
    client = PoliteClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200 if request.url.path == "/robots.txt" else 304
            )
        ),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        cache=cache,
    )

    async def run() -> httpx.Response:
        async with client:
            return await client.get(_BOARD, conditional=True, max_bytes=100)

    assert asyncio.run(run()).content == b"stored"


class _PathTransport(httpx.AsyncBaseTransport):
    """Answers each request from a per-path reply factory, and records the paths."""

    def __init__(self, replies: dict[str, Callable[[], httpx.Response]]) -> None:
        self._replies = replies
        self.paths: list[str] = []

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        return self._replies[request.url.path]()


def _path_client(transport: _PathTransport) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=transport, clock=fake_time.clock, sleep=fake_time.sleep
    )


_RULES = b"User-agent: *\nDisallow: /secret\n"


def test_robots_txt_over_the_size_limit_keeps_its_prefix() -> None:
    first = _RULES + b"#" * (PARSE_LIMIT_BYTES - len(_RULES))
    robots = _RecordingStream([first] + [b"Disallow: /\n" * 1000] * 50)
    transport = _PathTransport(
        {
            "/robots.txt": lambda: httpx.Response(200, stream=robots),
            "/v1/boards/x/jobs": lambda: httpx.Response(200, text="board"),
            "/secret": lambda: httpx.Response(200, text="secret"),
        }
    )

    async def run() -> tuple[str, type[Exception] | None]:
        async with _path_client(transport) as client:
            allowed = (await client.get(_BOARD)).text
            try:
                await client.get("https://boards.example.test/secret")
            except RobotsDisallowedError as error:
                return allowed, type(error)
            return allowed, None

    assert asyncio.run(run()) == ("board", RobotsDisallowedError)
    assert robots.yielded == 1
    assert robots.closed
    assert transport.paths.count("/robots.txt") == 1


def test_robots_txt_prefix_is_cut_inside_a_chunk() -> None:
    robots = _RecordingStream([_RULES + b"#" * 600_000, b"x"])
    transport = _PathTransport(
        {
            "/robots.txt": lambda: httpx.Response(200, stream=robots),
            "/v1/boards/x/jobs": lambda: httpx.Response(200, text="board"),
        }
    )

    assert asyncio.run(_get(_path_client(transport))).text == "board"
    assert robots.yielded == 1


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, SourceNotFoundError), (500, HttpStatusError)],
)
def test_error_page_over_the_limit_reports_its_status(
    status: int, error: type[HttpStatusError]
) -> None:
    stream = _RecordingStream([b"x" * 50] * 10)
    transport = _PathTransport(
        {
            "/robots.txt": lambda: httpx.Response(200, text=""),
            "/v1/boards/x/jobs": lambda: httpx.Response(status, stream=stream),
        }
    )

    with pytest.raises(error) as caught:
        asyncio.run(_get_limited(_path_client(transport), 120))

    assert not isinstance(caught.value, ResponseTooLargeError)
    assert caught.value.status == status
    assert stream.yielded == 3
    assert stream.closed


def test_robots_txt_server_error_over_the_limit_still_disallows() -> None:
    robots = _RecordingStream([b"x" * 1_048_576] * 6)
    transport = _PathTransport(
        {
            "/robots.txt": lambda: httpx.Response(503, stream=robots),
            "/v1/boards/x/jobs": lambda: httpx.Response(200, text="board"),
        }
    )

    with pytest.raises(RobotsDisallowedError):
        asyncio.run(_get(_path_client(transport)))

    assert robots.yielded == 1
    assert transport.paths == ["/robots.txt"]


def test_robots_txt_redirect_body_is_not_read_in_full() -> None:
    moved = _RecordingStream([b"x" * 1_048_576] * 200)
    transport = _PathTransport(
        {
            "/robots.txt": lambda: httpx.Response(
                301, headers={"Location": "/moved/robots.txt"}, stream=moved
            ),
            "/moved/robots.txt": lambda: httpx.Response(
                200, text="User-agent: *\nDisallow: /secret\n"
            ),
            "/v1/boards/x/jobs": lambda: httpx.Response(200, text="board"),
        }
    )

    assert asyncio.run(_get(_path_client(transport))).text == "board"
    assert moved.yielded == 1
    assert moved.closed


def _table(replies: dict[str, httpx.Response]) -> _Handler:
    """Answer each request by its full URL, robots.txt included."""
    return lambda request: replies[str(request.url)]


def test_robots_txt_redirect_is_followed_and_guarded() -> None:
    guard = _Recorder()
    client = _client(
        _table(
            {
                "https://boards.example.test/robots.txt": _redirect(
                    "https://other.example.test/robots.txt", status=301
                ),
                "https://other.example.test/robots.txt": httpx.Response(200),
                _BOARD: httpx.Response(200, text="board"),
            }
        ),
        _FakeTime(),
    )
    client.guard_requests(guard)

    assert asyncio.run(_get(client)).text == "board"
    assert guard.urls == [
        "https://boards.example.test/robots.txt",
        "https://other.example.test/robots.txt",
        _BOARD,
    ]


def test_robots_txt_redirect_to_a_disallowing_robots_txt_disallows() -> None:
    handler = _table(
        {
            "https://boards.example.test/robots.txt": _redirect("/elsewhere"),
            "https://boards.example.test/elsewhere": httpx.Response(
                200, text="User-agent: *\nDisallow: /\n"
            ),
        }
    )

    with pytest.raises(RobotsDisallowedError):
        asyncio.run(_get(_client(handler, _FakeTime())))


def test_robots_txt_redirect_hops_are_paced() -> None:
    handler = _table(
        {
            "https://boards.example.test/robots.txt": _redirect("/elsewhere"),
            "https://boards.example.test/elsewhere": httpx.Response(200, text=""),
            _BOARD: httpx.Response(200, text="board"),
        }
    )
    fake_time = _FakeTime()

    asyncio.run(_get(_client(handler, fake_time)))

    assert fake_time.waits == [1.0, 1.0]


def test_robots_txt_with_a_sixth_redirect_could_not_be_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": str(request.url) + "x"})

    with pytest.raises(FetchError) as caught:
        asyncio.run(_get(_client(handler, _FakeTime())))

    assert caught.value.reason == (
        "robots.txt could not be read: more than five redirects"
    )


def test_robots_txt_redirect_to_an_unusable_location_could_not_be_read() -> None:
    handler = _table(
        {"https://boards.example.test/robots.txt": _redirect("javascript:alert(1)")}
    )

    with pytest.raises(FetchError) as caught:
        asyncio.run(_get(_client(handler, _FakeTime())))

    assert caught.value.reason.startswith("robots.txt could not be read: ")


def test_non_ascii_response_header_is_kept_by_a_limited_get() -> None:
    cookie = (b"Set-Cookie", "name=J\u00fcrgen".encode())
    site = _Site(httpx.Response(200, headers=[cookie], text="body"))

    response = asyncio.run(_get_limited(_client(site, _FakeTime()), 100))

    assert response.text == "body"
    assert cookie in response.headers.raw


def test_non_ascii_response_header_of_robots_txt_is_accepted() -> None:
    owner = (b"X-Owner", "J\u00fcrgen".encode())
    transport = _PathTransport(
        {
            "/robots.txt": lambda: httpx.Response(200, headers=[owner], text=""),
            "/v1/boards/x/jobs": lambda: httpx.Response(200, text="board"),
        }
    )

    assert asyncio.run(_get(_path_client(transport))).text == "board"


def test_raw_utf8_location_is_followed_by_a_limited_get() -> None:
    moved = (b"Location", "/\u63a1\u7528/".encode())
    pages = _Pages(
        {
            "http://example.com/a": httpx.Response(302, headers=[moved]),
            "http://example.com/%E6%8E%A1%E7%94%A8/": httpx.Response(200, text="jobs"),
        }
    )

    async def run() -> httpx.Response:
        async with _client(pages, _FakeTime()) as client:
            return await client.get(
                "http://example.com/a", follow_redirects=True, max_bytes=100
            )

    assert asyncio.run(run()).text == "jobs"


def test_limited_get_drops_content_encoding_and_recomputes_content_length() -> None:
    body = b"jobs"
    compressed = gzip.compress(body)
    headers = {"Content-Encoding": "gzip", "Content-Length": str(len(compressed))}
    transport = _StreamingTransport(_RecordingStream([compressed]), headers)

    response = asyncio.run(_get_limited(_streaming_client(transport), 100))

    assert "Content-Encoding" not in response.headers
    assert response.headers["Content-Length"] == str(len(body))


def test_too_large_reason_states_the_body_limit() -> None:
    assert MAX_BODY_BYTES == 5 * 1024 * 1024
    assert TOO_LARGE_REASON == "the page is larger than 5 MiB"


class _VirtualTime:
    """A clock that only moves when every task is asleep, to the next wake-up."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        wake = self.now + seconds
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (wake, next(self._order), future))
        await future

    def sleeping(self) -> bool:
        """Say whether a task is waiting on this clock."""
        return any(not future.done() for _, _, future in self._sleepers)

    async def run(self, *coroutines: Awaitable[object]) -> None:
        tasks = [asyncio.ensure_future(coroutine) for coroutine in coroutines]
        while not all(task.done() for task in tasks):
            for _ in range(100):
                await asyncio.sleep(0)
            while self._sleepers:
                wake, _, future = heapq.heappop(self._sleepers)
                if not future.done():
                    self.now = max(self.now, wake)
                    future.set_result(None)
                    break
        for task in tasks:
            task.result()


def _pacing_pair(
    virtual: _VirtualTime, starts: list[tuple[str, float]]
) -> tuple[PoliteClient, PoliteClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        starts.append((request.url.host, virtual.now))
        return httpx.Response(200, text="")

    def build() -> PoliteClient:
        return PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=virtual.clock,
            sleep=virtual.sleep,
        )

    pacing = HostPacing()
    first, second = build(), build()
    first.share_pacing(pacing)
    second.share_pacing(pacing)
    return first, second


def _gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in itertools.pairwise(sorted(times))]


def test_clients_sharing_pacing_wait_between_requests_to_one_host() -> None:
    virtual = _VirtualTime()
    starts: list[tuple[str, float]] = []
    first, second = _pacing_pair(virtual, starts)

    async def run() -> None:
        async with first, second:
            await first.get(_BOARD)
            await second.get(_BOARD)

    asyncio.run(virtual.run(run()))

    assert len(starts) == 4
    assert all(gap >= 1.0 for gap in _gaps([at for _, at in starts]))


def test_clients_sharing_pacing_stay_apart_when_they_run_together() -> None:
    virtual = _VirtualTime()
    starts: list[tuple[str, float]] = []
    first, second = _pacing_pair(virtual, starts)

    async def run() -> None:
        async with first, second:
            await asyncio.gather(first.get(_BOARD), second.get(_BOARD))

    asyncio.run(virtual.run(run()))

    assert len(starts) == 4
    assert all(gap >= 1.0 for gap in _gaps([at for _, at in starts]))


def test_clients_sharing_pacing_do_not_slow_other_hosts() -> None:
    virtual = _VirtualTime()
    starts: list[tuple[str, float]] = []
    first, second = _pacing_pair(virtual, starts)

    async def run() -> None:
        async with first, second:
            await first.get(_BOARD)
            await second.get("https://other.example.test/jobs")

    asyncio.run(virtual.run(run()))

    other = [at for host, at in starts if host == "other.example.test"]
    assert other == [1.0, 2.0]


def test_clients_without_shared_pacing_do_not_wait_for_each_other() -> None:
    virtual = _VirtualTime()
    starts: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append((request.url.host, virtual.now))
        return httpx.Response(200, text="")

    def build() -> PoliteClient:
        return PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=virtual.clock,
            sleep=virtual.sleep,
        )

    first, second = build(), build()

    async def run() -> None:
        async with first, second:
            await first.get(_BOARD)
            await second.get(_BOARD)

    asyncio.run(virtual.run(run()))

    assert min(_gaps([at for _, at in starts])) == 0.0


def test_host_pacing_keeps_the_latest_start_of_each_host() -> None:
    pacing = HostPacing()
    assert pacing.last_start("a.test") is None

    pacing.record("a.test", 5.0)
    pacing.record("a.test", 3.0)
    pacing.record("b.test", 1.0)

    assert pacing.last_start("a.test") == 5.0
    assert pacing.last_start("b.test") == 1.0


def test_cancelled_pacing_wait_gives_its_reservation_back() -> None:
    virtual = _VirtualTime()
    robots_starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/robots.txt":
            return httpx.Response(200, text="")
        robots_starts.append(virtual.now)
        return httpx.Response(200, text="User-agent: *\nCrawl-delay: 600\n")

    def build() -> PoliteClient:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=virtual.clock,
            sleep=virtual.sleep,
        )
        client.share_pacing(pacing)
        return client

    pacing = HostPacing()
    first, second = build(), build()

    async def run() -> None:
        async with first, second:
            cancelled = asyncio.ensure_future(first.get(_BOARD))
            while not virtual.sleeping():
                await asyncio.sleep(0)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            await second.get(_BOARD)

    asyncio.run(virtual.run(run()))

    assert robots_starts == [0.0, 1.0]


def test_host_pacing_withdraws_only_the_reservation_it_was_given() -> None:
    pacing = HostPacing()
    pacing.record("a.test", 2.0)
    pacing.record("a.test", 9.0)

    pacing.withdraw("a.test", reserved=9.0, previous=2.0)
    assert pacing.last_start("a.test") == 2.0

    pacing.record("a.test", 9.0)
    pacing.record("a.test", 12.0)
    pacing.withdraw("a.test", reserved=9.0, previous=2.0)
    assert pacing.last_start("a.test") == 12.0


def _setting_cookie(cookie: str, seen: list[str | None]) -> _Handler:
    """Answer every request with `Set-Cookie`, recording any `Cookie` sent back."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Cookie"))
        header = (b"Set-Cookie", cookie.encode())
        if request.url.path == "/a":
            return httpx.Response(
                302, headers=[header, (b"Location", b"/b")], request=request
            )
        return httpx.Response(200, headers=[header], text="body", request=request)

    return handler


@pytest.mark.parametrize("cookie", ["name=value", "name=Jürgen"])
@pytest.mark.parametrize("max_bytes", [None, MAX_BODY_BYTES])
def test_cookies_are_neither_kept_nor_sent(cookie: str, max_bytes: int | None) -> None:
    seen: list[str | None] = []
    url = "https://site.example.test/"

    async def run() -> list[str]:
        client = _client(_setting_cookie(cookie, seen), _FakeTime())
        async with client:
            first = await client.get(f"{url}jobs", max_bytes=max_bytes)
            second = await client.get(f"{url}jobs", max_bytes=max_bytes)
            hop = await client.get(
                f"{url}a", follow_redirects=True, max_bytes=max_bytes
            )
            return [first.text, second.text, hop.text]

    assert asyncio.run(run()) == ["body", "body", "body"]
    assert len(seen) == 5
    assert set(seen) == {None}
