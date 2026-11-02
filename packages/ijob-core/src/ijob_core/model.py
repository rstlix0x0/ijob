"""Domain types for job sources, the postings seen on them and their sightings.

A source adapter returns `SightingDraft` values; the store turns them into
`Posting` and `Sighting` records. `parse_source_key`, `parse_source_ref` and
`SightingDraft` validate their input when called or built, so code that receives a
`SourceKey`, a `SourceRef` from `parse_source_ref`, or a draft can trust it;
`Posting` and `Sighting` are records the store builds from accepted drafts.

`SourceKind` names every kind of source. `RemoteScope` says where a remote job
may be worked from. A draft and a `Posting` can also carry salaries (`Money`), a
`CountryCode`, a remote scope, a role hint, a low-confidence flag and a
description in Markdown.

`Label` records the user's `Vote` on a posting, and `StoredCv` describes the CV
the user keeps in the store. `StoredRecipe` holds the recipe learned for a source,
as JSON text the model never parses.

`ResumeProfile`, `Instructions`, `Verdict` and `Score` hold what the assessment
tasks read and produce. Each validates its limits when built, using only the
standard library.

The display fields of a `Posting` are `title`, `company`, `location`, `url` and
`posted_at`.
"""

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, NewType, Self, override
from urllib.parse import urlsplit

from ijob_core.countries import CountryCode
from ijob_core.errors import InvalidValueError
from ijob_core.money import Money

_SOURCE_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]+")
_RESERVED_SOURCE_KEYS: Final = frozenset({".", ".."})
_HTTP_PREFIXES: Final = ("http://", "https://")
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
_REF_SEPARATOR: Final = ":"
_SUMMARY_MAX: Final = 600
_YEARS_MAX: Final = 60
_SHORT_LIST_MAX: Final = 20
_SKILLS_MAX: Final = 100
_ITEM_MAX: Final = 100
_INSTRUCTIONS_MAX: Final = 4_000
_RULES_MAX: Final = 10
_RULE_MAX: Final = 200
_NAME_MAX: Final = 200
_REASON_MAX: Final = 200
_REASONS_MAX: Final = 5
_SCORE_MAX: Final = 100


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
    RECIPE = "recipe"


class Vote(StrEnum):
    """The user's verdict on a posting."""

    UP = "up"
    DOWN = "down"


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
        description: The job's description as Markdown, when the source gives
            one.
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
    description: str | None = None

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
        description: The job's description as Markdown, when known.
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
    description: str | None = None


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


@dataclass(frozen=True, slots=True)
class Label:
    """The user's vote on one posting.

    Attributes:
        posting_id: The posting voted on.
        vote: The verdict.
        note: Why, when the user said; never empty or blank.
        labeled_at: When the vote was cast, timezone-aware.
    """

    posting_id: PostingId
    vote: Vote
    note: str | None
    labeled_at: datetime

    def __post_init__(self) -> None:
        """Reject a label with a blank note or a naive time.

        Raises:
            InvalidValueError: If `note` is set but empty or only whitespace, or
                if `labeled_at` has no timezone.
        """
        if self.note is not None and not self.note.strip():
            raise InvalidValueError("note", "is blank")
        if self.labeled_at.utcoffset() is None:
            raise InvalidValueError("labeled_at", "has no timezone")


@dataclass(frozen=True, slots=True)
class StoredCv:
    """The record of the CV the store holds, not the file itself.

    Attributes:
        name: The file's name.
        sha256: The SHA-256 of the file's bytes, as 64 lowercase hex characters.
        size: The file's size in bytes.
        stored_at: When the CV was stored, timezone-aware.
    """

    name: str
    sha256: str
    size: int
    stored_at: datetime

    def __post_init__(self) -> None:
        """Reject a CV record that cannot describe a file.

        Raises:
            InvalidValueError: If `name` is empty, `sha256` is not 64 lowercase
                hex characters, `size` is negative, or `stored_at` has no
                timezone.
        """
        if not self.name:
            raise InvalidValueError("name", "is empty")
        if _SHA256_PATTERN.fullmatch(self.sha256) is None:
            reason = f"{self.sha256!r} is not 64 lowercase hex characters"
            raise InvalidValueError("sha256", reason)
        if self.size < 0:
            raise InvalidValueError("size", f"{self.size} is negative")
        if self.stored_at.utcoffset() is None:
            raise InvalidValueError("stored_at", "has no timezone")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredRecipe:
    """The recipe the store holds for a source, as opaque JSON text.

    Attributes:
        ref: The source the recipe reads.
        recipe: The recipe as JSON text; the store never parses it.
        model: The model that learned the recipe.
        created_at: When the recipe was stored, timezone-aware.
    """

    ref: SourceRef
    recipe: str
    model: str
    created_at: datetime

    def __post_init__(self) -> None:
        """Reject a recipe record that cannot describe a learned recipe.

        Raises:
            InvalidValueError: If `recipe` or `model` is empty, or `created_at`
                has no timezone.
        """
        if not self.recipe:
            raise InvalidValueError("recipe", "is empty")
        if not self.model:
            raise InvalidValueError("model", "is empty")
        if self.created_at.utcoffset() is None:
            raise InvalidValueError("created_at", "has no timezone")


