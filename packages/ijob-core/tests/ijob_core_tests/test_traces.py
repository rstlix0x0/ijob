import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.traces import (
    PruneReport,
    SpanRow,
    TraceStore,
    TraceSummary,
    UnknownTraceError,
)

_SECOND = 1_000_000_000
_EPOCH_2026 = int(datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp()) * _SECOND


def _span(  # noqa: PLR0913  # keyword defaults keep call sites short
    span_id: str,
    *,
    trace_id: str = "t1",
    parent_id: str | None = None,
    name: str = "span",
    start_ns: int = _EPOCH_2026,
    end_ns: int | None = None,
    attributes: dict[str, object] | None = None,
) -> SpanRow:
    return SpanRow(
        trace_id=trace_id,
        span_id=span_id,
        parent_id=parent_id,
        name=name,
        start_ns=start_ns,
        end_ns=start_ns + _SECOND if end_ns is None else end_ns,
        status="ok",
        attributes={} if attributes is None else attributes,
    )


def _scalar(path: Path, sql: str) -> object:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchone()[0]


def test_open_creates_and_migrates(tmp_path: Path) -> None:
    path = tmp_path / "state" / "traces.db"
    with TraceStore.open(path) as store:
        busy = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert path.is_file()
    assert _scalar(path, "PRAGMA user_version") == 1
    assert busy == 5000
    indexed = _scalar(
        path,
        "SELECT group_concat(name) FROM pragma_index_list('spans') WHERE origin = 'c'",
    )
    assert indexed == "spans_start_ns"


def test_open_existing_file_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    with TraceStore.open(path) as store:
        store.insert([_span("a")])
    with TraceStore.open(path) as store:
        tree = store.tree("t1")
    assert [node.span.span_id for node in tree.roots] == ["a"]
    assert _scalar(path, "PRAGMA user_version") == 1


