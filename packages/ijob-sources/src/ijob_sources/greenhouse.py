"""Greenhouse job boards as a source of sighting drafts.

`GreenhouseBoard` fetches one board through the `PoliteClient` and hands the
body to `parse_jobs`, which turns it into a `SourceBatch`. `parse_jobs` is pure
and never lets anything but a `ParseError` escape. A job that cannot be read is
logged at `WARNING` and counted in `SourceBatch.rejected`.
"""

import json
import logging
from datetime import datetime
from typing import Any

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
_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{key}/jobs?content=true"


class GreenhouseBoard:
    """One Greenhouse job board, read through the Greenhouse boards API."""

    def __init__(self, key: SourceKey) -> None:
        """Name the board to read.

        Args:
            key: The board's token, as it appears in its Greenhouse URL.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.GREENHOUSE, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the board's jobs in one request.

        Args:
            client: The client the request goes through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped.

        Raises:
            SourceNotFoundError: Greenhouse has no board with this key.
            ParseError: The response is not a board listing.
            SourcesError: The client could not complete the request.
        """
        response = await client.get(_BOARD_URL.format(key=self._key))
        return parse_jobs(response.content, self._key)


def parse_jobs(body: bytes, key: SourceKey) -> SourceBatch:
    """Read the jobs out of a Greenhouse board response.

    Args:
        body: The response body, a JSON object with a `jobs` list.
        key: The board the response came from; it names the company of a job
            that does not name its own.

    Returns:
        The drafts of the readable jobs and the count of the jobs dropped.

    Raises:
        ParseError: The body is not JSON, or holds no `jobs` list.
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

    drafts: list[SightingDraft] = []
    rejected = 0
    for index, job in enumerate(jobs):
        try:
            drafts.append(_to_draft(job, key))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected greenhouse job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return SourceBatch(drafts=drafts, rejected=rejected)


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its id, or by its position when it has none.

    The id is written with `repr`, so a board cannot put line breaks or
    terminal escapes into the log.
    """
    if isinstance(job, dict) and (job_id := _raw_id(job)) is not None:
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
    company = _optional_text(job, "company_name")
    return SightingDraft(
        source=SourceRef(SourceKind.GREENHOUSE, key),
        external_id=ExternalId(_external_id(job)),
        title=_required_text(job, "title"),
        company=company or key,
        location=_location(job),
        url=_required_text(job, "absolute_url"),
        posted_at=_posted_at(job),
        raw=_raw(job),
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


def _raw_id(job: dict[str, Any]) -> str | int | None:
    """Return the job's `id` when it is text or an integer, else `None`."""
    job_id = job.get("id")
    if isinstance(job_id, str | int) and not isinstance(job_id, bool):
        return job_id
    return None


def _external_id(job: dict[str, Any]) -> str:
    """Return the job's `id` as text."""
    job_id = _raw_id(job)
    if job_id is None:
        raise InvalidValueError("id", "is missing or not text or an integer")
    return _encodable("id", str(job_id))


def _required_text(job: dict[str, Any], name: str) -> str:
    """Return a field that must be present and text."""
    value = job.get(name)
    if not isinstance(value, str):
        raise InvalidValueError(name, "is missing or not text")
    return _encodable(name, value)


def _optional_text(
    job: dict[str, Any], name: str, *, label: str | None = None
) -> str | None:
    """Return a field that may be absent or null, but is text when present."""
    value = job.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError(label or name, "is not text")
    return _encodable(label or name, value)


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


def _location(job: dict[str, Any]) -> str | None:
    """Return `location.name`, or `None` when the job names no location."""
    location = job.get("location")
    if location is None:
        return None
    if not isinstance(location, dict):
        raise InvalidValueError("location", "is not an object")
    return _optional_text(location, "name", label="location.name")


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `first_published` as a datetime, or `None` when it is absent."""
    value = job.get("first_published")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError("first_published", "is not text")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidValueError(
            "first_published", f"{value!r} is not a date"
        ) from error
