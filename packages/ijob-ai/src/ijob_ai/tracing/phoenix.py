"""Span mapping and the REST client for a local Phoenix.

`to_readable_span` turns a stored `SpanRow` into an OpenInference-shaped span that
keeps its trace and span ids, so sending it again is a duplicate Phoenix drops.
`PhoenixClient` sends spans through an OTLP exporter and makes the REST calls that
datasets, experiments, runs and scores need. This is the one module that holds
ijob's Phoenix mapping and HTTP.
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, cast

import requests
from ijob_core.traces import SpanRow
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from ijob_ai.errors import (
    PhoenixError,
    PhoenixHttpError,
    PhoenixReplyError,
    PhoenixUnreachableError,
    SpanBatchRefusedError,
)
from ijob_ai.tracing import attributes

SPAN_BATCH_SIZE: Final = 100
"""How many spans go in one OTLP request."""

_PROJECT: Final = "ijob"
_RESOURCE: Final = Resource(
    {"service.name": _PROJECT, "openinference.project.name": _PROJECT}
)
_KIND: Final = "openinference.span.kind"
_TOOL_PREFIX: Final = "tool "
_TIMEOUT_SECONDS: Final = 30.0
_ERROR_STATUS: Final = 400
_STATUSES: Final = {"ok": StatusCode.OK, "error": StatusCode.ERROR}
_EXPORTER_LOGGER: Final = "opentelemetry.exporter.otlp.proto.http"


def _context(trace_id: str, span_id: str) -> SpanContext:
    """Build a span context from stored hex ids.

    Raises:
        PhoenixError: If a stored id is not hexadecimal.
    """
    try:
        trace, span = int(trace_id, 16), int(span_id, 16)
    except ValueError as error:
        message = f"a stored span id is not hexadecimal: {trace_id}/{span_id}"
        raise PhoenixError(message) from error
    return SpanContext(
        trace_id=trace,
        span_id=span,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )


def _mapped(row: SpanRow) -> dict[str, Any]:
    """Map a row's attributes to the names Phoenix reads."""
    mapped = dict(row.attributes)
    prompt = mapped.pop(attributes.PROMPT, None)
    completion = mapped.pop(attributes.COMPLETION, None)
    if prompt is not None:
        mapped["input.value"] = prompt
    if completion is not None:
        mapped["output.value"] = completion
    if row.parent_id is None:
        mapped[_KIND] = "CHAIN"
    elif row.name.startswith(_TOOL_PREFIX):
        mapped[_KIND] = "TOOL"
        mapped["tool.name"] = mapped.get(
            attributes.TOOL_NAME, row.name[len(_TOOL_PREFIX) :]
        )
    else:
        mapped[_KIND] = "LLM"
        model = mapped.get(attributes.RESPONSE_MODEL) or mapped.get(
            attributes.REQUEST_MODEL
        )
        if model is not None:
            mapped["llm.model_name"] = model
        for source, target in (
            (attributes.INPUT_TOKENS, "llm.token_count.prompt"),
            (attributes.OUTPUT_TOKENS, "llm.token_count.completion"),
        ):
            if source in mapped:
                mapped[target] = mapped[source]
    return mapped


def to_readable_span(row: SpanRow) -> ReadableSpan:
    """Map a stored span to the span Phoenix shows.

    The trace and span ids are those of the row. `ijob.prompt` and
    `ijob.completion` become `input.value` and `output.value`. A span with no
    parent is a `CHAIN`, one named `tool …` a `TOOL` with `tool.name`, any other
    an `LLM` with its model name and token counts.

    Args:
        row: The stored span.

    Returns:
        A finished span in the `ijob` project.

    Raises:
        PhoenixError: If the row's trace, span or parent id is not hexadecimal.
    """
    return ReadableSpan(
        name=row.name,
        context=_context(row.trace_id, row.span_id),
        parent=(
            None if row.parent_id is None else _context(row.trace_id, row.parent_id)
        ),
        resource=_RESOURCE,
        attributes=_mapped(row),
        kind=SpanKind.INTERNAL,
        status=Status(_STATUSES.get(row.status, StatusCode.UNSET)),
        start_time=row.start_ns,
        end_time=row.end_ns,
    )


class _Response(Protocol):
    """The part of an HTTP reply the client reads."""

    @property
    def status_code(self) -> int:
        """The HTTP status."""
        ...

    @property
    def text(self) -> str:
        """The body as text."""
        ...

    def json(self) -> Any:  # noqa: ANN401  # the decoded JSON body
        """Decode the body."""
        ...


