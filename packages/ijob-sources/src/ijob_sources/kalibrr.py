"""Kalibrr software-engineer jobs as a source of sighting drafts.

`KalibrrBoard` walks the Kalibrr job-board search through the `PoliteClient`,
at most five pages, and hands each body to `parse_kalibrr`, which turns it into
a `KalibrrPage`. `parse_kalibrr` is pure and never lets anything but a
`ParseError` escape. A job that cannot be read is logged at `WARNING` and
counted in `SourceBatch.rejected`. Pages overlap, so a job whose id the same
fetch already saw, whether it was drafted or rejected, is neither a draft nor a
rejection the second time.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote

from ijob_core.countries import CountryCode, parse_country
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
_SEARCH_URL: Final = (
    "https://www.kalibrr.com/kjs/job_board/search"
    "?limit=15&offset={offset}&text=software%20engineer"
)
_JOB_URL: Final = "https://www.kalibrr.com/c/{code}/jobs/{id}/{slug}"
_PAGE_SIZE: Final = 15
_MAX_PAGES: Final = 5


@dataclass(frozen=True, slots=True)
class KalibrrPage:
    """What one page of the search held.

    Attributes:
        batch: The drafts of the readable jobs and the count of those dropped.
        count: How many jobs the search matched in all.
        rejected_ids: The ids of the dropped jobs that had a readable id, in
            page order. A dropped job without one is in `batch.rejected` only.
    """

    batch: SourceBatch
    count: int
    rejected_ids: tuple[ExternalId, ...] = ()


class KalibrrBoard:
    """The software-engineer search on Kalibrr, read through its job-board API."""

    def __init__(self, key: SourceKey) -> None:
        """Name the board to read.

        Args:
            key: The board's key within its source kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.KALIBRR, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the search's first five pages at most.

        The first page is requested conditionally. Paging goes by offset, in
        steps of one page, while the offset is below the count the search
        reports. A job whose id an earlier page, or the same page, already
        gave is dropped, and is not counted as rejected even when it was
        rejected the first time.

        Args:
            client: The client the requests go through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped.

        Raises:
            ParseError: A response is not a search page.
            SourcesError: The client could not complete a request.
        """
        drafts: list[SightingDraft] = []
        seen: set[ExternalId] = set()
        rejected = 0
        offset = 0
        for fetched in range(_MAX_PAGES):
            response = await client.get(
                _SEARCH_URL.format(offset=offset), conditional=fetched == 0
            )
            page = parse_kalibrr(response.content, self._key)
            for draft in page.batch.drafts:
                if draft.external_id not in seen:
                    seen.add(draft.external_id)
                    drafts.append(draft)
            rejected += page.batch.rejected - len(page.rejected_ids)
            for external_id in page.rejected_ids:
                if external_id not in seen:
                    seen.add(external_id)
                    rejected += 1
            offset += _PAGE_SIZE
            if offset >= page.count:
                break
        return SourceBatch(drafts=drafts, rejected=rejected)


def parse_kalibrr(body: bytes, key: SourceKey) -> KalibrrPage:
    """Read the jobs out of one page of the search.

    Args:
        body: The response body, a JSON object with a `jobs` list and the
            integer `count` of matches.
        key: The board the page came from.

    Returns:
        The drafts of the readable jobs, the count of the jobs dropped, the
        total count of matches and the ids of the dropped jobs that had one.

    Raises:
        ParseError: The body is not JSON, or lacks a `jobs` list or an integer
            `count`.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    if not isinstance(payload, dict):
        reason = "body is not an object"
        raise ParseError(reason)
    jobs = payload.get("jobs")
    count = payload.get("count")
    if not isinstance(jobs, list):
        reason = "body has no 'jobs' list"
        raise ParseError(reason)
    if not isinstance(count, int) or isinstance(count, bool):
        reason = "body has no integer 'count'"
        raise ParseError(reason)

    drafts: list[SightingDraft] = []
    rejected_ids: list[ExternalId] = []
    rejected = 0
    for index, job in enumerate(jobs):
        try:
            drafts.append(_to_draft(job, key))
        except InvalidValueError as error:
            rejected += 1
            if isinstance(job, dict) and (job_id := _raw_id(job)) is not None:
                rejected_ids.append(ExternalId(str(job_id)))
            _LOGGER.warning(
                "rejected kalibrr job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return KalibrrPage(
        batch=SourceBatch(drafts=drafts, rejected=rejected),
        count=count,
        rejected_ids=tuple(rejected_ids),
    )


