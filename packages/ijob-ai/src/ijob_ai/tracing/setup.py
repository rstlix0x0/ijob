"""Per-operation tracing: a private tracer provider and the operation's scope.

`start_tracing` builds a tracer provider for one operation and never installs
it as the process-wide provider, so later operations and tests can each build
their own. Every span goes to the local trace store; when a Langfuse endpoint
and both Langfuse keys are configured, spans are also exported there.

`operation_trace` marks the code of one operation. The root span of the trace
opens on the first model call, so an operation that calls no model writes no
trace.
"""

import base64
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import requests
from ijob_core.traces import TraceStore
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Span, Tracer, set_span_in_context

from ijob_ai.llm.settings import TracingSettings
from ijob_ai.tracing.sqlite_exporter import SqliteSpanExporter

_DISABLED_VARIABLE: Final = "OTEL_SDK_DISABLED"
_PUBLIC_KEY_VARIABLE: Final = "LANGFUSE_PUBLIC_KEY"
_SECRET_KEY_VARIABLE: Final = "LANGFUSE_SECRET_KEY"  # noqa: S105  # a variable name

_DISABLED_WARNING: Final = (
    f"warning: {_DISABLED_VARIABLE} ignored; ijob tracing cannot be turned off"
)
_MISSING_KEY_WARNING: Final = (
    "warning: Langfuse export skipped; "
    f"set {_PUBLIC_KEY_VARIABLE} and {_SECRET_KEY_VARIABLE}"
)

_EXPORT_LOGGERS: Final = (
    "opentelemetry.exporter.otlp",
    "opentelemetry.sdk._shared_internal",
    "opentelemetry.sdk.trace.export",
)

_LANGFUSE_TRACES_PATH: Final = "/api/public/otel/v1/traces"
_LANGFUSE_TIMEOUT_S: Final = 5
_FLUSH_TIMEOUT_MS: Final = 5000
_BYTES_PER_MB: Final = 2**20


@dataclass(frozen=True, slots=True)
class TracingReport:
    """What tracing did over one operation.

    Attributes:
        dropped: How many spans the local store could not keep.
        pruned: How many old traces were deleted to stay under the size cap.
    """

    dropped: int
    pruned: int


class Tracing:
    """The tracing of one operation: its provider, store and open root span.

    Build one with `start_tracing`, run the operation inside `operation_trace`,
    then call `finish` exactly once.

    Attributes:
        tracer: A tracer of the operation's own provider.
        warnings: Messages for the user about tracing set-up, ready to print.
    """

    def __init__(
        self,
        *,
        provider: TracerProvider,
        store: TraceStore,
        exporter: SqliteSpanExporter,
        max_size_mb: int,
        warnings: list[str],
    ) -> None:
        """Wrap the parts `start_tracing` built.

        Args:
            provider: The operation's tracer provider.
            store: The open trace store; `finish` closes it.
            exporter: The exporter writing into `store`, read for its drop count.
            max_size_mb: Size cap `finish` prunes the store to.
            warnings: Set-up warnings to expose.
        """
        self._provider = provider
        self._store = store
        self._exporter = exporter
        self._max_size_mb = max_size_mb
        self._root: Span | None = None
        self.tracer: Tracer = provider.get_tracer("ijob")
        self.warnings = warnings

    def root(self, name: str) -> Span:
        """Return the operation's root span, opening it on first use.

        Args:
            name: The operation's name; the root span is called `ijob <name>`.

        Returns:
            The open root span.
        """
        if self._root is None:
            self._root = self.tracer.start_span(f"ijob {name}")
        return self._root

    def finish(self) -> TracingReport:
        """End the operation's tracing and release its resources.

        Ends an open root span, flushes the exporters for up to five seconds,
        shuts the provider down, prunes the store to its size cap and closes
        it. A failed Langfuse export is swallowed by the exporter and never
        raises here.

        Returns:
            How many spans were dropped and how many traces were pruned.

        Raises:
            sqlite3.Error: If pruning the store fails.
        """
        if self._root is not None:
            self._root.end()
        self._provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
        self._provider.shutdown()
        try:
            pruned = self._store.prune(self._max_size_mb * _BYTES_PER_MB).removed
        finally:
            self._store.close()
        return TracingReport(dropped=self._exporter.dropped, pruned=pruned)


