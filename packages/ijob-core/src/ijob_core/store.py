"""The local SQLite store of postings and the sightings behind them.

`Store` owns the database file: it opens it, brings its schema up to date,
records sighting drafts without duplicating a posting from the same source,
reads postings and sightings back as the records of `ijob_core.model`, and closes
it. The schema version lives in `PRAGMA user_version`; opening a database
applies every migration newer than that version, each in one transaction.
"""

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    Posting,
    PostingId,
    Sighting,
    SightingDraft,
    SourceRef,
)

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
        updated: Drafts that refreshed a known posting.
    """

    new: int
    updated: int


def _to_text(moment: datetime) -> str:
    """Render an aware datetime as fixed-width ISO-8601 text in UTC."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _from_text(text: str) -> datetime:
    """Read ISO-8601 text written by `_to_text` as an aware UTC datetime."""
    return datetime.fromisoformat(text).astimezone(UTC)


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
        self, drafts: Iterable[SightingDraft], *, seen_at: datetime
    ) -> RecordResult:
        """Record what a source showed, without duplicating known postings.

        All drafts are written in one write transaction, taken before the first
        lookup so a concurrent writer cannot slip a duplicate in: if any write
        fails, none persists. A draft whose source and external id are new adds
        a posting and a sighting. A known one refreshes the sighting's `last_seen`
        and `raw` and the posting's `last_seen` and display fields from the draft.

        Args:
            drafts: The jobs a source showed.
            seen_at: When they were shown, timezone-aware.

        Returns:
            How many drafts were new and how many updated a known posting.

        Raises:
            InvalidValueError: If `seen_at` has no timezone.
            sqlite3.Error: If a write fails.
        """
        if seen_at.utcoffset() is None:
            raise InvalidValueError("seen_at", "has no timezone")
        new = 0
        updated = 0
        self._conn.execute("BEGIN IMMEDIATE")
        with self._conn:
            for draft in drafts:
                if self._write_draft(draft, seen_at):
                    new += 1
                else:
                    updated += 1
        return RecordResult(new=new, updated=updated)

    def _write_draft(self, draft: SightingDraft, seen_at: datetime) -> bool:
        """Write one draft; return whether it created a posting."""
        stamp = _to_text(seen_at)
        source = str(draft.source)
        known = self._conn.execute(
            "SELECT id, posting_id FROM sightings WHERE source = ? AND external_id = ?",
            (source, draft.external_id),
        ).fetchone()
        posted_at = None if draft.posted_at is None else _to_text(draft.posted_at)
        if known is None:
            posting_id = self._conn.execute(
                "INSERT INTO postings "
                "(title, company, location, url, posted_at, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    draft.title,
                    draft.company,
                    draft.location,
                    draft.url,
                    posted_at,
                    stamp,
                    stamp,
                ),
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO sightings "
                "(posting_id, source, external_id, first_seen, last_seen, raw) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (posting_id, source, draft.external_id, stamp, stamp, draft.raw),
            )
            return True
        sighting_id, posting_id = known
        self._conn.execute(
            "UPDATE sightings SET last_seen = ?, raw = ? WHERE id = ?",
            (stamp, draft.raw, sighting_id),
        )
        self._conn.execute(
            "UPDATE postings SET title = ?, company = ?, location = ?, url = ?, "
            "posted_at = ?, last_seen = ? WHERE id = ?",
            (
                draft.title,
                draft.company,
                draft.location,
                draft.url,
                posted_at,
                stamp,
                posting_id,
            ),
        )
        return False

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
            "SELECT id, title, company, location, url, posted_at, "
            "first_seen, last_seen FROM postings WHERE EXISTS ("
            "SELECT 1 FROM sightings WHERE sightings.posting_id = postings.id "
            "AND sightings.source IN (SELECT value FROM json_each(?))) "
            "ORDER BY posted_at IS NULL, posted_at DESC, id",
            (wanted,),
        ).fetchall()
        return [
            Posting(
                id=PostingId(row[0]),
                title=row[1],
                company=row[2],
                location=row[3],
                url=row[4],
                posted_at=None if row[5] is None else _from_text(row[5]),
                first_seen=_from_text(row[6]),
                last_seen=_from_text(row[7]),
            )
            for row in rows
        ]

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
