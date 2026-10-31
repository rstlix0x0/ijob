"""Lever job sites as a source of sighting drafts.

`LeverSite` fetches one site's postings through the `PoliteClient`, hands the
body to `parse_lever`, and checks that the postings belong to the company the
site was registered for. `parse_lever` is pure and never lets anything but a
`ParseError` escape. A posting whose id, title or address cannot be read is
logged at `WARNING` and counted in `SourceBatch.rejected`. Every other field
(location, department, country, creation time and workplace type) is optional,
and one that cannot be read is left out while the posting is kept.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
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
_POSTINGS_URL = "https://api.lever.co/v0/postings/{key}?mode=json"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NOT_REMOTE = frozenset({"onsite", "hybrid"})


class LeverSite:
    """One Lever job site, read through the Lever postings API."""

    def __init__(self, key: SourceKey, *, company: str) -> None:
        """Name the site to read and the company it must belong to.

        Args:
            key: The site's name, as it appears in its Lever URL.
            company: The company the site was registered for. Lever postings
                carry no company name, so this names the company of every
                draft, and the fetch fails if the postings do not mention it.
        """
        self._key = key
        self._company = company

    @property
    def ref(self) -> SourceRef:
        """The named source this site is."""
        return SourceRef(SourceKind.LEVER, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the site's postings in one conditional request.

        Args:
            client: The client the request goes through.

        Returns:
            The drafts of the readable postings and the count of the postings
            dropped.

        Raises:
            SourceNotFoundError: Lever has no site with this key.
            ParseError: The response is not a postings array.
            IdentityMismatchError: The postings do not mention the company.
            SourcesError: The client could not complete the request.
        """
        response = await client.get(
            _POSTINGS_URL.format(key=self._key), conditional=True
        )
        jobs = _load_jobs(response.content)
        batch = _to_batch(jobs, self._key, self._company)
        check_identity(
            self.ref,
            self._company,
            names=(),
            texts=_descriptions(jobs),
            count=len(jobs),
        )
        return batch


def parse_lever(body: bytes, key: SourceKey, company: str) -> SourceBatch:
    """Read the postings out of a Lever site response.

    Args:
        body: The response body, a JSON array of postings.
        key: The site the response came from.
        company: The company the site belongs to; Lever names none itself.

    Returns:
        The drafts of the readable postings and the count of the postings
        dropped.

    Raises:
        ParseError: The body is not JSON, or is not an array.
    """
    return _to_batch(_load_jobs(body), key, company)


def _load_jobs(body: bytes) -> list[Any]:
    """Decode the body as the postings array.

    Raises:
        ParseError: The body is not JSON, or is not an array.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    if not isinstance(payload, list):
        reason = "body is not an array of postings"
        raise ParseError(reason)
    return payload


def _descriptions(jobs: list[Any]) -> list[str]:
    """Collect the plain-text description of every listed posting that has one."""
    return [
        text
        for job in jobs
        if isinstance(job, dict)
        and isinstance(text := job.get("descriptionPlain"), str)
    ]


def _to_batch(jobs: list[Any], key: SourceKey, company: str) -> SourceBatch:
    """Turn each posting into a draft, dropping and counting the unreadable."""
    drafts: list[SightingDraft] = []
    rejected = 0
    for index, job in enumerate(jobs):
        try:
            drafts.append(_to_draft(job, key, company))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected lever posting %s from %s: %s",
                _job_label(job, index),
                key,
                error,
            )
    return SourceBatch(drafts=drafts, rejected=rejected)


def _job_label(job: object, index: int) -> str:
    """Name a posting for the log by its id, or by its position when it has none.

    The id is written with `repr`, so a site cannot put line breaks or
    terminal escapes into the log.
    """
    if isinstance(job, dict) and isinstance(job_id := job.get("id"), str):
        return f"id {job_id!r}"
    return f"index {index}"


def _to_draft(job: object, key: SourceKey, company: str) -> SightingDraft:
    """Map one posting object to a draft.

    Raises:
        InvalidValueError: The posting is not an object, a field it needs is
            missing or of the wrong type, or the draft fails validation.
    """
    if not isinstance(job, dict):
        raise InvalidValueError("posting", "is not an object")
    categories = _categories(job)
    return SightingDraft(
        source=SourceRef(SourceKind.LEVER, key),
        external_id=ExternalId(_required_text(job, "id")),
        title=_required_text(job, "text"),
        company=company,
        location=_readable_text(categories, "location"),
        url=_required_text(job, "hostedUrl"),
        posted_at=_posted_at(job),
        raw=_raw(job),
        country=_country(job),
        remote=_remote(job),
        role_hint=_readable_text(categories, "department") or None,
    )


def _raw(job: dict[str, Any]) -> str:
    """Serialise the posting back to JSON text.

    Raises:
        InvalidValueError: The posting is nested too deeply to encode.
    """
    try:
        return json.dumps(job)
    except RecursionError as error:
        raise InvalidValueError("posting", "is nested too deeply") from error


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


def _categories(job: dict[str, Any]) -> dict[str, Any]:
    """Return the posting's `categories` object, empty when it has none."""
    categories = job.get("categories")
    return categories if isinstance(categories, dict) else {}


def _country(job: dict[str, Any]) -> CountryCode | None:
    """Return the posting's `country` code, or `None` when unknown."""
    country = _readable_text(job, "country")
    return None if country is None else parse_country(country)


def _remote(job: dict[str, Any]) -> RemoteScope | None:
    """Return `NONE` for an on-site or hybrid posting, else `None` (unknown)."""
    workplace = job.get("workplaceType")
    if isinstance(workplace, str) and workplace in _NOT_REMOTE:
        return RemoteScope.NONE
    return None


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """Return `createdAt`, milliseconds since the epoch, as an aware UTC time."""
    value = job.get("createdAt")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        return None
