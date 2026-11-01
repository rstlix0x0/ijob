"""A span exporter that writes finished spans into the local trace store."""

import sqlite3
from collections.abc import Sequence
from typing import cast, override

from ijob_core.traces import SpanRow, TraceStore
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext


def _span_row(span: ReadableSpan) -> SpanRow:
    """Convert a finished span into the row the store keeps."""
    # The SDK types the context as optional, yet every span a tracer ends has one.
    context = cast(SpanContext, span.get_span_context())
    start_ns = span.start_time or 0
    return SpanRow(
        trace_id=f"{context.trace_id:032x}",
        span_id=f"{context.span_id:016x}",
        parent_id=None if span.parent is None else f"{span.parent.span_id:016x}",
        name=span.name,
        start_ns=start_ns,
        end_ns=span.end_time or start_ns,
        status=span.status.status_code.name.lower(),
        attributes=dict(span.attributes or {}),
    )


class SqliteSpanExporter(SpanExporter):
    """Writes each exported span as one row of a `TraceStore`.

    A failed write never raises or logs: the batch is counted in `dropped` and
    the export reports failure, so a full screen or a locked file cannot
    disturb the command being traced.

    Attributes:
        dropped: How many spans could not be stored so far.
    """

    def __init__(self, store: TraceStore) -> None:
        """Build an exporter on an open store.

        Args:
            store: Where spans are written. The caller keeps ownership and
                closes it.
        """
        self._store = store
        self.dropped = 0

    @override
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Store a batch of finished spans.

        Args:
            spans: The spans to store.

        Returns:
            `SUCCESS` once the batch is stored; `FAILURE` when SQLite refused
            it, in which case the whole batch is added to `dropped`.
        """
        try:
            self._store.insert([_span_row(span) for span in spans])
        except sqlite3.Error:
            # Isolation point: SimpleSpanProcessor logs any raised exception to
            # stderr, which would corrupt a terminal UI, so the loss is counted.
            self.dropped += len(spans)
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    @override
    def shutdown(self) -> None:
        """Do nothing; the store stays open for its owner to close."""

    @override
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Report that nothing is buffered, since every export writes at once.

        Args:
            timeout_millis: Ignored.

        Returns:
            Always `True`.
        """
        return True
