"""Jobicy remote engineering jobs as a source of sighting drafts.

`JobicyBoard` walks the Jobicy remote-jobs API one cursor page at a time through
the `PoliteClient` and hands each body to `parse_jobicy`, which turns it into a
`JobicyPage`. `parse_jobicy` is pure and never lets anything but a `ParseError`
escape. A job that cannot be read is logged at `WARNING` and counted in
`SourceBatch.rejected`.

Jobicy's terms allow one poll an hour. This module does not enforce that limit;
whatever schedules the fetches must keep to it.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote

from ijob_core.countries import parse_country
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKey,
    SourceKind,
    SourceRef,
)
from ijob_core.money import Money, Period

from ijob_sources.collect import SourceBatch
from ijob_sources.errors import ParseError
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_FIRST_URL: Final = (
    "https://jobicy.com/api/v2/remote-jobs?count=50&industry=engineering"
)
_MAX_PAGES: Final = 5
_PERIODS: Final = {"yearly": Period.YEAR, "hourly": Period.HOUR}


@dataclass(frozen=True, slots=True)
class JobicyPage:
    """What one page of the Jobicy remote-jobs API held.

    Attributes:
        batch: The drafts of the readable jobs and the count of the jobs dropped.
        next_cursor: The cursor of the next page, or `None` on the last page.
    """

    batch: SourceBatch
    next_cursor: str | None


class JobicyBoard:
    """The Jobicy remote engineering jobs, read page by page."""

    def __init__(self, key: SourceKey) -> None:
        """Name the source to read.

        Args:
            key: The source's key within its kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.JOBICY, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch up to five pages of jobs, following the cursor.

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
            page = parse_jobicy(response.content, self._key)
            drafts.extend(page.batch.drafts)
            rejected += page.batch.rejected
            if page.next_cursor is None:
                break
            url = f"{_FIRST_URL}&cursor={quote(page.next_cursor, safe='')}"
        return SourceBatch(drafts=drafts, rejected=rejected)


def parse_jobicy(body: bytes, key: SourceKey) -> JobicyPage:
    """Read the jobs out of one page of the Jobicy remote-jobs API.

    Args:
        body: The response body, a JSON object with a `jobs` list, a `hasMore`
            flag and a `nextCursor`.
        key: The source key the drafts are filed under.

    Returns:
        The drafts of the readable jobs, the count of the jobs dropped, and the
        cursor of the next page; the cursor is `None` unless `hasMore` is true
        and a cursor is given.

    Raises:
        ParseError: The body is not JSON, holds no `jobs` list, or has a
            `hasMore` that is not a boolean or a `nextCursor` that is neither
            text nor null.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        reason = "body has no 'jobs' list"
        raise ParseError(reason)
    has_more = payload.get("hasMore", False)
    if not isinstance(has_more, bool):
        reason = "'hasMore' is not a boolean"
        raise ParseError(reason)
    cursor = payload.get("nextCursor")
    if cursor is not None and not isinstance(cursor, str):
        reason = "'nextCursor' is not text"
        raise ParseError(reason)

    drafts: list[SightingDraft] = []
    rejected = 0
    for index, job in enumerate(jobs):
        try:
            drafts.append(_to_draft(job, key))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected jobicy job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return JobicyPage(
        batch=SourceBatch(drafts=drafts, rejected=rejected),
        next_cursor=(cursor or None) if has_more else None,
    )


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its id, or by its position when it has none."""
    if isinstance(job, dict) and (job_id := _raw_id(job)) is not None:
        return f"id {job_id}"
    return f"index {index}"


def _raw_id(job: dict[str, Any]) -> int | None:
    """Return the job's `id` when it is an integer, else `None`."""
    job_id = job.get("id")
    if isinstance(job_id, int) and not isinstance(job_id, bool):
        return job_id
    return None


def _to_draft(job: object, key: SourceKey) -> SightingDraft:
    """Map one job object to a draft.

    Raises:
        InvalidValueError: The job is not an object, a field it needs is
            missing or of the wrong type, or the draft fails validation.
    """
    if not isinstance(job, dict):
        raise InvalidValueError("job", "is not an object")
    job_id = _raw_id(job)
    if job_id is None:
        raise InvalidValueError("id", "is missing or not an integer")
    location = _optional_text(job, "jobGeo")
    return SightingDraft(
        source=SourceRef(SourceKind.JOBICY, key),
        external_id=ExternalId(str(job_id)),
        title=_required_text(job, "jobTitle"),
        company=_required_text(job, "companyName"),
        location=location,
        url=_required_text(job, "url"),
        posted_at=_posted_at(job),
        raw=_raw(job),
        salaries=_salaries(job),
        country=parse_country(location) if location is not None else None,
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


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `pubDate` as a datetime, or `None` when it is absent."""
    value = job.get("pubDate")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError("pubDate", "is not text")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidValueError("pubDate", f"{value!r} is not a date") from error


def _salaries(job: dict[str, Any]) -> tuple[Money, ...]:
    """Return the stated salary, or none when it is absent or cannot be built.

    A salary needs a currency and at least one positive bound. One that is
    present but unusable, such as a currency that is not text or not three
    capital letters, bounds the wrong way round or a bound that is not a finite
    number, is dropped without dropping the job.
    """
    currency = job.get("salaryCurrency")
    try:
        low = _bound(job, "salaryMin")
        high = _bound(job, "salaryMax")
        if not isinstance(currency, str) or (low is None and high is None):
            return ()
        period_name = job.get("salaryPeriod")
        period = _PERIODS.get(period_name) if isinstance(period_name, str) else None
        return (Money(low, high, currency, period),)
    except InvalidValueError:
        return ()


def _bound(job: dict[str, Any], name: str) -> Decimal | None:
    """Return a salary bound, or `None` when it is absent or not positive.

    Raises:
        InvalidValueError: The bound is neither a number nor null, or is not
            finite.
    """
    value = job.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidValueError(name, "is not a number")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise InvalidValueError(name, "is not a finite number")
    return amount if amount > 0 else None