def _raw_id(job: dict[str, Any]) -> int | None:
    """Return the job's `id` when it is an integer, else `None`."""
    job_id = job.get("id")
    if isinstance(job_id, int) and not isinstance(job_id, bool):
        return job_id
    return None


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its id, or by its position when it has none."""
    if isinstance(job, dict) and (job_id := _raw_id(job)) is not None:
        return f"id {job_id}"
    return f"index {index}"


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
    location, country = _place(job)
    salary = _salary(job)
    return SightingDraft(
        source=SourceRef(SourceKind.KALIBRR, key),
        external_id=ExternalId(str(job_id)),
        title=_required_text(job, "name"),
        company=_required_text(job, "company_name"),
        location=location,
        url=_url(job, job_id),
        posted_at=_posted_at(job),
        raw=_raw(job),
        salaries=() if salary is None else (salary,),
        country=country,
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


def _required_text(obj: dict[str, Any], name: str, *, label: str | None = None) -> str:
    """Return a field that must be present and text.

    Raises:
        InvalidValueError: The field is missing, not text, or not encodable as
            UTF-8, such as text with a lone surrogate.
    """
    value = obj.get(name)
    if not isinstance(value, str):
        raise InvalidValueError(label or name, "is missing or not text")
    return _encodable(label or name, value)


def _optional_text(obj: dict[str, Any], name: str, *, label: str) -> str | None:
    """Return a field that may be absent or null, but is text when present."""
    value = obj.get(name)
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


def _url(job: dict[str, Any], job_id: int) -> str:
    """Build the job's public address from its company code and slug.

    Raises:
        InvalidValueError: The company or its code, or the slug, is missing.
    """
    company = job.get("company")
    if not isinstance(company, dict):
        raise InvalidValueError("company", "is missing or not an object")
    code = _required_text(company, "code", label="company.code")
    slug = _required_text(job, "slug")
    return _JOB_URL.format(
        code=quote(code, safe=""), id=job_id, slug=quote(slug, safe="")
    )


def _place(job: dict[str, Any]) -> tuple[str | None, CountryCode | None]:
    """Return the job's city and the country code of its country.

    Raises:
        InvalidValueError: The location is present but not shaped as expected.
    """
    google = job.get("google_location")
    if google is None:
        return None, None
    if not isinstance(google, dict):
        raise InvalidValueError("google_location", "is not an object")
    components = google.get("address_components")
    if components is None:
        return None, None
    if not isinstance(components, dict):
        raise InvalidValueError("address_components", "is not an object")
    city = _optional_text(components, "city", label="address_components.city")
    country = _optional_text(components, "country", label="address_components.country")
    return city or None, parse_country(country) if country else None


def _salary(job: dict[str, Any]) -> Money | None:
    """Return the pay the job shows, or `None` when it shows none.

    The pay counts only when `salary_shown` is true, and only with a currency
    and at least one positive bound. Only a monthly interval gives a period.
    Pay that is present but unusable, such as a bound that is not a finite
    number, an unusable currency or bounds the wrong way round, is dropped
    without dropping the job.
    """
    if job.get("salary_shown") is not True:
        return None
    currency = job.get("salary_currency")
    try:
        low = _bound(job, "base_salary")
        high = _bound(job, "maximum_salary")
        if not isinstance(currency, str) or (low is None and high is None):
            return None
        period = Period.MONTH if job.get("salary_interval") == "month" else None
        return Money(min=low, max=high, currency=currency, period=period)
    except InvalidValueError:
        return None


def _bound(job: dict[str, Any], name: str) -> Decimal | None:
    """Return a salary bound when it is positive, or `None` when absent or not.

    A zero or negative bound counts as absent.

    Raises:
        InvalidValueError: The bound is neither null nor a finite number.
    """
    value = job.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidValueError(name, "is not a number")
    bound = Decimal(str(value))
    if not bound.is_finite():
        raise InvalidValueError(name, "is not a finite number")
    return bound if bound > 0 else None


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `activation_date` as a datetime, or `None` when it is absent."""
    value = job.get("activation_date")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError("activation_date", "is not text")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidValueError(
            "activation_date", f"{value!r} is not a date"
        ) from error