class Seniority(StrEnum):
    """The seniority level a profile or role is pitched at."""

    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    UNKNOWN = "unknown"


def _sha256_hex(text: str) -> str:
    """Return the SHA-256 of `text` encoded as UTF-8, as 64 lowercase hex characters."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    """Render `value` as JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _check_length(field: str, text: str, *, maximum: int, blank_ok: bool) -> None:
    """Raise `InvalidValueError` if `text` is too long or, unless allowed, blank."""
    if not blank_ok and not text.strip():
        raise InvalidValueError(field, "is blank")
    if len(text) > maximum:
        raise InvalidValueError(field, f"has {len(text)} characters, over {maximum}")


def _check_items(
    field: str, items: tuple[str, ...], *, minimum: int, maximum: int, item_max: int
) -> None:
    """Raise `InvalidValueError` unless `items` is a bounded list of bounded text."""
    if not minimum <= len(items) <= maximum:
        reason = f"has {len(items)} items, outside {minimum} to {maximum}"
        raise InvalidValueError(field, reason)
    for item in items:
        _check_length(field, item, maximum=item_max, blank_ok=False)


def _check_range(field: str, value: int, *, low: int, high: int) -> None:
    """Raise `InvalidValueError` unless `value` is an `int` from `low` to `high`."""
    if isinstance(value, bool) or not low <= value <= high:
        raise InvalidValueError(
            field, f"{value!r} is not an integer from {low} to {high}"
        )


def _check_aware(field: str, moment: datetime) -> None:
    """Raise `InvalidValueError` if `moment` has no timezone."""
    if moment.utcoffset() is None:
        raise InvalidValueError(field, "has no timezone")