class HttpSession(Protocol):
    """The part of `requests.Session` the client calls."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: object = None,
        timeout: float | None = None,
    ) -> _Response:
        """Make one HTTP request."""
        ...


@dataclass(frozen=True, slots=True)
class PhoenixExample:
    """One example of a Phoenix dataset.

    Attributes:
        id: Phoenix's id for the example.
        input: The example's input.
    """

    id: str
    input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PhoenixRun:
    """One run of a Phoenix experiment.

    Attributes:
        id: Phoenix's id for the run.
        example_id: The dataset example the run answered.
    """

    id: str
    example_id: str


@contextmanager
def _exporter_quiet() -> Iterator[None]:
    """Keep the exporter's own log lines off the terminal while spans are sent.

    The exporter logs each failed attempt; the caller reports the failure once.
    """
    logger = logging.getLogger(_EXPORTER_LOGGER)
    level = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        logger.setLevel(level)


@contextmanager
def _expected_shape(path: str) -> Iterator[None]:
    """Turn a reply that lacks what the client reads into a `PhoenixReplyError`."""
    try:
        yield
    except (KeyError, TypeError, AttributeError) as error:
        raise PhoenixReplyError(path) from error


class PhoenixClient:
    """Sends spans and calls the REST routes of a Phoenix 21.0.0.

    No auth header is sent: the shipped Phoenix runs with auth off on localhost.
    """

    def __init__(
        self,
        endpoint: str,
        session: HttpSession,
        exporter: SpanExporter,
        *,
        close_session: Callable[[], None] = lambda: None,
    ) -> None:
        """Build a client.

        Args:
            endpoint: The Phoenix base URL, such as `http://127.0.0.1:6006`.
            session: Makes the REST calls.
            exporter: Sends spans to `<endpoint>/v1/traces`.
            close_session: Releases the session's connections; `close` calls it.
        """
        self._endpoint = endpoint.rstrip("/")
        self._session = session
        self._exporter = exporter
        self._close_session = close_session

    def close(self) -> None:
        """Shut the exporter down and release the session's connections.

        It is safe to call after a failed send.
        """
        self._exporter.shutdown()
        self._close_session()

    def check_reachable(self) -> None:
        """Ask Phoenix for its front page.

        Raises:
            PhoenixUnreachableError: If nothing answers.
            PhoenixHttpError: If Phoenix answers 400 or above.
        """
        self._call("GET", "/", json_reply=False)

    def send_spans(self, rows: Sequence[SpanRow]) -> None:
        """Send spans in batches of 100.

        Args:
            rows: The stored spans.

        Raises:
            SpanBatchRefusedError: If the exporter reports failure for a batch.
            PhoenixError: If a row's stored id is not hexadecimal.
        """
        with _exporter_quiet():
            for start in range(0, len(rows), SPAN_BATCH_SIZE):
                batch = [
                    to_readable_span(row)
                    for row in rows[start : start + SPAN_BATCH_SIZE]
                ]
                if self._exporter.export(batch) is not SpanExportResult.SUCCESS:
                    raise SpanBatchRefusedError(self._endpoint)

    def find_dataset(self, name: str) -> str | None:
        """Find a dataset by name.

        Args:
            name: The dataset name.

        Returns:
            Its id, or `None` if Phoenix holds none of that name.
        """
        path = "/v1/datasets"
        for item in self._pages(path):
            with _expected_shape(path):
                if item["name"] == name:
                    return str(item["id"])
        return None

    def upload_dataset(
        self, action: str, name: str, examples: Sequence[Mapping[str, Any]]
    ) -> str:
        """Create a dataset or append examples to it.

        Args:
            action: `create` or `append`.
            name: The dataset name.
            examples: Mappings with `input`, `output` and `metadata`.

        Returns:
            The dataset id.

        Raises:
            PhoenixHttpError: Including 409 when `create` names a dataset that exists.
        """
        path = "/v1/datasets/upload"
        reply = self._call(
            "POST",
            path,
            params={"sync": "true"},
            body={
                "action": action,
                "name": name,
                "inputs": [e["input"] for e in examples],
                "outputs": [e["output"] for e in examples],
                "metadata": [e["metadata"] for e in examples],
            },
        )
        with _expected_shape(path):
            return str(reply["data"]["dataset_id"])

    def dataset_examples(self, dataset_id: str) -> list[PhoenixExample]:
        """Read the examples of a dataset.

        Args:
            dataset_id: The dataset.

        Returns:
            Its examples.
        """
        path = f"/v1/datasets/{dataset_id}/examples"
        reply = self._call("GET", path)
        with _expected_shape(path):
            return [
                PhoenixExample(str(item["id"]), item["input"])
                for item in reply["data"]["examples"]
            ]

    def find_experiment(self, dataset_id: str, name: str) -> str | None:
        """Find an experiment of a dataset by name.

        Args:
            dataset_id: The dataset.
            name: The experiment name.

        Returns:
            The id of the first experiment of that name, or `None`.
        """
        path = f"/v1/datasets/{dataset_id}/experiments"
        for item in self._pages(path):
            with _expected_shape(path):
                if item["name"] == name:
                    return str(item["id"])
        return None

    def create_experiment(
        self, dataset_id: str, name: str, metadata: Mapping[str, Any]
    ) -> str:
        """Create an experiment on a dataset.

        Args:
            dataset_id: The dataset.
            name: The experiment name.
            metadata: Facts about the experiment.

        Returns:
            The experiment id.
        """
        path = f"/v1/datasets/{dataset_id}/experiments"
        reply = self._call(
            "POST", path, body={"name": name, "metadata": dict(metadata)}
        )
        with _expected_shape(path):
            return str(reply["data"]["id"])

    def experiment_runs(self, experiment_id: str) -> list[PhoenixRun]:
        """Read the runs an experiment already holds.

        Args:
            experiment_id: The experiment.

        Returns:
            Its runs.
        """
        path = f"/v1/experiments/{experiment_id}/runs"
        reply = self._call("GET", path)
        with _expected_shape(path):
            return [
                PhoenixRun(str(item["id"]), str(item["dataset_example_id"]))
                for item in reply["data"]
            ]

    def post_run(  # noqa: PLR0913, PLR0917  # the fields Phoenix asks for
        self,
        experiment_id: str,
        example_id: str,
        output: object,
        start: datetime,
        end: datetime,
        trace_id: str | None,
    ) -> str:
        """Post one run of an experiment.

        Args:
            experiment_id: The experiment.
            example_id: The dataset example answered.
            output: What the run produced.
            start: When the run began.
            end: When it ended.
            trace_id: The run's trace, or `None` when no span is linked.

        Returns:
            The run id.
        """
        body: dict[str, Any] = {
            "dataset_example_id": example_id,
            "output": output,
            "repetition_number": 1,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }
        if trace_id is not None:
            body["trace_id"] = trace_id
        path = f"/v1/experiments/{experiment_id}/runs"
        reply = self._call("POST", path, body=body)
        with _expected_shape(path):
            return str(reply["data"]["id"])

    def post_score(  # noqa: PLR0913, PLR0917  # the fields Phoenix asks for
        self,
        run_id: str,
        name: str,
        score: float,
        label: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Post or replace one evaluation of a run.

        Args:
            run_id: The experiment run.
            name: The evaluation name.
            score: The numeric result.
            label: The result as a word.
            start: When the evaluation began.
            end: When it ended.
        """
        self._call(
            "POST",
            "/v1/experiment_evaluations",
            body={
                "experiment_run_id": run_id,
                "name": name,
                "annotator_kind": "CODE",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "result": {"score": score, "label": label},
            },
        )

    def _pages(self, path: str) -> Iterator[Mapping[str, Any]]:
        """Yield the items of a listing, following its cursor."""
        cursor: str | None = None
        while True:
            params = None if cursor is None else {"cursor": cursor}
            reply = self._call("GET", path, params=params)
            with _expected_shape(path):
                items = list(reply["data"])
                cursor = reply.get("next_cursor")
            yield from items
            if not cursor:
                return

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: object = None,
        json_reply: bool = True,
    ) -> Any:  # noqa: ANN401  # the decoded JSON reply
        """Make one REST call and decode the reply.

        Args:
            method: The HTTP method.
            path: The route, below the endpoint.
            params: Query parameters.
            body: The JSON request body.
            json_reply: Whether the reply must be JSON. The front page is HTML,
                so the reachability check sets this to `False` and gets `{}`.

        Raises:
            PhoenixUnreachableError: If the request fails before a reply arrives.
            PhoenixHttpError: If the status is 400 or above.
            PhoenixReplyError: If a reply that must be JSON is not.
        """
        try:
            response = self._session.request(
                method,
                f"{self._endpoint}{path}",
                params=params,
                json=body,
                timeout=_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise PhoenixUnreachableError(self._endpoint) from error
        if response.status_code >= _ERROR_STATUS:
            raise PhoenixHttpError(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as error:
            if json_reply:
                raise PhoenixReplyError(path) from error
            return {}


def phoenix_client(endpoint: str) -> PhoenixClient:
    """Build a client that talks to a real Phoenix.

    Args:
        endpoint: The Phoenix base URL, such as `http://127.0.0.1:6006`.

    Returns:
        A client whose REST calls and span exports share one HTTP session, which
        ignores proxy and netrc settings of the environment so that the local
        endpoint is the only place the data goes. Call `close` when done.
    """
    base = endpoint.rstrip("/")
    session = requests.Session()
    session.trust_env = False
    exporter = OTLPSpanExporter(
        endpoint=f"{base}/v1/traces", session=session, timeout=_TIMEOUT_SECONDS
    )
    # `requests.Session.request` takes the arguments `HttpSession` names, and more.
    return PhoenixClient(
        base,
        cast("HttpSession", session),
        exporter,
        close_session=session.close,
    )
