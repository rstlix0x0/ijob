"""The SQLite cache that lets `PoliteClient` revalidate a stored response.

`SqliteHttpCache` keeps each response body with the `ETag` and `Last-Modified`
the server gave for it, keyed by URL. It drops entries that have gone unused
for 30 days when it is opened, so the file stays bounded. It satisfies the
`ConditionalCache` protocol of `ijob_sources.polite_http` without importing it.
"""

import sqlite3
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from ijob_sources.polite_http import CachedResponse

_MAX_IDLE: Final = timedelta(days=30)

_CREATE_TABLE: Final = """
    CREATE TABLE IF NOT EXISTS responses (
        url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, body BLOB,
        used_at TEXT
    ) STRICT
"""


def default_cache_path(environ: Mapping[str, str]) -> Path:
    """Locate the cache file when the caller names none.

    Args:
        environ: The process environment.

    Returns:
        `$XDG_CACHE_HOME/ijob/http.sqlite`, or `~/.cache/ijob/http.sqlite` when
        `XDG_CACHE_HOME` is unset or empty.
    """
    cache_home = environ.get("XDG_CACHE_HOME")
    base = Path(cache_home) if cache_home else Path.home() / ".cache"
    return base / "ijob" / "http.sqlite"


def _to_text(moment: datetime) -> str:
    """Render an aware datetime as fixed-width ISO-8601 text in UTC.

    Fixed width makes text order the same as time order, which the eviction
    query relies on.
    """
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


class SqliteHttpCache:
    """An open connection to the response cache.

    Build one with `SqliteHttpCache.open`. It is a context manager that closes
    its connection on exit.
    """

    def __init__(self, conn: sqlite3.Connection, now: Callable[[], datetime]) -> None:
        """Wrap a connection to a database that has the `responses` table.

        Args:
            conn: The open connection. The cache takes ownership and closes it.
            now: The wall clock, returning timezone-aware times; it stamps each
                entry's last use.
        """
        self._conn = conn
        self._now = now

    @classmethod
    def open(cls, path: Path, *, now: Callable[[], datetime]) -> Self:
        """Open the cache at `path`, creating it and dropping stale entries.

        Entries last used more than 30 days before `now()` are deleted.

        Args:
            path: The database file. Missing parent directories are created.
            now: The wall clock, returning timezone-aware times.

        Returns:
            A cache whose table exists and holds no stale entries.

        Raises:
            sqlite3.Error: If the file cannot be opened or prepared.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with ExitStack() as cleanup:
            conn = cleanup.enter_context(closing(sqlite3.connect(path)))
            with conn:
                conn.execute(_CREATE_TABLE)
                conn.execute(
                    "DELETE FROM responses WHERE used_at < ?",
                    (_to_text(now() - _MAX_IDLE),),
                )
            cleanup.pop_all()
        return cls(conn, now)

    def lookup(self, url: str) -> CachedResponse | None:
        """Return the stored response for a URL and mark it as just used.

        Args:
            url: The absolute URL that was fetched.

        Returns:
            The stored response, or `None` if there is none.

        Raises:
            sqlite3.Error: If the read or the update fails.
        """
        with self._conn:
            row = self._conn.execute(
                "UPDATE responses SET used_at = ? WHERE url = ? "
                "RETURNING etag, last_modified, body",
                (_to_text(self._now()), url),
            ).fetchone()
        if row is None:
            return None
        return CachedResponse(etag=row[0], last_modified=row[1], body=row[2])

    def store(self, url: str, response: CachedResponse) -> None:
        """Keep a response for a URL, replacing any earlier one.

        Args:
            url: The absolute URL that was fetched.
            response: The response body and its validators.

        Raises:
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO responses (url, etag, last_modified, body, used_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (url) DO UPDATE SET "
                "etag = excluded.etag, last_modified = excluded.last_modified, "
                "body = excluded.body, used_at = excluded.used_at",
                (
                    url,
                    response.etag,
                    response.last_modified,
                    response.body,
                    _to_text(self._now()),
                ),
            )

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    def __enter__(self) -> Self:
        """Return the cache itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection; exceptions propagate."""
        self.close()
