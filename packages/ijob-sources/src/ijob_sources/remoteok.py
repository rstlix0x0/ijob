"""RemoteOK remote jobs as a source of sighting drafts.

`RemoteOkBoard` reads the RemoteOK API in one conditional request through the
`PoliteClient` and hands the body to `parse_remoteok`, which turns it into a
`SourceBatch`. `parse_remoteok` is pure and never lets anything but a
`ParseError` escape. A job that cannot be read is logged at `WARNING` and
counted in `SourceBatch.rejected`.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any, Final

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
_URL: Final = "https://remoteok.com/api"


class RemoteOkBoard:
    """The RemoteOK API, read in one request."""

    def __init__(self, key: SourceKey) -> None:
        """Name the source to read.

        Args:
            key: The source's key within its kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.REMOTEOK, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the listing with one conditional request.

        Args:
            client: The client the request goes through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped.

        Raises:
            ParseError: The response is not a jobs listing.
            SourcesError: The client could not complete the request.
        """
        response = await client.get(_URL, conditional=True)
        return parse_remoteok(response.content, self._key)


def parse_remoteok(body: bytes, key: SourceKey) -> SourceBatch:
    """Read the jobs out of a RemoteOK API response.

    The first element of the list is the API's legal notice, an object with a
    `legal` key, not a job; it is skipped without being counted as rejected.
    Should the first element be anything else, it is read as a job. RemoteOK
    states no salary currency, so no salary is mapped.

    Args:
        body: The response body, a JSON list.
        key: The source key the drafts are filed under.

    Returns:
        The drafts of the readable jobs and the count of the jobs dropped.

    Raises:
        ParseError: The body is not a JSON list.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    if not isinstance(payload, list):
        reason = "body is not a list"
        raise ParseError(reason)

    drafts: list[SightingDraft] = []
    rejected = 0
    first = 1 if payload and _is_notice(payload[0]) else 0
    for index, job in enumerate(payload[first:], start=first):
        try:
            drafts.append(_to_draft(job, key))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected remoteok job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return SourceBatch(drafts=drafts, rejected=rejected)


def _is_notice(element: object) -> bool:
    """Say whether an element is the API's legal notice, an object with `legal`."""
    return isinstance(element, dict) and "legal" in element


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its id, or by its position when it has none.

    The id is written with `repr`, so a board cannot put line breaks or
    terminal escapes into the log.
    """
    if isinstance(job, dict) and (job_id := _raw_id(job)) is not None:
        return f"id {job_id!r}"
    return f"index {index}"


def _raw_id(job: dict[str, Any]) -> str | int | None:
    """Return the job's `id` when it is text or an integer, else `None`."""
    job_id = job.get("id")
    if isinstance(job_id, str | int) and not isinstance(job_id, bool):
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
        raise InvalidValueError("id", "is missing or not text or an integer")
    return SightingDraft(
        source=SourceRef(SourceKind.REMOTEOK, key),
        external_id=ExternalId(_encodable("id", str(job_id))),
        title=_required_text(job, "position"),
        company=_required_text(job, "company"),
        location=_optional_text(job, "location") or None,
        url=_required_text(job, "url"),
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
    """Return `epoch`, seconds since 1970, as a datetime; `None` when absent."""
    value = job.get("epoch")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidValueError("epoch", "is not a number")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise InvalidValueError("epoch", f"{value!r} is not a date") from error
