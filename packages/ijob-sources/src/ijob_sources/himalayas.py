"""Himalayas remote jobs as a source of sighting drafts.

`HimalayasBoard` walks the Himalayas jobs API one cursor page at a time through
the `PoliteClient` and hands each body to `parse_himalayas`, which turns it into
a `HimalayasPage`. `parse_himalayas` is pure and never lets anything but a
`ParseError` escape. A job that cannot be read is logged at `WARNING` and
counted in `SourceBatch.rejected`.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote

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
from ijob_core.money import Money, Period

from ijob_sources.collect import SourceBatch
from ijob_sources.errors import ParseError
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_FIRST_URL: Final = "https://himalayas.app/jobs/api?limit=20"
_MAX_PAGES: Final = 5
_LOCATION_SEPARATOR: Final = ", "
_PERIODS: Final = {
    "annual": Period.YEAR,
    "monthly": Period.MONTH,
    "hourly": Period.HOUR,
}


@dataclass(frozen=True, slots=True)
class HimalayasPage:
    """What one page of the Himalayas jobs API held.

    Attributes:
        batch: The drafts of the readable jobs and the count of the jobs dropped.
        next_cursor: The cursor of the next page, or `None` on the last page.
    """

    batch: SourceBatch
    next_cursor: str | None


class HimalayasBoard:
    """The Himalayas jobs API, read page by page."""

    def __init__(self, key: SourceKey) -> None:
        """Name the source to read.

        Args:
            key: The source's key within its kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.HIMALAYAS, self._key)

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
            page = parse_himalayas(response.content, self._key)
            drafts.extend(page.batch.drafts)
            rejected += page.batch.rejected
            if page.next_cursor is None:
                break
            url = f"{_FIRST_URL}&cursor={quote(page.next_cursor, safe='')}"
        return SourceBatch(drafts=drafts, rejected=rejected)


def parse_himalayas(body: bytes, key: SourceKey) -> HimalayasPage:
    """Read the jobs out of one page of the Himalayas jobs API.

    Args:
        body: The response body, a JSON object with a `jobs` list and an
            optional `nextCursor`.
        key: The source key the drafts are filed under.

    Returns:
        The drafts of the readable jobs, the count of the jobs dropped, and the
        cursor of the next page.

    Raises:
        ParseError: The body is not JSON, holds no `jobs` list, or has a
            `nextCursor` that is neither text nor null.
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
                "rejected himalayas job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return HimalayasPage(
        batch=SourceBatch(drafts=drafts, rejected=rejected),
        next_cursor=cursor or None,
    )


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its `guid`, or by its position when it has none.

    The guid is written with `repr`, so a board cannot put line breaks or
    terminal escapes into the log.
    """
    if isinstance(job, dict) and isinstance(guid := job.get("guid"), str):
        return f"id {guid!r}"
    return f"index {index}"


def _to_draft(job: object, key: SourceKey) -> SightingDraft:
    """Map one job object to a draft.

    Raises:
        InvalidValueError: The job is not an object, a field it needs is
            missing or of the wrong type, or the draft fails validation.
    """
    if not isinstance(job, dict):
        raise InvalidValueError("job", "is not an object")
    restrictions = _restrictions(job)
    country, remote = _remote_scope(restrictions)
    return SightingDraft(
        source=SourceRef(SourceKind.HIMALAYAS, key),
        external_id=ExternalId(_required_text(job, "guid")),
        title=_required_text(job, "title"),
        company=_required_text(job, "companyName"),
        location=_LOCATION_SEPARATOR.join(restrictions) or None,
        url=_required_text(job, "applicationLink"),
        posted_at=_posted_at(job),
        raw=_raw(job),
        salaries=_salaries(job),
        country=country,
        remote=remote,
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
    """Return a field that must be present and text that UTF-8 can encode."""
    value = job.get(name)
    if not isinstance(value, str):
        raise InvalidValueError(name, "is missing or not text")
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


def _restrictions(job: dict[str, Any]) -> list[str]:
    """Return the countries the job is limited to; empty when it is not limited."""
    value = job.get("locationRestrictions")
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidValueError("locationRestrictions", "is not a list")
    return [_encodable("locationRestrictions", _restriction(item)) for item in value]


def _restriction(item: object) -> str:
    """Return one location restriction, which must be text."""
    if not isinstance(item, str):
        raise InvalidValueError(
            "locationRestrictions", "holds a value that is not text"
        )
    return item


def _remote_scope(restrictions: list[str]) -> tuple[CountryCode | None, RemoteScope]:
    """Read where the job may be worked from out of its location restrictions.

    No restriction is worldwide, one that names a country is that country, and
    anything else, including one that names no country, is a region.
    """
    if not restrictions:
        return None, RemoteScope.WORLDWIDE
    if len(restrictions) == 1 and (country := parse_country(restrictions[0])):
        return country, RemoteScope.COUNTRY
    return None, RemoteScope.REGION


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `pubDate`, epoch seconds, as a datetime; `None` when it is absent."""
    value = job.get("pubDate")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidValueError("pubDate", "is not a number")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise InvalidValueError("pubDate", f"{value!r} is not a date") from error


def _salaries(job: dict[str, Any]) -> tuple[Money, ...]:
    """Return the stated salary, or none when it is absent or cannot be built.

    A salary needs a currency and at least one positive bound. One that is
    present but unusable, such as a currency that is not text or not three
    capital letters, bounds the wrong way round or a bound that is not a finite
    number, is dropped without dropping the job.
    """
    currency = job.get("currency")
    try:
        low = _bound(job, "minSalary")
        high = _bound(job, "maxSalary")
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
