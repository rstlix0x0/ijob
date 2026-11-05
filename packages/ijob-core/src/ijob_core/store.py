"""The local SQLite store of postings and the sightings behind them.

`Store` owns the database file: it opens it, brings its schema up to date,
records sighting drafts without duplicating a posting from the same source and
merges the sightings of one job seen by several sources into a single posting,
reads postings and sightings back as the records of `ijob_core.model`, lists the
postings a source's latest fetch still showed, and closes it. It also keeps the
table of sources, the boards ijob collects from: seeded or added by the user,
removable, and stamped with the time each was last fetched. It also keeps a
posting's description, the user's vote on each posting and the one CV the user
stored. For the assessment tasks it keeps the profile read from that CV, the
user's standing instructions, and the verdicts and scores already made, each
keyed by what it was made from so a repeat call is a cache hit. A source can be
stored together with its recipe, kept as opaque JSON text that outlives the
source's removal.
It also keeps the items a source's parser could not read, so a model reads each
once: a pending item becomes a job or not a job and is not read again. The
records of the evaluation jury and of eval runs are read and written through
`Store.evals`, and the frozen tuning split through `Store.tunes`.

The schema version lives in `PRAGMA user_version`; opening a database applies
every migration newer than that version, each in one transaction.
"""

import hashlib
import json
import sqlite3
from collections.abc import Collection, Iterable
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum, auto
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from ijob_core.countries import CountryCode
from ijob_core.dedupe import canonical_url, fingerprint
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalRecords
from ijob_core.model import (
    ExternalId,
    Instructions,
    Label,
    Posting,
    PostingId,
    RemoteScope,
    ResumeProfile,
    Score,
    Sighting,
    SightingDraft,
    SourceRef,
    StoredCv,
    StoredRecipe,
    Verdict,
    Vote,
    parse_source_ref,
)
from ijob_core.money import Money, Period
from ijob_core.tune_records import TuneRecords

_MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (
    (
        """
        CREATE TABLE postings (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL,
            location TEXT, url TEXT NOT NULL, posted_at TEXT,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE sightings (
            id INTEGER PRIMARY KEY,
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            source TEXT NOT NULL, external_id TEXT NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, raw TEXT NOT NULL,
            UNIQUE (source, external_id)
        ) STRICT
        """,
    ),
    (
        "ALTER TABLE postings ADD COLUMN salaries TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE postings ADD COLUMN country TEXT",
        "ALTER TABLE postings ADD COLUMN remote TEXT",
        "ALTER TABLE postings ADD COLUMN role_hint TEXT",
        "ALTER TABLE postings ADD COLUMN low_confidence INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE postings ADD COLUMN canonical_url TEXT",
        "ALTER TABLE postings ADD COLUMN fingerprint TEXT",
        "CREATE INDEX postings_canonical_url ON postings(canonical_url)",
        "CREATE INDEX postings_fingerprint ON postings(fingerprint)",
        "CREATE INDEX sightings_posting_id ON sightings(posting_id)",
        """
        CREATE TABLE sources (
            ref TEXT PRIMARY KEY, company TEXT NOT NULL, target_url TEXT,
            origin TEXT NOT NULL CHECK (origin IN ('seed', 'user')),
            removed INTEGER NOT NULL DEFAULT 0, last_fetched_at TEXT
        ) STRICT
        """,
    ),
    (
        "ALTER TABLE postings ADD COLUMN description TEXT",
        """
        CREATE TABLE labels (
            posting_id INTEGER PRIMARY KEY REFERENCES postings(id),
            vote TEXT NOT NULL CHECK (vote IN ('up', 'down')),
            note TEXT, labeled_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE cv (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
            stored_at TEXT NOT NULL
        ) STRICT
        """,
    ),
    (
        """
        CREATE TABLE cv_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cv_sha256 TEXT NOT NULL, profile TEXT NOT NULL,
            profile_sha256 TEXT NOT NULL, model TEXT NOT NULL,
            task_version TEXT NOT NULL, made_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE instructions (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            text TEXT NOT NULL, text_sha256 TEXT NOT NULL,
            filters TEXT NOT NULL, rules TEXT NOT NULL, rules_sha256 TEXT NOT NULL,
            model TEXT NOT NULL, task_version TEXT NOT NULL, set_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE verdicts (
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            input_sha256 TEXT NOT NULL, rules_sha256 TEXT NOT NULL,
            model TEXT NOT NULL, task_version TEXT NOT NULL,
            keep INTEGER NOT NULL CHECK (keep IN (0, 1)), reason TEXT NOT NULL,
            rule INTEGER, made_at TEXT NOT NULL,
            PRIMARY KEY (posting_id, input_sha256, rules_sha256, model, task_version)
        ) STRICT
        """,
        """
        CREATE TABLE scores (
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            input_sha256 TEXT NOT NULL, profile_sha256 TEXT NOT NULL,
            model TEXT NOT NULL, task_version TEXT NOT NULL,
            score INTEGER NOT NULL, reasons TEXT NOT NULL, made_at TEXT NOT NULL,
            PRIMARY KEY (posting_id, input_sha256, profile_sha256, model, task_version)
        ) STRICT
        """,
    ),
    (
        """
        CREATE TABLE source_recipes (
            ref TEXT PRIMARY KEY REFERENCES sources(ref),
            recipe TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL
        ) STRICT
        """,
    ),
    (
        """
        CREATE TABLE skipped_items (
            source TEXT NOT NULL, item_id TEXT NOT NULL, text TEXT NOT NULL,
            url TEXT, reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'job', 'not_job')),
            external_id TEXT, seen_at TEXT NOT NULL,
            PRIMARY KEY (source, item_id)
        ) STRICT
        """,
    ),
    (
        "CREATE INDEX sightings_open ON sightings(posting_id, last_seen, source)",
        """
        CREATE INDEX postings_listed ON postings(
            posted_at, id, title, company, location, url, first_seen, last_seen,
            salaries, country, remote, role_hint, low_confidence
        )
        """,
    ),
    (
        """
        CREATE TABLE eval_sets (
            id INTEGER PRIMARY KEY,
            rules_sha256 TEXT NOT NULL, profile_sha256 TEXT NOT NULL,
            jury TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (rules_sha256, profile_sha256, jury)
        ) STRICT
        """,
        """
        CREATE TABLE eval_cases (
            set_id INTEGER NOT NULL REFERENCES eval_sets(id),
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            position INTEGER NOT NULL,
            bucket TEXT NOT NULL CHECK (bucket IN ('kept', 'rejected', 'unjudged')),
            input_sha256 TEXT NOT NULL,
            PRIMARY KEY (set_id, posting_id)
        ) STRICT
        """,
        """
        CREATE TABLE eval_votes (
            set_id INTEGER NOT NULL REFERENCES eval_sets(id),
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            task TEXT NOT NULL CHECK (task IN ('judge', 'scorer')),
            juror TEXT NOT NULL, task_version TEXT NOT NULL,
            keep INTEGER CHECK (keep IN (0, 1)), rule INTEGER, score INTEGER,
            reasons TEXT NOT NULL, made_at TEXT NOT NULL,
            PRIMARY KEY (set_id, posting_id, task, juror)
        ) STRICT
        """,
        """
        CREATE TABLE eval_labels (
            set_id INTEGER NOT NULL REFERENCES eval_sets(id),
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            task TEXT NOT NULL CHECK (task IN ('judge', 'scorer')),
            keep INTEGER CHECK (keep IN (0, 1)), rule INTEGER, score INTEGER,
            origin TEXT NOT NULL CHECK (origin IN ('agreed', 'user')),
            spot_checked INTEGER NOT NULL CHECK (spot_checked IN (0, 1)),
            decided_at TEXT NOT NULL,
            PRIMARY KEY (set_id, posting_id, task)
        ) STRICT
        """,
        """
        CREATE TABLE eval_runs (
            id INTEGER PRIMARY KEY,
            suite TEXT NOT NULL, model TEXT NOT NULL, task_version TEXT NOT NULL,
            set_id INTEGER REFERENCES eval_sets(id),
            status TEXT NOT NULL
                CHECK (status IN ('running', 'complete', 'incomplete')),
            limited INTEGER NOT NULL CHECK (limited IN (0, 1)),
            metrics TEXT NOT NULL DEFAULT '{}',
            spent_usd REAL NOT NULL DEFAULT 0,
            subscription_usd REAL NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL, finished_at TEXT
        ) STRICT
        """,
        """
        CREATE TABLE eval_results (
            run_id INTEGER NOT NULL REFERENCES eval_runs(id),
            case_key TEXT NOT NULL,
            passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
            answer TEXT NOT NULL, detail TEXT NOT NULL, latency_ms INTEGER NOT NULL,
            PRIMARY KEY (run_id, case_key)
        ) STRICT
        """,
    ),
    (
        """
        CREATE TABLE tune_splits (
            set_id INTEGER NOT NULL REFERENCES eval_sets(id),
            posting_id INTEGER NOT NULL REFERENCES postings(id),
            part TEXT NOT NULL
                CHECK (part IN ('train', 'val', 'held_out', 'unused')),
            PRIMARY KEY (set_id, posting_id)
        ) STRICT
        """,
    ),
    (
        """
        CREATE TABLE programs (
            id INTEGER PRIMARY KEY,
            task TEXT NOT NULL CHECK (task IN ('judge', 'scorer')),
            model TEXT NOT NULL,
            optimizer TEXT NOT NULL,
            base_version TEXT NOT NULL,
            set_id INTEGER NOT NULL REFERENCES eval_sets(id),
            bound_sha256 TEXT NOT NULL,
            instructions TEXT NOT NULL,
            demos_json TEXT NOT NULL,
            version TEXT NOT NULL,
            tune_run_id INTEGER,
            created_at TEXT NOT NULL,
            accepted_at TEXT,
            UNIQUE (task, model, version, bound_sha256)
        ) STRICT
        """,
    ),
)


