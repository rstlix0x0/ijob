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
    Instructions,
    Label,
    PostingId,
    RemoteScope,
    ResumeProfile,
    Score,
    Seniority,
    SightingDraft,
    SourceKind,
    SourceRef,
    StoredCv,
    StoredRecipe,
    Verdict,
    Vote,
    parse_source_key,
)
from ijob_core.money import Money, Period
from ijob_core.store import (
    _MIGRATIONS,
    RecordResult,
    SkippedStatus,
    SourceOrigin,
    SourceRow,
    Store,
    StoredProfile,
    _Outcome,
    skipped_item_id,
)

_A = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_B = SourceRef(SourceKind.GREENHOUSE, parse_source_key("beta"))
_C = SourceRef(SourceKind.GREENHOUSE, parse_source_key("gamma"))
_T1 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
_T2 = datetime(2026, 10, 2, 9, 30, 15, 250000, tzinfo=UTC)
_T1_TEXT = "2026-10-01T08:00:00.000000+00:00"
_T2_TEXT = "2026-10-02T09:30:15.250000+00:00"
_ALL_TABLES = (
    "cv,cv_profile,eval_cases,eval_labels,eval_results,eval_runs,eval_sets,"
    "eval_votes,instructions,labels,postings,scores,sightings,skipped_items,"
    "source_recipes,sources,verdicts"
)


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


def test_fresh_database_is_version_8(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path):
        pass
    assert _scalar(path, "PRAGMA user_version") == 8
    tables = _scalar(
        path,
        "SELECT group_concat(name) FROM "
        "(SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name)",
    )
    assert tables == _ALL_TABLES
    indexes = _scalar(
        path,
        "SELECT group_concat(name) FROM "
        "(SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name)",
    )
    assert indexes == (
        "postings_canonical_url,postings_fingerprint,postings_listed,"
        "sightings_open,sightings_posting_id"
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
    assert _scalar(path, "PRAGMA user_version") == 8
    assert _scalar(path, "SELECT count(*) FROM postings") == 1


def test_open_creates_missing_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "ijob.db"
    with Store.open(path):
        pass
    assert path.is_file()


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

    def tracking_connect(database: Path, timeout: float) -> _TrackedConnection:
        conn = real_connect(database, timeout=timeout, factory=_TrackedConnection)
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


def test_version_1_database_migrates_and_keeps_rows(tmp_path: Path) -> None:
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
    assert _scalar(path, "PRAGMA user_version") == 8
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


def _version_2_database(path: Path) -> None:
    """Build a database by applying only the first two migrations."""
    with closing(sqlite3.connect(path)) as conn, conn:
        for statements in _MIGRATIONS[:2]:
            for statement in statements:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO postings (title, company, location, url, first_seen, "
            "last_seen) VALUES ('Engineer', 'Acme', 'Remote', "
            "'https://example.com/jobs/1', ?, ?)",
            (_T1_TEXT, _T1_TEXT),
        )
        conn.execute(
            "INSERT INTO sightings (posting_id, source, external_id, first_seen, "
            "last_seen, raw) VALUES (1, 'greenhouse:alpha', '1', ?, ?, '{}')",
            (_T1_TEXT, _T1_TEXT),
        )
        conn.execute("PRAGMA user_version = 2")


def test_migration_3_on_version_2_database(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    _version_2_database(path)
    with Store.open(path) as store:
        (posting,) = store.postings(sources=[_A])
    assert _scalar(path, "PRAGMA user_version") == 8
    assert posting.title == "Engineer"
    assert posting.description is None
    tables = _scalar(
        path,
        "SELECT group_concat(name) FROM "
        "(SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name)",
    )
    assert tables == _ALL_TABLES


def test_record_stores_description(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(description="## About")], seen_at=_T1)
        (posting,) = store.postings(sources=[_A])
    assert posting.description == "## About"


def test_record_keeps_description_when_draft_has_none(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(description="## About")], seen_at=_T1)
        store.record([_draft(description=None)], seen_at=_T2)
        (posting,) = store.postings(sources=[_A])
    assert posting.description == "## About"


