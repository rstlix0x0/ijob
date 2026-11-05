import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from ijob_ai.llm.settings import TracingSettings
from ijob_ai.tracing.setup import (
    Tracing,
    current_operation_trace,
    operation_trace,
    start_tracing,
)
from ijob_core.traces import SpanRow, TraceStore, TraceSummary
from opentelemetry import trace
from opentelemetry.trace import ProxyTracerProvider

_DISABLED_WARNING = (
    "warning: OTEL_SDK_DISABLED ignored; ijob tracing cannot be turned off"
)


def _one_call(tracing: Tracing, name: str = "search") -> None:
    with operation_trace(tracing, name) as operation, operation.child("chat m"):
        pass


def _summaries(path: Path) -> list[TraceSummary]:
    with TraceStore.open(path) as store:
        return store.summaries(10)


@contextmanager
def _no_root_handlers() -> Iterator[None]:
    """Hide the root logger's handlers, as in a host app that never set logging up.

    Python then prints unhandled warnings and errors to stderr, which is what
    pytest's own log capture would otherwise hide.
    """
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers[:] = []
    try:
        yield
    finally:
        root.handlers[:] = saved


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OTEL_SDK_DISABLED", "OTEL_TRACES_SAMPLER"):
        monkeypatch.delenv(name, raising=False)


def test_disabled_env_is_removed_and_spans_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    path = tmp_path / "traces.db"
    tracing = start_tracing(path, TracingSettings())
    _one_call(tracing)
    report = tracing.finish()
    assert "OTEL_SDK_DISABLED" not in os.environ
    assert tracing.warnings == [_DISABLED_WARNING]
    assert report.dropped == 0
    with TraceStore.open(path) as store:
        [summary] = store.summaries(10)
        tree = store.tree(summary.trace_id)
    assert summary.operation == "ijob search"
    assert [node.span.name for node in tree.roots[0].children] == ["chat m"]


def test_no_warning_when_nothing_to_warn_about(tmp_path: Path) -> None:
    tracing = start_tracing(tmp_path / "traces.db", TracingSettings())
    assert tracing.warnings == []
    assert tracing.finish().pruned == 0


def test_every_child_span_sits_directly_under_operation_root(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    tracing = start_tracing(path, TracingSettings())
    with operation_trace(tracing, "search") as operation:
        assert current_operation_trace() is operation
        with operation.child("chat a"), operation.child("chat b"):
            pass
        with operation.child("chat c"):
            pass
    assert current_operation_trace() is None
    tracing.finish()
    with TraceStore.open(path) as store:
        [summary] = store.summaries(10)
        [root] = store.tree(summary.trace_id).roots
    assert root.span.name == "ijob search"
    assert [node.span.name for node in root.children] == ["chat a", "chat b", "chat c"]
    assert all(node.children == () for node in root.children)


def test_two_operations_in_one_process_both_traced(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    for name in ("search", "judge"):
        tracing = start_tracing(path, TracingSettings())
        _one_call(tracing, name)
        tracing.finish()
    summaries = _summaries(path)
    assert sorted(summary.operation for summary in summaries) == [
        "ijob judge",
        "ijob search",
    ]
    assert len({summary.trace_id for summary in summaries}) == 2


def test_operation_without_calls_writes_no_trace(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    tracing = start_tracing(path, TracingSettings())
    with operation_trace(tracing, "search"):
        pass
    report = tracing.finish()
    assert report.dropped == 0
    assert _summaries(path) == []


def test_spans_go_only_to_the_local_store(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    tracing = start_tracing(path, TracingSettings())
    processors = tracing._provider._active_span_processor._span_processors
    assert len(processors) == 1
    _one_call(tracing)
    tracing.finish()
    assert [summary.operation for summary in _summaries(path)] == ["ijob search"]


def test_finish_prunes_and_reports(tmp_path: Path) -> None:
    path = tmp_path / "traces.db"
    blob = "x" * 1_000_000
    with TraceStore.open(path) as store:
        store.insert(
            SpanRow(
                trace_id=f"old{index:029d}",
                span_id="0000000000000001",
                parent_id=None,
                name="ijob old",
                start_ns=1_000 + index,
                end_ns=2_000 + index,
                status="ok",
                attributes={"ijob.prompt": blob},
            )
            for index in range(20)
        )
    tracing = start_tracing(path, TracingSettings(max_size_mb=16))
    _one_call(tracing)
    report = tracing.finish()
    assert report.pruned > 0
    assert report.dropped == 0
    assert path.stat().st_size <= 16 * 2**20
    operations = [summary.operation for summary in _summaries(path)]
    assert "ijob search" in operations


def test_repeated_start_adds_no_log_handlers(tmp_path: Path) -> None:
    for _ in range(2):
        start_tracing(tmp_path / "traces.db", TracingSettings()).finish()
    handlers = logging.getLogger("opentelemetry.exporter.otlp").handlers
    null_handlers = [h for h in handlers if isinstance(h, logging.NullHandler)]
    assert len(null_handlers) == 1


@pytest.mark.parametrize(
    "logger_name",
    [
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.sdk._shared_internal",
        "opentelemetry.sdk.trace.export",
    ],
)
def test_export_failures_print_nothing_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], logger_name: str
) -> None:
    start_tracing(tmp_path / "traces.db", TracingSettings()).finish()
    with _no_root_handlers():
        logging.getLogger(logger_name).error("Failed to export batch")
        logging.getLogger(logger_name).warning("Exception while exporting Span")

    assert capsys.readouterr().err == ""


def test_the_stderr_check_sees_an_unsilenced_logger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _no_root_handlers():
        logging.getLogger("ijob.test.unsilenced").error("Failed to export batch")

    assert "Failed to export batch" in capsys.readouterr().err


def test_global_tracer_provider_is_untouched(tmp_path: Path) -> None:
    tracing = start_tracing(tmp_path / "traces.db", TracingSettings())
    _one_call(tracing)
    tracing.finish()
    assert isinstance(trace.get_tracer_provider(), ProxyTracerProvider)