def _read_text_list(field: str, value: object) -> tuple[str, ...]:
    """Read a JSON array of strings as a tuple, or raise `InvalidValueError`."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidValueError(field, "is not a list of text")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ResumeProfile:
    """The structured facts the assessment tasks read from the user's CV.

    Every list holds non-blank items of at most 100 characters.

    Attributes:
        summary: A short summary of the candidate, at most 600 characters.
        years_experience: Years of professional experience, 0 to 60, when known.
        seniority: The level the candidate works at.
        roles: The roles the candidate fills, at most 20.
        spoken_languages: The languages the candidate speaks, at most 20.
        locations: Where the candidate is or will work from, at most 20.
        work_authorization: Where the candidate may work, at most 20.
        skills: The candidate's skills, at most 100.
    """

    summary: str
    years_experience: int | None
    seniority: Seniority
    roles: tuple[str, ...]
    spoken_languages: tuple[str, ...]
    locations: tuple[str, ...]
    work_authorization: tuple[str, ...]
    skills: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject a profile outside the documented limits.

        Raises:
            InvalidValueError: Naming the first field that is over its limit, or
                holds a blank or over-long list item.
        """
        _check_length("summary", self.summary, maximum=_SUMMARY_MAX, blank_ok=True)
        if self.years_experience is not None:
            _check_range(
                "years_experience", self.years_experience, low=0, high=_YEARS_MAX
            )
        for field, items, maximum in (
            ("roles", self.roles, _SHORT_LIST_MAX),
            ("spoken_languages", self.spoken_languages, _SHORT_LIST_MAX),
            ("locations", self.locations, _SHORT_LIST_MAX),
            ("work_authorization", self.work_authorization, _SHORT_LIST_MAX),
            ("skills", self.skills, _SKILLS_MAX),
        ):
            _check_items(field, items, minimum=0, maximum=maximum, item_max=_ITEM_MAX)

    def to_json(self) -> str:
        """Render the profile as canonical JSON: sorted keys, no extra whitespace."""
        return _canonical_json(
            {
                "summary": self.summary,
                "years_experience": self.years_experience,
                "seniority": self.seniority.value,
                "roles": self.roles,
                "spoken_languages": self.spoken_languages,
                "locations": self.locations,
                "work_authorization": self.work_authorization,
                "skills": self.skills,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Read a profile from JSON, as `to_json` writes it; key order is free.

        Args:
            text: A JSON object holding exactly the profile's fields.

        Returns:
            The profile the text describes.

        Raises:
            InvalidValueError: If `text` is not a JSON object with exactly the
                profile's fields of the right types, or the values break the
                profile's limits.
        """
        try:
            data = json.loads(text)
        except ValueError as error:
            raise InvalidValueError("resume_profile", "is not valid JSON") from error
        if not isinstance(data, dict):
            raise InvalidValueError("resume_profile", "is not a JSON object")
        names = {field.name for field in dataclasses.fields(cls)}
        if data.keys() != names:
            raise InvalidValueError(
                "resume_profile", f"fields are not exactly {sorted(names)}"
            )
        summary = data["summary"]
        years = data["years_experience"]
        if not isinstance(summary, str):
            raise InvalidValueError("summary", "is not text")
        if years is not None and not isinstance(years, int):
            raise InvalidValueError("years_experience", "is not an integer or null")
        try:
            seniority = Seniority(data["seniority"])
        except ValueError as error:
            reason = f"{data['seniority']!r} is not a seniority"
            raise InvalidValueError("seniority", reason) from error
        return cls(
            summary=summary,
            years_experience=years,
            seniority=seniority,
            roles=_read_text_list("roles", data["roles"]),
            spoken_languages=_read_text_list(
                "spoken_languages", data["spoken_languages"]
            ),
            locations=_read_text_list("locations", data["locations"]),
            work_authorization=_read_text_list(
                "work_authorization", data["work_authorization"]
            ),
            skills=_read_text_list("skills", data["skills"]),
        )

    def sha256(self) -> str:
        """Return the SHA-256 of `to_json()`, as 64 lowercase hex characters."""
        return _sha256_hex(self.to_json())


@dataclass(frozen=True, slots=True)
class Instructions:
    """The user's standing instructions for judging postings.

    Attributes:
        text: The instructions as the user wrote them; not blank, at most 4,000
            characters.
        filters_json: The filters the instructions imply, as a JSON object in the
            shape of the TOML `[filters]` table with role words already escaped.
            This is the instruction layer alone, not the merged filter spec.
        rules: Up to 10 rules, each 1 to 200 characters, numbered from 1 by
            position.
        model: The model that read `text` into `filters_json` and `rules`.
        task_version: The version of the task that did so.
        set_at: When the instructions were set, timezone-aware.
    """

    text: str
    filters_json: str
    rules: tuple[str, ...]
    model: str
    task_version: str
    set_at: datetime

    def __post_init__(self) -> None:
        """Reject instructions outside the documented limits.

        Raises:
            InvalidValueError: Naming the field that is blank, over its limit, not
                a JSON object (`filters_json`) or naive (`set_at`).
        """
        _check_length("text", self.text, maximum=_INSTRUCTIONS_MAX, blank_ok=False)
        try:
            filters = json.loads(self.filters_json)
        except ValueError as error:
            raise InvalidValueError("filters_json", "is not valid JSON") from error
        if not isinstance(filters, dict):
            raise InvalidValueError("filters_json", "is not a JSON object")
        _check_items(
            "rules", self.rules, minimum=0, maximum=_RULES_MAX, item_max=_RULE_MAX
        )
        _check_length("model", self.model, maximum=_NAME_MAX, blank_ok=False)
        _check_length(
            "task_version", self.task_version, maximum=_NAME_MAX, blank_ok=False
        )
        _check_aware("set_at", self.set_at)

    @property
    def text_sha256(self) -> str:
        """The SHA-256 of `text`, as 64 lowercase hex characters."""
        return _sha256_hex(self.text)

    @property
    def rules_sha256(self) -> str:
        """The SHA-256 of `rules` encoded as a JSON array.

        Encoding as an array keeps one rule holding a line break apart from the
        same text split into two rules.
        """
        return _sha256_hex(_canonical_json(self.rules))


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether the instructions keep a posting, and why.

    Attributes:
        keep: True if the posting passes the instructions.
        reason: Why, 1 to 200 characters.
        rule: The 1-based number of the rule that rejected the posting; None when
            `keep` is true, at least 1 when it is false. The upper bound depends
            on the stored rules and is not checked here.
    """

    keep: bool
    reason: str
    rule: int | None

    def __post_init__(self) -> None:
        """Reject a verdict whose reason or rule is inconsistent.

        Raises:
            InvalidValueError: If `reason` is blank or over 200 characters, if
                `rule` is set while `keep` is true, or if `rule` is missing or
                below 1 while `keep` is false.
        """
        _check_length("reason", self.reason, maximum=_REASON_MAX, blank_ok=False)
        if self.keep:
            if self.rule is not None:
                raise InvalidValueError("rule", "must be None when keep is true")
        elif self.rule is None or self.rule < 1:
            raise InvalidValueError("rule", "must be at least 1 when keep is false")


@dataclass(frozen=True, slots=True)
class Score:
    """How well a posting fits the user's CV.

    Attributes:
        score: The fit, an integer from 0 to 100.
        reasons: 1 to 5 reasons, each 1 to 200 characters.
    """

    score: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject a score outside 0 to 100 or with too few or too many reasons.

        Raises:
            InvalidValueError: Naming `score` or `reasons`.
        """
        _check_range("score", self.score, low=0, high=_SCORE_MAX)
        _check_items(
            "reasons",
            self.reasons,
            minimum=1,
            maximum=_REASONS_MAX,
            item_max=_REASON_MAX,
        )
