"""The local SQLite store of trace spans.

`TraceStore` owns a database file separate from the postings database. It
opens the file and brings its schema up to date, records finished spans
without duplicating one it already holds, lists traces newest first with the
model calls and cost each made, reads one trace back as a tree of spans, prunes
old traces to a size cap, and closes the file. It knows nothing of how spans are
produced: callers hand it plain `SpanRow` values.

The schema version lives in `PRAGMA user_version`; opening a database applies
every migration newer than that version, each in one transaction.

`TraceStore.prune` keeps the file under a size cap by deleting the oldest
traces whole.
"""

import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from ijob_core.errors import InvalidValueError

_BUSY_TIMEOUT_MS: Final = 5000

_MODEL_ATTRIBUTE: Final = "gen_ai.request.model"
_COST_ATTRIBUTE: Final = "gen_ai.usage.cost"

_INCOMPLETE: Final = "(incomplete)"

_MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (
    (
        """
        CREATE TABLE spans (
            trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_id TEXT,
            name TEXT NOT NULL, start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL,
            status TEXT NOT NULL, attributes TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id)
        ) STRICT
        """,
        "CREATE INDEX spans_start_ns ON spans(start_ns)",
    ),
)

# One row per trace. The root is the span with no parent; a trace whose root
# row is missing falls back to its earliest start and latest end. The model
# attribute and cost are read from the JSON attributes of every span.
_SELECT_SUMMARIES: Final = f"""
WITH roots AS (
    SELECT trace_id, name, start_ns, end_ns,
           ROW_NUMBER() OVER (
               PARTITION BY trace_id ORDER BY start_ns, span_id
           ) AS rank
    FROM spans WHERE parent_id IS NULL
), totals AS (
    SELECT trace_id, MIN(start_ns) AS first_start, MAX(end_ns) AS last_end,
           COALESCE(SUM(
               json_type(attributes, '$."{_MODEL_ATTRIBUTE}"') IS NOT NULL
           ), 0) AS calls,
           COALESCE(SUM(
               json_extract(attributes, '$."{_COST_ATTRIBUTE}"')
           ), 0.0) AS cost
    FROM spans GROUP BY trace_id
)
SELECT totals.trace_id,
       COALESCE(roots.start_ns, totals.first_start) AS started,
       COALESCE(roots.name, '{_INCOMPLETE}'),
       totals.calls, totals.cost,
       COALESCE(roots.end_ns, totals.last_end)
           - COALESCE(roots.start_ns, totals.first_start)
FROM totals LEFT JOIN roots ON roots.trace_id = totals.trace_id AND roots.rank = 1
ORDER BY started DESC, totals.trace_id
LIMIT ?
"""  # noqa: S608  # only module constants are interpolated

_SELECT_SPANS: Final = (
    "SELECT trace_id, span_id, parent_id, name, start_ns, end_ns, status, attributes "
    "FROM spans"
)


_OLDEST_TRACE: Final = "SELECT trace_id FROM spans ORDER BY start_ns, trace_id LIMIT 1"

_LIVE_BYTES: Final = (
    "SELECT (page_count.page_count - freelist_count.freelist_count) "
    "* page_size.page_size "
    "FROM pragma_page_count AS page_count, pragma_freelist_count AS freelist_count, "
    "pragma_page_size AS page_size"
)

_FILE_BYTES: Final = (
    "SELECT page_count.page_count * page_size.page_size "
    "FROM pragma_page_count AS page_count, pragma_page_size AS page_size"
)


class UnknownTraceError(InvalidValueError):
    """A trace id that the store holds no span for."""

    def __init__(self, trace_id: str) -> None:
        """Build the error from the unknown trace id.

        Args:
            trace_id: The id that matched no trace.
        """
        super().__init__("trace_id", f"{trace_id} is not a trace")


