"""The one HTTP client every source request goes through.

`PoliteClient` sends ijob's own `User-Agent`, spaces requests to one host by the
strictest of a minimum interval, the host's robots.txt `Crawl-delay` and a
per-host override, and maps failures to the errors of `ijob_sources.errors`. It
owns the `httpx.AsyncClient` and the `RobotsPolicy` that guards it. The
monotonic clock, the wall clock and the sleep are injected, so tests need no
real time. Given a `ConditionalCache`, it can revalidate a stored response with
`If-None-Match` and `If-Modified-Since` and replay it when the server answers
`304 Not Modified`.
"""

import asyncio
import email.utils
import importlib.metadata
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, Protocol, Self
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


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """A 2xx response body kept with the validators the server gave for it.

    Attributes:
        etag: The `ETag` header value, or `None` if the response had none.
        last_modified: The `Last-Modified` header value, or `None` if the
            response had none.
        body: The response body.
    """

    etag: str | None
    last_modified: str | None
    body: bytes


class ConditionalCache(Protocol):
    """Where `PoliteClient` keeps responses to revalidate; owned by the client.

    A persistent store implements it in production and a dict-backed double
    implements it in tests.
    """

    def lookup(self, url: str) -> CachedResponse | None:
        """Return the stored response for a URL, or `None` if there is none.

        Args:
            url: The absolute URL that was fetched.
        """
        ...

    def store(self, url: str, response: CachedResponse) -> None:
        """Keep a response for a URL, replacing any earlier one.

        Args:
            url: The absolute URL that was fetched.
            response: The response body and its validators.
        """
        ...


class PoliteClient:
    """An HTTP client that is paced, honest about who it is and obeys robots.txt.

    Requests to one host start at least `min_interval` apart, or further if
    that host's robots.txt sets a larger `Crawl-delay` or `host_intervals`
    names a larger interval for it.

    Use it as an async context manager so the underlying connection pool is
    closed.
    """

    def __init__(  # noqa: PLR0913 - keyword-only injection points, one per dependency
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        min_interval: float = 1.0,
        cache: ConditionalCache | None = None,
        host_intervals: Mapping[str, float] | None = None,
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
                A host's robots.txt `Crawl-delay` or `host_intervals` entry
                raises it for that host, never lowers it.
            cache: Where conditional requests keep and find responses; without
                one, `get` never sends a validator.
            host_intervals: Minimum seconds between the start of two requests
                to a host, keyed by netloc and matched case-insensitively; a
                host absent from it has no override.
        """
        self._clock = clock
        self._now = now
        self._sleep = sleep
        self._min_interval = min_interval
        self._cache = cache
        self._host_intervals = {
            host.lower(): seconds for host, seconds in (host_intervals or {}).items()
        }
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

    async def get(self, url: str, *, conditional: bool = False) -> httpx.Response:
        """Fetch a source URL politely and return its 2xx response.

        Checks robots.txt, sends one paced request that follows no redirect,
        and on 429 or 503 waits for `Retry-After` and resends, at most three
        attempts in all.

        With `conditional` and a cache, a stored entry for the URL adds
        whichever of `If-None-Match` and `If-Modified-Since` it holds to
        every attempt, a `304` answer is replayed as a 200 response holding
        the stored body, and a 2xx answer carrying `ETag` or `Last-Modified`
        is stored.

        Args:
            url: The absolute URL to fetch.
            conditional: Whether to revalidate against and update the cache.
                It has no effect on a client built without a cache.

        Returns:
            The response, whose status is 2xx.

        Raises:
            RobotsDisallowedError: robots.txt does not allow the URL.
            SourceNotFoundError: The server answered 404.
            HttpStatusError: The server answered any other non-2xx status,
                including a redirect or a 304 with no stored entry, or kept
                answering 429 or 503, or asked for a wait above 60 seconds.
            FetchError: The request failed before a usable response came back.

        Any error the cache raises, such as `sqlite3.Error` from
        `SqliteHttpCache`, propagates unmapped.
        """
        if not await self._robots.allowed(url):
            raise RobotsDisallowedError(url)
        cache = self._cache if conditional else None
        entry = cache.lookup(url) if cache is not None else None
        headers = self._validators(entry)
        attempt = 1
        while True:
            response = await self._send(url, follow_redirects=False, headers=headers)
            if response.status_code not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS:
                break
            wait = self._retry_wait(response)
            if wait > _MAX_RETRY_WAIT_SECONDS:
                break
            await self._sleep(wait)
            attempt += 1
        if response.status_code == httpx.codes.NOT_MODIFIED and entry is not None:
            return httpx.Response(
                httpx.codes.OK, content=entry.body, request=response.request
            )
        if httpx.codes.is_success(response.status_code):
            if cache is not None:
                self._remember(cache, url, response)
            return response
        if response.status_code == httpx.codes.NOT_FOUND:
            raise SourceNotFoundError(url)
        raise HttpStatusError(url, response.status_code)

    @staticmethod
    def _validators(entry: CachedResponse | None) -> dict[str, str]:
        """Return the conditional request headers a stored entry supports."""
        headers: dict[str, str] = {}
        if entry is not None:
            if entry.etag is not None:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified is not None:
                headers["If-Modified-Since"] = entry.last_modified
        return headers

    @staticmethod
    def _remember(cache: ConditionalCache, url: str, response: httpx.Response) -> None:
        """Store a 2xx response in the cache if it carries a validator."""
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if etag is not None or last_modified is not None:
            cache.store(url, CachedResponse(etag, last_modified, response.content))

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

    async def _send(
        self,
        url: str,
        *,
        follow_redirects: bool,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send one paced GET and return the response with its status unmapped.

        Waits until the host's interval has passed since the start of the
        previous request to the same host. That interval is the largest of
        `min_interval`, the `Crawl-delay` robots.txt sets for the URL (none
        while the origin's robots.txt is not yet cached) and the host's
        `host_intervals` entry.

        Args:
            url: The absolute URL to request.
            follow_redirects: Whether to follow redirects, up to five.
            headers: Extra request headers sent with this request only.

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
                interval = max(
                    self._min_interval,
                    self._robots.crawl_delay(url) or 0.0,
                    self._host_intervals.get(host, 0.0),
                )
                wait = interval - (self._clock() - last_start)
                if wait > 0:
                    await self._sleep(wait)
            self._last_start[host] = self._clock()
            try:
                return await self._http.get(
                    url, headers=headers, follow_redirects=follow_redirects
                )
            except httpx.RequestError as error:
                raise FetchError(url, str(error) or type(error).__name__) from error
