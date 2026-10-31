"""4dayweek.io engineering jobs as a source of sighting drafts.

`FourDayWeekBoard` walks the 4dayweek.io jobs API through the `PoliteClient`,
at most five pages, and hands each body to `parse_fourdayweek`, which turns it
into a `FourDayWeekPage`. `parse_fourdayweek` is pure and never lets anything
but a `ParseError` escape. A job that cannot be read is logged at `WARNING` and
counted in `SourceBatch.rejected`. No salary is mapped, because the API does
not document the units of its salary fields.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from ijob_core.countries import CountryCode, parse_country
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
_FIRST_PAGE_URL: Final = (
    "https://4dayweek.io/api/v2/jobs?category=engineering&limit=100"
)
_MAX_PAGES: Final = 5
_ON_SITE: Final = frozenset({"onsite", "hybrid"})


@dataclass(frozen=True, slots=True)
class FourDayWeekPage:
    """What one page of the jobs API held.

    Attributes:
        batch: The drafts of the readable jobs and the count of those dropped.
        next_page: The number of the page to request next, or `None` when the
            API says there are no more.
    """

    batch: SourceBatch
    next_page: int | None


class FourDayWeekBoard:
    """The engineering jobs on 4dayweek.io, read through its jobs API."""

    def __init__(self, key: SourceKey) -> None:
        """Name the board to read.

        Args:
            key: The board's key within its source kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.FOURDAYWEEK, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the board's first five pages at most.

        The first page is requested conditionally. Each next page number comes
        from the page number the API reported for the page before it.

        Args:
            client: The client the requests go through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped.

        Raises:
            ParseError: A response is not a jobs page.
            SourcesError: The client could not complete a request.
        """
        drafts: list[SightingDraft] = []
        rejected = 0
        url = _FIRST_PAGE_URL
        for fetched in range(_MAX_PAGES):
            response = await client.get(url, conditional=fetched == 0)
            page = parse_fourdayweek(response.content, self._key)
            drafts.extend(page.batch.drafts)
            rejected += page.batch.rejected
            if page.next_page is None:
                break
            url = f"{_FIRST_PAGE_URL}&page={page.next_page}"
        return SourceBatch(drafts=drafts, rejected=rejected)


def parse_fourdayweek(body: bytes, key: SourceKey) -> FourDayWeekPage:
    """Read the jobs out of one page of the jobs API.

    Args:
        body: The response body, a JSON object with a `data` list, the `page`
            number and the `has_more` flag.
        key: The board the page came from.

    Returns:
        The drafts of the readable jobs, the count of the jobs dropped and the
        number of the next page.

    Raises:
        ParseError: The body is not JSON, or lacks a `data` list, an integer
            `page` or a boolean `has_more`.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    if not isinstance(payload, dict):
        reason = "body is not an object"
        raise ParseError(reason)
    jobs = payload.get("data")
    page = payload.get("page")
    has_more = payload.get("has_more")
    if not isinstance(jobs, list):
        reason = "body has no 'data' list"
        raise ParseError(reason)
    if not isinstance(page, int) or isinstance(page, bool):
        reason = "body has no integer 'page'"
        raise ParseError(reason)
    if not isinstance(has_more, bool):
        reason = "body has no boolean 'has_more'"
        raise ParseError(reason)

    drafts: list[SightingDraft] = []
    rejected = 0
    for index, job in enumerate(jobs):
        try:
            drafts.append(_to_draft(job, key))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected 4dayweek job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return FourDayWeekPage(
        batch=SourceBatch(drafts=drafts, rejected=rejected),
        next_page=page + 1 if has_more else None,
    )


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its id, or by its position when it has none.

    The id is written with `repr`, so a board cannot put line breaks or
    terminal escapes into the log.
    """
    if isinstance(job, dict) and isinstance(job_id := job.get("id"), str):
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
    location, country = _place(job)
    return SightingDraft(
        source=SourceRef(SourceKind.FOURDAYWEEK, key),
        external_id=ExternalId(_required_text(job, "id")),
        title=_required_text(job, "title"),
        company=_company(job),
        location=location,
        url=_required_text(job, "url"),
        posted_at=_posted_at(job),
        raw=_raw(job),
        country=country,
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


def _optional_text(job: dict[str, Any], name: str, *, label: str) -> str | None:
    """Return a field that may be absent or null, but is text when present."""
    value = job.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError(label, "is not text")
    return _encodable(label, value)


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


def _company(job: dict[str, Any]) -> str:
    """Return `company.name`, which every job must have."""
    company = job.get("company")
    if not isinstance(company, dict):
        raise InvalidValueError("company", "is missing or not an object")
    return _required_text(company, "name")


def _place(job: dict[str, Any]) -> tuple[str | None, CountryCode | None]:
    """Return the first location as `"city, country"` and its country code.

    A location without a city is just the country's name. A job without
    locations has neither.

    Raises:
        InvalidValueError: `locations` is not a list, its first element is not
            an object, or a place name in the first location is not text.
    """
    locations = job.get("locations")
    if locations is None:
        return None, None
    if not isinstance(locations, list):
        raise InvalidValueError("locations", "is not a list")
    if not locations:
        return None, None
    first = locations[0]
    if not isinstance(first, dict):
        raise InvalidValueError("locations", "holds something that is not an object")
    city = _optional_text(first, "city", label="locations.city")
    country = _optional_text(first, "country", label="locations.country")
    parts = [part for part in (city, country) if part]
    location = ", ".join(parts) or None
    return location, parse_country(country) if country else None


def _remote(job: dict[str, Any]) -> RemoteScope | None:
    """Return `NONE` for an on-site or hybrid job, and `None` otherwise.

    A remote job says nothing about where it may be worked from.
    """
    arrangement = job.get("work_arrangement")
    if isinstance(arrangement, str) and arrangement in _ON_SITE:
        return RemoteScope.NONE
    return None


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `posted_at` as a datetime, or `None` when it is absent."""
    value = job.get("posted_at")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError("posted_at", "is not text")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidValueError("posted_at", f"{value!r} is not a date") from error