def test_record_keeps_description_when_draft_description_is_empty(
    tmp_path: Path,
) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(description="## About")], seen_at=_T1)
        store.record([_draft(description="")], seen_at=_T2)
        (posting,) = store.postings(sources=[_A])
    assert posting.description == "## About"


def test_record_replaces_description_when_draft_has_one(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(description="## About")], seen_at=_T1)
        store.record([_draft(description="## Newer")], seen_at=_T2)
        (posting,) = store.postings(sources=[_A])
    assert posting.description == "## Newer"


def _record_ids(store: Store, *drafts: SightingDraft) -> list[PostingId]:
    """Record each draft as its own batch and return the ids of source `_A` postings."""
    for draft in drafts:
        store.record([draft], seen_at=_T1)
    return [posting.id for posting in store.postings(sources=[_A])]


def _distinct_drafts() -> list[SightingDraft]:
    return [
        _draft(
            external_id=str(n),
            title=f"Engineer {n}",
            company=f"Company {n}",
            url=f"https://example.com/jobs/{n}",
        )
        for n in (1, 2, 3)
    ]


def test_posting_by_id(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(description="## About")], seen_at=_T1)
        (listed,) = store.postings(sources=[_A])
        assert store.posting(listed.id) == listed
        assert store.posting(PostingId(999)) is None


def _record_companies(store: Store, names: list[str]) -> None:
    store.record(
        [
            _draft(
                _A,
                f"c-{n}",
                title=f"Engineer {n}",
                company=name,
                url=f"https://example.com/jobs/c-{n}",
            )
            for n, name in enumerate(names)
        ],
        seen_at=_T1,
    )