@dataclass(frozen=True, slots=True)
class StoredProfile:
    """The profile read from the CV, with what it was made from.

    Attributes:
        profile: The structured facts read from the CV.
        cv_sha256: The SHA-256 of the CV the profile was read from.
        model: The model that read the CV.
        task_version: The version of the task that did so.
        made_at: When the profile was made, timezone-aware.
    """

    profile: ResumeProfile
    cv_sha256: str
    model: str
    task_version: str
    made_at: datetime


class SkippedStatus(StrEnum):
    """Where a skipped item stands: not yet read, read as a job, or read as no job."""

    PENDING = "pending"
    JOB = "job"
    NOT_JOB = "not_job"


@dataclass(frozen=True, slots=True)
class SkippedRow:
    """One item a parser could not read, kept so a model can read it once.

    Attributes:
        source: The source the item came from.
        item_id: The SHA-256 hex digest of `text`.
        text: The item as the source gave it.
        url: The item's address, when one was known.
        reason: Why the parser could not read it.
        status: Whether the item is still unread.
        external_id: The external id of the sighting made from the item, once
            its status is `job`.
        seen_at: When the item was first seen, timezone-aware.
    """

    source: SourceRef
    item_id: str
    text: str
    url: str | None
    reason: str
    status: SkippedStatus
    external_id: ExternalId | None
    seen_at: datetime


def skipped_item_id(text: str) -> str:
    """Return the id of a skipped item: the SHA-256 hex digest of its text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RecordResult:
    """What `Store.record` did with a batch of drafts.

    Attributes:
        new: Drafts that created a posting.
        updated: Drafts that refreshed a known sighting.
        merged: Drafts that attached a new sighting to an existing posting.
    """

    new: int
    updated: int
    merged: int = 0


class SourceOrigin(StrEnum):
    """Where a row of the sources table came from."""

    SEED = "seed"
    USER = "user"


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One row of the sources table.

    Attributes:
        ref: The source the row describes.
        company: The company the source belongs to.
        target_url: The page the source is read from, if one was given.
        origin: Whether the row was seeded or added by the user.
        removed: Whether the user removed the source; removed rows are kept.
        last_fetched_at: When the source was last fetched, or `None` if never.
    """

    ref: SourceRef
    company: str
    target_url: str | None
    origin: SourceOrigin
    removed: bool
    last_fetched_at: datetime | None


_LIKE_ESCAPE: Final = "\\"


class _Outcome(Enum):
    """What `Store._write_draft` did with one draft."""

    NEW = auto()
    UPDATED = auto()
    MERGED = auto()


