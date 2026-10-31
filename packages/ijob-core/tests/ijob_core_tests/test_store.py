import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, override

import pytest
from ijob_core.countries import CountryCode, parse_country
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    PostingId,
    RemoteScope,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.money import Money, Period
from ijob_core.store import (
    RecordResult,
    SourceOrigin,
    SourceRow,
    Store,
    _Outcome,
    default_db_path,
)

_A = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_B = SourceRef(SourceKind.GREENHOUSE, parse_source_key("beta"))
_C = SourceRef(SourceKind.GREENHOUSE, parse_source_key("gamma"))
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


def test_fresh_database_is_version_2(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path):
        pass
    assert _scalar(path, "PRAGMA user_version") == 2
    tables = _scalar(
        path,
        "SELECT group_concat(name) FROM "
        "(SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name)",
    )
    assert tables == "postings,sightings,sources"
    indexes = _scalar(
        path,
        "SELECT group_concat(name) FROM "
        "(SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name)",
    )
    assert indexes == (
        "postings_canonical_url,postings_fingerprint,sightings_posting_id"
    )


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
    assert _scalar(path, "PRAGMA user_version") == 2
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
        result = store.record(
            [_draft(_A), _draft(_B, url="https://example.com/jobs/b")], seen_at=_T1
        )
    assert result == RecordResult(new=2, updated=0)
    assert _scalar(path, "SELECT count(*) FROM postings") == 2
    assert _scalar(path, "SELECT count(*) FROM sightings") == 2