@dataclass(frozen=True, slots=True)
class SpanRow:
    """One finished span as the store keeps it.

    Attributes:
        trace_id: The trace the span belongs to, as a hex string.
        span_id: The span's id within the trace, as a hex string.
        parent_id: The parent span's id, or `None` for a root.
        name: The span's name.
        start_ns: When the span began, in nanoseconds since the Unix epoch.
        end_ns: When the span ended, in nanoseconds since the Unix epoch.
        status: The span's outcome as text.
        attributes: The span's attributes; stored as JSON, so every value
            must be JSON-serialisable.
    """

    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    start_ns: int
    end_ns: int
    status: str
    attributes: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """One line of the trace listing.

    Attributes:
        trace_id: The trace described.
        started: When the root span began; for a trace with no root row, when
            its earliest span began.
        operation: The root span's name, or `(incomplete)` for a trace with no
            root row.
        calls: How many spans carry a `gen_ai.request.model` attribute.
        cost_usd: The sum of the spans' `gen_ai.usage.cost` attributes.
        duration_ms: The root span's duration in milliseconds; for a trace
            with no root row, from its earliest start to its latest end.
    """

    trace_id: str
    started: datetime
    operation: str
    calls: int
    cost_usd: float
    duration_ms: float


@dataclass(frozen=True, slots=True)
class SpanNode:
    """A span with the spans nested under it.

    Attributes:
        span: The span itself.
        children: Its child spans, ordered by start time.
    """

    span: SpanRow
    children: tuple["SpanNode", ...]


@dataclass(frozen=True, slots=True)
class TraceTree:
    """One trace read back as nested spans.

    Attributes:
        trace_id: The trace read.
        roots: The spans with no parent in the store, ordered by start time.
            A complete trace has exactly one; a trace whose parent rows were
            never written lists the spans left without a parent.
    """

    trace_id: str
    roots: tuple[SpanNode, ...]


@dataclass(frozen=True, slots=True)
class PruneReport:
    """What a prune did.

    Attributes:
        removed: How many whole traces were deleted.
        size_bytes: The database file's size once the prune finished.
    """

    removed: int
    size_bytes: int


def _span_row(row: tuple[Any, ...]) -> SpanRow:
    """Build a `SpanRow` from a row selected with `_SELECT_SPANS`."""
    return SpanRow(
        trace_id=row[0],
        span_id=row[1],
        parent_id=row[2],
        name=row[3],
        start_ns=row[4],
        end_ns=row[5],
        status=row[6],
        attributes=json.loads(row[7]),
    )


def _from_ns(nanoseconds: int) -> datetime:
    """Read nanoseconds since the Unix epoch as an aware UTC datetime."""
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=nanoseconds // 1000
    )


def _summary(row: tuple[Any, ...]) -> TraceSummary:
    """Build a `TraceSummary` from a row selected with `_SELECT_SUMMARIES`."""
    return TraceSummary(
        trace_id=row[0],
        started=_from_ns(row[1]),
        operation=row[2],
        calls=int(row[3]),
        cost_usd=float(row[4]),
        duration_ms=row[5] / 1_000_000,
    )


def _nest(spans: list[SpanRow]) -> tuple[SpanNode, ...]:
    """Nest spans, ordered by start time, under the parents the list holds."""
    ids = {span.span_id for span in spans}
    children: dict[str | None, list[SpanRow]] = {}
    for span in spans:
        parent = span.parent_id if span.parent_id in ids else None
        children.setdefault(parent, []).append(span)

    def build(span: SpanRow) -> SpanNode:
        below = children.get(span.span_id, [])
        return SpanNode(span, tuple(build(child) for child in below))

    return tuple(build(span) for span in children.get(None, []))


