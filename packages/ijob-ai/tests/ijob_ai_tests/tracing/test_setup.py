import base64
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import override

import pytest
import requests
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

_ENDPOINT = "http://localhost:3000"
_DISABLED_WARNING = (
    "warning: OTEL_SDK_DISABLED ignored; ijob tracing cannot be turned off"
)
_KEY_WARNING = (
    "warning: Langfuse export skipped; set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY"
)


class RecordingAdapter(requests.adapters.BaseAdapter):
    """A transport that records requests, or fails them, without any network."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.requests: list[requests.PreparedRequest] = []
        self._error = error

    @override
    def send(
        self,
        request: requests.PreparedRequest,
        *_args: object,
        **_kwargs: object,
    ) -> requests.Response:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        response = requests.Response()
        response.status_code = 200
        response.request = request
        return response

    @override
    def close(self) -> None:
        return None


def _session(adapter: RecordingAdapter) -> requests.Session:
    session = requests.Session()
    session.mount("http://", adapter)
    return session


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
    for name in (
        "OTEL_SDK_DISABLED",
        "OTEL_TRACES_SAMPLER",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
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


def test_langfuse_export_headers_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    adapter = RecordingAdapter()
    tracing = start_tracing(
        tmp_path / "traces.db",
        TracingSettings(langfuse_endpoint=_ENDPOINT + "/"),
        session=_session(adapter),
    )
    _one_call(tracing)
    tracing.finish()
    assert tracing.warnings == []
    [request] = adapter.requests
    token = base64.b64encode(b"pk-test:sk-test").decode()
    assert request.method == "POST"
    assert request.url == "http://localhost:3000/api/public/otel/v1/traces"
    assert request.headers["Authorization"] == f"Basic {token}"
    assert request.headers["x-langfuse-ingestion-version"] == "4"
    assert request.headers["Content-Type"] == "application/x-protobuf"


@pytest.mark.parametrize("missing", ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"])
def test_langfuse_missing_key_warns_and_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    keys = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test"}
    del keys[missing]
    for name, value in keys.items():
        monkeypatch.setenv(name, value)
    adapter = RecordingAdapter()
    path = tmp_path / "traces.db"
    tracing = start_tracing(
        path,
        TracingSettings(langfuse_endpoint=_ENDPOINT),
        session=_session(adapter),
    )
    _one_call(tracing)
    tracing.finish()
    assert adapter.requests == []
    assert tracing.warnings == [_KEY_WARNING]
    assert len(_summaries(path)) == 1


def test_no_endpoint_means_no_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    adapter = RecordingAdapter()
    tracing = start_tracing(
        tmp_path / "traces.db", TracingSettings(), session=_session(adapter)
    )
    _one_call(tracing)
    tracing.finish()
    assert adapter.requests == []
    assert tracing.warnings == []


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


@pytest.fixture
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the OTLP exporter's retry backoff so a failing export ends at once."""
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.common.http._OTLPHTTPClient._compute_backoff",
        lambda *_args: 0.001,
    )


@pytest.mark.usefixtures("_fast_retry")
def test_export_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    adapter = RecordingAdapter(error=requests.ConnectionError())
    path = tmp_path / "traces.db"
    tracing = start_tracing(
        path,
        TracingSettings(langfuse_endpoint=_ENDPOINT),
        session=_session(adapter),
    )
    _one_call(tracing)
    report = tracing.finish()
    assert report.dropped == 0
    assert adapter.requests
    assert len(_summaries(path)) == 1


@pytest.mark.usefixtures("_fast_retry")
def test_export_failure_prints_nothing_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    adapter = RecordingAdapter(error=requests.ConnectionError())
    with _no_root_handlers():
        tracing = start_tracing(
            tmp_path / "traces.db",
            TracingSettings(langfuse_endpoint=_ENDPOINT),
            session=_session(adapter),
        )
        _one_call(tracing)
        tracing.finish()
    assert len(adapter.requests) > 2
    assert capfd.readouterr().err == ""


def test_repeated_start_adds_no_log_handlers(tmp_path: Path) -> None:
    for _ in range(2):
        start_tracing(tmp_path / "traces.db", TracingSettings()).finish()
    handlers = logging.getLogger("opentelemetry.exporter.otlp").handlers
    null_handlers = [h for h in handlers if isinstance(h, logging.NullHandler)]
    assert len(null_handlers) == 1


def test_global_tracer_provider_is_untouched(tmp_path: Path) -> None:
    tracing = start_tracing(tmp_path / "traces.db", TracingSettings())
    _one_call(tracing)
    tracing.finish()
    assert isinstance(trace.get_tracer_provider(), ProxyTracerProvider)
