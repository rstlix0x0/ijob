"""Domain types for job sources, the postings seen on them and their sightings.

A source adapter returns `SightingDraft` values; the store turns them into
`Posting` and `Sighting` records. `parse_source_key`, `parse_source_ref` and
`SightingDraft` validate their input when called or built, so code that receives a
`SourceKey`, a `SourceRef` from `parse_source_ref`, or a draft can trust it;
`Posting` and `Sighting` are records the store builds from accepted drafts.

`SourceKind` names every kind of source. `RemoteScope` says where a remote job
may be worked from. A draft and a `Posting` can also carry salaries (`Money`), a
`CountryCode`, a remote scope, a role hint and a low-confidence flag.

The display fields of a `Posting` are `title`, `company`, `location`, `url` and
`posted_at`.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, NewType, override
from urllib.parse import urlsplit

from ijob_core.countries import CountryCode
from ijob_core.errors import InvalidValueError
from ijob_core.money import Money

_SOURCE_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]+")
_RESERVED_SOURCE_KEYS: Final = frozenset({".", ".."})
_HTTP_PREFIXES: Final = ("http://", "https://")
_REF_SEPARATOR: Final = ":"


class SourceKind(StrEnum):
    """The kind of job source a posting was seen on."""

    GREENHOUSE = "greenhouse"
    GEM = "gem"
    ASHBY = "ashby"
    LEVER = "lever"
    WORKABLE = "workable"
    HIMALAYAS = "himalayas"
    JOBICY = "jobicy"
    REMOTEOK = "remoteok"
    ARBEITNOW = "arbeitnow"
    MYCAREERSFUTURE = "mycareersfuture"
    KALIBRR = "kalibrr"
    RSS = "rss"
    YC = "yc"
    LETSGETRUSTY = "letsgetrusty"
    JAPANDEV = "japandev"
    HN = "hn"
    JOBPOSTING = "jobposting"
    FEED = "feed"
    FOURDAYWEEK = "4dayweek"


class RemoteScope(StrEnum):
    """Where a remote job may be worked from."""

    NONE = "none"
    COUNTRY = "country"
    REGION = "region"
    WORLDWIDE = "worldwide"


SourceKey = NewType("SourceKey", str)
ExternalId = NewType("ExternalId", str)
PostingId = NewType("PostingId", int)


def parse_source_key(text: str) -> SourceKey:
    """Check text as a source key and mark it as one.

    A source key is a single URL-safe token: ASCII letters, digits, `.`, `_`
    and `-`, other than `.` and `..`. Such a token cannot change the path,
    query or fragment of a URL it is placed in.

    Args:
        text: The text to check.

    Returns:
        The same text, typed as a source key.

    Raises:
        InvalidValueError: If `text` is empty, holds a character outside the
            allowed set, or is `.` or `..`.

    Examples:
        >>> parse_source_key("mistral.ai")
        'mistral.ai'
        >>> parse_source_key("..")
        Traceback (most recent call last):
            ...
        ijob_core.errors.InvalidValueError: invalid source_key: '..' is a path segment
    """
    if _SOURCE_KEY_PATTERN.fullmatch(text) is None:
        reason = f"{text!r} is not made of letters, digits, '.', '_' and '-'"
        raise InvalidValueError("source_key", reason)
    if text in _RESERVED_SOURCE_KEYS:
        raise InvalidValueError("source_key", f"{text!r} is a path segment")
    return SourceKey(text)


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A named source of postings: one board on one kind of job source.

    Attributes:
        kind: The kind of source.
        key: The source's token within its kind.
    """

    kind: SourceKind
    key: SourceKey

    @override
    def __str__(self) -> str:
        """Render the reference as `<kind>:<key>`, e.g. `greenhouse:stripe`."""
        return f"{self.kind}:{self.key}"