class TraceStore:
    """An open connection to the trace database.

    Build one with `TraceStore.open`. A `TraceStore` is a context manager that
    closes its connection on exit.
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

        The connection waits up to 5 seconds for another writer's lock.

        Args:
            path: The database file. Missing parent directories are created.

        Returns:
            A store on a database at the current schema version.

        Raises:
            sqlite3.Error: If the file cannot be opened or migrated.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with ExitStack() as cleanup:
            # One operation may hand the store to a worker thread to flush and
            # close it; its callers never use the store from two threads at once.
            conn = cleanup.enter_context(
                closing(sqlite3.connect(path, check_same_thread=False))
            )
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            _migrate(conn)
            cleanup.pop_all()
        return cls(conn)

    def insert(self, rows: Iterable[SpanRow]) -> None:
        """Record spans, skipping any whose trace and span id are already held.

        The rows are written in one transaction. They may arrive in any order,
        children before their parents included.

        Args:
            rows: The spans to record.

        Raises:
            sqlite3.Error: If the write fails.
            TypeError: If an attribute value is not JSON-serialisable.
        """
        values = [
            (
                row.trace_id,
                row.span_id,
                row.parent_id,
                row.name,
                row.start_ns,
                row.end_ns,
                row.status,
                json.dumps(row.attributes),
            )
            for row in rows
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO spans "
                "(trace_id, span_id, parent_id, name, start_ns, end_ns, status, "
                "attributes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def summaries(self, limit: int) -> list[TraceSummary]:
        """List traces, newest first.

        A trace whose root row is missing, such as one from a process that was
        killed after exporting only its children, is still listed: its
        `operation` is `(incomplete)`, it started with its earliest span and its
        duration runs from that start to its latest end.

        Args:
            limit: The most traces to return.

        Returns:
            Up to `limit` summaries ordered by `started`, newest first.

        Raises:
            InvalidValueError: If `limit` is negative.
        """
        if limit < 0:
            raise InvalidValueError("limit", "is negative")
        rows = self._conn.execute(_SELECT_SUMMARIES, (limit,)).fetchall()
        return [_summary(row) for row in rows]

    def tree(self, trace_id: str) -> TraceTree:
        """Read one trace as nested spans.

        Args:
            trace_id: The trace to read.

        Returns:
            The trace's spans nested under their parents, siblings ordered by
            start time.

        Raises:
            UnknownTraceError: If the store holds no span of that trace.
        """
        rows = self._conn.execute(
            f"{_SELECT_SPANS} WHERE trace_id = ? ORDER BY start_ns, span_id",
            (trace_id,),
        ).fetchall()
        if not rows:
            raise UnknownTraceError(trace_id)
        return TraceTree(trace_id, _nest([_span_row(row) for row in rows]))

    def prune(self, max_bytes: int) -> PruneReport:
        """Delete the oldest traces until the database fits `max_bytes`.

        Nothing happens while the file is within the cap. Otherwise whole
        traces are deleted, each ordered by its earliest span start whether or
        not it has a root, until the live size, which leaves out pages freed by
        earlier deletes, fits the cap; then `VACUUM` returns the freed pages to
        the file system. If the cap is smaller than an empty database, every
        trace is deleted.

        Args:
            max_bytes: The size the database file should not exceed.

        Returns:
            How many traces were deleted and the file size afterwards.

        Raises:
            sqlite3.Error: If a delete or the `VACUUM` fails.
        """
        size = self._scalar(_FILE_BYTES)
        if size <= max_bytes:
            return PruneReport(removed=0, size_bytes=size)
        removed = 0
        with self._conn:
            while self._scalar(_LIVE_BYTES) > max_bytes:
                oldest = self._conn.execute(_OLDEST_TRACE).fetchone()
                if oldest is None:
                    break
                self._conn.execute("DELETE FROM spans WHERE trace_id = ?", oldest)
                removed += 1
        self._conn.execute("VACUUM")
        return PruneReport(removed=removed, size_bytes=self._scalar(_FILE_BYTES))

    def _scalar(self, sql: str) -> int:
        """Run a query that returns one integer."""
        return int(self._conn.execute(sql).fetchone()[0])

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
