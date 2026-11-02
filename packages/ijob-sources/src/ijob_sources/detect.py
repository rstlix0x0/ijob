"""Turn a pasted URL into a source reference, and check a new source before it is kept.

`resolve_url` decides what a URL is by four steps. A URL on a known hiring system
names a source outright and is not fetched. Any other URL is fetched once, with
its redirects followed and at most 5 MiB read. If the address the redirects end
on is on a known hiring system, that board is the source, even when the board's
page itself cannot be read. Otherwise a page that states exactly one schema.org
`JobPosting` becomes a `jobposting` source, and an RSS 2.0 body becomes a `feed`
source, each stored under the final address because refreshing a source follows
no redirect; when none happened that address is the URL as given. In both of
those cases the result carries the batch read from the body that was already
fetched, so the page is never requested twice. A feed that yields no drafts is
refused, as is a new source that yields none in `verify_new_source`, because an
empty board cannot show whose it is.
"""

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx
from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceKey, SourceKind, SourceRef, parse_source_key

from ijob_sources.collect import Source, SourceBatch
from ijob_sources.errors import (
    FetchedPage,
    HttpStatusError,
    ParseError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    UnrecognizedPageError,
    UnsupportedUrlError,
)
from ijob_sources.manual import manual_key, parse_feed, parse_jobposting_page
from ijob_sources.polite_http import MAX_BODY_BYTES, TOO_LARGE_REASON, PoliteClient


def _board(host: str) -> re.Pattern[str]:
    """Match a URL on `host` in any case, capturing its first path segment."""
    return re.compile(rf"^https?://{host}/([^/?#]+)", re.IGNORECASE)


