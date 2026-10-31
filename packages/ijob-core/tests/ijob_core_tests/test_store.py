import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, override

import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.store import RecordResult, Store, default_db_path

_A = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_B = SourceRef(SourceKind.GREENHOUSE, parse_source_key("beta"))
_T1 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
_T2 = datetime(2026, 10, 2, 9, 30, 15, 250000, tzinfo=UTC)
_T1_TEXT = "2026-10-01T08:00:00.000000+00:00"
_T2_TEXT = "2026-10-02T09:30:15.250000+00:00"


class _TrackedConnection(sqlite3.Connection):
    """A connection that remembers whether it was closed."""

    closed = False

    @override
    def close(self) -> None:
        self.closed = True
        super().close()


def _scalar(path: Path, sql: str) -> object:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchone()[0]


def _rows(path: Path, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def _draft(
    source: SourceRef = _A,
    external_id: str = "1",
    **overrides: Any,  # noqa: ANN401  # forwards arbitrary field overrides
) -> SightingDraft:
    values: dict[str, Any] = {
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote",
        "url": "https://example.com/jobs/1",
        "posted_at": None,
        "raw": '{"id": 1}',
    }
    values.update(overrides)
    return SightingDraft(source=source, external_id=ExternalId(external_id), **values)


def test_open_creates_schema_version_1(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path):
        pass
    assert _scalar(path, "PRAGMA user_version") == 1
    tables = _scalar(
        path,
        "SELECT group_concat(name) FROM "
        "(SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name)",
    )
    assert tables == "postings,sightings"


def test_open_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path):
        pass
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO postings (title, company, url, first_seen, last_seen) "
            "VALUES ('t', 'c', 'https://x', 'a', 'b')"
        )
    with Store.open(path):
        pass
    assert _scalar(path, "PRAGMA user_version") == 1
    assert _scalar(path, "SELECT count(*) FROM postings") == 1


def test_open_creates_missing_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "ijob.db"
    with Store.open(path):
        pass
    assert path.is_file()


def test_default_db_path_uses_xdg_data_home() -> None:
    assert default_db_path({"XDG_DATA_HOME": "/x"}) == Path("/x/ijob/ijob.db")


@pytest.mark.parametrize("environ", [{}, {"XDG_DATA_HOME": ""}])
def test_default_db_path_falls_back_to_local_share(
    environ: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        default_db_path(environ) == tmp_path / ".local" / "share" / "ijob" / "ijob.db"
    )


