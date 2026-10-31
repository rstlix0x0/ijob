import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from ijob_sources.http_cache import SqliteHttpCache, default_cache_path
from ijob_sources.polite_http import CachedResponse, PoliteClient

_T = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
_URL = "https://boards.example.test/v1/boards/x/jobs"
_ENTRY = CachedResponse('W/"v1"', "Sat, 01 Aug 2026 00:00:00 GMT", b"\x00\xffbody")


def _open(path: Path, now: datetime = _T) -> SqliteHttpCache:
    return SqliteHttpCache.open(path, now=lambda: now)


def test_store_then_lookup(tmp_path: Path) -> None:
    with _open(tmp_path / "http.sqlite") as cache:
        cache.store(_URL, _ENTRY)
        assert cache.lookup(_URL) == _ENTRY


def test_store_keeps_missing_validators(tmp_path: Path) -> None:
    only_etag = CachedResponse('"e"', None, b"a")
    only_date = CachedResponse(None, "Sat, 01 Aug 2026 00:00:00 GMT", b"b")
    with _open(tmp_path / "http.sqlite") as cache:
        cache.store("https://x.test/a", only_etag)
        cache.store("https://x.test/b", only_date)
        assert cache.lookup("https://x.test/a") == only_etag
        assert cache.lookup("https://x.test/b") == only_date


def test_store_replaces(tmp_path: Path) -> None:
    newer = CachedResponse('"v2"', None, b"newer")
    with _open(tmp_path / "http.sqlite") as cache:
        cache.store(_URL, _ENTRY)
        cache.store(_URL, newer)
        assert cache.lookup(_URL) == newer


def test_lookup_missing_is_none(tmp_path: Path) -> None:
    with _open(tmp_path / "http.sqlite") as cache:
        assert cache.lookup(_URL) is None


def test_entries_persist_across_open(tmp_path: Path) -> None:
    path = tmp_path / "http.sqlite"
    with _open(path) as cache:
        cache.store(_URL, _ENTRY)
    with _open(path) as cache:
        assert cache.lookup(_URL) == _ENTRY


def test_open_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "nested" / "http.sqlite"
    with _open(path):
        pass
    assert path.is_file()


def test_open_creates_strict_responses_table(tmp_path: Path) -> None:
    path = tmp_path / "http.sqlite"
    with _open(path):
        pass
    with closing(sqlite3.connect(path)) as conn:
        columns = conn.execute("PRAGMA table_info(responses)").fetchall()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'responses'"
        ).fetchone()[0]
    assert [(row[1], row[2], row[5]) for row in columns] == [
        ("url", "TEXT", 1),
        ("etag", "TEXT", 0),
        ("last_modified", "TEXT", 0),
        ("body", "BLOB", 0),
        ("used_at", "TEXT", 0),
    ]
    assert sql.rstrip().endswith("STRICT")


def test_exit_closes_connection(tmp_path: Path) -> None:
    with _open(tmp_path / "http.sqlite") as cache:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        cache.lookup(_URL)


def test_old_rows_evicted_on_open(tmp_path: Path) -> None:
    path = tmp_path / "http.sqlite"
    with _open(path) as cache:
        cache.store(_URL, _ENTRY)
    with _open(path, _T + timedelta(days=31)) as cache:
        assert cache.lookup(_URL) is None


def test_recent_rows_kept_on_open(tmp_path: Path) -> None:
    path = tmp_path / "http.sqlite"
    with _open(path) as cache:
        cache.store(_URL, _ENTRY)
    with _open(path, _T + timedelta(days=29)) as cache:
        assert cache.lookup(_URL) == _ENTRY


def test_old_rows_gone_from_file_after_eviction(tmp_path: Path) -> None:
    path = tmp_path / "http.sqlite"
    with _open(path) as cache:
        cache.store(_URL, _ENTRY)
    with _open(path, _T + timedelta(days=31)):
        pass
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM responses").fetchone()[0] == 0


def test_lookup_refreshes_used_at(tmp_path: Path) -> None:
    path = tmp_path / "http.sqlite"
    with _open(path) as cache:
        cache.store(_URL, _ENTRY)
    with _open(path, _T + timedelta(days=20)) as cache:
        assert cache.lookup(_URL) == _ENTRY
    with _open(path, _T + timedelta(days=31)) as cache:
        assert cache.lookup(_URL) == _ENTRY


def test_store_refreshes_used_at(tmp_path: Path) -> None:
    path = tmp_path / "http.sqlite"
    with _open(path) as cache:
        cache.store(_URL, _ENTRY)
    with _open(path, _T + timedelta(days=20)) as cache:
        cache.store(_URL, _ENTRY)
    with _open(path, _T + timedelta(days=45)) as cache:
        assert cache.lookup(_URL) == _ENTRY


def test_used_at_normalised_to_utc(tmp_path: Path) -> None:
    path = tmp_path / "http.sqlite"
    with _open(path) as cache:
        cache.store(_URL, _ENTRY)
    # 16:00 at +09:00 is 07:00Z, so the cutoff is 07:00Z on the first, an hour
    # before the stored 08:00Z; read as plain local text it would sort after it.
    tokyo = timezone(timedelta(hours=9))
    later = datetime(2026, 10, 31, 16, 0, tzinfo=tokyo)
    with _open(path, later) as cache:
        assert cache.lookup(_URL) == _ENTRY


def test_cache_path_xdg() -> None:
    assert default_cache_path({"XDG_CACHE_HOME": "/xdg/cache"}) == Path(
        "/xdg/cache/ijob/http.sqlite"
    )


def test_cache_path_falls_back_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".cache" / "ijob" / "http.sqlite"
    assert default_cache_path({}) == expected
    assert default_cache_path({"XDG_CACHE_HOME": ""}) == expected


def test_client_with_sqlite_cache(tmp_path: Path) -> None:
    seen: list[httpx.Headers] = []

    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *")
        seen.append(request.headers)
        if len(seen) == 1:
            return httpx.Response(200, content=b"the board", headers={"ETag": '"v1"'})
        return httpx.Response(304)

    async def fetch_twice(cache: SqliteHttpCache) -> list[httpx.Response]:
        async with PoliteClient(
            transport=httpx.MockTransport(site), min_interval=0.0, cache=cache
        ) as client:
            return [
                await client.get(_URL, conditional=True),
                await client.get(_URL, conditional=True),
            ]

    with _open(tmp_path / "http.sqlite") as cache:
        first, second = asyncio.run(fetch_twice(cache))
    assert (first.status_code, first.content) == (200, b"the board")
    assert (second.status_code, second.content) == (200, b"the board")
    assert "if-none-match" not in seen[0]
    assert seen[1]["if-none-match"] == '"v1"'