_HIRING_SYSTEMS: Final = (
    (SourceKind.GREENHOUSE, _board(r"(?:boards|job-boards)\.greenhouse\.io")),
    (SourceKind.LEVER, _board(r"jobs\.lever\.co")),
    (SourceKind.ASHBY, _board(r"jobs\.ashbyhq\.com")),
    (SourceKind.WORKABLE, _board(r"apply\.workable\.com")),
    (SourceKind.GEM, _board(r"jobs\.gem\.com")),
)
_NOT_ACCOUNTS: Final = {
    SourceKind.GREENHOUSE: frozenset({"embed"}),
    SourceKind.WORKABLE: frozenset({"j", "api"}),
}
_EMPTY_BOARD: Final = "no jobs; cannot verify the company"
_NOT_WEB_ADDRESS: Final = "not an http or https address"
_WEB_SCHEMES: Final = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a pasted URL turned out to be.

    Attributes:
        ref: The source the URL names.
        target_url: The URL the source reads, for a `jobposting` or `feed`
            source: the address the fetch ended on after redirects. `None` for a
            hiring-system source, which is found by its key alone.
        batch: The jobs already read from the page this resolution fetched, or
            `None` when it fetched nothing.
    """

    ref: SourceRef
    target_url: str | None
    batch: SourceBatch | None


async def resolve_url(url: str, *, company: str, client: PoliteClient) -> Resolution:
    """Decide what kind of source a pasted URL is.

    Args:
        url: The URL the user pasted.
        company: The company the source is registered for; a job page must name
            it.
        client: The client every request goes through.

    Returns:
        The resolution. A hiring-system URL is resolved without any request. A
        job page or feed is fetched once, with redirects followed, and its
        batch is in the result; its `target_url` and key come from the final
        address, which is `url` as given when no redirect happened. A page that
        redirects to a hiring-system board resolves to that board, also when
        the board's page is disallowed by robots.txt or answers 403 or 404.

    Raises:
        UnrecognizedPageError: The fetched page is none of a page with exactly one
            `JobPosting` or an RSS feed. Its `url` is `url`, as given, and it
            holds the page that was read.
        UnsupportedUrlError: `url` is malformed or is not an http or https
            address, so nothing is fetched; the page is over 5 MiB; the feed
            yields no draft; or a hiring-system token, of `url` or of the final
            address, is not a valid source key.
        IdentityMismatchError: The job page names a different company.
        SourcesError: The client could not complete the request.
    """
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise UnsupportedUrlError(url, str(error)) from error
    if parts.scheme not in _WEB_SCHEMES or not parts.hostname:
        raise UnsupportedUrlError(url, _NOT_WEB_ADDRESS)
    ref = _hiring_system_ref(url)
    if ref is not None:
        return Resolution(ref=ref, target_url=None, batch=None)
    try:
        response = await client.get(
            url, follow_redirects=True, max_bytes=MAX_BODY_BYTES
        )
    except ResponseTooLargeError as error:
        raise UnsupportedUrlError(url, TOO_LARGE_REASON) from error
    except (RobotsDisallowedError, HttpStatusError) as error:
        board = _hiring_system_ref(error.url) if error.url != url else None
        if board is None:
            raise
        return Resolution(ref=board, target_url=None, batch=None)
    final = _final_address(url, response)
    ref = _hiring_system_ref(final)
    if ref is not None:
        return Resolution(ref=ref, target_url=None, batch=None)
    key = manual_key(final)
    posting_batch = _read_posting_page(response.content, key, final, company)
    if posting_batch is not None:
        return Resolution(
            ref=SourceRef(SourceKind.JOBPOSTING, key),
            target_url=final,
            batch=posting_batch,
        )
    try:
        feed_batch = parse_feed(
            response.content, key, target_url=final, company=company
        )
    except ParseError as error:
        page = FetchedPage(
            body=response.content,
            content_type=response.headers.get("Content-Type"),
            final_url=final,
        )
        raise UnrecognizedPageError(url, page=page) from error
    if not feed_batch.drafts:
        raise UnsupportedUrlError(url, _EMPTY_BOARD)
    return Resolution(
        ref=SourceRef(SourceKind.FEED, key), target_url=final, batch=feed_batch
    )


def _final_address(url: str, response: httpx.Response) -> str:
    """Return the address a fetch ended on: `url` itself unless a redirect moved it.

    `httpx` normalizes an address it was given (host case, a default port,
    spaces, non-ASCII characters), so the response URL differs from `url` in
    spelling alone; only a different address means a redirect happened.
    """
    final = str(response.url)
    return url if str(httpx.URL(url)) == final else final


def _read_posting_page(
    body: bytes, key: SourceKey, url: str, company: str
) -> SourceBatch | None:
    """Read a page that states exactly one `JobPosting`, or return `None`.

    Raises:
        IdentityMismatchError: The posting names a different company.
    """
    try:
        return parse_jobposting_page(body, key, target_url=url, company=company)
    except ParseError:
        return None


async def verify_new_source(source: Source, client: PoliteClient) -> SourceBatch:
    """Fetch a new source once and refuse it if it yields no drafts.

    Args:
        source: The source about to be added.
        client: The client the request goes through.

    Returns:
        The batch the source returned.

    Raises:
        UnsupportedUrlError: The source yields no drafts, so its company cannot be
            checked. The error's `url` is the source's reference text, because a
            source exposes no URL.
        IdentityMismatchError: The source belongs to a different company.
        SourcesError: The client could not complete the request, or the
            response could not be read.
    """
    batch = await source.fetch(client)
    if not batch.drafts:
        raise UnsupportedUrlError(str(source.ref), _EMPTY_BOARD)
    return batch


def _hiring_system_ref(url: str) -> SourceRef | None:
    """Name the hiring-system board a URL is on, or `None` when it is on none.

    Raises:
        UnsupportedUrlError: The URL is on a hiring system but its token is not a
            valid source key.
    """
    for kind, pattern in _HIRING_SYSTEMS:
        found = pattern.match(url)
        if found is None:
            continue
        token = found.group(1)
        if token.lower() in _NOT_ACCOUNTS.get(kind, frozenset()):
            continue
        try:
            return SourceRef(kind, parse_source_key(token))
        except InvalidValueError as error:
            raise UnsupportedUrlError(url, str(error)) from error
    return None
