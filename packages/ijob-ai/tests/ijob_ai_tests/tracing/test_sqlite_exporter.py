import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest
from ijob_ai.tracing.sqlite_exporter import SqliteSpanExporter
from ijob_core.traces import SpanRow, TraceStore
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode


def _finished_span() -> ReadableSpan:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with provider.get_tracer("test").start_as_current_span("only"):
        pass
    return memory.get_finished_spans()[0]


def test_export_writes_tree(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        exporter = SqliteSpanExporter(store)
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("root") as root:
            with tracer.start_as_current_span("child") as child:
                child.set_attribute("gen_ai.request.model", "m")
                child.set_attribute("tags", ["a", "b"])
                child.set_status(StatusCode.ERROR)
            trace_id = f"{root.get_span_context().trace_id:032x}"
            root_id = f"{root.get_span_context().span_id:016x}"
            child_id = f"{child.get_span_context().span_id:016x}"
        tree = store.tree(trace_id)
    assert exporter.dropped == 0
    assert len(tree.roots) == 1
    parent = tree.roots[0]
    assert parent.span.name == "root"
    assert parent.span.span_id == root_id
    assert parent.span.parent_id is None
    assert parent.span.status == "unset"
    assert [node.span.name for node in parent.children] == ["child"]
    stored = parent.children[0].span
    assert stored.span_id == child_id
    assert stored.parent_id == root_id
    assert stored.status == "error"
    assert stored.attributes == {"gen_ai.request.model": "m", "tags": ["a", "b"]}
    assert 0 < stored.start_ns <= stored.end_ns


def test_export_sqlite_error_counts_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_insert(_rows: Iterable[SpanRow]) -> None:
        raise sqlite3.OperationalError

    span = _finished_span()
    with TraceStore.open(tmp_path / "traces.db") as store:
        monkeypatch.setattr(store, "insert", failing_insert)
        exporter = SqliteSpanExporter(store)
        result = exporter.export([span])
    assert result is SpanExportResult.FAILURE
    assert exporter.dropped == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_export_success_leaves_dropped_zero(tmp_path: Path) -> None:
    span = _finished_span()
    with TraceStore.open(tmp_path / "traces.db") as store:
        exporter = SqliteSpanExporter(store)
        result = exporter.export([span])
    assert result is SpanExportResult.SUCCESS
    assert exporter.dropped == 0


def test_shutdown_and_force_flush(tmp_path: Path) -> None:
    with TraceStore.open(tmp_path / "traces.db") as store:
        exporter = SqliteSpanExporter(store)
        assert exporter.force_flush() is True
        exporter.shutdown()
