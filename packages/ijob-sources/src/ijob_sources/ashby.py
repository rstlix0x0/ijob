"""Ashby job boards as a source of sighting drafts.

`AshbyBoard` fetches one board through the `PoliteClient`, hands the body to
`parse_ashby`, which turns it into a `SourceBatch`, and checks that the board
describes the company it was registered for. `parse_ashby` is pure and never
lets anything but a `ParseError` escape. A job that cannot be read is logged at
`WARNING` and counted in `SourceBatch.rejected`. A job whose id, title or
address cannot be read is rejected; every other field (location, publication
date, department, country, workplace type and pay) is optional, and one that
cannot be read is left out while the job is kept.
"""

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, NamedTuple

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
from ijob_sources.identity import check_identity
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_BOARD_URL = (
    "https://api.ashbyhq.com/posting-api/job-board/{key}?includeCompensation=true"
)
_ON_SITE_TYPES: Final = frozenset({"OnSite", "Hybrid"})
_PERIODS: Final = {"1 YEAR": Period.YEAR, "1 MONTH": Period.MONTH}


class _Listing(NamedTuple):
    """A parsed board with the descriptions the identity check reads."""

    batch: SourceBatch
    listed: int
    texts: list[str]


class AshbyBoard:
    """One Ashby job board, read through the Ashby posting API."""

    def __init__(self, key: SourceKey, *, company: str) -> None:
        """Name the board to read.

        Args:
            key: The board's name, as it appears in its Ashby URL.
            company: The company the board is registered for; Ashby names none.
        """
        self._key = key
        self._company = company

    @property
    def ref(self) -> SourceRef:
        """The named source this board is."""
        return SourceRef(SourceKind.ASHBY, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the board's jobs and their pay in one conditional request.

        Args:
            client: The client the request goes through.

        Returns:
            The drafts of the readable jobs and the count of the jobs dropped.

        Raises:
            SourceNotFoundError: Ashby has no board with this key.
            ParseError: The response is not a board listing.
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


def parse_ashby(body: bytes, key: SourceKey, company: str) -> SourceBatch:
    """Read the jobs out of an Ashby job board response.

    Args:
        body: The response body, a JSON object with a `jobs` list.
        key: The board the response came from.
        company: The company the board is registered for; an Ashby job does not
            name its own.

    Returns:
        The drafts of the readable jobs and the count of the jobs dropped.

    Raises:
        ParseError: The body is not JSON, or holds no `jobs` list.
    """
    return _read(body, key, company).batch


def _read(body: bytes, key: SourceKey, company: str) -> _Listing:
    """Read the jobs out of a board response, keeping every job's description.

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
    texts: list[str] = []
    rejected = 0
    for index, job in enumerate(jobs):
        text = job.get("descriptionPlain") if isinstance(job, dict) else None
        texts.append(text if isinstance(text, str) else "")
        try:
            drafts.append(_to_draft(job, key, company))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected ashby job %s from %s: %s", _job_label(job, index), key, error
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
    return SightingDraft(
        source=SourceRef(SourceKind.ASHBY, key),
        external_id=ExternalId(_required_text(job, "id")),
        title=_required_text(job, "title").strip(),
        company=company,
        location=_readable_text(job, "location"),
        url=_required_text(job, "jobUrl"),
        posted_at=_posted_at(job),
        raw=_raw(job),
        salaries=_salaries(job),
        country=_country(job),
        remote=_remote(job),
        role_hint=_readable_text(job, "department") or None,
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


def _readable_text(job: dict[str, Any], name: str) -> str | None:
    """Return a field that is text storable as UTF-8, else `None`."""
    value = job.get(name)
    if not isinstance(value, str):
        return None
    try:
        value.encode()
    except UnicodeEncodeError:
        return None
    return value


def _remote(job: dict[str, Any]) -> RemoteScope | None:
    """Return `NONE` for an on-site or hybrid job, else `None` (unknown)."""
    workplace_type = job.get("workplaceType")
    if isinstance(workplace_type, str) and workplace_type in _ON_SITE_TYPES:
        return RemoteScope.NONE
    return None


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
    """Return `publishedAt` as an aware datetime, else `None`."""
    value = job.get("publishedAt")
    if not isinstance(value, str):
        return None
    try:
        posted = datetime.fromisoformat(value)
    except ValueError:
        return None
    return posted if posted.utcoffset() is not None else None


def _country(job: dict[str, Any]) -> CountryCode | None:
    """Return the country named by `address.postalAddress.addressCountry`."""
    address = job.get("address")
    postal = address.get("postalAddress") if isinstance(address, dict) else None
    country = postal.get("addressCountry") if isinstance(postal, dict) else None
    return parse_country(country) if isinstance(country, str) else None


def _salaries(job: dict[str, Any]) -> tuple[Money, ...]:
    """Return one `Money` per usable `Salary` summary component.

    A component without a currency or a positive bound, or one that `Money`
    refuses, is left out. An interval other than a year or a month gives a
    `Money` with no period.
    """
    compensation = job.get("compensation")
    components = (
        compensation.get("summaryComponents")
        if isinstance(compensation, dict)
        else None
    )
    if not isinstance(components, list):
        return ()
    salaries: list[Money] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        if component.get("compensationType") != "Salary":
            continue
        low = _amount(component.get("minValue"))
        high = _amount(component.get("maxValue"))
        currency = component.get("currencyCode")
        if not isinstance(currency, str) or (low is None and high is None):
            continue
        interval = component.get("interval")
        period = _PERIODS.get(interval) if isinstance(interval, str) else None
        try:
            salaries.append(Money(low, high, currency, period))
        except InvalidValueError:
            continue
    return tuple(salaries)


def _amount(value: object) -> Decimal | None:
    """Return a positive number as a `Decimal`, else `None`."""
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return Decimal(str(value))
    return None
