"""Gem job boards as a source of sighting drafts.

`GemBoard` fetches one board through the `PoliteClient`, hands the body to
`parse_gem`, which turns it into a `SourceBatch`, and checks that the board
describes the company it was registered for. `parse_gem` is pure and never lets
anything but a `ParseError` escape. A job that cannot be read is logged at
`WARNING` and counted in `SourceBatch.rejected`. A job whose id, title or
address cannot be read is rejected; every other field (location, publication
date, location type and department) is optional, and one that cannot be read
is left out while the job is kept.
"""

import json
import logging
from datetime import datetime
from typing import Any, NamedTuple

from ijob_core.countries import parse_country
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
from ijob_sources.identity import check_identity
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_BOARD_URL = "https://api.gem.com/job_board/v0/{key}/job_posts/"
_ON_SITE_TYPES = frozenset({"in_office", "hybrid"})


class _Listing(NamedTuple):
    """A parsed board with the descriptions the identity check reads."""

    batch: SourceBatch
    listed: int
    texts: list[str]


class GemBoard:
    """One Gem job board, read through the Gem job board API."""

    def __init__(self, key: SourceKey, *, company: str) -> None:
        """Name the board to read.

        Args:
            key: The board's slug, as it appears in its Gem URL.
            company: The company the board is registered for; Gem names none.
        """
        self._key = key
        self._company = company

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.GEM, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the board's jobs in one conditional request.

        Args:
            client: The client the request goes through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped.

        Raises:
            SourceNotFoundError: Gem has no board with this key.
            ParseError: The response is not a list of job posts.
            IdentityMismatchError: No job description names the company.
            SourcesError: The client could not complete the request.
        """
        response = await client.get(_BOARD_URL.format(key=self._key), conditional=True)
        listing = _read(response.content, self._key, self._company)
        check_identity(
            self.ref,
            self._company,
            names=[],
            texts=listing.texts,
            count=listing.listed,
        )
        return listing.batch


def parse_gem(body: bytes, key: SourceKey, company: str) -> SourceBatch:
    """Read the jobs out of a Gem job board response.

    Args:
        body: The response body, a JSON array of job posts.
        key: The board the response came from.
        company: The company the board is registered for; a Gem job does not
            name its own.

    Returns:
        The drafts of the readable jobs and the count of the jobs dropped.

    Raises:
        ParseError: The body is not JSON, or is not an array.
    """
    return _read(body, key, company).batch


def _read(body: bytes, key: SourceKey, company: str) -> _Listing:
    """Read the jobs out of a board response, keeping every job's description.

    Raises:
        ParseError: The body is not JSON, or is not an array.
    """
    try:
        jobs = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    if not isinstance(jobs, list):
        reason = "body is not an array of job posts"
        raise ParseError(reason)

    drafts: list[SightingDraft] = []
    texts: list[str] = []
    rejected = 0
    for index, job in enumerate(jobs):
        text = job.get("content_plain") if isinstance(job, dict) else None
        texts.append(text if isinstance(text, str) else "")
        try:
            drafts.append(_to_draft(job, key, company))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected gem job %s from %s: %s", _job_label(job, index), key, error
            )
    return _Listing(SourceBatch(drafts=drafts, rejected=rejected), len(jobs), texts)


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its id, or by its position when it has none.

    The id is written with `repr`, so a board cannot put line breaks or
    terminal escapes into the log.
    """
    if isinstance(job, dict) and isinstance(job_id := job.get("id"), str):
        return f"id {job_id!r}"
    return f"index {index}"


def _to_draft(job: object, key: SourceKey, company: str) -> SightingDraft:
    """Map one job object to a draft.

    Raises:
        InvalidValueError: The job is not an object, a field it needs is
            missing or of the wrong type, or the draft fails validation.
    """
    if not isinstance(job, dict):
        raise InvalidValueError("job", "is not an object")
    location = _location(job)
    return SightingDraft(
        source=SourceRef(SourceKind.GEM, key),
        external_id=ExternalId(_required_text(job, "id")),
        title=_required_text(job, "title"),
        company=company,
        location=location,
        url=_required_text(job, "absolute_url"),
        posted_at=_posted_at(job),
        raw=_raw(job),
        country=parse_country(location) if location is not None else None,
        remote=_remote(job),
        role_hint=_role_hint(job),
    )


def _remote(job: dict[str, Any]) -> RemoteScope | None:
    """Return `NONE` for an in-office or hybrid job, else `None` (unknown)."""
    location_type = job.get("location_type")
    if isinstance(location_type, str) and location_type in _ON_SITE_TYPES:
        return RemoteScope.NONE
    return None


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
    """Return `location.name`, or `None` when it is absent or unreadable."""
    location = job.get("location")
    return _readable_text(location, "name") if isinstance(location, dict) else None


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `first_published_at` as an aware datetime, else `None`."""
    value = job.get("first_published_at")
    if not isinstance(value, str):
        return None
    try:
        posted = datetime.fromisoformat(value)
    except ValueError:
        return None
    return posted if posted.utcoffset() is not None else None


def _readable_text(obj: dict[str, Any], name: str) -> str | None:
    """Return a field that is text storable as UTF-8, else `None`."""
    value = obj.get(name)
    if not isinstance(value, str):
        return None
    try:
        value.encode()
    except UnicodeEncodeError:
        return None
    return value


def _role_hint(job: dict[str, Any]) -> str | None:
    """Return the name of the first department that has a readable one."""
    departments = job.get("departments")
    if not isinstance(departments, list):
        return None
    for department in departments:
        if isinstance(department, dict) and (
            name := _readable_text(department, "name")
        ):
            return name
    return None