def parse_source_ref(text: str) -> SourceRef:
    """Read `<kind>:<key>` as a source reference.

    The text is split at its first `:`. This is the inverse of `str(SourceRef)`.

    Args:
        text: The text to read, e.g. `greenhouse:stripe`.

    Returns:
        The source reference the text names.

    Raises:
        InvalidValueError: If `text` has no `:`, its kind is not a `SourceKind`
            value, or its key is not a valid source key.

    Examples:
        >>> str(parse_source_ref("4dayweek:engineering"))
        '4dayweek:engineering'
        >>> parse_source_ref("greenhouse")
        Traceback (most recent call last):
            ...
        ijob_core.errors.InvalidValueError: invalid source_ref: 'greenhouse' has no ':'
    """
    kind_text, separator, key_text = text.partition(_REF_SEPARATOR)
    if not separator:
        raise InvalidValueError("source_ref", f"{text!r} has no {_REF_SEPARATOR!r}")
    try:
        kind = SourceKind(kind_text)
    except ValueError as error:
        reason = f"{kind_text!r} is not a source kind"
        raise InvalidValueError("source_ref", reason) from error
    return SourceRef(kind, parse_source_key(key_text))


@dataclass(frozen=True, slots=True)
class SightingDraft:
    """A job seen on a source, as an adapter reports it.

    Attributes:
        source: The source the job was seen on.
        external_id: The job's identifier within that source.
        title: The job title.
        company: The hiring company.
        location: Where the job is, when the source says.
        url: The job's `http://` or `https://` address.
        posted_at: When the source published the job, timezone-aware, when
            the source says.
        raw: The source's payload for the job, as text.
        salaries: The pay the source states, one `Money` per range; empty when
            it states none.
        country: The country the job is in, when known.
        remote: Where the job may be worked from, when the source says.
        role_hint: The source's own label for the kind of role, when it gives
            one.
        low_confidence: Whether the fields were parsed heuristically (as from
            free-text comments) and may be mis-assigned.
    """

    source: SourceRef
    external_id: ExternalId
    title: str
    company: str
    location: str | None
    url: str
    posted_at: datetime | None
    raw: str
    salaries: tuple[Money, ...] = ()
    country: CountryCode | None = None
    remote: RemoteScope | None = None
    role_hint: str | None = None
    low_confidence: bool = False

    def __post_init__(self) -> None:
        """Reject a draft that cannot describe a job.

        Raises:
            InvalidValueError: If `external_id`, `title`, `company` or `url` is
                empty, if `url` does not start with `http://` or `https://` or
                cannot be parsed as an address, or if `posted_at` is set but
                has no timezone.
        """
        required = (
            ("external_id", self.external_id),
            ("title", self.title),
            ("company", self.company),
            ("url", self.url),
        )
        for name, value in required:
            if not value:
                raise InvalidValueError(name, "is empty")
        if not self.url.startswith(_HTTP_PREFIXES):
            raise InvalidValueError("url", f"{self.url!r} is not an http(s) address")
        try:
            urlsplit(self.url)
        except ValueError as error:
            raise InvalidValueError("url", f"{self.url!r} cannot be parsed") from error
        if self.posted_at is not None and self.posted_at.utcoffset() is None:
            raise InvalidValueError("posted_at", "has no timezone")


@dataclass(frozen=True, slots=True)
class Posting:
    """A job, however many sources saw it.

    Built by the store.

    Attributes:
        id: The store's identifier for the posting.
        title: The job title.
        company: The hiring company.
        location: Where the job is, when known.
        url: The job's address.
        posted_at: When the job was published, when known.
        first_seen: When the posting was first recorded.
        last_seen: When the posting was last recorded.
        salaries: The pay the posting states, one `Money` per range; empty when
            none is stated.
        country: The country the job is in, when known.
        remote: Where the job may be worked from, when known.
        role_hint: The source's own label for the kind of role, when known.
        low_confidence: Whether the fields were parsed heuristically (as from
            free-text comments) and may be mis-assigned.
    """

    id: PostingId
    title: str
    company: str
    location: str | None
    url: str
    posted_at: datetime | None
    first_seen: datetime
    last_seen: datetime
    salaries: tuple[Money, ...] = ()
    country: CountryCode | None = None
    remote: RemoteScope | None = None
    role_hint: str | None = None
    low_confidence: bool = False


@dataclass(frozen=True, slots=True)
class Sighting:
    """One source's record of a posting, identified by source and external id.

    Built by the store.

    Attributes:
        posting_id: The posting this sighting belongs to.
        source: The source the posting was seen on.
        external_id: The posting's identifier within that source.
        first_seen: When this source first showed the posting.
        last_seen: When this source last showed the posting.
        raw: The source's latest payload for the posting, as text.
    """

    posting_id: PostingId
    source: SourceRef
    external_id: ExternalId
    first_seen: datetime
    last_seen: datetime
    raw: str
