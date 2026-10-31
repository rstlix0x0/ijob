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
from ijob_sources.polite_http import USER_AGENT, PoliteClient

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

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self._robots)
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
