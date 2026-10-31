"""Arbeitnow jobs as a source of sighting drafts.

`ArbeitnowBoard` walks the Arbeitnow job-board API one page at a time through
the `PoliteClient` and hands each body to `parse_arbeitnow`, which turns it into
an `ArbeitnowPage`. `parse_arbeitnow` is pure and never lets anything but a
`ParseError` escape. A job that cannot be read is logged at `WARNING` and
counted in `SourceBatch.rejected`.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    RemoteScope,
    SightingDraft,
    SourceKey,
    SourceKind,
    SourceRef,
)

from ijob_sources.collect import SourceBatch
from ijob_sources.errors import ParseError
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_FIRST_URL: Final = "https://www.arbeitnow.com/api/job-board-api"
_PAGE_URL_PREFIX: Final = "https://www.arbeitnow.com/"
_MAX_PAGES: Final = 5


@dataclass(frozen=True, slots=True)
class ArbeitnowPage:
    """What one page of the Arbeitnow job-board API held.

    Attributes:
        batch: The drafts of the readable jobs and the count of the jobs dropped.
        next_url: The address of the next page, or `None` on the last page.
    """

    batch: SourceBatch
    next_url: str | None


class ArbeitnowBoard:
    """The Arbeitnow job-board API, read page by page."""

    def __init__(self, key: SourceKey) -> None:
        """Name the source to read.

        Args:
            key: The source's key within its kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.ARBEITNOW, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch up to five pages of jobs, following each page's `links.next`.

        The first page is requested conditionally; later pages are not.

        Args:
            client: The client the requests go through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped,
            over every page read.

        Raises:
            ParseError: A response is not a jobs listing.
            SourcesError: The client could not complete a request.
        """
        drafts: list[SightingDraft] = []
        rejected = 0
        url = _FIRST_URL
        for page_number in range(_MAX_PAGES):
            response = await client.get(url, conditional=page_number == 0)
            page = parse_arbeitnow(response.content, self._key)
            drafts.extend(page.batch.drafts)
            rejected += page.batch.rejected
            if page.next_url is None:
                break
            url = page.next_url
        return SourceBatch(drafts=drafts, rejected=rejected)


def parse_arbeitnow(body: bytes, key: SourceKey) -> ArbeitnowPage:
    """Read the jobs out of one page of the Arbeitnow job-board API.

    Arbeitnow states no salary and no country, so none is mapped.

    Args:
        body: The response body, a JSON object with a `data` list and an
            optional `links.next`.
        key: The source key the drafts are filed under.

    Returns:
        The drafts of the readable jobs, the count of the jobs dropped, and the
        address of the next page.

    Raises:
        ParseError: The body is not JSON, holds no `data` list, or has a
            `links` that is not an object or a `links.next` that is neither
            null nor an `https://www.arbeitnow.com/` address.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    jobs = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        reason = "body has no 'data' list"
        raise ParseError(reason)
    next_url = _next_url(payload)

    drafts: list[SightingDraft] = []
    rejected = 0
    for index, job in enumerate(jobs):
        try:
            drafts.append(_to_draft(job, key))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected arbeitnow job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return ArbeitnowPage(
        batch=SourceBatch(drafts=drafts, rejected=rejected), next_url=next_url
    )


def _next_url(payload: dict[str, Any]) -> str | None:
    """Return the address of the next page, or `None` when there is none.

    Raises:
        ParseError: `links` is not an object, or `links.next` is not text, or
            not an `https://www.arbeitnow.com/` address.
    """
    links = payload.get("links", {})
    if not isinstance(links, dict):
        reason = "'links' is not an object"
        raise ParseError(reason)
    next_url = links.get("next")
    if next_url is None or next_url == "":
        return None
    if not isinstance(next_url, str):
        reason = "'links.next' is not text"
        raise ParseError(reason)
    if not next_url.startswith(_PAGE_URL_PREFIX):
        reason = f"'links.next' {next_url!r} is not on arbeitnow.com"
        raise ParseError(reason)
    return next_url


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its slug, or by its position when it has none.

    The slug is written with `repr`, so a board cannot put line breaks or
    terminal escapes into the log.
    """
    if isinstance(job, dict) and isinstance(slug := job.get("slug"), str):
        return f"id {slug!r}"
    return f"index {index}"


def _to_draft(job: object, key: SourceKey) -> SightingDraft:
    """Map one job object to a draft.

    Raises:
        InvalidValueError: The job is not an object, a field it needs is
            missing or of the wrong type, or the draft fails validation.
    """
    if not isinstance(job, dict):
        raise InvalidValueError("job", "is not an object")
    return SightingDraft(
        source=SourceRef(SourceKind.ARBEITNOW, key),
        external_id=ExternalId(_required_text(job, "slug")),
        title=_required_text(job, "title"),
        company=_required_text(job, "company_name"),
        location=_optional_text(job, "location"),
        url=_required_text(job, "url"),
        posted_at=_posted_at(job),
        raw=_raw(job),
        remote=_remote(job),
    )


def _raw(job: dict[str, Any]) -> str:
    """Serialise the job back to JSON text.

    Raises:
        InvalidValueError: The job is nested too deeply to encode.
    """
    try:
        return json.dumps(job)
    except RecursionError as error:
        raise InvalidValueError("job", "is nested too deeply") from error


def _required_text(job: dict[str, Any], name: str) -> str:
    """Return a field that must be present and text."""
    value = job.get(name)
    if not isinstance(value, str):
        raise InvalidValueError(name, "is missing or not text")
    return _encodable(name, value)


def _optional_text(job: dict[str, Any], name: str) -> str | None:
    """Return a field that may be absent or null, but is text when present."""
    value = job.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError(name, "is not text")
    return _encodable(name, value)


def _encodable(name: str, value: str) -> str:
    """Return text that can be stored as UTF-8, such as one without lone surrogates.

    Raises:
        InvalidValueError: The text cannot be encoded as UTF-8.
    """
    try:
        value.encode()
    except UnicodeEncodeError as error:
        raise InvalidValueError(name, "is not valid Unicode text") from error
    return value


def _remote(job: dict[str, Any]) -> RemoteScope | None:
    """Read the `remote` flag: `False` is on-site, and `True` or absent is unknown.

    The flag does not say where a remote job may be worked from, so `True`
    gives `None` rather than a scope.

    Raises:
        InvalidValueError: The flag is neither a boolean nor null.
    """
    value = job.get("remote")
    if value is None or value is True:
        return None
    if value is False:
        return RemoteScope.NONE
    raise InvalidValueError("remote", "is not a boolean")


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `created_at`, epoch seconds, as a datetime; `None` when absent."""
    value = job.get("created_at")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidValueError("created_at", "is not a number")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise InvalidValueError("created_at", f"{value!r} is not a date") from error