def test_companies_matching_is_distinct_and_limited(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        _record_companies(
            store, ["Acme Corp", "Acme Corp", "Acme Labs", "Acme Works", "Other Inc"]
        )
        assert store.companies_matching("acme", 50) == [
            "Acme Corp",
            "Acme Labs",
            "Acme Works",
        ]
        assert store.companies_matching("acme", 2) == ["Acme Corp", "Acme Labs"]
        assert store.companies_matching("zzz", 50) == []


def test_companies_matching_escapes_the_query(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        _record_companies(store, ["100% Remote", "100x Remote", "a_b Ltd", "axb Ltd"])
        assert store.companies_matching("%", 50) == ["100% Remote"]
        assert store.companies_matching("_", 50) == ["a_b Ltd"]
        assert store.companies_matching("a_b", 50) == ["a_b Ltd"]


def test_companies_matching_takes_a_backslash_literally(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        _record_companies(store, ["a\\b Ltd", "ab Ltd", "a\\%b Ltd", "a%b Ltd"])
        assert store.companies_matching("a\\b", 50) == ["a\\b Ltd"]
        assert store.companies_matching("\\", 50) == ["a\\%b Ltd", "a\\b Ltd"]
        assert store.companies_matching("a\\%", 50) == ["a\\%b Ltd"]


def test_companies_matching_limit_of_zero_or_less_returns_none(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        _record_companies(store, ["Acme Corp", "Acme Labs"])
        assert store.companies_matching("acme", 0) == []
        assert store.companies_matching("acme", -1) == []
        assert store.companies_matching("", 1) == ["Acme Corp"]


def test_sightings_of_in_id_order(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([_draft(_B, "b-1", raw="b")], seen_at=_T1)
        store.record([_draft(_A, "a-1", raw="a")], seen_at=_T2)
        (posting,) = store.postings(sources=[_A, _B])
        sightings = store.sightings_of(posting.id)
        assert store.sightings_of(PostingId(999)) == []
        assert sightings == [
            store.sighting(_B, ExternalId("b-1")),
            store.sighting(_A, ExternalId("a-1")),
        ]
    assert [s.raw for s in sightings] == ["b", "a"]


def test_set_description_writes_text(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft())
        store.set_description(posting_id, "## Full text")
        posting = store.posting(posting_id)
        assert posting is not None
        assert posting.description == "## Full text"
        with pytest.raises(InvalidValueError, match="posting_id"):
            store.set_description(PostingId(999), "x")


@pytest.mark.parametrize("text", ["", "  \n\t "])
def test_set_description_rejects_blank_text(tmp_path: Path, text: str) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft(description="## Kept"))
        with pytest.raises(InvalidValueError, match="text"):
            store.set_description(posting_id, text)
        posting = store.posting(posting_id)
        assert posting is not None
        assert posting.description == "## Kept"


def test_set_label_then_replace(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft())
        first = store.set_label(posting_id, Vote.UP, "fit", at=_T1)
        assert first == Label(posting_id, Vote.UP, "fit", _T1)
        second = store.set_label(posting_id, Vote.DOWN, None, at=_T2)
        assert second == Label(posting_id, Vote.DOWN, None, _T2)
        assert store.label(posting_id) == second
    assert _scalar(tmp_path / "ijob.db", "SELECT count(*) FROM labels") == 1


def test_set_label_returns_what_label_reads_back(tmp_path: Path) -> None:
    jakarta = timezone(timedelta(hours=7))
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft())
        stored = store.set_label(
            posting_id, Vote.UP, None, at=datetime(2026, 10, 1, 15, 0, tzinfo=jakarta)
        )
        read = store.label(posting_id)
    assert stored == read
    assert repr(stored) == repr(read)


def test_set_label_rejects_naive_time_and_unknown_posting(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft())
        with pytest.raises(InvalidValueError, match="invalid at:"):
            store.set_label(
                posting_id,
                Vote.UP,
                None,
                at=datetime(2026, 10, 1, 8, 0),  # noqa: DTZ001  # naive on purpose
            )
        with pytest.raises(InvalidValueError, match="posting_id"):
            store.set_label(PostingId(999), Vote.UP, None, at=_T1)
        assert store.label(posting_id) is None


def test_clear_label(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        cleared, kept = _record_ids(store, *_distinct_drafts()[:2])
        store.set_label(cleared, Vote.UP, None, at=_T1)
        survivor = store.set_label(kept, Vote.DOWN, "no", at=_T2)
        store.clear_label(cleared)
        assert store.label(cleared) is None
        assert store.label(kept) == survivor
        store.clear_label(cleared)
        store.clear_label(PostingId(999))


def test_labels_for_ids(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        a, b, c = _record_ids(store, *_distinct_drafts())
        store.set_label(a, Vote.UP, "fit", at=_T1)
        store.set_label(c, Vote.DOWN, None, at=_T2)
        found = store.labels([a, b, c])
        assert found == {
            a: Label(a, Vote.UP, "fit", _T1),
            c: Label(c, Vote.DOWN, None, _T2),
        }
        assert store.labels([]) == {}
        assert store.labels([b]) == {}


def test_label_survives_new_sighting(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft())
        label = store.set_label(posting_id, Vote.UP, "fit", at=_T1)
        result = store.record([_draft(_B, "b-1")], seen_at=_T2)
        assert result.merged == 1
        assert len(store.sightings_of(posting_id)) == 2
        assert store.label(posting_id) == label


def test_cv_round_trip_and_replace(tmp_path: Path) -> None:
    first = StoredCv("cv.pdf", "ab" * 32, 100, _T1)
    second = StoredCv("cv2.pdf", "cd" * 32, 200, _T2)
    with Store.open(tmp_path / "ijob.db") as store:
        assert store.cv() is None
        store.set_cv(first)
        assert store.cv() == first
        store.set_cv(second)
        assert store.cv() == second
    assert _scalar(tmp_path / "ijob.db", "SELECT count(*) FROM cv") == 1


def _version_3_database(path: Path) -> None:
    """Build a database by applying only the first three migrations."""
    with closing(sqlite3.connect(path)) as conn, conn:
        for statements in _MIGRATIONS[:3]:
            for statement in statements:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO postings (title, company, location, url, first_seen, "
            "last_seen) VALUES ('Engineer', 'Acme', 'Remote', "
            "'https://example.com/jobs/1', ?, ?)",
            (_T1_TEXT, _T1_TEXT),
        )
        conn.execute(
            "INSERT INTO cv (id, name, sha256, size, stored_at) "
            "VALUES (1, 'cv.pdf', ?, 10, ?)",
            ("ab" * 32, _T1_TEXT),
        )
        conn.execute("PRAGMA user_version = 3")


def test_migration_4_creates_tables(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    _version_3_database(path)
    with Store.open(path) as store:
        posting = store.posting(PostingId(1))
        cv = store.cv()
        assert store.cv_profile() is None
        assert store.instructions() is None
    assert _scalar(path, "PRAGMA user_version") == 8
    assert posting is not None
    assert posting.title == "Engineer"
    assert cv == StoredCv("cv.pdf", "ab" * 32, 10, _T1)
    tables = _scalar(
        path,
        "SELECT group_concat(name) FROM "
        "(SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name)",
    )
    assert tables == _ALL_TABLES


def _version_4_database(path: Path) -> None:
    """Build a database by applying only the first four migrations."""
    with closing(sqlite3.connect(path)) as conn, conn:
        for statements in _MIGRATIONS[:4]:
            for statement in statements:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO sources (ref, company, origin) "
            "VALUES ('greenhouse:alpha', 'Alpha', 'seed')"
        )
        conn.execute("PRAGMA user_version = 4")


def test_migration_5_adds_source_recipes(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    with Store.open(fresh):
        pass
    assert _scalar(fresh, "PRAGMA user_version") == 8
    assert _rows(fresh, "PRAGMA table_info(source_recipes)") == [
        (0, "ref", "TEXT", 1, None, 1),
        (1, "recipe", "TEXT", 1, None, 0),
        (2, "model", "TEXT", 1, None, 0),
        (3, "created_at", "TEXT", 1, None, 0),
    ]
    assert _rows(fresh, "PRAGMA foreign_key_list(source_recipes)") == [
        (0, 0, "sources", "ref", "ref", "NO ACTION", "NO ACTION", "NONE")
    ]

    upgraded = tmp_path / "upgraded.db"
    _version_4_database(upgraded)
    assert _rows(upgraded, "PRAGMA table_info(source_recipes)") == []
    with Store.open(upgraded) as store:
        row = store.source(_A)
        assert store.recipe(_A) is None
    assert _scalar(upgraded, "PRAGMA user_version") == 8
    assert _rows(upgraded, "PRAGMA table_info(source_recipes)") == _rows(
        fresh, "PRAGMA table_info(source_recipes)"
    )
    assert row is not None
    assert row.company == "Alpha"
    assert row.origin is SourceOrigin.SEED


def test_set_recipe_source_adds_both_rows(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.set_recipe_source(
            _A,
            company="Alpha",
            target_url="https://example.com/jobs",
            recipe='{"steps": []}',
            model="model-a",
            at=_T2,
        )
        row = store.source(_A)
        stored = store.recipe(_A)
    assert row == SourceRow(
        ref=_A,
        company="Alpha",
        target_url="https://example.com/jobs",
        origin=SourceOrigin.USER,
        removed=False,
        last_fetched_at=None,
    )
    assert stored == StoredRecipe(
        ref=_A, recipe='{"steps": []}', model="model-a", created_at=_T2
    )


def test_set_recipe_source_writes_utc_text(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    local = datetime(
        2026, 10, 2, 11, 30, 15, 250000, tzinfo=timezone(timedelta(hours=2))
    )
    with Store.open(path) as store:
        store.set_recipe_source(
            _A,
            company="Alpha",
            target_url=None,
            recipe="{}",
            model="model-a",
            at=local,
        )
    assert _scalar(path, "SELECT created_at FROM source_recipes") == _T2_TEXT


def test_set_recipe_source_replaces_recipe_and_restores(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.set_recipe_source(
            _A,
            company="First",
            target_url="https://example.com/one",
            recipe='{"v": 1}',
            model="model-a",
            at=_T1,
        )
        store.remove_source(_A)
        store.set_recipe_source(
            _A,
            company="Second",
            target_url="https://example.com/two",
            recipe='{"v": 2}',
            model="model-b",
            at=_T2,
        )
        row = store.source(_A)
        stored = store.recipe(_A)
    assert row is not None
    assert not row.removed
    assert row.company == "First"
    assert stored == StoredRecipe(
        ref=_A, recipe='{"v": 2}', model="model-b", created_at=_T2
    )


def test_removed_source_keeps_recipe(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.set_recipe_source(
            _A,
            company="Alpha",
            target_url=None,
            recipe="{}",
            model="model-a",
            at=_T1,
        )
        store.remove_source(_A)
        stored = store.recipe(_A)
    assert stored == StoredRecipe(ref=_A, recipe="{}", model="model-a", created_at=_T1)


def test_set_recipe_source_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "CREATE TRIGGER fail_recipe BEFORE INSERT ON source_recipes "
                "BEGIN SELECT RAISE(ABORT, 'x'); END"
            )
        with pytest.raises(sqlite3.Error, match="x"):
            store.set_recipe_source(
                _B,
                company="Beta",
                target_url=None,
                recipe="{}",
                model="model-a",
                at=_T1,
            )
        assert store.source(_B) is None
        assert store.recipe(_B) is None


def test_recipe_unknown_ref_is_none(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        assert store.recipe(_A) is None


def test_set_recipe_source_rejects_naive_time(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        with pytest.raises(InvalidValueError, match="at"):
            store.set_recipe_source(
                _A,
                company="Alpha",
                target_url=None,
                recipe="{}",
                model="model-a",
                at=datetime(2026, 10, 1, 8, 0),  # noqa: DTZ001  # naive on purpose
            )
        assert store.source(_A) is None


def _profile(summary: str = "Backend engineer") -> ResumeProfile:
    return ResumeProfile(
        summary=summary,
        years_experience=7,
        seniority=Seniority.SENIOR,
        roles=("backend",),
        spoken_languages=("English",),
        locations=("Jakarta",),
        work_authorization=("ID",),
        skills=("python", "sql"),
    )


def _instructions(text: str = "Backend only") -> Instructions:
    return Instructions(
        text=text,
        filters_json='{"include": ["backend"]}',
        rules=("no on-site", "no crypto"),
        model="model-a",
        task_version="1",
        set_at=_T1,
    )


def test_cv_profile_roundtrip_and_replace(tmp_path: Path) -> None:
    first = StoredProfile(_profile(), "ab" * 32, "model-a", "1", _T1)
    second = StoredProfile(_profile("Staff engineer"), "cd" * 32, "model-b", "2", _T2)
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        assert store.cv_profile() is None
        store.set_cv_profile(
            first.profile,
            cv_sha256=first.cv_sha256,
            model=first.model,
            task_version=first.task_version,
            made_at=first.made_at,
        )
        assert store.cv_profile() == first
        store.set_cv_profile(
            second.profile,
            cv_sha256=second.cv_sha256,
            model=second.model,
            task_version=second.task_version,
            made_at=second.made_at,
        )
        assert store.cv_profile() == second
    assert _scalar(path, "SELECT count(*) FROM cv_profile") == 1
    assert _scalar(path, "SELECT profile_sha256 FROM cv_profile") == (
        second.profile.sha256()
    )


def test_set_cv_profile_rejects_naive_time(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store, pytest.raises(InvalidValueError):
        store.set_cv_profile(
            _profile(),
            cv_sha256="ab" * 32,
            model="m",
            task_version="1",
            made_at=datetime(2026, 10, 1),  # noqa: DTZ001  # the naive time is the case
        )


def test_instructions_roundtrip_and_clear(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        assert store.instructions() is None
        store.clear_instructions()
        assert store.instructions() is None
        first = _instructions()
        store.set_instructions(first)
        assert store.instructions() == first
        second = _instructions("Remote only")
        store.set_instructions(second)
        assert store.instructions() == second
        store.clear_instructions()
        assert store.instructions() is None
    assert _scalar(path, "SELECT count(*) FROM instructions") == 0


def test_instructions_store_hashes(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    stored = _instructions()
    with Store.open(path) as store:
        store.set_instructions(stored)
    assert _scalar(path, "SELECT text_sha256 FROM instructions") == stored.text_sha256
    assert _scalar(path, "SELECT rules_sha256 FROM instructions") == stored.rules_sha256


_KEY = ("in-hash", "rules-hash", "model-a", "1")


def test_verdict_cache_hit_and_miss(tmp_path: Path) -> None:
    keep = Verdict(keep=True, reason="fits", rule=None)
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft())
        assert store.verdict(posting_id, *_KEY) is None
        store.put_verdict(posting_id, *_KEY, keep, made_at=_T1)
        assert store.verdict(posting_id, *_KEY) == keep
        for index in range(len(_KEY)):
            changed = tuple("other" if i == index else v for i, v in enumerate(_KEY))
            assert store.verdict(posting_id, *changed) is None
        assert store.verdict(PostingId(posting_id + 1), *_KEY) is None


def test_put_verdict_replaces_same_key(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    reject = Verdict(keep=False, reason="on-site", rule=1)
    with Store.open(path) as store:
        (posting_id,) = _record_ids(store, _draft())
        store.put_verdict(
            posting_id, *_KEY, Verdict(keep=True, reason="fits", rule=None), made_at=_T1
        )
        store.put_verdict(posting_id, *_KEY, reject, made_at=_T2)
        assert store.verdict(posting_id, *_KEY) == reject
    assert _scalar(path, "SELECT count(*) FROM verdicts") == 1
    assert _scalar(path, "SELECT made_at FROM verdicts") == _T2_TEXT


def test_put_verdict_unknown_posting_fails(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.put_verdict(
                PostingId(99),
                *_KEY,
                Verdict(keep=True, reason="fits", rule=None),
                made_at=_T1,
            )
        assert store.verdict(PostingId(99), *_KEY) is None
    assert _scalar(path, "SELECT count(*) FROM verdicts") == 0


_SCORE_KEY = ("in-hash", "profile-hash", "model-a", "1")


def test_score_cache_hit_and_miss(tmp_path: Path) -> None:
    fit = Score(score=82, reasons=("python match", "senior"))
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft())
        assert store.score(posting_id, *_SCORE_KEY) is None
        store.put_score(posting_id, *_SCORE_KEY, fit, made_at=_T1)
        assert store.score(posting_id, *_SCORE_KEY) == fit
        for index in range(len(_SCORE_KEY)):
            changed = tuple(
                "other" if i == index else v for i, v in enumerate(_SCORE_KEY)
            )
            assert store.score(posting_id, *changed) is None
        better = Score(score=90, reasons=("python match",))
        store.put_score(posting_id, *_SCORE_KEY, better, made_at=_T2)
        assert store.score(posting_id, *_SCORE_KEY) == better


def test_put_score_unknown_posting_fails(tmp_path: Path) -> None:
    with (
        Store.open(tmp_path / "ijob.db") as store,
        pytest.raises(sqlite3.IntegrityError),
    ):
        store.put_score(
            PostingId(99), *_SCORE_KEY, Score(score=5, reasons=("none",)), made_at=_T1
        )


def test_put_rejects_naive_time(tmp_path: Path) -> None:
    naive = datetime(2026, 10, 1)  # noqa: DTZ001  # the naive time is the case
    with Store.open(tmp_path / "ijob.db") as store:
        (posting_id,) = _record_ids(store, _draft())
        with pytest.raises(InvalidValueError):
            store.put_verdict(
                posting_id,
                *_KEY,
                Verdict(keep=True, reason="fits", rule=None),
                made_at=naive,
            )
        with pytest.raises(InvalidValueError):
            store.put_score(
                posting_id, *_SCORE_KEY, Score(score=5, reasons=("x",)), made_at=naive
            )


def test_skipped_items_are_kept_once_and_listed_while_pending(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.add_skipped(_A, [("one", "https://x.test/1", "no title")], seen_at=_T1)
        store.add_skipped(_A, [("one", None, "other reason")], seen_at=_T2)
        store.add_skipped(_B, [("two", None, "no url")], seen_at=_T2)
        rows = store.pending_skipped([_A])
        assert store.pending_skipped([]) == []

    assert [(r.item_id, r.text, r.url, r.reason) for r in rows] == [
        (skipped_item_id("one"), "one", "https://x.test/1", "no title")
    ]
    assert rows[0].status is SkippedStatus.PENDING
    assert rows[0].seen_at == _T1


def test_marked_skipped_items_leave_the_pending_list(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.add_skipped(_A, [("one", None, "r"), ("two", None, "r")], seen_at=_T1)
        store.mark_skipped(_A, skipped_item_id("one"), SkippedStatus.NOT_JOB)
        store.mark_skipped(
            _A, skipped_item_id("two"), SkippedStatus.JOB, external_id=ExternalId("e")
        )
        pending = store.pending_skipped([_A])
        again = store.add_skipped(
            _A, [("one", None, "r"), ("two", None, "r")], seen_at=_T2
        )

    assert pending == []
    assert again == [ExternalId("e")]


def test_marking_a_job_needs_an_external_id(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store, pytest.raises(InvalidValueError):
        store.mark_skipped(_A, skipped_item_id("one"), SkippedStatus.JOB)


def test_list_open_matches_open_postings_without_descriptions(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha"), (_B, "Beta")])
        store.record(
            [_draft(_A, "1", description="Long text", url="https://example.com/a")],
            seen_at=_T1,
        )
        store.record([_job(_B, "1")], seen_at=_T1)
        store.mark_fetched(_A, _T1)
        store.mark_fetched(_B, _T1)
        full = store.open_postings(now=_T1)
        listed = store.list_open(now=_T1)
        narrowed = store.list_open(now=_T1, sources=[_B])

    assert [posting.id for posting in listed] == [posting.id for posting in full]
    assert any(posting.description for posting in full)
    assert all(posting.description is None for posting in listed)
    assert [posting.title for posting in narrowed] == [f"Engineer {_B} 1"]


def test_list_open_rejects_a_time_with_no_zone(tmp_path: Path) -> None:
    with (
        Store.open(tmp_path / "ijob.db") as store,
        pytest.raises(InvalidValueError),
    ):
        store.list_open(now=_T1.replace(tzinfo=None))


def test_sources_by_posting_reads_every_sighting(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha"), (_B, "Beta")])
        store.record([_draft(_B, "1", url="https://example.com/same")], seen_at=_T1)
        store.record([_draft(_A, "1", url="https://example.com/same")], seen_at=_T1)
        [posting] = store.postings(sources=[_A, _B])
        shown = store.sources_by_posting()

    assert shown == {posting.id: [_A, _B]}


def test_saved_scores_keep_the_newest_for_one_profile(tmp_path: Path) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds([(_A, "Alpha")])
        store.record([_draft(_A, "1")], seen_at=_T1)
        [posting] = store.postings(sources=[_A])
        older = Score(score=10, reasons=("older",))
        newer = Score(score=90, reasons=("newer",))
        other = Score(score=50, reasons=("other",))
        store.put_score(posting.id, "a", "p", "m", "1", older, made_at=_T1)
        store.put_score(posting.id, "b", "p", "m", "1", newer, made_at=_T2)
        store.put_score(posting.id, "c", "q", "m", "1", other, made_at=_T2)

        assert store.saved_scores("p") == {posting.id: newer}
        assert store.saved_scores("q") == {posting.id: other}
        assert store.saved_scores("none") == {}


def test_two_connections_can_read_while_one_writes(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with Store.open(path) as writer, Store.open(path) as reader:
        writer.import_seeds([(_A, "Alpha")])
        assert _scalar(path, "PRAGMA journal_mode") == "wal"
        assert [row.ref for row in reader.sources()] == [_A]


def test_migration_8_upgrades_a_version_7_database(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        for statements in _MIGRATIONS[:7]:
            for statement in statements:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO postings (title, company, location, url, first_seen, "
            "last_seen) VALUES ('Engineer', 'Acme', 'Remote', "
            "'https://example.com/jobs/1', ?, ?)",
            (_T1_TEXT, _T1_TEXT),
        )
        conn.execute(
            "INSERT INTO sources (ref, company, origin) "
            "VALUES ('greenhouse:alpha', 'Alpha', 'seed')"
        )
        conn.execute("PRAGMA user_version = 7")
    assert _rows(path, "PRAGMA table_info(eval_sets)") == []

    with Store.open(path) as store:
        posting = store.posting(PostingId(1))
        [source] = store.sources()
        assert store.evals.runs() == []

    assert _scalar(path, "PRAGMA user_version") == 8
    assert posting is not None
    assert posting.title == "Engineer"
    assert source.company == "Alpha"
    tables = _scalar(
        path,
        "SELECT group_concat(name) FROM "
        "(SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name LIKE 'eval_%' ORDER BY name)",
    )
    assert tables == (
        "eval_cases,eval_labels,eval_results,eval_runs,eval_sets,eval_votes"
    )
