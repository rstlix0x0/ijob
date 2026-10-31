"""Per-origin robots.txt decisions (RFC 9309).

`RobotsPolicy` answers whether a URL may be fetched. It reaches the network only
through the `send` callable it is given, so it never depends on the HTTP client
that uses it. It also reports the `Crawl-delay` a cached robots.txt sets for the
crawler. Each origin's robots.txt is fetched once and cached; an entry is
refetched after 24 hours, except one built from a 429 answer, which holds for
the policy's life.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import SplitResult, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ijob_sources.errors import FetchError

_PARSE_LIMIT_BYTES: Final = 500 * 1024
_CACHE_LIFETIME_SECONDS: Final = 24 * 60 * 60.0
_ROBOTS_PATH: Final = "/robots.txt"


def _origin(parts: SplitResult) -> str:
    """Return the lowercase `scheme://host[:port]` cache key of a split URL."""
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2].lower()}"


@dataclass(frozen=True, slots=True)
class _Entry:
    """What one origin's robots.txt said, and when.

    Attributes:
        rules: The parsed rules, or a fixed answer (`True` allows every URL,
            `False` disallows every URL) when the file was missing or unusable.
        fetched_at: The policy's clock reading when the entry was built.
        permanent: Whether the entry never expires.
    """

    rules: RobotFileParser | bool
    fetched_at: float
    permanent: bool = False


class RobotsPolicy:
    """Decides, per origin, whether robots.txt allows a URL for this crawler."""

    def __init__(
        self,
        send: Callable[[str], Awaitable[httpx.Response]],
        clock: Callable[[], float],
        product_token: str = "ijob",  # noqa: S107  # RFC 9309 crawler name, not a secret
    ) -> None:
        """Build a policy with an empty cache.

        Args:
            send: Fetches a URL, following redirects, and returns the response
                with its status unmapped. It raises `FetchError` when no
                response can be had.
            clock: Monotonic seconds, used to age cache entries.
            product_token: The crawler name that robots.txt groups are matched
                against.
        """
        self._send = send
        self._clock = clock
        self._token = product_token
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def allowed(self, url: str) -> bool:
        """Report whether robots.txt allows fetching a URL.

        The `/robots.txt` path itself is always allowed, without a fetch.
        Otherwise the origin's robots.txt is fetched on a cold or expired cache;
        concurrent callers for one origin share a single fetch.

        Args:
            url: The absolute URL about to be requested.

        Returns:
            `True` when the URL may be fetched.
        """
        parts = urlsplit(url)
        if parts.path == _ROBOTS_PATH:
            return True
        origin = _origin(parts)
        entry = self._entries.get(origin)
        if entry is None or self._expired(entry):
            async with self._locks.setdefault(origin, asyncio.Lock()):
                entry = self._entries.get(origin)
                if entry is None or self._expired(entry):
                    entry = await self._fetch(origin)
                    self._entries[origin] = entry
        if isinstance(entry.rules, bool):
            return entry.rules
        return entry.rules.can_fetch(self._token, url)

    def crawl_delay(self, url: str) -> float | None:
        """Report the `Crawl-delay` robots.txt sets for this crawler.

        This reads the cache only and never sends a request, so it is safe to
        call while the origin's robots.txt is itself being fetched. A delay
        read from an entry past its lifetime is still returned until
        `allowed` refetches it.

        Args:
            url: An absolute URL on the origin of interest.

        Returns:
            The delay in seconds, or `None` when the origin has no cached
            entry, the entry holds only a fixed allow or deny answer, or the
            file sets no delay the standard-library parser can read. A value
            it cannot read, such as a fraction like `1.5`, counts as no delay.
        """
        entry = self._entries.get(_origin(urlsplit(url)))
        if entry is None or isinstance(entry.rules, bool):
            return None
        delay = entry.rules.crawl_delay(self._token)
        return None if delay is None else float(delay)

    def _expired(self, entry: _Entry) -> bool:
        """Report whether an entry is old enough to refetch."""
        return (
            not entry.permanent
            and self._clock() - entry.fetched_at > _CACHE_LIFETIME_SECONDS
        )

    async def _fetch(self, origin: str) -> _Entry:
        """Fetch an origin's robots.txt and turn the answer into an entry."""
        fetched_at = self._clock()
        try:
            response = await self._send(origin + _ROBOTS_PATH)
        except FetchError:
            return _Entry(rules=False, fetched_at=fetched_at)
        status = response.status_code
        if status == httpx.codes.TOO_MANY_REQUESTS:
            return _Entry(rules=False, fetched_at=fetched_at, permanent=True)
        if httpx.codes.is_client_error(status):
            return _Entry(rules=True, fetched_at=fetched_at)
        if not httpx.codes.is_success(status):
            return _Entry(rules=False, fetched_at=fetched_at)
        text = response.content[:_PARSE_LIMIT_BYTES].decode("utf-8", errors="replace")
        parser = RobotFileParser()
        try:
            parser.parse(text.splitlines())
        except ValueError:
            return _Entry(rules=False, fetched_at=fetched_at)
        return _Entry(rules=parser, fetched_at=fetched_at)