def test_record_new_draft_creates_posting_and_sighting(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        result = store.record([_draft()], seen_at=_T1)
    assert result == RecordResult(new=1, updated=0)
    assert _scalar(path, "SELECT count(*) FROM postings") == 1
    assert _scalar(path, "SELECT count(*) FROM sightings") == 1
    assert _rows(
        path, "SELECT source, external_id, first_seen, last_seen FROM sightings"
    ) == [("greenhouse:alpha", "1", _T1_TEXT, _T1_TEXT)]


def test_record_same_draft_again_updates_in_place(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        store.record([_draft()], seen_at=_T1)
        result = store.record(
            [_draft(title="Senior Engineer", raw='{"id": 2}')], seen_at=_T2
        )
    assert result == RecordResult(new=0, updated=1)
    assert _scalar(path, "SELECT count(*) FROM postings") == 1
    assert _scalar(path, "SELECT count(*) FROM sightings") == 1
    assert _rows(path, "SELECT first_seen, last_seen, raw FROM sightings") == [
        (_T1_TEXT, _T2_TEXT, '{"id": 2}')
    ]
    assert _rows(path, "SELECT title, first_seen, last_seen FROM postings") == [
        ("Senior Engineer", _T1_TEXT, _T2_TEXT)
    ]


def test_record_same_external_id_from_two_sources_is_two_postings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        result = store.record([_draft(_A), _draft(_B)], seen_at=_T1)
    assert result == RecordResult(new=2, updated=0)
    assert _scalar(path, "SELECT count(*) FROM postings") == 2
    assert _scalar(path, "SELECT count(*) FROM sightings") == 2


def test_update_touches_only_its_own_source(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        store.record(
            [_draft(_A, title="A title"), _draft(_B, title="B title")], seen_at=_T1
        )
        store.record([_draft(_A, title="A changed")], seen_at=_T2)
    rows = _rows(
        path,
        "SELECT s.source, p.title, p.last_seen FROM postings p "
        "JOIN sightings s ON s.posting_id = p.id ORDER BY s.source",
    )
    assert rows == [
        ("greenhouse:alpha", "A changed", _T2_TEXT),
        ("greenhouse:beta", "B title", _T1_TEXT),
    ]


def test_record_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "ijob.db"
    original = Store._write_draft  # the failure-injection seam
    calls = 0

    def flaky(self: Store, draft: SightingDraft, seen_at: datetime) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError
        return original(self, draft, seen_at)

    monkeypatch.setattr(Store, "_write_draft", flaky)
    with Store.open(path) as store, pytest.raises(sqlite3.OperationalError):
        store.record([_draft(external_id="1"), _draft(external_id="2")], seen_at=_T1)
    assert _scalar(path, "SELECT count(*) FROM postings") == 0
    assert _scalar(path, "SELECT count(*) FROM sightings") == 0


def test_postings_orders_newest_first_with_undated_last(tmp_path: Path) -> None:
    day1 = datetime(2026, 9, 1, tzinfo=UTC)
    day3 = datetime(2026, 9, 3, tzinfo=UTC)
    with Store.open(tmp_path / "ijob.db") as store:
        store.record(
            [
                _draft(external_id="undated"),
                _draft(external_id="day1", posted_at=day1),
                _draft(external_id="day3", posted_at=day3),
            ],
            seen_at=_T1,
        )
        postings = store.postings(sources=[_A])
    assert [p.posted_at for p in postings] == [day3, day1, None]
    assert all(p.first_seen == _T1 and p.last_seen == _T1 for p in postings)


def test_postings_filters_by_source(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record(
            [_draft(_A, title="from A"), _draft(_B, title="from B")],
            seen_at=_T1,
        )
        only_a = store.postings(sources=[_A])
        none = store.postings(sources=[])
        both = store.postings(sources={_A, _B})
    assert [p.title for p in only_a] == ["from A"]
    assert none == []
    assert sorted(p.title for p in both) == ["from A", "from B"]


def test_sighting_round_trips_raw_and_times(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(raw='{"k": "v"}')], seen_at=_T1)
        store.record([_draft(raw='{"k": "w"}')], seen_at=_T2)
        sighting = store.sighting(_A, ExternalId("1"))
    assert sighting is not None
    assert sighting.source == _A
    assert sighting.external_id == "1"
    assert sighting.raw == '{"k": "w"}'
    assert sighting.first_seen == _T1
    assert sighting.last_seen == _T2
    assert sighting.first_seen.utcoffset() == timedelta(0)


def test_sighting_unknown_is_none(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft()], seen_at=_T1)
        assert store.sighting(_A, ExternalId("missing")) is None
        assert store.sighting(_B, ExternalId("1")) is None


def test_schema_enforces_strict_and_unique(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        store.record([_draft()], seen_at=_T1)
    insert = (
        "INSERT INTO sightings "
        "(posting_id, source, external_id, first_seen, last_seen, raw) "
        "VALUES (?, 'greenhouse:alpha', ?, 'a', 'b', '{}')"
    )
    with closing(sqlite3.connect(path)) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(insert, (1, "1"))
        with pytest.raises(sqlite3.IntegrityError, match="INTEGER"):
            conn.execute(insert, ("not a number", "2"))


def test_record_stores_non_utc_times_as_utc(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    minus4 = timezone(timedelta(hours=-4))
    plus9 = timezone(timedelta(hours=9))
    # Instants: late is 2026-09-02T03:00Z, early is 2026-09-01T20:00Z. As offset
    # text "2026-09-02T05:00+09:00" sorts after "2026-09-01T23:00-04:00".
    late = datetime(2026, 9, 1, 23, 0, tzinfo=minus4)
    early = datetime(2026, 9, 2, 5, 0, tzinfo=plus9)
    with Store.open(path) as store:
        store.record(
            [
                _draft(external_id="late", posted_at=late),
                _draft(external_id="early", posted_at=early),
            ],
            seen_at=datetime(2026, 10, 1, 20, 0, tzinfo=minus4),
        )
        store.record(
            [_draft(external_id="late", posted_at=late)],
            seen_at=datetime(2026, 10, 3, 18, 30, tzinfo=plus9),
        )
        postings = store.postings(sources=[_A])
    assert _rows(
        path, "SELECT posted_at, first_seen, last_seen FROM postings ORDER BY id"
    ) == [
        (
            "2026-09-02T03:00:00.000000+00:00",
            "2026-10-02T00:00:00.000000+00:00",
            "2026-10-03T09:30:00.000000+00:00",
        ),
        (
            "2026-09-01T20:00:00.000000+00:00",
            "2026-10-02T00:00:00.000000+00:00",
            "2026-10-02T00:00:00.000000+00:00",
        ),
    ]
    assert [p.posted_at for p in postings] == [late, early]
    assert [p.posted_at for p in postings] == sorted((late, early), reverse=True)


def test_failed_migration_leaves_version_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ijob.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE sightings (id INTEGER)")
    opened: list[_TrackedConnection] = []
    real_connect = sqlite3.connect

    def tracking_connect(database: Path) -> _TrackedConnection:
        conn = real_connect(database, factory=_TrackedConnection)
        opened.append(conn)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", tracking_connect)
        with pytest.raises(sqlite3.OperationalError):
            Store.open(path)
    assert len(opened) == 1
    assert opened[0].closed
    assert _scalar(path, "PRAGMA user_version") == 0
    assert (
        _scalar(path, "SELECT count(*) FROM sqlite_master WHERE name = 'postings'") == 0
    )


def test_record_rejects_naive_seen_at(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store, pytest.raises(InvalidValueError):
        store.record([_draft()], seen_at=datetime(2026, 10, 1, 8, 0))  # noqa: DTZ001  # naive on purpose
