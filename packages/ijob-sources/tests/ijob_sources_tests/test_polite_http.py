import asyncio
import email.utils
import importlib.metadata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    RobotsDisallowedError,
    SourceNotFoundError,
)
from ijob_sources.polite_http import USER_AGENT, CachedResponse, PoliteClient

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


def test_get_disallowed_when_robots_redirects_forever() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": str(request.url)})

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


def test_override_below_min_interval_does_not_lower_it() -> None:
    fake_time = _FakeTime()
    client = _spaced_client(
        _Site(httpx.Response(200)), fake_time, {"apply.workable.com": 0.5}
    )
    asyncio.run(_get_each(client, _WORKABLE, _WORKABLE))
    assert fake_time.waits == [1.0, 1.0]


def test_host_override_key_is_case_insensitive() -> None:
    fake_time = _FakeTime()
    client = _spaced_client(
        _Site(httpx.Response(200)), fake_time, {"Apply.Workable.com": 3.0}
    )
    asyncio.run(_get_each(client, _WORKABLE, _WORKABLE))
    assert fake_time.waits == [3.0, 3.0]
