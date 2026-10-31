"""schema.org `JobPosting` blocks, read out of HTML pages.

`job_postings` finds the `JobPosting` objects in a page's
`<script type="application/ld+json">` elements. `read_job_posting` turns one
into the `JobPostingFields` a source adapter needs to build a `SightingDraft`.
Neither function fetches anything. A field that is absent reads as `None` or
empty; a field that is present but unusable raises `InvalidValueError`, which
the adapter counts as one rejected job.
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Final, override

from ijob_core.countries import CountryCode, parse_country
from ijob_core.errors import InvalidValueError
from ijob_core.model import RemoteScope
from ijob_core.money import Money, Period

_JOB_POSTING: Final = "JobPosting"
_TELECOMMUTE: Final = "TELECOMMUTE"
_COUNTRY_TYPE: Final = "Country"
_LD_JSON: Final = "application/ld+json"
_PERIODS: Final = {
    "YEAR": Period.YEAR,
    "MONTH": Period.MONTH,
    "HOUR": Period.HOUR,
}


@dataclass(frozen=True, slots=True)
class JobPostingFields:
    """What a `JobPosting` states, in the shape a draft needs.

    Attributes:
        title: The job title, or `None` when the posting has none.
        company: The hiring organization's name, or `None`.
        location: A place name, or `None` when the posting names none.
        country: The country the posting's location names, or `None`.
        remote: `RemoteScope.COUNTRY` for a telecommute job open to one country,
            else `None`: the posting does not say how far remoteness reaches.
        salaries: The stated pay, empty when none is usable.
        posted_at: When the job was posted, as an aware datetime, or `None`
            when the posting gives no date or a date without an offset.
        identifier: The posting's own id as text, or `None`.
        url: The posting's own URL, or `None`.
    """

    title: str | None
    company: str | None
    location: str | None
    country: CountryCode | None
    remote: RemoteScope | None
    salaries: tuple[Money, ...]
    posted_at: datetime | None
    identifier: str | None
    url: str | None


class _LdJsonCollector(HTMLParser):
    """Gathers the text of every `application/ld+json` script element."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._open: list[str] | None = None

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        script_type = dict(attrs).get("type") or ""
        if script_type.strip().lower() == _LD_JSON:
            self._open = []

    @override
    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._open.append(data)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._open is not None:
            self.blocks.append("".join(self._open))
            self._open = None


def ld_json_blocks(html_text: str) -> list[object]:
    """Parse the JSON-LD scripts of a page.

    Args:
        html_text: The page's HTML.

    Returns:
        The parsed content of every `<script type="application/ld+json">`
        element, in page order. The script type is matched without regard to
        case. A script that is not valid JSON is skipped.
    """
    collector = _LdJsonCollector()
    collector.feed(html_text)
    collector.close()
    blocks: list[object] = []
    for text in collector.blocks:
        try:
            blocks.append(json.loads(text, strict=False))
        except (ValueError, RecursionError):
            continue
    return blocks


def job_postings(html_text: str) -> list[dict[str, Any]]:
    """Find the `JobPosting` objects in a page.

    Args:
        html_text: The page's HTML.

    Returns:
        Every object of type `JobPosting` in the page's JSON-LD scripts, in page
        order. Top-level lists and `@graph` members are searched too. A script
        that is not valid JSON is skipped.
    """
    found: list[dict[str, Any]] = []
    for block in ld_json_blocks(html_text):
        found.extend(_walk(block))
    return found


def _walk(root: object) -> list[dict[str, Any]]:
    """Return the `JobPosting` objects in a parsed block, in document order."""
    found: list[dict[str, Any]] = []
    pending: list[object] = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(reversed(node))
        elif isinstance(node, dict):
            if _is_job_posting(node):
                found.append(node)
            graph = node.get("@graph")
            if isinstance(graph, list):
                pending.extend(reversed(graph))
    return found


def _is_job_posting(node: dict[str, Any]) -> bool:
    """Say whether an object's `@type` is, or includes, `JobPosting`."""
    kind = node.get("@type")
    if isinstance(kind, list):
        return _JOB_POSTING in kind
    return kind == _JOB_POSTING


def read_job_posting(obj: dict[str, Any]) -> JobPostingFields:
    """Read the fields a draft needs from one `JobPosting` object.

    Args:
        obj: An object from `job_postings`.

    Returns:
        The fields the object states.

    Raises:
        InvalidValueError: A field is present but of the wrong type, holds text
            that cannot be stored, or states a date or a salary that is invalid.
    """
    location, country, remote = _place(obj)
    return JobPostingFields(
        title=_text(obj.get("title"), "title"),
        company=_company(obj),
        location=location,
        country=country,
        remote=remote,
        salaries=_salaries(obj.get("baseSalary")),
        posted_at=_posted_at(obj.get("datePosted")),
        identifier=_identifier(obj.get("identifier")),
        url=_text(obj.get("url"), "url"),
    )


