"""MyCareersFuture software-engineer jobs as a source of sighting drafts.

`MyCareersFutureBoard` walks the MyCareersFuture jobs API through the
`PoliteClient`, at most five pages, and hands each body to
`parse_mycareersfuture`, which turns it into a `MyCareersFuturePage`.
`parse_mycareersfuture` is pure and never lets anything but a `ParseError`
escape. A job that cannot be read is logged at `WARNING` and counted in
`SourceBatch.rejected`. No salary is mapped, because the API gives no currency.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

from ijob_core.countries import CountryCode
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKey,
    SourceKind,
    SourceRef,
)

from ijob_sources.collect import SourceBatch
from ijob_sources.errors import ParseError
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_API_ORIGIN: Final = "https://api.mycareersfuture.gov.sg/"
_FIRST_PAGE_URL: Final = (
    _API_ORIGIN
    + "v2/jobs?search=software%20engineer&limit=100&page=0&sortBy=new_posting_date"
)
_MAX_PAGES: Final = 5
_SINGAPORE: Final = CountryCode("SG")


@dataclass(frozen=True, slots=True)
class MyCareersFuturePage:
    """What one page of the jobs API held.

    Attributes:
        batch: The drafts of the readable jobs and the count of those dropped.
        next_href: The address of the next page as the API gave it, or `None`
            when the API links to none.
    """

    batch: SourceBatch
    next_href: str | None


class MyCareersFutureBoard:
    """The software-engineer search on MyCareersFuture, read through its API."""

    def __init__(self, key: SourceKey) -> None:
        """Name the board to read.

        Args:
            key: The board's key within its source kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.MYCAREERSFUTURE, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the search's first five pages at most.

        The first page is requested conditionally. Each next page is the
        address the page before it linked to.

        Args:
            client: The client the requests go through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped.

        Raises:
            ParseError: A response is not a jobs page, or links off the API.
            SourcesError: The client could not complete a request.
        """
        drafts: list[SightingDraft] = []
        rejected = 0
        url: str | None = _FIRST_PAGE_URL
        for fetched in range(_MAX_PAGES):
            if url is None:
                break
            response = await client.get(url, conditional=fetched == 0)
            page = parse_mycareersfuture(response.content, self._key)
            drafts.extend(page.batch.drafts)
            rejected += page.batch.rejected
            url = page.next_href
        return SourceBatch(drafts=drafts, rejected=rejected)


def parse_mycareersfuture(body: bytes, key: SourceKey) -> MyCareersFuturePage:
    """Read the jobs out of one page of the jobs API.

    Args:
        body: The response body, a JSON object with a `results` list and
            `_links`.
        key: The board the page came from.

    Returns:
        The drafts of the readable jobs, the count of the jobs dropped and the
        address of the next page.

    Raises:
        ParseError: The body is not JSON, holds no `results` list, or links to
            a next page that is not an address on the MyCareersFuture API.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    jobs = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        reason = "body has no 'results' list"
        raise ParseError(reason)
    next_href = _next_href(payload)

    drafts: list[SightingDraft] = []
    rejected = 0
    for index, job in enumerate(jobs):
        try:
            drafts.append(_to_draft(job, key))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected mycareersfuture job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return MyCareersFuturePage(
        batch=SourceBatch(drafts=drafts, rejected=rejected), next_href=next_href
    )


def _next_href(payload: dict[str, Any]) -> str | None:
    """Return the address `_links.next` gives, or `None` when it gives none.

    Raises:
        ParseError: The link is malformed, or is not an address on the API.
    """
    links = payload.get("_links")
    if links is None:
        return None
    if not isinstance(links, dict):
        reason = "'_links' is not an object"
        raise ParseError(reason)
    link = links.get("next")
    if link is None:
        return None
    href = link.get("href") if isinstance(link, dict) else None
    if not isinstance(href, str):
        reason = "'_links.next' has no 'href' text"
        raise ParseError(reason)
    if not href.startswith(_API_ORIGIN):
        reason = f"'_links.next' points off the API: {href!r}"
        raise ParseError(reason)
    return href


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its id, or by its position when it has none.

    The id is written with `repr`, so a board cannot put line breaks or
    terminal escapes into the log.
    """
    if isinstance(job, dict) and isinstance(job_id := job.get("uuid"), str):
        return f"id {job_id!r}"
    return f"index {index}"


def _to_draft(job: object, key: SourceKey) -> SightingDraft:
    """Map one job object to a draft.

    Raises:
        InvalidValueError: The job is not an object, a field it needs is
            missing or of the wrong type, or the draft fails validation.
    """
    if not isinstance(job, dict):
        raise InvalidValueError("job", "is not an object")
    metadata = job.get("metadata")
    if not isinstance(metadata, dict):
        raise InvalidValueError("metadata", "is missing or not an object")
    company = job.get("postedCompany")
    if not isinstance(company, dict):
        raise InvalidValueError("postedCompany", "is missing or not an object")
    return SightingDraft(
        source=SourceRef(SourceKind.MYCAREERSFUTURE, key),
        external_id=ExternalId(_required_text(job, "uuid")),
        title=_required_text(job, "title"),
        company=_required_text(company, "name"),
        location=None,
        url=_required_text(metadata, "jobDetailsUrl"),
        posted_at=_posted_at(metadata),
        raw=_raw(job),
        country=_SINGAPORE if _is_in_singapore(job) else None,
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


def _required_text(obj: dict[str, Any], name: str) -> str:
    """Return a field that must be present and text.

    Raises:
        InvalidValueError: The field is missing, not text, or not encodable as
            UTF-8, such as text with a lone surrogate.
    """
    value = obj.get(name)
    if not isinstance(value, str):
        raise InvalidValueError(name, "is missing or not text")
    try:
        value.encode()
    except UnicodeEncodeError as error:
        raise InvalidValueError(name, "is not valid Unicode text") from error
    return value


def _is_in_singapore(job: dict[str, Any]) -> bool:
    """Say whether the job's address is stated to be in Singapore.

    The address says only whether it is overseas, so an address that does not
    say is not taken to be in Singapore.
    """
    address = job.get("address")
    return isinstance(address, dict) and address.get("isOverseas") is False


def _posted_at(metadata: dict[str, Any]) -> datetime | None:
    """Return `newPostingDate` as midnight UTC, or `None` when it is absent."""
    value = metadata.get("newPostingDate")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError("newPostingDate", "is not text")
    try:
        day = date.fromisoformat(value)
    except ValueError as error:
        raise InvalidValueError("newPostingDate", f"{value!r} is not a date") from error
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
