"""The one HTTP client every source request goes through.

`PoliteClient` sends ijob's own `User-Agent`, spaces requests to one host by the
strictest of a minimum interval, the host's robots.txt `Crawl-delay` and a
per-host override, and maps failures to the errors of `ijob_sources.errors`. It
owns the `httpx.AsyncClient` and the `RobotsPolicy` that guards it. The
monotonic clock, the wall clock and the sleep are injected, so tests need no
real time. Clients that read the same hosts can take turns by sharing one
`HostPacing`. Given a `ConditionalCache`, it can revalidate a stored response with
`If-None-Match` and `If-Modified-Since` and replay it when the server answers
`304 Not Modified`. A `RequestGuard` attached with `guard_requests` sees, and can
refuse, every request the client sends, and `get` can follow redirects one
guarded, paced hop at a time. `get` can also stop reading a response once it
passes a size limit. A robots.txt is read the same way, hop by hop through its
redirects, and only its first 500 KiB. One that cannot be fetched is an error,
not a refusal: `get` raises `FetchError` and requests nothing else.
"""

import asyncio
import email.utils
import importlib.metadata
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import DefaultCookiePolicy
from types import TracebackType
from typing import Final, Protocol, Self
from urllib.parse import urlsplit

import httpx

from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    SourceNotFoundError,
)
from ijob_sources.robots import PARSE_LIMIT_BYTES, RobotsPolicy

USER_AGENT: Final = (
    f"ijob/{importlib.metadata.version('ijob-sources')}"
    " (+https://github.com/rstlix0x0/ijob)"
)

MAX_BODY_BYTES: Final = 5 * 1024 * 1024
"""The most decoded bytes the callers that read a page ask `PoliteClient.get` for.

`get` itself reads without a limit unless it is given `max_bytes`.
"""

TOO_LARGE_REASON: Final = f"the page is larger than {MAX_BODY_BYTES // 2**20} MiB"
"""Why a page that passed `MAX_BODY_BYTES` is refused."""

_TIMEOUT_SECONDS: Final = 30.0
_MAX_REDIRECTS: Final = 5
_MAX_ATTEMPTS: Final = 3
_MAX_RETRY_WAIT_SECONDS: Final = 60.0
_DECODED_BODY_HEADERS: Final = frozenset({b"content-encoding", b"content-length"})
_REDIRECT_STATUSES: Final = frozenset(
    {
        httpx.codes.MOVED_PERMANENTLY,
        httpx.codes.FOUND,
        httpx.codes.SEE_OTHER,
        httpx.codes.TEMPORARY_REDIRECT,
        httpx.codes.PERMANENT_REDIRECT,
    }
)
_RETRY_STATUSES: Final = frozenset(
    {httpx.codes.TOO_MANY_REQUESTS, httpx.codes.SERVICE_UNAVAILABLE}
)


type RequestGuard = Callable[[httpx.Request], Awaitable[None]]
"""A hook awaited before a request is sent; raising an error refuses the request."""


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


class HostPacing:
    """When the latest request to each host started, for clients that share it.

    It holds plain state and no lock, so it is safe across event loops; the
    clients sharing it must run on one loop at a time, as an `asyncio.Lock`
    they each hold per host is bound to the loop.
    """

    def __init__(self) -> None:
        """Build a pacing with no request recorded."""
        self._started: dict[str, float] = {}

    def last_start(self, host: str) -> float | None:
        """Return when the latest request to a host started, or `None` if none did.

        Args:
            host: The lowercase netloc of the requests.
        """
        return self._started.get(host)

    def record(self, host: str, started: float) -> None:
        """Note that a request to a host starts at a time, keeping the later time.

        Args:
            host: The lowercase netloc of the request.
            started: Monotonic seconds. A reserved start in the future is
                kept when an earlier time is recorded after it.
        """
        self._started[host] = max(started, self._started.get(host, started))

    def withdraw(self, host: str, *, reserved: float, previous: float) -> None:
        """Take back a reserved start, unless a later one was made after it.

        Args:
            host: The lowercase netloc of the request.
            reserved: The start that was recorded for the request.
            previous: The latest start that was recorded before it. It is
                restored only when `reserved` is still the latest.
        """
        if self._started.get(host) == reserved:
            self._started[host] = previous