def test_update_touches_only_its_own_source(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        store.record(
            [
                _draft(_A, title="A title"),
                _draft(_B, title="B title", url="https://example.com/jobs/b"),
            ],
            seen_at=_T1,
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

    def flaky(self: Store, draft: SightingDraft, seen_at: datetime) -> _Outcome:
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
            [
                _draft(_A, title="from A"),
                _draft(_B, title="from B", url="https://example.com/jobs/b"),
            ],
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


def test_migration_1_to_2_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE postings (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
            "company TEXT NOT NULL, location TEXT, url TEXT NOT NULL, "
            "posted_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL"
            ") STRICT"
        )
        conn.execute(
            "CREATE TABLE sightings (id INTEGER PRIMARY KEY, "
            "posting_id INTEGER NOT NULL REFERENCES postings(id), "
            "source TEXT NOT NULL, external_id TEXT NOT NULL, "
            "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, raw TEXT NOT NULL, "
            "UNIQUE (source, external_id)) STRICT"
        )
        conn.execute(
            "INSERT INTO postings (title, company, location, url, posted_at, "
            "first_seen, last_seen) VALUES "
            "('Engineer', 'Acme', 'Remote', 'https://example.com/jobs/1', NULL, ?, ?)",
            (_T1_TEXT, _T1_TEXT),
        )
        conn.execute(
            "INSERT INTO sightings (posting_id, source, external_id, first_seen, "
            "last_seen, raw) VALUES (1, 'greenhouse:alpha', '1', ?, ?, '{}')",
            (_T1_TEXT, _T1_TEXT),
        )
        conn.execute("PRAGMA user_version = 1")
    with Store.open(path) as store:
        postings = store.postings(sources=[_A])
    assert _scalar(path, "PRAGMA user_version") == 2
    assert len(postings) == 1
    posting = postings[0]
    assert (posting.title, posting.company) == ("Engineer", "Acme")
    assert posting.salaries == ()
    assert posting.country is None
    assert posting.remote is None
    assert posting.role_hint is None
    assert posting.low_confidence is False


def test_new_fields_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    salaries = (
        Money(Decimal("100000.50"), Decimal(150000), "USD", Period.YEAR),
        Money(None, Decimal(90), "EUR", None),
    )
    country = parse_country("Germany")
    assert country is not None
    draft = _draft(
        salaries=salaries,
        country=country,
        remote=RemoteScope.WORLDWIDE,
        role_hint="backend",
        low_confidence=True,
    )
    with Store.open(path) as store:
        store.record([draft], seen_at=_T1)
        store.record([draft], seen_at=_T2)
        (posting,) = store.postings(sources=[_A])
    assert posting.salaries == salaries
    assert posting.country == country
    assert posting.remote is RemoteScope.WORLDWIDE
    assert posting.role_hint == "backend"
    assert posting.low_confidence is True
    assert _rows(path, "SELECT canonical_url, fingerprint FROM postings") == [
        ("https://example.com/jobs/1", "acme|engineer|DE")
    ]


def _country(name: str) -> CountryCode:
    country = parse_country(name)
    assert country is not None
    return country


def test_merge_by_canonical_url(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        first = store.record(
            [_draft(_A, url="https://Example.com/jobs/1?utm_source=a")], seen_at=_T1
        )
        second = store.record(
            [_draft(_B, "9", url="https://example.com/jobs/1/?utm_source=b")],
            seen_at=_T2,
        )
    assert first == RecordResult(new=1, updated=0, merged=0)
    assert second == RecordResult(new=0, updated=0, merged=1)
    assert _scalar(path, "SELECT count(*) FROM postings") == 1
    assert _scalar(path, "SELECT count(*) FROM sightings") == 2
    assert _rows(path, "SELECT first_seen, last_seen FROM postings") == [
        (_T1_TEXT, _T2_TEXT)
    ]


def test_merge_updates_display_fields_from_draft(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        store.record([_draft(_A, title="Old title")], seen_at=_T1)
        store.record(
            [_draft(_B, "9", title="New title", location="Berlin")], seen_at=_T2
        )
    assert _rows(path, "SELECT title, location FROM postings") == [
        ("New title", "Berlin")
    ]


def test_merge_in_one_batch(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        result = store.record([_draft(_A), _draft(_B, "9")], seen_at=_T1)
    assert result == RecordResult(new=1, updated=0, merged=1)
    assert _scalar(path, "SELECT count(*) FROM postings") == 1


def test_merge_keeps_fingerprint_without_draft_country(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    gb = _country("United Kingdom")
    with Store.open(path) as store:
        store.record([_draft(_A, country=gb)], seen_at=_T1)
        store.record(
            [_draft(_B, "9", url="https://Example.com/jobs/1", country=None)],
            seen_at=_T1,
        )
        (posting,) = store.postings(sources=[_A])
        assert posting.country == gb
        third = store.record(
            [_draft(_C, "5", url="https://other.example/x", country=gb)], seen_at=_T2
        )
    assert third == RecordResult(new=0, updated=0, merged=1)
    assert _scalar(path, "SELECT count(*) FROM postings") == 1
    assert _scalar(path, "SELECT fingerprint FROM postings") == f"acme|engineer|{gb}"


def test_lowest_id_wins_on_tie(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        store.record([_draft(_A, "1"), _draft(_A, "2")], seen_at=_T1)
        result = store.record([_draft(_B, "9")], seen_at=_T2)
    assert result == RecordResult(new=0, updated=0, merged=1)
    assert _rows(
        path, "SELECT posting_id FROM sightings WHERE source = 'greenhouse:beta'"
    ) == [(1,)]


def test_canonical_url_match_beats_fingerprint_match(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    de = _country("Germany")
    with Store.open(path) as store:
        store.record(
            [
                _draft(_A, "1", url="https://y.example/1", country=de),
                _draft(_A, "2", url="https://x.example/1", title="Other"),
            ],
            seen_at=_T1,
        )
        result = store.record(
            [_draft(_B, "9", url="https://x.example/1", country=de)], seen_at=_T2
        )
    assert result == RecordResult(new=0, updated=0, merged=1)
    assert _rows(
        path, "SELECT posting_id FROM sightings WHERE source = 'greenhouse:beta'"
    ) == [(2,)]


def test_merge_by_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    de = _country("Germany")
    with Store.open(path) as store:
        store.record([_draft(_A, url="https://a.example/1", country=de)], seen_at=_T1)
        result = store.record(
            [
                _draft(
                    _B,
                    "9",
                    url="https://b.example/2",
                    company="ACME Ltd",
                    title="engineer",
                    country=de,
                )
            ],
            seen_at=_T2,
        )
    assert result == RecordResult(new=0, updated=0, merged=1)
    assert _scalar(path, "SELECT count(*) FROM postings") == 1
    assert _scalar(path, "SELECT count(*) FROM sightings") == 2


def test_no_merge_without_country(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        result = store.record(
            [
                _draft(_A, url="https://a.example/1", country=None),
                _draft(_B, "9", url="https://b.example/2", country=None),
            ],
            seen_at=_T1,
        )
    assert result == RecordResult(new=2, updated=0, merged=0)
    assert _scalar(path, "SELECT count(*) FROM postings") == 2


def test_no_merge_within_one_source(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    us = _country("United States")
    with Store.open(path) as store:
        result = store.record(
            [
                _draft(_A, "1", url="https://a.example/1", country=us),
                _draft(_A, "2", url="https://a.example/2", country=us),
            ],
            seen_at=_T1,
        )
    assert result == RecordResult(new=2, updated=0, merged=0)
    assert _scalar(path, "SELECT count(*) FROM postings") == 2


def test_empty_draft_value_does_not_overwrite(tmp_path: Path) -> None:
    salaries = (Money(Decimal(100), Decimal(200), "USD", Period.YEAR),)
    de = _country("Germany")
    full = _draft(
        _A,
        salaries=salaries,
        country=de,
        remote=RemoteScope.WORLDWIDE,
        role_hint="backend",
    )
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([full], seen_at=_T1)
        store.record([_draft(_B, "9")], seen_at=_T2)
        store.record([_draft(_A)], seen_at=_T2)
        (posting,) = store.postings(sources=[_A, _B])
    assert posting.salaries == salaries
    assert posting.country == de
    assert posting.remote is RemoteScope.WORLDWIDE
    assert posting.role_hint == "backend"


def test_non_empty_draft_value_overwrites(tmp_path: Path) -> None:
    salaries = (Money(Decimal(1), Decimal(2), "EUR", Period.MONTH),)
    fr = _country("France")
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(_A, role_hint="old")], seen_at=_T1)
        store.record(
            [
                _draft(
                    _B,
                    "9",
                    salaries=salaries,
                    country=fr,
                    remote=RemoteScope.COUNTRY,
                    role_hint="new",
                )
            ],
            seen_at=_T2,
        )
        (posting,) = store.postings(sources=[_A])
    assert posting.salaries == salaries
    assert posting.country == fr
    assert posting.remote is RemoteScope.COUNTRY
    assert posting.role_hint == "new"


def test_still_listed_refreshes_last_seen(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        store.record([_draft(_A, "1"), _draft(_A, "2")], seen_at=_T1)
        result = store.record(
            [],
            seen_at=_T2,
            still_listed=[(_A, ExternalId("1")), (_A, ExternalId("missing"))],
        )
    assert result == RecordResult(new=0, updated=0, merged=0)
    assert _rows(
        path, "SELECT external_id, last_seen FROM sightings ORDER BY external_id"
    ) == [("1", _T2_TEXT), ("2", _T1_TEXT)]
    assert _rows(path, "SELECT id, last_seen FROM postings ORDER BY id") == [
        (1, _T2_TEXT),
        (2, _T1_TEXT),
    ]
    assert _scalar(path, "SELECT count(*) FROM sightings") == 2


def test_import_seeds_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    rows = [(_A, "Alpha"), (_B, "Beta")]
    with Store.open(path) as store:
        assert store.import_seeds(rows) == 2
        assert store.import_seeds(rows) == 0
        assert store.source(_A) == SourceRow(
            ref=_A,
            company="Alpha",
            target_url=None,
            origin=SourceOrigin.SEED,
            removed=False,
            last_fetched_at=None,
        )
    assert (
        _scalar(path, "SELECT origin FROM sources WHERE ref = 'greenhouse:alpha'")
        == "seed"
    )


def test_import_seeds_leaves_present_rows_untouched(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.add_source(_A, "Mine", "https://example.com/a")
        assert store.import_seeds([(_A, "Seed Name"), (_B, "Beta")]) == 1
        row = store.source(_A)
    assert row is not None
    assert row.company == "Mine"
    assert row.origin is SourceOrigin.USER


def test_removed_seed_stays_removed(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha")])
        store.remove_source(_A)
        assert store.import_seeds([(_A, "Alpha")]) == 0
        assert store.sources() == []


def test_add_revives_and_keeps_origin(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha")])
        store.remove_source(_A)
        store.add_source(_A, "Alpha")
        (row,) = store.sources()
    assert row.ref == _A
    assert row.origin.value == "seed"
    assert not row.removed


def test_add_user_source(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.add_source(_A, "Alpha", "https://example.com/jobs")
        row = store.source(_A)
    assert row is not None
    assert row.origin == SourceOrigin.USER
    assert row.target_url == "https://example.com/jobs"
    assert row.company == "Alpha"


def test_source_reports_removed(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha")])
        store.remove_source(_A)
        row = store.source(_A)
        assert store.sources() == []
    assert row is not None
    assert row.removed


def test_source_unknown_is_none(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        assert store.source(_A) is None


def test_sources_are_ordered_by_ref(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_C, "Gamma"), (_A, "Alpha"), (_B, "Beta")])
        assert [row.ref for row in store.sources()] == [_A, _B, _C]


def test_mark_fetched(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    plus_nine = datetime(
        2026, 10, 2, 18, 30, 15, 250000, tzinfo=timezone(timedelta(hours=9))
    )
    with Store.open(path) as store:
        store.import_seeds([(_A, "Alpha")])
        store.mark_fetched(_A, _T2)
        row = store.source(_A)
        assert row is not None
        assert row.last_fetched_at == _T2
        store.mark_fetched(_A, plus_nine)
    assert _scalar(path, "SELECT last_fetched_at FROM sources") == _T2_TEXT


def test_mark_fetched_rejects_naive_time(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha")])
        with pytest.raises(InvalidValueError):
            store.mark_fetched(_A, datetime(2026, 10, 2, 9, 30))  # noqa: DTZ001  # naive on purpose
        row = store.source(_A)
    assert row is not None
    assert row.last_fetched_at is None


def _job(source: SourceRef, external_id: str) -> SightingDraft:
    """A draft whose url and title belong to its source, so nothing merges."""
    return _draft(
        source,
        external_id,
        title=f"Engineer {source} {external_id}",
        url=f"https://example.com/{source}/{external_id}",
    )


def _open_titles(store: Store, **kwargs: Any) -> list[str]:  # noqa: ANN401  # forwards open_postings arguments
    return [posting.title for posting in store.open_postings(**kwargs)]


def test_open_postings_follow_latest_fetch(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha")])
        store.record([_job(_A, "1"), _job(_A, "2")], seen_at=_T1)
        store.mark_fetched(_A, _T1)
        store.record([_job(_A, "1")], seen_at=_T2)
        store.mark_fetched(_A, _T2)
        titles = _open_titles(store, now=_T2)
    assert titles == [f"Engineer {_A} 1"]


def test_failed_source_keeps_last_known(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha"), (_B, "Beta")])
        store.record([_job(_B, "1")], seen_at=_T1)
        store.mark_fetched(_B, _T1)
        store.record([_job(_A, "1")], seen_at=_T2)
        store.mark_fetched(_A, _T2)
        titles = _open_titles(store, now=_T1 + timedelta(days=3))
    assert sorted(titles) == [f"Engineer {_A} 1", f"Engineer {_B} 1"]


def test_stale_source_hidden(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha"), (_B, "Beta")])
        store.record([_job(_A, "1"), _job(_B, "1")], seen_at=_T1)
        store.mark_fetched(_A, _T2)
        store.mark_fetched(_B, _T1)
        edge = _T1 + timedelta(days=7)
        assert store.stale_sources(now=edge) == []
        assert f"Engineer {_B} 1" in _open_titles(store, now=edge)
        late = edge + timedelta(microseconds=1)
        stale = store.stale_sources(now=late)
        titles = _open_titles(store, now=late)
    assert [row.ref for row in stale] == [_B]
    assert f"Engineer {_B} 1" not in titles


def test_removed_source_hidden(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha"), (_B, "Beta")])
        store.record([_job(_A, "1"), _job(_B, "1")], seen_at=_T1)
        store.mark_fetched(_A, _T1)
        store.mark_fetched(_B, _T1)
        store.remove_source(_B)
        titles = _open_titles(store, now=_T1)
        stale = store.stale_sources(now=_T1 + timedelta(days=30))
    assert titles == [f"Engineer {_A} 1"]
    assert [row.ref for row in stale] == [_A]


def test_open_postings_narrowed_by_sources(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha"), (_B, "Beta")])
        store.record([_job(_A, "1"), _job(_B, "1")], seen_at=_T1)
        store.mark_fetched(_A, _T1)
        store.mark_fetched(_B, _T1)
        only_a = _open_titles(store, now=_T1, sources=[_A])
        none = _open_titles(store, now=_T1, sources=[])
        everything = _open_titles(store, now=_T1)
    assert only_a == [f"Engineer {_A} 1"]
    assert none == []
    assert len(everything) == 2


def test_merged_posting_open_via_either_source(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha"), (_B, "Beta")])
        store.record([_draft(_A, "1")], seen_at=_T1)
        store.record([_draft(_B, "9")], seen_at=_T1)
        store.mark_fetched(_A, _T1)
        store.mark_fetched(_B, _T1)
        assert len(store.open_postings(now=_T1)) == 1
        store.record([], seen_at=_T2)
        store.mark_fetched(_B, _T2)
        via_a = store.open_postings(now=_T2)
        store.mark_fetched(_A, _T2)
        store.mark_fetched(_B, _T1)
        via_b = store.open_postings(now=_T2)
        store.mark_fetched(_B, _T2)
        closed = store.open_postings(now=_T2)
    assert len(via_a) == 1
    assert len(via_b) == 1
    assert closed == []


def test_external_ids(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_job(_A, "1"), _job(_A, "2"), _job(_B, "3")], seen_at=_T1)
        assert store.external_ids(_A) == frozenset({ExternalId("1"), ExternalId("2")})
        assert store.external_ids(_B) == frozenset({ExternalId("3")})
        assert store.external_ids(_C) == frozenset()


def test_posting_sources(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(_B, "9")], seen_at=_T1)
        store.record([_draft(_A, "1")], seen_at=_T1)
        store.record([_job(_C, "5")], seen_at=_T1)
        (merged, single) = store.postings(sources=[_A, _B, _C])
        assert store.posting_sources(merged.id) == [_A, _B]
        assert store.posting_sources(single.id) == [_C]
        assert store.posting_sources(PostingId(999)) == []


def test_open_queries_reject_naive_now(tmp_path: Path) -> None:
    naive = datetime(2026, 10, 2, 9, 30)  # noqa: DTZ001  # naive on purpose
    with Store.open(tmp_path / "ijob.db") as store:
        with pytest.raises(InvalidValueError):
            store.open_postings(now=naive)
        with pytest.raises(InvalidValueError):
            store.stale_sources(now=naive)
