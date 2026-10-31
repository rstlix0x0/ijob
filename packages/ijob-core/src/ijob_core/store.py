"""The local SQLite store of postings and the sightings behind them.

`Store` owns the database file: it opens it, brings its schema up to date,
records sighting drafts without duplicating a posting from the same source and
merges the sightings of one job seen by several sources into a single posting,
reads postings and sightings back as the records of `ijob_core.model`, lists the
postings a source's latest fetch still showed, and closes it. It also keeps the
table of sources, the boards ijob collects from: seeded or added by the user,
removable, and stamped with the time each was last fetched. The schema version
lives in `PRAGMA user_version`; opening a database applies every migration newer
than that version, each in one transaction.
"""

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping
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
from ijob_core.model import (
    ExternalId,
    Posting,
    PostingId,
    RemoteScope,
    Sighting,
    SightingDraft,
    SourceRef,
    parse_source_ref,
)
from ijob_core.money import Money, Period

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
)


def default_db_path(environ: Mapping[str, str]) -> Path:
    """Locate the database file when the caller names none.

    Args:
        environ: The process environment.

    Returns:
        `$XDG_DATA_HOME/ijob/ijob.db`, or `~/.local/share/ijob/ijob.db` when
        `XDG_DATA_HOME` is unset or empty.
    """
    data_home = environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "ijob" / "ijob.db"


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
    "low_confidence FROM postings"
)

_NEWEST_FIRST: Final = "ORDER BY posted_at IS NULL, posted_at DESC, id"

_OPEN_WINDOW: Final = timedelta(days=7)


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
            conn = cleanup.enter_context(closing(sqlite3.connect(path)))
            conn.execute("PRAGMA foreign_keys = ON")
            _migrate(conn)
            cleanup.pop_all()
        return cls(conn)

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
        salaries, country, remote scope and role hint follow the draft only when
        the draft has a value for them, and its confidence flag always follows
        the draft. The posting's `canonical_url` and `fingerprint` are then
        recomputed from its resulting values. Each draft is matched after the
        earlier ones in the batch are written.

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
                "canonical_url, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
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
            "role_hint = COALESCE(NULLIF(?, ''), role_hint) WHERE id = ?",
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
            "SELECT posting_id, first_seen, last_seen, raw FROM sightings "
            "WHERE source = ? AND external_id = ?",
            (str(source), external_id),
        ).fetchone()
        if row is None:
            return None
        return Sighting(
            posting_id=PostingId(row[0]),
            source=source,
            external_id=external_id,
            first_seen=_from_text(row[1]),
            last_seen=_from_text(row[2]),
            raw=row[3],
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