def test_insert_child_before_parent_reads_as_tree(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert([_span("child", parent_id="root", start_ns=_EPOCH_2026 + 5)])
        store.insert(
            [_span("root", name="agent", attributes={"gen_ai.request.model": "m"})]
        )
        tree = store.tree("t1")
    assert tree.trace_id == "t1"
    assert [node.span.span_id for node in tree.roots] == ["root"]
    root = tree.roots[0]
    assert root.span.name == "agent"
    assert root.span.attributes == {"gen_ai.request.model": "m"}
    assert [node.span.span_id for node in root.children] == ["child"]
    assert root.children[0].children == ()


def test_tree_orders_siblings_by_start_time(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert(
            [
                _span("late", parent_id="root", start_ns=_EPOCH_2026 + 30),
                _span("root"),
                _span("early", parent_id="root", start_ns=_EPOCH_2026 + 10),
                _span("grand", parent_id="early", start_ns=_EPOCH_2026 + 11),
            ]
        )
        tree = store.tree("t1")
    root = tree.roots[0]
    assert [node.span.span_id for node in root.children] == ["early", "late"]
    assert [node.span.span_id for node in root.children[0].children] == ["grand"]


def test_tree_of_rootless_trace_lists_orphans_as_roots(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert([_span("orphan", parent_id="gone")])
        tree = store.tree("t1")
    assert [node.span.span_id for node in tree.roots] == ["orphan"]


def test_tree_keeps_traces_apart(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert([_span("a", trace_id="t1"), _span("a", trace_id="t2")])
        tree = store.tree("t2")
    assert tree.trace_id == "t2"
    assert len(tree.roots) == 1


def test_insert_is_idempotent(tmp_path: Path) -> None:
    rows = [_span("root"), _span("child", parent_id="root")]
    path = tmp_path / "traces.db"
    with TraceStore.open(path) as store:
        store.insert(rows)
        store.insert(rows)
    assert _scalar(path, "SELECT count(*) FROM spans") == 2


def test_insert_round_trips_every_field(tmp_path: Path) -> None:
    row = SpanRow(
        trace_id="abc",
        span_id="def",
        parent_id=None,
        name="op",
        start_ns=_EPOCH_2026,
        end_ns=_EPOCH_2026 + 7,
        status="error",
        attributes={"a": 1, "b": [1, 2], "c": {"d": None}},
    )
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert([row])
        tree = store.tree("abc")
    assert tree.roots[0].span == row


def test_summaries_newest_first_with_calls_and_cost(tmp_path: Path) -> None:
    older = _EPOCH_2026
    newer = _EPOCH_2026 + 100 * _SECOND
    llm = {"gen_ai.request.model": "m", "gen_ai.usage.cost": 0.25}
    cheap = {"gen_ai.request.model": "m", "gen_ai.usage.cost": 0.5}
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert(
            [
                _span(
                    "r1",
                    trace_id="old",
                    name="rank",
                    start_ns=older,
                    end_ns=older + 3 * _SECOND,
                ),
                _span("c1", trace_id="old", parent_id="r1", attributes=llm),
                _span(
                    "r2",
                    trace_id="new",
                    name="tailor",
                    start_ns=newer,
                    end_ns=newer + 2_500_000_000,
                ),
                _span("c2", trace_id="new", parent_id="r2", attributes=llm),
                _span("c3", trace_id="new", parent_id="r2", attributes=cheap),
                _span("tool", trace_id="new", parent_id="r2"),
            ]
        )
        summaries = store.summaries(10)
        limited = store.summaries(1)
    assert summaries == [
        TraceSummary(
            trace_id="new",
            started=datetime(2026, 11, 1, 12, 1, 40, tzinfo=UTC),
            operation="tailor",
            calls=2,
            cost_usd=0.75,
            duration_ms=2500.0,
        ),
        TraceSummary(
            trace_id="old",
            started=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            operation="rank",
            calls=1,
            cost_usd=0.25,
            duration_ms=3000.0,
        ),
    ]
    assert [summary.trace_id for summary in limited] == ["new"]


def test_summaries_of_empty_store_is_empty(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        assert store.summaries(5) == []


def test_summaries_rejects_negative_limit(tmp_path: Path) -> None:
    with (
        TraceStore.open(tmp_path / "traces.db") as store,
        pytest.raises(InvalidValueError),
    ):
        store.summaries(-1)


def test_summary_without_model_calls_has_zero_cost(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert([_span("root", name="fetch")])
        (summary,) = store.summaries(5)
    assert (summary.calls, summary.cost_usd) == (0, 0.0)


def test_tree_unknown_id_raises(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert([_span("root")])
        with pytest.raises(UnknownTraceError) as raised:
            store.tree("nope")
    assert isinstance(raised.value, InvalidValueError)
    assert raised.value.field == "trace_id"


def test_summary_of_trace_without_root(tmp_path: Path) -> None:
    start = _EPOCH_2026 + 4 * _SECOND
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert(
            [
                _span(
                    "late",
                    parent_id="gone",
                    start_ns=start + _SECOND,
                    end_ns=start + 6 * _SECOND,
                    attributes={"gen_ai.request.model": "m", "gen_ai.usage.cost": 0.1},
                ),
                _span(
                    "early",
                    parent_id="gone",
                    start_ns=start,
                    end_ns=start + 2 * _SECOND,
                ),
            ]
        )
        (summary,) = store.summaries(5)
    assert summary == TraceSummary(
        trace_id="t1",
        started=datetime(2026, 11, 1, 12, 0, 4, tzinfo=UTC),
        operation="(incomplete)",
        calls=1,
        cost_usd=0.1,
        duration_ms=6000.0,
    )


def test_close_closes_the_connection(tmp_path: Path) -> None:
    store = TraceStore.open(tmp_path / "traces.db")
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.summaries(1)


_BULK = "x" * 4000


def _bulky_trace(trace_id: str, start_ns: int) -> list[SpanRow]:
    """A three-span trace whose attributes fill several database pages."""
    return [
        _span("root", trace_id=trace_id, start_ns=start_ns),
        _span(
            "a",
            trace_id=trace_id,
            parent_id="root",
            start_ns=start_ns + 1,
            attributes={"blob": _BULK},
        ),
        _span(
            "b",
            trace_id=trace_id,
            parent_id="root",
            start_ns=start_ns + 2,
            attributes={"blob": _BULK},
        ),
    ]


def _fill(store: TraceStore, count: int) -> None:
    for index in range(count):
        store.insert(_bulky_trace(f"t{index:03d}", _EPOCH_2026 + index * _SECOND))


def test_prune_under_cap_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    with TraceStore.open(path) as store:
        _fill(store, 5)
        size = path.stat().st_size
        report = store.prune(size * 2)
        remaining = len(store.summaries(100))
    assert report == PruneReport(removed=0, size_bytes=size)
    assert remaining == 5
    assert path.stat().st_size == size


def test_prune_removes_oldest_whole_traces(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    with TraceStore.open(path) as store:
        _fill(store, 30)
        cap = path.stat().st_size // 2
        report = store.prune(cap)
        survivors = {summary.trace_id for summary in store.summaries(100)}
    assert 0 < report.removed < 30
    assert report.size_bytes == path.stat().st_size <= cap
    assert "t000" not in survivors
    assert "t029" in survivors
    assert survivors == {f"t{index:03d}" for index in range(report.removed, 30)}
    assert _scalar(path, "SELECT COUNT(*) FROM spans") == 3 * len(survivors)
    assert (
        _scalar(
            path,
            "SELECT COUNT(*) FROM (SELECT trace_id FROM spans GROUP BY trace_id "
            "HAVING COUNT(*) <> 3)",
        )
        == 0
    )


def test_prune_reports_removed_count(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    with TraceStore.open(path) as store:
        _fill(store, 12)
        report = store.prune(path.stat().st_size // 3)
        remaining = len(store.summaries(100))
    assert report.removed > 0
    assert report.removed == 12 - remaining


def test_prune_removes_rootless_traces(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    orphan = [
        _span(
            "child",
            trace_id="orphan",
            parent_id="lost-root",
            start_ns=_EPOCH_2026 - 100 * _SECOND,
            attributes={"blob": _BULK * 3},
        ),
        _span(
            "late-child",
            trace_id="orphan",
            parent_id="lost-root",
            start_ns=_EPOCH_2026 + 500 * _SECOND,
        ),
    ]
    with TraceStore.open(path) as store:
        store.insert(orphan)
        _fill(store, 5)
        report = store.prune(path.stat().st_size - 1)
        survivors = {summary.trace_id for summary in store.summaries(100)}
    assert report.removed == 1
    assert survivors == {f"t{index:03d}" for index in range(5)}
    assert _scalar(path, "SELECT COUNT(*) FROM spans WHERE trace_id = 'orphan'") == 0


def test_span_batches_read_all_rows_in_order(tmp_path: Path) -> None:
    rows = [
        _span(f"s{n}", trace_id=f"t{n % 3}", start_ns=_EPOCH_2026 + (7 - n) * _SECOND)
        for n in range(7)
    ]
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert(rows)
        batches = list(store.span_batches(3))
    assert [len(batch) for batch in batches] == [3, 3, 1]
    flat = [row for batch in batches for row in batch]
    assert sorted(r.span_id for r in flat) == sorted(r.span_id for r in rows)
    assert [r.start_ns for r in flat] == sorted(r.start_ns for r in rows)


def test_span_batches_keep_rows_that_share_a_start_across_a_boundary(
    tmp_path: Path,
) -> None:
    rows = [_span(f"s{n}", trace_id="t1", start_ns=_EPOCH_2026) for n in range(5)]
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert(rows)
        batches = list(store.span_batches(2))
    assert [len(batch) for batch in batches] == [2, 2, 1]
    flat = [row.span_id for batch in batches for row in batch]
    assert flat == sorted(r.span_id for r in rows)


def test_span_batches_of_an_empty_store_is_empty(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        assert list(store.span_batches(5)) == []


def test_span_batches_reject_a_size_below_one(tmp_path: Path) -> None:
    with (
        TraceStore.open(tmp_path / "traces.db") as store,
        pytest.raises(InvalidValueError),
    ):
        list(store.span_batches(0))


def test_spans_between_read_a_window(tmp_path: Path) -> None:
    rows = [_span(f"s{n}", start_ns=_EPOCH_2026 + n * _SECOND) for n in range(6)]
    with TraceStore.open(tmp_path / "traces.db") as store:
        store.insert(rows)
        found = store.spans_between(
            _EPOCH_2026 + 2 * _SECOND, _EPOCH_2026 + 4 * _SECOND
        )
    assert [row.span_id for row in found] == ["s2", "s3", "s4"]