class OperationTrace:
    """The trace of one operation, handed to each model call to open its span."""

    def __init__(self, tracing: Tracing, name: str) -> None:
        """Bind the trace to an operation.

        Args:
            tracing: The operation's tracing.
            name: The operation's name.
        """
        self._tracing = tracing
        self._name = name

    @contextmanager
    def child(self, name: str) -> Iterator[Span]:
        """Open a span for one model call under the operation's root span.

        The span's parent is always the root span, whatever span is current.
        The first call opens the root span, `ijob <operation name>`.

        Args:
            name: The child span's name.

        Yields:
            The open child span, current while the block runs.
        """
        root = self._tracing.root(self._name)
        with self._tracing.tracer.start_as_current_span(
            name, context=set_span_in_context(root)
        ) as span:
            yield span


_CURRENT: ContextVar[OperationTrace | None] = ContextVar(
    "ijob_operation_trace", default=None
)


def current_operation_trace() -> OperationTrace | None:
    """Return the operation trace the running code belongs to, if any."""
    return _CURRENT.get()


@contextmanager
def operation_trace(tracing: Tracing, name: str) -> Iterator[OperationTrace]:
    """Mark the code of one operation, so its model calls are traced.

    Tasks started inside the block inherit the trace.

    Args:
        tracing: The operation's tracing.
        name: The operation's name, used in the root span's name.

    Yields:
        The operation's trace.
    """
    trace = OperationTrace(tracing, name)
    token = _CURRENT.set(trace)
    try:
        yield trace
    finally:
        _CURRENT.reset(token)


def start_tracing(
    traces_path: Path,
    settings: TracingSettings,
    *,
    session: requests.Session | None = None,
) -> Tracing:
    """Build the tracing of one operation.

    Removes `OTEL_SDK_DISABLED` from the process environment, since ijob's
    tracing is always on, and records a warning when it was set. Also keeps the
    OpenTelemetry exporter and span-processor loggers off stderr, where their
    retry and failure lines would corrupt a terminal UI.

    Args:
        traces_path: The trace database file.
        settings: Size cap and optional Langfuse endpoint.
        session: HTTP session for the Langfuse export; the exporter makes its
            own when omitted.

    Returns:
        The operation's tracing. Call `Tracing.finish` once when done.

    Raises:
        sqlite3.Error: If the trace database cannot be opened.
    """
    _silence_export_logs()
    warnings: list[str] = []
    if os.environ.pop(_DISABLED_VARIABLE, None) is not None:
        warnings.append(_DISABLED_WARNING)
    store = TraceStore.open(traces_path)
    exporter = SqliteSpanExporter(store)
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    if settings.langfuse_endpoint is not None:
        public_key = os.environ.get(_PUBLIC_KEY_VARIABLE)
        secret_key = os.environ.get(_SECRET_KEY_VARIABLE)
        if public_key and secret_key:
            provider.add_span_processor(
                BatchSpanProcessor(
                    _langfuse_exporter(
                        settings.langfuse_endpoint, public_key, secret_key, session
                    )
                )
            )
        else:
            warnings.append(_MISSING_KEY_WARNING)
    return Tracing(
        provider=provider,
        store=store,
        exporter=exporter,
        max_size_mb=settings.max_size_mb,
        warnings=warnings,
    )


def _silence_export_logs() -> None:
    """Stop the OpenTelemetry export loggers from reaching stderr, idempotently.

    Each logger gets one `NullHandler`, so Python's last-resort stderr handler
    never fires for its records. Records still propagate, so logging the
    application configures keeps receiving them. Other loggers, the root logger
    included, are left as they are.
    """
    for name in _EXPORT_LOGGERS:
        logger = logging.getLogger(name)
        if not any(
            isinstance(handler, logging.NullHandler) for handler in logger.handlers
        ):
            logger.addHandler(logging.NullHandler())


def _langfuse_exporter(
    endpoint: str, public_key: str, secret_key: str, session: requests.Session | None
) -> OTLPSpanExporter:
    """Build the OTLP exporter that sends spans to a Langfuse endpoint."""
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=endpoint.rstrip("/") + _LANGFUSE_TRACES_PATH,
        headers={
            "Authorization": f"Basic {token}",
            "x-langfuse-ingestion-version": "4",
        },
        timeout=_LANGFUSE_TIMEOUT_S,
        session=session,
    )
