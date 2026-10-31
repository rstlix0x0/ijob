"""The one HTTP client every source request goes through.

`PoliteClient` sends ijob's own `User-Agent`, spaces requests to one host by a
minimum interval, and maps failures to the errors of `ijob_sources.errors`. It
owns the `httpx.AsyncClient` and the `RobotsPolicy` that guards it. The
monotonic clock, the wall clock and the sleep are injected, so tests need no
real time.
"""

import asyncio
import email.utils
import importlib.metadata
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, Self
from urllib.parse import urlsplit

import httpx

from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    RobotsDisallowedError,
    SourceNotFoundError,
)
from ijob_sources.robots import RobotsPolicy

USER_AGENT: Final = (
    f"ijob/{importlib.metadata.version('ijob-sources')}"
    " (+https://github.com/rstlix0x0/ijob)"
)

_TIMEOUT_SECONDS: Final = 30.0
_MAX_REDIRECTS: Final = 5
_MAX_ATTEMPTS: Final = 3
_MAX_RETRY_WAIT_SECONDS: Final = 60.0
_RETRY_STATUSES: Final = frozenset(
    {httpx.codes.TOO_MANY_REQUESTS, httpx.codes.SERVICE_UNAVAILABLE}
)


def _utc_now() -> datetime:
    """Return the current wall-clock time in UTC."""
    return datetime.now(UTC)


class PoliteClient:
    """An HTTP client that is paced, honest about who it is and obeys robots.txt.

    Use it as an async context manager so the underlying connection pool is
    closed.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        min_interval: float = 1.0,
    ) -> None:
        """Build a client with an open connection pool.

        Args:
            transport: Replaces the network transport; tests pass an
                `httpx.MockTransport`.
            clock: Monotonic seconds, used for pacing and to age robots.txt
                cache entries.
            now: The wall clock an HTTP-date `Retry-After` is measured against.
            sleep: Waits for the given number of seconds.
            min_interval: Seconds between the start of two requests to one host.
        """
        self._clock = clock
        self._now = now
        self._sleep = sleep
        self._min_interval = min_interval
        self._http = httpx.AsyncClient(
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
            max_redirects=_MAX_REDIRECTS,
        )
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_start: dict[str, float] = {}
        self._robots = RobotsPolicy(self._send_following_redirects, clock)

    async def __aenter__(self) -> Self:
        """Return the client itself."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def get(self, url: str) -> httpx.Response:
        """Fetch a source URL politely and return its 2xx response.

        Checks robots.txt, sends one paced request that follows no redirect,
        and on 429 or 503 waits for `Retry-After` and resends, at most three
        attempts in all.

        Args:
            url: The absolute URL to fetch.

        Returns:
            The response, whose status is 2xx.

        Raises:
            RobotsDisallowedError: robots.txt does not allow the URL.
            SourceNotFoundError: The server answered 404.
            HttpStatusError: The server answered any other non-2xx status,
                including a redirect, or kept answering 429 or 503, or asked
                for a wait above 60 seconds.
            FetchError: The request failed before a usable response came back.
        """
        if not await self._robots.allowed(url):
            raise RobotsDisallowedError(url)
        attempt = 1
        while True:
            response = await self._send(url, follow_redirects=False)
            if response.status_code not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS:
                break
            wait = self._retry_wait(response)
            if wait > _MAX_RETRY_WAIT_SECONDS:
                break
            await self._sleep(wait)
            attempt += 1
        if httpx.codes.is_success(response.status_code):
            return response
        if response.status_code == httpx.codes.NOT_FOUND:
            raise SourceNotFoundError(url)
        raise HttpStatusError(url, response.status_code)

    def _retry_wait(self, response: httpx.Response) -> float:
        """Return the seconds a `Retry-After` header asks for.

        The header is either a count of seconds or an HTTP-date, which is
        measured against the injected wall clock; a date in the past asks for
        no wait. A missing or unreadable header asks for `min_interval`.
        """
        value = response.headers.get("Retry-After", "").strip()
        if value.isascii() and value.isdigit():
            return float(value)
        try:
            date = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return self._min_interval
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max((date - self._now()).total_seconds(), 0.0)

    async def _send_following_redirects(self, url: str) -> httpx.Response:
        """Send a paced request that follows redirects; used for robots.txt."""
        return await self._send(url, follow_redirects=True)

    async def _send(self, url: str, *, follow_redirects: bool) -> httpx.Response:
        """Send one paced GET and return the response with its status unmapped.

        Waits until `min_interval` seconds have passed since the start of the
        previous request to the same host.

        Args:
            url: The absolute URL to request.
            follow_redirects: Whether to follow redirects, up to five.

        Returns:
            The response, whatever its status.

        Raises:
            FetchError: The request failed before a usable response came back:
                a transport failure, a body that could not be decoded, or more
                than five redirects.
        """
        host = urlsplit(url).netloc.lower()
        async with self._host_locks.setdefault(host, asyncio.Lock()):
            last_start = self._last_start.get(host)
            if last_start is not None:
                wait = self._min_interval - (self._clock() - last_start)
                if wait > 0:
                    await self._sleep(wait)
            self._last_start[host] = self._clock()
            try:
                return await self._http.get(url, follow_redirects=follow_redirects)
            except httpx.RequestError as error:
                raise FetchError(url, str(error) or type(error).__name__) from error