def _to_text(moment: datetime) -> str:
    """Render an aware datetime as fixed-width ISO-8601 text in UTC."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _from_text(text: str) -> datetime:
    """Read ISO-8601 text written by `_to_text` as an aware UTC datetime."""
    return datetime.fromisoformat(text).astimezone(UTC)


def _stamp(field: str, moment: datetime) -> str:
    """Render `moment` as `_to_text` does, rejecting a naive one by `field` name."""
    if moment.utcoffset() is None:
        raise InvalidValueError(field, "has no timezone")
    return _to_text(moment)


_SELECT_SOURCES: Final = (
    "SELECT ref, company, target_url, origin, removed, last_fetched_at FROM sources"
)


def _source_row(row: tuple[str, str, str | None, str, int, str | None]) -> SourceRow:
    """Build a `SourceRow` from a row selected with `_SELECT_SOURCES`."""
    return SourceRow(
        ref=parse_source_ref(row[0]),
        company=row[1],
        target_url=row[2],
        origin=SourceOrigin(row[3]),
        removed=bool(row[4]),
        last_fetched_at=None if row[5] is None else _from_text(row[5]),
    )


_SELECT_POSTINGS: Final = (
    "SELECT id, title, company, location, url, posted_at, "
    "first_seen, last_seen, salaries, country, remote, role_hint, "
    "low_confidence, description FROM postings"
)

_NEWEST_FIRST: Final = "ORDER BY posted_at IS NULL, posted_at DESC, id"

_OPEN_WINDOW: Final = timedelta(days=7)

_BUSY_TIMEOUT_S: Final = 30.0

_SELECT_LISTED: Final = (
    "SELECT id, title, company, location, url, posted_at, "
    "first_seen, last_seen, salaries, country, remote, role_hint, "
    "low_confidence, NULL FROM postings"
)
"""Every posting column but the description, which `postings_listed` leaves out."""


def _posting(row: tuple[Any, ...]) -> Posting:
    """Build a `Posting` from a row selected with `_SELECT_POSTINGS`."""
    return Posting(
        id=PostingId(row[0]),
        title=row[1],
        company=row[2],
        location=row[3],
        url=row[4],
        posted_at=None if row[5] is None else _from_text(row[5]),
        first_seen=_from_text(row[6]),
        last_seen=_from_text(row[7]),
        salaries=_salaries_from_text(row[8]),
        country=None if row[9] is None else CountryCode(row[9]),
        remote=None if row[10] is None else RemoteScope(row[10]),
        role_hint=row[11],
        low_confidence=bool(row[12]),
        description=row[13],
    )


_SELECT_SIGHTINGS: Final = (
    "SELECT posting_id, source, external_id, first_seen, last_seen, raw FROM sightings"
)

_SELECT_LABELS: Final = "SELECT posting_id, vote, note, labeled_at FROM labels"


def _sighting(row: tuple[Any, ...]) -> Sighting:
    """Build a `Sighting` from a row selected with `_SELECT_SIGHTINGS`."""
    return Sighting(
        posting_id=PostingId(row[0]),
        source=parse_source_ref(row[1]),
        external_id=ExternalId(row[2]),
        first_seen=_from_text(row[3]),
        last_seen=_from_text(row[4]),
        raw=row[5],
    )


def _label(row: tuple[Any, ...]) -> Label:
    """Build a `Label` from a row selected with `_SELECT_LABELS`."""
    return Label(
        posting_id=PostingId(row[0]),
        vote=Vote(row[1]),
        note=row[2],
        labeled_at=_from_text(row[3]),
    )


def _cutoff(now: datetime) -> str:
    """Render the oldest `last_fetched_at` that still counts as recent at `now`."""
    if now.utcoffset() is None:
        raise InvalidValueError("now", "has no timezone")
    return _to_text(now - _OPEN_WINDOW)


def _salaries_to_text(salaries: Iterable[Money]) -> str:
    """Render salaries as a JSON list of objects, amounts as strings."""
    return json.dumps(
        [
            {
                "min": None if money.min is None else str(money.min),
                "max": None if money.max is None else str(money.max),
                "currency": money.currency,
                "period": None if money.period is None else money.period.value,
            }
            for money in salaries
        ]
    )


def _salaries_from_text(text: str) -> tuple[Money, ...]:
    """Read the JSON written by `_salaries_to_text` back into `Money` values."""
    return tuple(
        Money(
            min=None if item["min"] is None else Decimal(item["min"]),
            max=None if item["max"] is None else Decimal(item["max"]),
            currency=item["currency"],
            period=None if item["period"] is None else Period(item["period"]),
        )
        for item in json.loads(text)
    )


class Store:
    """An open connection to the postings database.

    Build one with `Store.open`. A `Store` is a context manager that closes its
    connection on exit.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap a connection to a database whose schema is current.

        Args:
            conn: The open connection. The store takes ownership and closes it.
        """
        self._conn = conn

    @classmethod
    def open(cls, path: Path) -> Self:
        """Open the database at `path`, creating and migrating it as needed.

        Args:
            path: The database file. Missing parent directories are created.

        Returns:
            A store on a database at the current schema version.

        Raises:
            sqlite3.Error: If the file cannot be opened or migrated.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with ExitStack() as cleanup:
            conn = cleanup.enter_context(
                closing(sqlite3.connect(path, timeout=_BUSY_TIMEOUT_S))
            )
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            _migrate(conn)
            cleanup.pop_all()
        return cls(conn)

    @property
    def evals(self) -> EvalRecords:
        """The jury and eval-run records, on this store's connection."""
        return EvalRecords(self._conn)

    @property
    def tunes(self) -> TuneRecords:
        """The frozen tuning split, on this store's connection."""
        return TuneRecords(self._conn)

    def record(
        self,
        drafts: Iterable[SightingDraft],
        *,
        seen_at: datetime,
        still_listed: Collection[tuple[SourceRef, ExternalId]] = (),
    ) -> RecordResult:
        """Record what a source showed, without duplicating known postings.

        All drafts are written in one write transaction, taken before the first
        lookup so a concurrent writer cannot slip a duplicate in: if any write
        fails, none persists. A draft whose source and external id are known
        refreshes that sighting's `last_seen` and `raw`. A new one attaches to
        the lowest-id posting that has no sighting from the same source and
        shares its `canonical_url`, or failing that its `fingerprint`; with no
        such posting it adds a posting. Either way the sighting is added to the
        posting, whose display fields and `last_seen` follow the draft. Its
        salaries, country, remote scope, role hint and description follow the
        draft only when the draft has a value for them (an empty string counts as
        no value for the role hint and the description), and its confidence
        flag always follows the draft. The posting's `canonical_url` and
        `fingerprint` are then recomputed from its resulting values. Each draft
        is matched after the earlier ones in the batch are written.

        Args:
            drafts: The jobs a source showed.
            seen_at: When they were shown, timezone-aware.
            still_listed: Sightings the source still lists but did not
                re-describe. Each known pair gets `last_seen` set to `seen_at`
                on its sighting and its posting; unknown pairs are ignored.

        Returns:
            How many drafts created a posting, refreshed a known sighting and
            attached a new sighting to an existing posting.

        Raises:
            InvalidValueError: If `seen_at` has no timezone.
            sqlite3.Error: If a write fails.
        """
        if seen_at.utcoffset() is None:
            raise InvalidValueError("seen_at", "has no timezone")
        counts = dict.fromkeys(_Outcome, 0)
        self._conn.execute("BEGIN IMMEDIATE")
        with self._conn:
            for draft in drafts:
                counts[self._write_draft(draft, seen_at)] += 1
            self._refresh_listed(still_listed, _to_text(seen_at))
        return RecordResult(
            new=counts[_Outcome.NEW],
            updated=counts[_Outcome.UPDATED],
            merged=counts[_Outcome.MERGED],
        )

    def _refresh_listed(
        self, still_listed: Collection[tuple[SourceRef, ExternalId]], stamp: str
    ) -> None:
        """Move `last_seen` forward on each known sighting and its posting."""
        for source, external_id in still_listed:
            row = self._conn.execute(
                "UPDATE sightings SET last_seen = ? "
                "WHERE source = ? AND external_id = ? RETURNING posting_id",
                (stamp, str(source), external_id),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE postings SET last_seen = ? WHERE id = ?", (stamp, row[0])
                )

    def _write_draft(self, draft: SightingDraft, seen_at: datetime) -> _Outcome:
        """Write one draft and report what it did to the store."""
        stamp = _to_text(seen_at)
        source = str(draft.source)
        known = self._conn.execute(
            "SELECT id, posting_id FROM sightings WHERE source = ? AND external_id = ?",
            (source, draft.external_id),
        ).fetchone()
        if known is not None:
            sighting_id, posting_id = known
            self._conn.execute(
                "UPDATE sightings SET last_seen = ?, raw = ? WHERE id = ?",
                (stamp, draft.raw, sighting_id),
            )
            self._update_posting(posting_id, draft, stamp)
            return _Outcome.UPDATED
        posting_id = self._find_match(draft)
        outcome = _Outcome.MERGED
        if posting_id is None:
            posting_id = self._insert_posting(draft, stamp)
            outcome = _Outcome.NEW
        self._conn.execute(
            "INSERT INTO sightings "
            "(posting_id, source, external_id, first_seen, last_seen, raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (posting_id, source, draft.external_id, stamp, stamp, draft.raw),
        )
        if outcome is _Outcome.MERGED:
            self._update_posting(posting_id, draft, stamp)
        return outcome

    def _find_match(self, draft: SightingDraft) -> int | None:
        """Find the lowest-id posting another source showed for the same job."""
        keys = (
            ("canonical_url", canonical_url(draft.url)),
            ("fingerprint", fingerprint(draft.company, draft.title, draft.country)),
        )
        for column, key in keys:
            if key is None:
                continue
            row = self._conn.execute(
                f"SELECT id FROM postings WHERE {column} = ? AND NOT EXISTS ("  # noqa: S608  # column is a literal above
                "SELECT 1 FROM sightings WHERE sightings.posting_id = postings.id "
                "AND sightings.source = ?) ORDER BY id LIMIT 1",
                (key, str(draft.source)),
            ).fetchone()
            if row is not None:
                return int(row[0])
        return None

    def _insert_posting(self, draft: SightingDraft, stamp: str) -> int:
        """Add a posting made from one draft; return its id."""
        return int(
            self._conn.execute(
                "INSERT INTO postings "
                "(title, company, location, url, posted_at, first_seen, last_seen, "
                "salaries, country, remote, role_hint, low_confidence, "
                "canonical_url, fingerprint, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    draft.title,
                    draft.company,
                    draft.location,
                    draft.url,
                    None if draft.posted_at is None else _to_text(draft.posted_at),
                    stamp,
                    stamp,
                    _salaries_to_text(draft.salaries),
                    draft.country,
                    None if draft.remote is None else draft.remote.value,
                    draft.role_hint,
                    int(draft.low_confidence),
                    canonical_url(draft.url),
                    fingerprint(draft.company, draft.title, draft.country),
                    draft.description,
                ),
            ).fetchone()[0]
        )

    def _update_posting(
        self, posting_id: int, draft: SightingDraft, stamp: str
    ) -> None:
        """Apply a draft to its posting, then recompute the posting's keys."""
        self._conn.execute(
            "UPDATE postings SET title = ?, company = ?, location = ?, url = ?, "
            "posted_at = ?, last_seen = ?, low_confidence = ?, "
            "salaries = CASE WHEN ? = '[]' THEN salaries ELSE ? END, "
            "country = COALESCE(?, country), remote = COALESCE(?, remote), "
            "role_hint = COALESCE(NULLIF(?, ''), role_hint), "
            "description = COALESCE(NULLIF(?, ''), description) WHERE id = ?",
            (
                draft.title,
                draft.company,
                draft.location,
                draft.url,
                None if draft.posted_at is None else _to_text(draft.posted_at),
                stamp,
                int(draft.low_confidence),
                (salaries := _salaries_to_text(draft.salaries)),
                salaries,
                draft.country,
                None if draft.remote is None else draft.remote.value,
                draft.role_hint,
                draft.description,
                posting_id,
            ),
        )
        url, company, title, country = self._conn.execute(
            "SELECT url, company, title, country FROM postings WHERE id = ?",
            (posting_id,),
        ).fetchone()
        self._conn.execute(
            "UPDATE postings SET canonical_url = ?, fingerprint = ? WHERE id = ?",
            (
                canonical_url(url),
                fingerprint(
                    company, title, None if country is None else CountryCode(country)
                ),
                posting_id,
            ),
        )

    def postings(self, *, sources: Collection[SourceRef]) -> list[Posting]:
        """Read the postings that any of `sources` has shown.

        Args:
            sources: The sources to read postings for.

        Returns:
            The postings, newest `posted_at` first and those without one last.
            Empty when `sources` is empty.
        """
        wanted = json.dumps([str(source) for source in sources])
        rows = self._conn.execute(
            f"{_SELECT_POSTINGS} WHERE EXISTS ("  # noqa: S608  # constants above
            "SELECT 1 FROM sightings WHERE sightings.posting_id = postings.id "
            "AND sightings.source IN (SELECT value FROM json_each(?))) "
            f"{_NEWEST_FIRST}",
            (wanted,),
        ).fetchall()
        return [_posting(row) for row in rows]

    def open_postings(
        self, *, now: datetime, sources: Collection[SourceRef] | None = None
    ) -> list[Posting]:
        """Read the postings that some source's latest fetch still showed.

        A posting is open when one of its sightings has a `last_seen` equal to
        the `last_fetched_at` of its source, that source is not removed and was
        fetched at most 7 days before `now`.

        Args:
            now: The current time, timezone-aware.
            sources: When given, only sightings from these sources count.

        Returns:
            The postings, newest `posted_at` first and those without one last.
            Empty when `sources` is empty.

        Raises:
            InvalidValueError: If `now` has no timezone.
        """
        cutoff = _cutoff(now)
        wanted = (
            None if sources is None else json.dumps([str(source) for source in sources])
        )
        rows = self._conn.execute(
            f"{_SELECT_POSTINGS} WHERE EXISTS ("  # noqa: S608  # constants above
            "SELECT 1 FROM sightings JOIN sources ON sources.ref = sightings.source "
            "WHERE sightings.posting_id = postings.id "
            "AND sightings.last_seen = sources.last_fetched_at "
            "AND sources.removed = 0 AND sources.last_fetched_at >= ? "
            "AND (? IS NULL OR sources.ref IN (SELECT value FROM json_each(?)))) "
            f"{_NEWEST_FIRST}",
            (cutoff, wanted, wanted),
        ).fetchall()
        return [_posting(row) for row in rows]

    def list_open(
        self, *, now: datetime, sources: Collection[SourceRef] | None = None
    ) -> list[Posting]:
        """Read the open postings without their descriptions, which is fast.

        The same postings in the same order as `open_postings`, each with its
        `description` set to `None` whether it has one or not: use it to list
        jobs, and read a posting with `posting` to know its description.

        Args:
            now: The current time, timezone-aware.
            sources: When given, only sightings from these sources count.

        Returns:
            The postings, newest `posted_at` first and those without one last.
            Empty when `sources` is empty.

        Raises:
            InvalidValueError: If `now` has no timezone.
        """
        cutoff = _cutoff(now)
        wanted = (
            None if sources is None else json.dumps([str(source) for source in sources])
        )
        rows = self._conn.execute(
            f"{_SELECT_LISTED} WHERE EXISTS ("  # noqa: S608  # constants above
            "SELECT 1 FROM sightings JOIN sources ON sources.ref = sightings.source "
            "WHERE sightings.posting_id = postings.id "
            "AND sightings.last_seen = sources.last_fetched_at "
            "AND sources.removed = 0 AND sources.last_fetched_at >= ? "
            "AND (? IS NULL OR sources.ref IN (SELECT value FROM json_each(?)))) "
            f"{_NEWEST_FIRST}",
            (cutoff, wanted, wanted),
        ).fetchall()
        return [_posting(row) for row in rows]

    def sources_by_posting(self) -> dict[PostingId, list[SourceRef]]:
        """Read which sources have shown each posting, in one query.

        Returns:
            The sources of every posting that has a sighting, sorted by their
            text form.
        """
        rows = self._conn.execute(
            "SELECT posting_id, source FROM sightings "
            "INDEXED BY sightings_open ORDER BY posting_id, source"
        ).fetchall()
        shown: dict[PostingId, list[SourceRef]] = {}
        for posting_id, source in rows:
            shown.setdefault(PostingId(posting_id), []).append(parse_source_ref(source))
        return shown

    def saved_scores(
        self, profile_sha256: str, *, version: str | None = None
    ) -> dict[PostingId, Score]:
        """Read the newest stored score of each posting against one profile.

        Unlike `score`, this does not check the posting text or the model, so a
        list can show what is known without reading any description.

        Args:
            profile_sha256: The SHA-256 of the profile the scores were made from.
            version: The task version whose scores to read. With `None`, the
                scores of base versions, which have no `+`, so a tuned program's
                scores never stand in for them.

        Returns:
            The newest score of every posting scored against that profile.
        """
        if version is None:
            rows = self._conn.execute(
                "SELECT posting_id, score, reasons FROM scores "
                "WHERE profile_sha256 = ? AND instr(task_version, '+') = 0 "
                "ORDER BY made_at",
                (profile_sha256,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT posting_id, score, reasons FROM scores "
                "WHERE profile_sha256 = ? AND task_version = ? ORDER BY made_at",
                (profile_sha256, version),
            ).fetchall()
        return {
            PostingId(row[0]): Score(score=row[1], reasons=tuple(json.loads(row[2])))
            for row in rows
        }

    def stale_sources(self, *, now: datetime) -> list[SourceRow]:
        """Read the sources whose last fetch is too old for their postings to show.

        Args:
            now: The current time, timezone-aware.

        Returns:
            The rows not removed whose `last_fetched_at` is set and more than 7
            days before `now`, ordered by `ref`.

        Raises:
            InvalidValueError: If `now` has no timezone.
        """
        rows = self._conn.execute(
            f"{_SELECT_SOURCES} WHERE removed = 0 AND last_fetched_at < ? ORDER BY ref",
            (_cutoff(now),),
        ).fetchall()
        return [_source_row(row) for row in rows]

    def external_ids(self, ref: SourceRef) -> frozenset[ExternalId]:
        """Read every external id a source has shown.

        Args:
            ref: The source to read.

        Returns:
            The ids of all its sightings; empty if it never showed any.
        """
        rows = self._conn.execute(
            "SELECT external_id FROM sightings WHERE source = ?", (str(ref),)
        ).fetchall()
        return frozenset(ExternalId(row[0]) for row in rows)

    def posting_sources(self, posting_id: PostingId) -> list[SourceRef]:
        """Read which sources have shown a posting.

        Args:
            posting_id: The posting to read.

        Returns:
            The sources of its sightings, sorted by their text form; empty if
            the posting is unknown.
        """
        rows = self._conn.execute(
            "SELECT source FROM sightings WHERE posting_id = ? ORDER BY source",
            (posting_id,),
        ).fetchall()
        return [parse_source_ref(row[0]) for row in rows]

    def sighting(self, source: SourceRef, external_id: ExternalId) -> Sighting | None:
        """Read one source's record of a posting.

        Args:
            source: The source the posting was seen on.
            external_id: The posting's identifier within that source.

        Returns:
            The sighting with its latest raw payload, or `None` if that source
            never showed that external id.
        """
        row = self._conn.execute(
            f"{_SELECT_SIGHTINGS} WHERE source = ? AND external_id = ?",
            (str(source), external_id),
        ).fetchone()
        return None if row is None else _sighting(row)

    def sightings_of(self, posting_id: PostingId) -> list[Sighting]:
        """Read every source's record of a posting.

        Args:
            posting_id: The posting to read.

        Returns:
            Its sightings in the order they were first recorded; empty if the
            posting is unknown.
        """
        rows = self._conn.execute(
            f"{_SELECT_SIGHTINGS} WHERE posting_id = ? ORDER BY id", (posting_id,)
        ).fetchall()
        return [_sighting(row) for row in rows]

    def posting(self, posting_id: PostingId) -> Posting | None:
        """Read one posting.

        Args:
            posting_id: The posting to read.

        Returns:
            The posting, or `None` if the id is unknown.
        """
        row = self._conn.execute(
            f"{_SELECT_POSTINGS} WHERE id = ?", (posting_id,)
        ).fetchone()
        return None if row is None else _posting(row)

    def companies_matching(self, query: str, limit: int) -> list[str]:
        """Read the distinct companies of stored postings whose name has a substring.

        The match ignores ASCII letter case, and `%` and `_` in `query` match
        themselves. Every stored posting counts, open or not.

        Args:
            query: The text a company name must contain; empty matches all.
            limit: The most names to return; zero or less returns none.

        Returns:
            The distinct names, sorted, at most `limit`.
        """
        escaped = (
            query.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
            .replace("%", f"{_LIKE_ESCAPE}%")
            .replace("_", f"{_LIKE_ESCAPE}_")
        )
        rows = self._conn.execute(
            "SELECT DISTINCT company FROM postings "
            "WHERE company LIKE ? ESCAPE '\\' "
            "ORDER BY company LIMIT ?",
            (f"%{escaped}%", max(limit, 0)),
        ).fetchall()
        return [row[0] for row in rows]

    def set_description(self, posting_id: PostingId, text: str) -> None:
        """Store a posting's description, replacing any earlier one.

        Args:
            posting_id: The posting to describe.
            text: The description, as Markdown.

        Raises:
            InvalidValueError: If `text` is empty or only whitespace, or the
                posting is unknown.
            sqlite3.Error: If the write fails.
        """
        if not text.strip():
            raise InvalidValueError("text", "is blank")
        with self._conn:
            changed = self._conn.execute(
                "UPDATE postings SET description = ? WHERE id = ?", (text, posting_id)
            ).rowcount
        if not changed:
            raise InvalidValueError("posting_id", f"{posting_id} is not a posting")

    def set_label(
        self, posting_id: PostingId, vote: Vote, note: str | None, *, at: datetime
    ) -> Label:
        """Store the user's vote on a posting, replacing any earlier one.

        Args:
            posting_id: The posting voted on.
            vote: The verdict.
            note: Why, or `None`.
            at: When the vote was cast, timezone-aware.

        Returns:
            The stored label, with `labeled_at` in UTC as `label` reads it back.

        Raises:
            InvalidValueError: If `at` has no timezone, `note` is blank or the
                posting is unknown.
            sqlite3.Error: If the write fails.
        """
        if at.utcoffset() is None:
            raise InvalidValueError("at", "has no timezone")
        stamp = _to_text(at)
        label = Label(posting_id, vote, note, _from_text(stamp))
        with self._conn:
            known = self._conn.execute(
                "SELECT 1 FROM postings WHERE id = ?", (posting_id,)
            ).fetchone()
            if known is None:
                raise InvalidValueError("posting_id", f"{posting_id} is not a posting")
            self._conn.execute(
                "INSERT OR REPLACE INTO labels (posting_id, vote, note, labeled_at) "
                "VALUES (?, ?, ?, ?)",
                (posting_id, vote.value, note, stamp),
            )
        return label

    def clear_label(self, posting_id: PostingId) -> None:
        """Remove the vote on a posting; a posting without one is ignored.

        Args:
            posting_id: The posting whose vote to remove.

        Raises:
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.execute("DELETE FROM labels WHERE posting_id = ?", (posting_id,))

    def label(self, posting_id: PostingId) -> Label | None:
        """Read the vote on a posting.

        Args:
            posting_id: The posting to read.

        Returns:
            The label, or `None` if the posting has none.
        """
        row = self._conn.execute(
            f"{_SELECT_LABELS} WHERE posting_id = ?", (posting_id,)
        ).fetchone()
        return None if row is None else _label(row)

    def labels(self, ids: Collection[PostingId]) -> dict[PostingId, Label]:
        """Read the votes on several postings at once.

        Args:
            ids: The postings to read.

        Returns:
            The labels by posting id; ids without a label are absent, and `{}`
            when `ids` is empty.
        """
        wanted = json.dumps(list(ids))
        rows = self._conn.execute(
            f"{_SELECT_LABELS} WHERE posting_id IN "  # noqa: S608  # constants above
            "(SELECT value FROM json_each(?))",
            (wanted,),
        ).fetchall()
        labels = (_label(row) for row in rows)
        return {label.posting_id: label for label in labels}

    def set_cv(self, cv: StoredCv) -> None:
        """Store the CV record, replacing the earlier one.

        Args:
            cv: The CV to record.

        Raises:
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO cv (id, name, sha256, size, stored_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (cv.name, cv.sha256, cv.size, _to_text(cv.stored_at)),
            )

    def cv(self) -> StoredCv | None:
        """Read the stored CV record.

        Returns:
            The record, or `None` if no CV was stored.
        """
        row = self._conn.execute(
            "SELECT name, sha256, size, stored_at FROM cv WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return StoredCv(
            name=row[0], sha256=row[1], size=row[2], stored_at=_from_text(row[3])
        )

    def set_cv_profile(
        self,
        profile: ResumeProfile,
        *,
        cv_sha256: str,
        model: str,
        task_version: str,
        made_at: datetime,
    ) -> None:
        """Store the profile read from the CV, replacing the earlier one.

        Args:
            profile: The profile to keep.
            cv_sha256: The SHA-256 of the CV it was read from.
            model: The model that read the CV.
            task_version: The version of the task that did so.
            made_at: When the profile was made, timezone-aware.

        Raises:
            InvalidValueError: If `made_at` has no timezone.
            sqlite3.Error: If the write fails.
        """
        stamp = _stamp("made_at", made_at)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO cv_profile (id, cv_sha256, profile, "
                "profile_sha256, model, task_version, made_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    cv_sha256,
                    profile.to_json(),
                    profile.sha256(),
                    model,
                    task_version,
                    stamp,
                ),
            )

    def cv_profile(self) -> StoredProfile | None:
        """Read the stored CV profile.

        Returns:
            The profile with what it was made from, or `None` if none was stored.
        """
        row = self._conn.execute(
            "SELECT profile, cv_sha256, model, task_version, made_at "
            "FROM cv_profile WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return StoredProfile(
            profile=ResumeProfile.from_json(row[0]),
            cv_sha256=row[1],
            model=row[2],
            task_version=row[3],
            made_at=_from_text(row[4]),
        )

    def set_instructions(self, instructions: Instructions) -> None:
        """Store the user's instructions, replacing the earlier ones.

        Args:
            instructions: The instructions to keep.

        Raises:
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO instructions (id, text, text_sha256, "
                "filters, rules, rules_sha256, model, task_version, set_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instructions.text,
                    instructions.text_sha256,
                    instructions.filters_json,
                    json.dumps(instructions.rules),
                    instructions.rules_sha256,
                    instructions.model,
                    instructions.task_version,
                    _to_text(instructions.set_at),
                ),
            )

    def instructions(self) -> Instructions | None:
        """Read the stored instructions.

        Returns:
            The instructions, or `None` if none were stored.
        """
        row = self._conn.execute(
            "SELECT text, filters, rules, model, task_version, set_at "
            "FROM instructions WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return Instructions(
            text=row[0],
            filters_json=row[1],
            rules=tuple(json.loads(row[2])),
            model=row[3],
            task_version=row[4],
            set_at=_from_text(row[5]),
        )

    def clear_instructions(self) -> None:
        """Remove the stored instructions; clearing with none stored is a no-op.

        Raises:
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.execute("DELETE FROM instructions WHERE id = 1")

    def verdict(
        self,
        posting_id: PostingId,
        input_sha256: str,
        rules_sha256: str,
        model: str,
        task_version: str,
    ) -> Verdict | None:
        """Read the verdict made from exactly these inputs.

        Args:
            posting_id: The posting judged.
            input_sha256: The SHA-256 of the posting text the verdict read.
            rules_sha256: The SHA-256 of the rules it applied.
            model: The model that judged.
            task_version: The version of the task that did so.

        Returns:
            The stored verdict, or `None` if any of the five key fields differs
            from every stored verdict.
        """
        row = self._conn.execute(
            "SELECT keep, reason, rule FROM verdicts WHERE posting_id = ? "
            "AND input_sha256 = ? AND rules_sha256 = ? AND model = ? "
            "AND task_version = ?",
            (posting_id, input_sha256, rules_sha256, model, task_version),
        ).fetchone()
        if row is None:
            return None
        return Verdict(keep=bool(row[0]), reason=row[1], rule=row[2])

    def put_verdict(  # noqa: PLR0913, PLR0917  # five key fields, verdict, time
        self,
        posting_id: PostingId,
        input_sha256: str,
        rules_sha256: str,
        model: str,
        task_version: str,
        verdict: Verdict,
        *,
        made_at: datetime,
    ) -> None:
        """Store a verdict under its five key fields, replacing one under the same.

        Args:
            posting_id: The posting judged.
            input_sha256: The SHA-256 of the posting text the verdict read.
            rules_sha256: The SHA-256 of the rules it applied.
            model: The model that judged.
            task_version: The version of the task that did so.
            verdict: The verdict to keep.
            made_at: When it was made, timezone-aware.

        Raises:
            InvalidValueError: If `made_at` has no timezone.
            sqlite3.IntegrityError: If the posting is unknown.
            sqlite3.Error: If the write fails.
        """
        stamp = _stamp("made_at", made_at)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO verdicts (posting_id, input_sha256, "
                "rules_sha256, model, task_version, keep, reason, rule, made_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    posting_id,
                    input_sha256,
                    rules_sha256,
                    model,
                    task_version,
                    int(verdict.keep),
                    verdict.reason,
                    verdict.rule,
                    stamp,
                ),
            )

    def score(
        self,
        posting_id: PostingId,
        input_sha256: str,
        profile_sha256: str,
        model: str,
        task_version: str,
    ) -> Score | None:
        """Read the score made from exactly these inputs.

        Args:
            posting_id: The posting scored.
            input_sha256: The SHA-256 of the posting text the score read.
            profile_sha256: The SHA-256 of the profile it was scored against.
            model: The model that scored.
            task_version: The version of the task that did so.

        Returns:
            The stored score, or `None` if any of the five key fields differs
            from every stored score.
        """
        row = self._conn.execute(
            "SELECT score, reasons FROM scores WHERE posting_id = ? "
            "AND input_sha256 = ? AND profile_sha256 = ? AND model = ? "
            "AND task_version = ?",
            (posting_id, input_sha256, profile_sha256, model, task_version),
        ).fetchone()
        if row is None:
            return None
        return Score(score=row[0], reasons=tuple(json.loads(row[1])))

    def put_score(  # noqa: PLR0913, PLR0917  # five key fields, score, time
        self,
        posting_id: PostingId,
        input_sha256: str,
        profile_sha256: str,
        model: str,
        task_version: str,
        score: Score,
        *,
        made_at: datetime,
    ) -> None:
        """Store a score under its five key fields, replacing one under the same.

        Args:
            posting_id: The posting scored.
            input_sha256: The SHA-256 of the posting text the score read.
            profile_sha256: The SHA-256 of the profile it was scored against.
            model: The model that scored.
            task_version: The version of the task that did so.
            score: The score to keep.
            made_at: When it was made, timezone-aware.

        Raises:
            InvalidValueError: If `made_at` has no timezone.
            sqlite3.IntegrityError: If the posting is unknown.
            sqlite3.Error: If the write fails.
        """
        stamp = _stamp("made_at", made_at)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO scores (posting_id, input_sha256, "
                "profile_sha256, model, task_version, score, reasons, made_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    posting_id,
                    input_sha256,
                    profile_sha256,
                    model,
                    task_version,
                    score.score,
                    json.dumps(score.reasons),
                    stamp,
                ),
            )

    def import_seeds(self, rows: Iterable[tuple[SourceRef, str]]) -> int:
        """Add seed sources that the table does not hold yet.

        Rows already present are left untouched, removed ones included, so a
        source the user removed is not brought back by a later import.

        Args:
            rows: Each source and the company it belongs to.

        Returns:
            How many rows were inserted.

        Raises:
            sqlite3.Error: If a write fails.
        """
        inserted = 0
        with self._conn:
            for ref, company in rows:
                inserted += self._conn.execute(
                    "INSERT OR IGNORE INTO sources (ref, company, origin) "
                    "VALUES (?, ?, ?)",
                    (str(ref), company, SourceOrigin.SEED.value),
                ).rowcount
        return inserted

    def add_source(
        self, ref: SourceRef, company: str, target_url: str | None = None
    ) -> None:
        """Add a source the user named, or bring a removed one back.

        A new ref gets a `user` row. A ref already in the table keeps its row,
        origin included, and only has `removed` cleared.

        Args:
            ref: The source to add.
            company: The company the source belongs to.
            target_url: The page the source is read from, if any.

        Raises:
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO sources (ref, company, target_url, origin) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (ref) DO UPDATE SET removed = 0",
                (str(ref), company, target_url, SourceOrigin.USER.value),
            )

    def set_recipe_source(  # noqa: PLR0913  # the source's fields and the recipe's
        self,
        ref: SourceRef,
        *,
        company: str,
        target_url: str | None,
        recipe: str,
        model: str,
        at: datetime,
    ) -> None:
        """Add a source together with its recipe, in one transaction.

        The source row is written as `add_source` does: a new ref gets a `user`
        row, and a ref already in the table keeps its row and only has `removed`
        cleared. The recipe row is inserted, or its recipe, model and time are
        replaced. If either write fails neither is kept.

        Args:
            ref: The source to add.
            company: The company the source belongs to.
            target_url: The page the source is read from, if any.
            recipe: The recipe as JSON text; it is stored, not parsed.
            model: The model that learned the recipe.
            at: When the recipe was learned, timezone-aware.

        Raises:
            InvalidValueError: If `at` has no timezone, or `recipe` or `model` is
                empty.
            sqlite3.Error: If a write fails.
        """
        stamp = _stamp("at", at)
        record = StoredRecipe(ref=ref, recipe=recipe, model=model, created_at=at)
        with self._conn:
            self._conn.execute(
                "INSERT INTO sources (ref, company, target_url, origin) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (ref) DO UPDATE SET removed = 0",
                (str(ref), company, target_url, SourceOrigin.USER.value),
            )
            self._conn.execute(
                "INSERT INTO source_recipes (ref, recipe, model, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (ref) DO UPDATE SET "
                "recipe = excluded.recipe, model = excluded.model, "
                "created_at = excluded.created_at",
                (str(ref), record.recipe, record.model, stamp),
            )

    def recipe(self, ref: SourceRef) -> StoredRecipe | None:
        """Read the recipe stored for a source, removed or not.

        Args:
            ref: The source to read.

        Returns:
            The stored recipe, or `None` if the source has none.
        """
        row = self._conn.execute(
            "SELECT ref, recipe, model, created_at FROM source_recipes WHERE ref = ?",
            (str(ref),),
        ).fetchone()
        if row is None:
            return None
        return StoredRecipe(
            ref=parse_source_ref(row[0]),
            recipe=row[1],
            model=row[2],
            created_at=_from_text(row[3]),
        )

    def remove_source(self, ref: SourceRef) -> None:
        """Mark a source removed; an unknown ref is ignored.

        Args:
            ref: The source to remove.

        Raises:
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE sources SET removed = 1 WHERE ref = ?", (str(ref),)
            )

    def source(self, ref: SourceRef) -> SourceRow | None:
        """Read one source, removed or not.

        Args:
            ref: The source to read.

        Returns:
            The row, whose `removed` field says whether it was removed, or
            `None` if the table has no such source.
        """
        row = self._conn.execute(
            f"{_SELECT_SOURCES} WHERE ref = ?",
            (str(ref),),
        ).fetchone()
        return None if row is None else _source_row(row)

    def sources(self) -> list[SourceRow]:
        """Read every source that is not removed.

        Returns:
            The rows, ordered by `ref`.
        """
        rows = self._conn.execute(
            f"{_SELECT_SOURCES} WHERE removed = 0 ORDER BY ref"
        ).fetchall()
        return [_source_row(row) for row in rows]

    def mark_fetched(self, ref: SourceRef, at: datetime) -> None:
        """Record when a source was last fetched; an unknown ref is ignored.

        Args:
            ref: The source that was fetched.
            at: When it was fetched, timezone-aware.

        Raises:
            InvalidValueError: If `at` has no timezone.
            sqlite3.Error: If the write fails.
        """
        if at.utcoffset() is None:
            raise InvalidValueError("at", "has no timezone")
        with self._conn:
            self._conn.execute(
                "UPDATE sources SET last_fetched_at = ? WHERE ref = ?",
                (_to_text(at), str(ref)),
            )

    def add_skipped(
        self,
        source: SourceRef,
        items: Iterable[tuple[str, str | None, str]],
        *,
        seen_at: datetime,
    ) -> list[ExternalId]:
        """Keep the items a parser could not read, leaving known ones untouched.

        An item whose source and text are already stored keeps its row, status
        and time. All items are written in one transaction.

        Args:
            source: The source the items came from.
            items: Each item's `(text, url, reason)`.
            seen_at: When they were seen, timezone-aware.

        Returns:
            The external ids of the sightings made from those items that were
            already read as jobs, so the caller can keep their postings listed.

        Raises:
            InvalidValueError: If `seen_at` has no timezone.
            sqlite3.Error: If a write fails.
        """
        stamp = _stamp("seen_at", seen_at)
        jobs: list[ExternalId] = []
        with self._conn:
            for text, url, reason in items:
                item_id = skipped_item_id(text)
                self._conn.execute(
                    "INSERT INTO skipped_items "
                    "(source, item_id, text, url, reason, seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (source, item_id) DO NOTHING",
                    (str(source), item_id, text, url, reason, stamp),
                )
                row = self._conn.execute(
                    "SELECT external_id FROM skipped_items WHERE source = ? "
                    "AND item_id = ? AND status = 'job' AND external_id IS NOT NULL",
                    (str(source), item_id),
                ).fetchone()
                if row is not None:
                    jobs.append(ExternalId(row[0]))
        return jobs

    def pending_skipped(self, sources: Collection[SourceRef]) -> list[SkippedRow]:
        """Read the skipped items of some sources that no model has read yet.

        Args:
            sources: The sources to read; empty means none.

        Returns:
            The pending items, oldest first, then by source and id.
        """
        wanted = json.dumps([str(source) for source in sources])
        rows = self._conn.execute(
            "SELECT source, item_id, text, url, reason, status, external_id, seen_at "
            "FROM skipped_items WHERE status = 'pending' "
            "AND source IN (SELECT value FROM json_each(?)) "
            "ORDER BY seen_at, source, item_id",
            (wanted,),
        ).fetchall()
        return [
            SkippedRow(
                source=parse_source_ref(row[0]),
                item_id=row[1],
                text=row[2],
                url=row[3],
                reason=row[4],
                status=SkippedStatus(row[5]),
                external_id=None if row[6] is None else ExternalId(row[6]),
                seen_at=_from_text(row[7]),
            )
            for row in rows
        ]

    def mark_skipped(
        self,
        source: SourceRef,
        item_id: str,
        status: SkippedStatus,
        *,
        external_id: ExternalId | None = None,
    ) -> None:
        """Record what the model made of a skipped item.

        Args:
            source: The item's source.
            item_id: The item's id.
            status: The result; `job` needs `external_id`.
            external_id: The external id of the sighting made from the item.

        Raises:
            InvalidValueError: If `status` is `job` without an `external_id`.
            sqlite3.Error: If the write fails.
        """
        if status is SkippedStatus.JOB and external_id is None:
            raise InvalidValueError("external_id", "is needed for a job")
        with self._conn:
            self._conn.execute(
                "UPDATE skipped_items SET status = ?, external_id = ? "
                "WHERE source = ? AND item_id = ?",
                (status.value, external_id, str(source), item_id),
            )

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    def __enter__(self) -> Self:
        """Return the store itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection; exceptions propagate."""
        self.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply each migration newer than the database's `user_version`.

    Each migration runs in one write transaction that re-reads `user_version`
    after taking the lock, so two processes opening a fresh database cannot both
    apply it.
    """
    current = _user_version(conn)
    for version, statements in enumerate(_MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            if _user_version(conn) >= version:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")


def _user_version(conn: sqlite3.Connection) -> int:
    """Read the database's schema version."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