class PoliteClient:
    """An HTTP client that is paced, honest about who it is and obeys robots.txt.

    Requests to one host start at least `min_interval` apart, or further if
    that host's robots.txt sets a larger `Crawl-delay` or `host_intervals`
    names a larger interval for it. Clients given the same `HostPacing` with
    `share_pacing` keep that spacing between each other's requests too.

    A guard attached with `guard_requests` sees every request the client
    sends, robots.txt requests and redirect hops included, before it reaches
    the network. Only the first 500 KiB of a robots.txt are read, as
    RFC 9309 parses no more.

    It keeps no cookies: a `Set-Cookie` answer is ignored and no request carries
    a `Cookie` header.

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
        self._http.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._pacing = HostPacing()
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

    def guard_requests(self, guard: RequestGuard) -> None:
        """Run a guard before every request this client sends.

        Guards run in the order they were attached. An error a guard raises
        reaches the caller of `get` unchanged, and the request is not sent.
        A guard that raises `FetchError` while robots.txt is being fetched
        reaches the caller as a `FetchError` for the page, saying that
        robots.txt could not be read.

        Args:
            guard: The hook to await with each `httpx.Request`.
        """
        self._http.event_hooks["request"].append(guard)

    def share_pacing(self, pacing: HostPacing) -> None:
        """Pace requests against a record that other clients also write to.

        Call it before the first request. Requests this client already sent
        are forgotten, and its own wait is counted from the shared record.

        Args:
            pacing: The record of request starts to share.
        """
        self._pacing = pacing

    async def get(
        self,
        url: str,
        *,
        conditional: bool = False,
        follow_redirects: bool = False,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        """Fetch a source URL politely and return its 2xx response.

        Checks robots.txt, sends one paced request, and on 429 or 503 waits
        for `Retry-After` and resends, at most three attempts in all. A
        robots.txt that cannot be fetched is an error, not a refusal, and
        nothing else is requested; a 429, a 5xx or an unparseable robots.txt
        does disallow the URL.

        A 3xx answer is an error unless `follow_redirects` is set. Then a 301,
        302, 303, 307 or 308 answer with a `Location` header is followed, at
        most five times. The location is resolved against the URL that
        answered, and each hop is a whole new round: robots.txt of the hop's
        origin, pacing, the 429 or 503 retry and every request guard.

        With `conditional` and a cache, a stored entry for the URL adds
        whichever of `If-None-Match` and `If-Modified-Since` it holds to
        every attempt, a `304` answer is replayed as a 200 response holding
        the stored body, and a 2xx answer carrying `ETag` or `Last-Modified`
        is stored.

        With `max_bytes`, each response is streamed and its decoded bytes are
        counted; a 2xx body that passes the limit stops the read, closes the
        response and is an error. A response with any other status is cut at
        the limit instead, so its status still decides the error. The response
        returned is rebuilt from the bytes read: `Content-Encoding` is dropped
        because the body is already decoded, and `Content-Length` is the
        decoded length.

        Args:
            url: The absolute URL to fetch.
            conditional: Whether to revalidate against and update the cache.
                It has no effect on a client built without a cache.
            follow_redirects: Whether to follow redirects by hand. It cannot
                be combined with `conditional`.
            max_bytes: The most decoded body bytes to read from any one
                response, or `None` for no limit.

        Returns:
            The response, whose status is 2xx. When redirects were followed,
            its `url` is the final hop's.

        Raises:
            ValueError: `conditional` and `follow_redirects` are both set.
            RobotsDisallowedError: robots.txt does not allow the URL, or the
                URL of a hop.
            ResponseTooLargeError: A 2xx response body passed `max_bytes`.
            SourceNotFoundError: The server answered 404.
            HttpStatusError: The server answered any other non-2xx status,
                including a redirect when `follow_redirects` is unset, a
                redirect with no `Location` header or a 304 with no stored
                entry, or kept answering 429 or 503, or asked for a wait
                above 60 seconds.
            FetchError: The request failed before a usable response came
                back, a `Location` header is not a usable URL, the sixth
                redirect arrived, or robots.txt could not be read (the
                reason starts `robots.txt could not be read`).

        Any error a request guard raises, other than the `FetchError` case
        `guard_requests` describes, and any error the cache raises, such as
        `sqlite3.Error` from `SqliteHttpCache`, propagates unmapped.
        """
        if conditional and follow_redirects:
            msg = "conditional cannot be combined with follow_redirects"
            raise ValueError(msg)
        redirects = 0
        while True:
            response = await self._round(
                url, conditional=conditional, max_bytes=max_bytes
            )
            if not (follow_redirects and response.status_code in _REDIRECT_STATUSES):
                break
            location = response.headers.get("Location")
            if location is None:
                raise HttpStatusError(url, response.status_code)
            if redirects == _MAX_REDIRECTS:
                raise FetchError(url, "more than five redirects")
            url = str(response.request.url.join(location))
            redirects += 1
        if httpx.codes.is_success(response.status_code):
            return response
        if response.status_code == httpx.codes.NOT_FOUND:
            raise SourceNotFoundError(url)
        raise HttpStatusError(url, response.status_code)

    async def _round(
        self, url: str, *, conditional: bool, max_bytes: int | None
    ) -> httpx.Response:
        """Run robots.txt, pacing and the 429 or 503 retry for one URL.

        Returns:
            The last response, whatever its status, except that a `304`
            answered to a stored entry comes back as the 200 replay of it.

        Raises:
            RobotsDisallowedError: robots.txt does not allow the URL.
            FetchError: The request failed before a response came back, or
                robots.txt could not be read.
            ResponseTooLargeError: A 2xx response body passed `max_bytes`.
        """
        if not await self._robots.allowed(url):
            raise RobotsDisallowedError(url)
        cache = self._cache if conditional else None
        entry = cache.lookup(url) if cache is not None else None
        headers = self._validators(entry)
        attempt = 1
        while True:
            response = await self._send(
                url, follow_redirects=False, headers=headers, max_bytes=max_bytes
            )
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
        if cache is not None and httpx.codes.is_success(response.status_code):
            self._remember(cache, url, response)
        return response

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
        """Send paced, guarded requests along the redirects of a robots.txt.

        Each hop is a whole request of its own, read no further than
        `PARSE_LIMIT_BYTES`, redirect bodies included, with a longer body cut
        there rather than refused. At most five redirects are followed.

        Raises:
            FetchError: A request failed, a `Location` header is not a usable
                URL, or the sixth redirect arrived.
        """
        redirects = 0
        while True:
            response = await self._send(
                url,
                follow_redirects=False,
                max_bytes=PARSE_LIMIT_BYTES,
                keep_prefix=True,
            )
            location = response.headers.get("Location")
            if response.status_code not in _REDIRECT_STATUSES or location is None:
                return response
            if redirects == _MAX_REDIRECTS:
                raise FetchError(url, "more than five redirects")
            try:
                url = str(response.request.url.join(location))
            except httpx.InvalidURL as error:
                raise FetchError(url, str(error) or type(error).__name__) from error
            redirects += 1

    async def _send(
        self,
        url: str,
        *,
        follow_redirects: bool,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        keep_prefix: bool = False,
    ) -> httpx.Response:
        """Send one paced GET and return the response with its status unmapped.

        Waits until the host's interval has passed since the start of the
        previous request to the same host, whichever client sent it when
        clients share a `HostPacing`. When a wait is due, the start it ends at
        is recorded before sleeping, so a client that arrives meanwhile waits
        for it; if the sleep is cancelled, that record is withdrawn. The
        interval is the largest of `min_interval`, the `Crawl-delay` robots.txt
        sets for the URL (none while the origin's robots.txt is not yet cached)
        and the host's `host_intervals` entry.

        Args:
            url: The absolute URL to request.
            follow_redirects: Whether to follow redirects, up to five.
            headers: Extra request headers sent with this request only.
            max_bytes: The most decoded body bytes to read, or `None` for no
                limit. With a limit the body is streamed and the response
                returned is rebuilt from the bytes read.
            keep_prefix: Whether a body that reaches `max_bytes` is cut there
                and returned instead of refused, even on a 2xx answer.

        Returns:
            The response, whatever its status.

        Raises:
            ResponseTooLargeError: A 2xx body passed `max_bytes`, and
                `keep_prefix` is unset.
            FetchError: The request failed before a usable response came back:
                a transport failure, a body that could not be decoded or
                read, a host name that is not valid IDNA, a `Location` header
                that is not a usable URL, or more than five redirects.
        """
        host = urlsplit(url).netloc.lower()
        async with self._host_locks.setdefault(host, asyncio.Lock()):
            last_start = self._pacing.last_start(host)
            if last_start is not None:
                interval = max(
                    self._min_interval,
                    self._robots.crawl_delay(url) or 0.0,
                    self._host_intervals.get(host, 0.0),
                )
                wait = interval - (self._clock() - last_start)
                if wait > 0:
                    reserved = self._clock() + wait
                    self._pacing.record(host, reserved)
                    try:
                        await self._sleep(wait)
                    except asyncio.CancelledError:
                        self._pacing.withdraw(
                            host, reserved=reserved, previous=last_start
                        )
                        raise
            self._pacing.record(host, self._clock())
            try:
                if max_bytes is None:
                    return await self._http.get(
                        url, headers=headers, follow_redirects=follow_redirects
                    )
                return await self._get_limited(
                    url,
                    headers=headers,
                    follow_redirects=follow_redirects,
                    max_bytes=max_bytes,
                    keep_prefix=keep_prefix,
                )
            except (httpx.RequestError, httpx.InvalidURL, UnicodeError) as error:
                raise FetchError(url, str(error) or type(error).__name__) from error

    async def _get_limited(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        follow_redirects: bool,
        max_bytes: int,
        keep_prefix: bool,
    ) -> httpx.Response:
        """Stream one GET and stop reading once the decoded body reaches the limit.

        A body of a non-2xx response, or of any response when `keep_prefix` is
        set, is cut at the limit and the read stops there. Any other body that
        passes the limit is an error.

        Returns:
            A response with the status, request and body read, whose headers
            are the original raw ones less `Content-Encoding` and
            `Content-Length`: the body is the decoded bytes, which `httpx`
            must not decode a second time, and it gives the rebuilt response a
            `Content-Length` of its own.

        Raises:
            ResponseTooLargeError: The decoded body of a 2xx response passed
                `max_bytes` and `keep_prefix` is unset; the response is closed
                with the rest of it unread.
            httpx.RequestError: The request or the body failed; the caller
                maps it.
        """
        request = self._http.build_request("GET", url, headers=headers)
        response = await self._http.send(
            request, stream=True, follow_redirects=follow_redirects
        )
        try:
            cut = keep_prefix or not httpx.codes.is_success(response.status_code)
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                room = max_bytes - size
                if len(chunk) > room and not cut:
                    raise ResponseTooLargeError(url, max_bytes)
                chunks.append(chunk[:room])
                size += len(chunks[-1])
                if cut and size == max_bytes:
                    break
        finally:
            await response.aclose()
        kept = [
            (name, value)
            for name, value in response.headers.raw
            if name.lower() not in _DECODED_BODY_HEADERS
        ]
        return httpx.Response(
            response.status_code,
            headers=kept,
            content=b"".join(chunks),
            request=response.request,
        )
