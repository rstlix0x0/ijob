"""Domain types for job sources, the postings seen on them and their sightings.

A source adapter returns `SightingDraft` values; the store turns them into
`Posting` and `Sighting` records. `parse_source_key` and `SightingDraft` validate
their input when called or built, so code that receives a `SourceKey` or a draft can
trust it; `Posting` and `Sighting` are records the store builds from accepted drafts.

The display fields of a `Posting` are `title`, `company`, `location`, `url` and
`posted_at`.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, NewType, override

from ijob_core.errors import InvalidValueError

_SOURCE_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]+")
_RESERVED_SOURCE_KEYS: Final = frozenset({".", ".."})
_HTTP_PREFIXES: Final = ("http://", "https://")


class SourceKind(StrEnum):
    """The kind of job source a posting was seen on."""

    GREENHOUSE = "greenhouse"


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
    """

    source: SourceRef
    external_id: ExternalId
    title: str
    company: str
    location: str | None
    url: str
    posted_at: datetime | None
    raw: str

    def __post_init__(self) -> None:
        """Reject a draft that cannot describe a job.

        Raises:
            InvalidValueError: If `external_id`, `title`, `company` or `url` is
                empty, if `url` does not start with `http://` or `https://`,
                or if `posted_at` is set but has no timezone.
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
    """

    id: PostingId
    title: str
    company: str
    location: str | None
    url: str
    posted_at: datetime | None
    first_seen: datetime
    last_seen: datetime


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