def _text(value: object, name: str) -> str | None:
    """Return stripped text, or `None` when absent, null or blank.

    Raises:
        InvalidValueError: The value is not text, or cannot be encoded as UTF-8.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError(name, "is not text")
    try:
        value.encode()
    except UnicodeEncodeError as error:
        raise InvalidValueError(name, "is not valid Unicode text") from error
    return value.strip() or None


def _company(obj: dict[str, Any]) -> str | None:
    """Return `hiringOrganization.name`."""
    organization = obj.get("hiringOrganization")
    if organization is None:
        return None
    if not isinstance(organization, dict):
        raise InvalidValueError("hiringOrganization", "is not an object")
    return _text(organization.get("name"), "hiringOrganization.name")


def _as_list(value: object) -> list[object]:
    """Wrap a single value in a list; `None` becomes an empty list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _place(
    obj: dict[str, Any],
) -> tuple[str | None, CountryCode | None, RemoteScope | None]:
    """Read the location, country and remote scope of a posting."""
    localities: list[str] = []
    country_names: list[str] = []
    string_addresses: list[str] = []
    for place in _as_list(obj.get("jobLocation")):
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if isinstance(address, str):
            if text := _text(address, "jobLocation.address"):
                string_addresses.append(text)
        elif isinstance(address, dict):
            if locality := _text(
                address.get("addressLocality"), "jobLocation.address.addressLocality"
            ):
                localities.append(locality)
            if name := _country_name(address.get("addressCountry")):
                country_names.append(name)

    named = localities + string_addresses
    location = named[0] if named else (country_names[0] if country_names else None)
    return location, _country(country_names, string_addresses), _remote(obj)


def _country_name(value: object) -> str | None:
    """Return a country given as text or as an object with a `name`."""
    if isinstance(value, dict):
        value = value.get("name")
    return _text(value, "addressCountry")


def _country(names: list[str], string_addresses: list[str]) -> CountryCode | None:
    """Return the first country `parse_country` accepts.

    The `addressCountry` values are tried before a string address.
    """
    for text in (*names, *string_addresses):
        if (code := parse_country(text)) is not None:
            return code
    return None


def _remote(obj: dict[str, Any]) -> RemoteScope | None:
    """Return `COUNTRY` for a telecommute job that names exactly one country."""
    if obj.get("jobLocationType") != _TELECOMMUTE:
        return None
    countries = [
        requirement
        for requirement in _as_list(obj.get("applicantLocationRequirements"))
        if isinstance(requirement, dict) and requirement.get("@type") == _COUNTRY_TYPE
    ]
    return RemoteScope.COUNTRY if len(countries) == 1 else None


def _salaries(salary: object) -> tuple[Money, ...]:
    """Read `baseSalary` into zero or one `Money`.

    Raises:
        InvalidValueError: A bound is not a number, or the range is invalid.
    """
    if not isinstance(salary, dict):
        return ()
    currency = salary.get("currency")
    value = salary.get("value")
    if not isinstance(currency, str) or not isinstance(value, dict):
        return ()
    low = _bound(value.get("minValue"), "minValue")
    high = _bound(value.get("maxValue"), "maxValue")
    if low is None and high is None:
        return ()
    unit = value.get("unitText")
    period = _PERIODS.get(unit.upper()) if isinstance(unit, str) else None
    return (Money(low, high, currency, period),)


def _bound(value: object, name: str) -> Decimal | None:
    """Return a positive salary bound, or `None` when it is absent or not positive.

    Raises:
        InvalidValueError: The value is neither a number nor numeric text, or
            is not finite.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise InvalidValueError(name, "is not a number")
    try:
        bound = Decimal(str(value))
    except InvalidOperation as error:
        raise InvalidValueError(name, f"{value!r} is not a number") from error
    if not bound.is_finite():
        raise InvalidValueError(name, f"{value!r} is not a finite number")
    return bound if bound > 0 else None


def _posted_at(value: object) -> datetime | None:
    """Read `datePosted`: a date is midnight UTC, a naive datetime is `None`.

    Raises:
        InvalidValueError: The value is not text, or not an ISO 8601 date.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError("datePosted", "is not text")
    text = value.strip()
    try:
        if len(text) == len("2026-01-01"):
            return datetime.combine(date.fromisoformat(text), datetime.min.time(), UTC)
        posted = datetime.fromisoformat(text)
    except ValueError as error:
        raise InvalidValueError("datePosted", f"{value!r} is not a date") from error
    return posted if posted.tzinfo is not None else None


def _identifier(value: object) -> str | None:
    """Return `identifier.value`, or a scalar `identifier`, as text."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    return _text(str(value), "identifier")
