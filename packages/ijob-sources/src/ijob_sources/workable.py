"""Workable accounts as a source of sighting drafts.

`WorkableAccount` fetches one account's widget through the `PoliteClient`,
hands the body to `parse_workable`, and checks that the widget belongs to the
company the account was registered for. `parse_workable` is pure and never lets
anything but a `ParseError` escape. A job whose shortcode, title or address
cannot be read is logged at `WARNING` and counted in `SourceBatch.rejected`.
Every other field (city, publication date, function, country and the
telecommuting flag) is optional, and one that cannot be read is left out while
the job is kept.
"""

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

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
from ijob_sources.identity import check_identity
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_WIDGET_URL = "https://apply.workable.com/api/v1/widget/accounts/{key}"


class WorkableAccount:
    """One Workable account, read through its public widget API."""

    def __init__(self, key: SourceKey, *, company: str) -> None:
        """Name the account to read and the company it must belong to.

        Args:
            key: The account's name, as it appears in its Workable URL.
            company: The company the account was registered for. It names the
                company of every draft, and the fetch fails if the widget does
                not mention it.
        """
        self._key = key
        self._company = company

    @property
    def ref(self) -> SourceRef:
        """The named source this account is."""
        return SourceRef(SourceKind.WORKABLE, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the account's widget in one conditional request.

        Args:
            client: The client the request goes through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped.

        Raises:
            SourceNotFoundError: Workable has no account with this key.
            ParseError: The response is not a widget listing.
            IdentityMismatchError: The widget does not mention the company.
            SourcesError: The client could not complete the request.
        """
        response = await client.get(_WIDGET_URL.format(key=self._key), conditional=True)
        widget = _load_widget(response.content)
        jobs: list[Any] = widget["jobs"]
        batch = _to_batch(jobs, self._key, self._company)
        check_identity(
            self.ref,
            self._company,
            names=_text_of(widget, "name"),
            texts=_text_of(widget, "description"),
            count=len(jobs),
        )
        return batch


def parse_workable(body: bytes, key: SourceKey, company: str) -> SourceBatch:
    """Read the jobs out of a Workable widget response.

    Args:
        body: The response body, a JSON object with a `jobs` list.
        key: The account the response came from.
        company: The company the account belongs to.

    Returns:
        The drafts of the readable jobs and the count of the jobs dropped.

    Raises:
        ParseError: The body is not JSON, or holds no `jobs` list.
    """
    return _to_batch(_load_widget(body)["jobs"], key, company)


def _load_widget(body: bytes) -> dict[str, Any]:
    """Decode the body as a widget object with a `jobs` list.

    Raises:
        ParseError: The body is not JSON, or holds no `jobs` list.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        reason = "body has no 'jobs' list"
        raise ParseError(reason)
    return payload


def _text_of(widget: dict[str, Any], name: str) -> list[str]:
    """Return the widget's top-level field as a one-item list, or none if not text."""
    value = widget.get(name)
    return [value] if isinstance(value, str) else []


def _to_batch(jobs: list[Any], key: SourceKey, company: str) -> SourceBatch:
    """Turn each job into a draft, dropping and counting the unreadable."""
    drafts: list[SightingDraft] = []
    rejected = 0
    for index, job in enumerate(jobs):
        try:
            drafts.append(_to_draft(job, key, company))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected workable job %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return SourceBatch(drafts=drafts, rejected=rejected)


def _job_label(job: object, index: int) -> str:
    """Name a job for the log by its shortcode, or by its position when it has none.

    The shortcode is written with `repr`, so an account cannot put line breaks
    or terminal escapes into the log.
    """
    if isinstance(job, dict) and isinstance(code := job.get("shortcode"), str):
        return f"id {code!r}"
    return f"index {index}"


def _to_draft(job: object, key: SourceKey, company: str) -> SightingDraft:
    """Map one job object to a draft.

    Raises:
        InvalidValueError: The job is not an object, a field it needs is
            missing or of the wrong type, or the draft fails validation.
    """
    if not isinstance(job, dict):
        raise InvalidValueError("job", "is not an object")
    return SightingDraft(
        source=SourceRef(SourceKind.WORKABLE, key),
        external_id=ExternalId(_required_text(job, "shortcode")),
        title=_required_text(job, "title"),
        company=company,
        location=_readable_text(job, "city") or None,
        url=_required_text(job, "url"),
        posted_at=_posted_at(job),
        raw=_raw(job),
        country=_country(job),
        remote=_remote(job),
        role_hint=_readable_text(job, "function") or None,
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


def _country(job: dict[str, Any]) -> CountryCode | None:
    """Return the country code of the job's first location, or `None`."""
    locations = job.get("locations")
    if not isinstance(locations, list) or not locations:
        return None
    first = locations[0]
    code = _readable_text(first, "countryCode") if isinstance(first, dict) else None
    return parse_country(code) if code else None


def _remote(job: dict[str, Any]) -> RemoteScope | None:
    """Return `NONE` for a job that is not telecommuting, else `None` (unknown)."""
    telecommuting = job.get("telecommuting")
    if telecommuting is False:
        return RemoteScope.NONE
    return None


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `published_on` as midnight UTC of that day, or `None` if unreadable."""
    value = job.get("published_on")
    if not isinstance(value, str):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
