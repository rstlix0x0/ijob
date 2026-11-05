import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast, override

import pytest
import requests
from ijob_ai.errors import (
    LlmError,
    PhoenixError,
    PhoenixHttpError,
    PhoenixReplyError,
    PhoenixUnreachableError,
    SpanBatchRefusedError,
)
from ijob_ai.tracing.phoenix import (
    HttpSession,
    PhoenixClient,
    phoenix_client,
    to_readable_span,
)
from ijob_core.traces import SpanRow
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

_MOMENT = datetime(2026, 11, 1, tzinfo=UTC)
_URL = "http://127.0.0.1:6006"
_TRACE = "0123456789abcdef0123456789abcdef"
_SPAN = "0123456789abcdef"
_PARENT = "fedcba9876543210"


def _row(
    *,
    status: str = "ok",
    name: str = "chat",
    parent_id: str | None = _PARENT,
    attributes: Mapping[str, object] | None = None,
    span_id: str = _SPAN,
) -> SpanRow:
    return SpanRow(
        trace_id=_TRACE,
        span_id=span_id,
        parent_id=parent_id,
        name=name,
        start_ns=1_000,
        end_ns=2_000,
        status=status,
        attributes={} if attributes is None else attributes,
    )


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def test_prompt_and_completion_become_input_and_output() -> None:
    span = to_readable_span(
        _row(attributes={"ijob.prompt": "p", "ijob.completion": "c", "ijob.task": "j"})
    )
    attrs = _attrs(span)
    assert attrs["input.value"] == "p"
    assert attrs["output.value"] == "c"
    assert "ijob.prompt" not in attrs
    assert "ijob.completion" not in attrs
    assert attrs["ijob.task"] == "j"


def test_llm_span_kind_and_tokens() -> None:
    span = to_readable_span(
        _row(
            attributes={
                "gen_ai.request.model": "asked",
                "gen_ai.response.model": "answered",
                "gen_ai.usage.input_tokens": 11,
                "gen_ai.usage.output_tokens": 7,
            }
        )
    )
    attrs = _attrs(span)
    assert attrs["openinference.span.kind"] == "LLM"
    assert attrs["llm.model_name"] == "answered"
    assert attrs["llm.token_count.prompt"] == 11
    assert attrs["llm.token_count.completion"] == 7


def test_llm_model_falls_back_to_the_request_model() -> None:
    span = to_readable_span(_row(attributes={"gen_ai.request.model": "asked"}))
    assert _attrs(span)["llm.model_name"] == "asked"


def test_tool_span_kind_and_name() -> None:
    span = to_readable_span(
        _row(name="tool search", attributes={"ijob.tool.name": "search"})
    )
    attrs = _attrs(span)
    assert attrs["openinference.span.kind"] == "TOOL"
    assert attrs["tool.name"] == "search"


def test_tool_name_falls_back_to_the_span_name() -> None:
    span = to_readable_span(_row(name="tool fetch"))
    assert _attrs(span)["tool.name"] == "fetch"


def test_root_span_is_a_chain() -> None:
    span = to_readable_span(_row(name="ijob tune-judge", parent_id=None))
    assert _attrs(span)["openinference.span.kind"] == "CHAIN"
    assert span.parent is None


def test_stored_ids_are_kept() -> None:
    span = to_readable_span(_row())
    context = span.get_span_context()
    assert context is not None
    assert f"{context.trace_id:032x}" == _TRACE
    assert f"{context.span_id:016x}" == _SPAN
    assert span.parent is not None
    assert f"{span.parent.span_id:016x}" == _PARENT
    assert (span.start_time, span.end_time) == (1_000, 2_000)


def test_resource_names_the_project() -> None:
    resource = to_readable_span(_row()).resource
    assert resource.attributes["service.name"] == "ijob"
    assert resource.attributes["openinference.project.name"] == "ijob"


class _Exporter(SpanExporter):
    def __init__(self, result: SpanExportResult = SpanExportResult.SUCCESS) -> None:
        self.batches: list[Sequence[ReadableSpan]] = []
        self.result = result

    @override
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.batches.append(spans)
        return self.result


class _Response:
    def __init__(self, status: int = 200, body: object = None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text

    def json(self) -> object:
        return self._body


class _Session:
    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []
        self.kwargs: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url))
        self.kwargs.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client(
    session: HttpSession | None = None, exporter: _Exporter | None = None
) -> PhoenixClient:
    return PhoenixClient(
        _URL,
        _Session(_Response(200, {})) if session is None else session,
        _Exporter() if exporter is None else exporter,
    )


def test_spans_go_in_batches_of_100() -> None:
    exporter = _Exporter()
    rows = [_row(span_id=f"{n:016x}") for n in range(250)]
    _client(exporter=exporter).send_spans(rows)
    assert [len(batch) for batch in exporter.batches] == [100, 100, 50]


def test_batch_refused_raises() -> None:
    client = _client(exporter=_Exporter(SpanExportResult.FAILURE))
    with pytest.raises(SpanBatchRefusedError) as raised:
        client.send_spans([_row()])
    assert raised.value.url == _URL


def test_unreachable_raises() -> None:
    session = _Session(requests.ConnectionError("refused"))
    with pytest.raises(PhoenixUnreachableError) as raised:
        _client(session).check_reachable()
    assert session.calls == [("GET", f"{_URL}/")]
    assert raised.value.url == _URL


def test_a_timeout_is_unreachable_too() -> None:
    with pytest.raises(PhoenixUnreachableError):
        _client(_Session(requests.Timeout("slow"))).check_reachable()


def test_phoenix_errors_exit_through_llm_failures() -> None:
    for error in (
        PhoenixUnreachableError(_URL),
        PhoenixHttpError(500, "x"),
        SpanBatchRefusedError(_URL),
    ):
        assert isinstance(error, LlmError)


def test_http_error_holds_status_and_200_characters() -> None:
    session = _Session(_Response(500, None, "y" * 500))
    with pytest.raises(PhoenixHttpError) as raised:
        _client(session).find_dataset("d")
    assert raised.value.status == 500
    assert raised.value.body == "y" * 200


def test_a_409_raises_like_any_other() -> None:
    session = _Session(_Response(409, None, "exists"))
    with pytest.raises(PhoenixHttpError) as raised:
        _client(session).upload_dataset("create", "d", [])
    assert raised.value.status == 409


class _Paged:
    """Answers a listing by cursor, as Phoenix does."""

    def __init__(self, pages: Mapping[str | None, _Response]) -> None:
        self.pages = pages
        self.cursors: list[object] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        **_k: object,
    ) -> _Response:
        del method, url
        cursor = None if params is None else params.get("cursor")
        self.cursors.append(cursor)
        return self.pages[cursor]


_TWO_PAGES = {
    None: _Response(200, {"data": [{"name": "a", "id": "1"}], "next_cursor": "c"}),
    "c": _Response(200, {"data": [{"name": "b", "id": "2"}], "next_cursor": None}),
}


def test_find_dataset_follows_the_cursor() -> None:
    paged = _Paged(_TWO_PAGES)
    assert _client(paged).find_dataset("b") == "2"
    assert paged.cursors == [None, "c"]


def test_find_experiment_follows_the_cursor() -> None:
    paged = _Paged(_TWO_PAGES)
    assert _client(paged).find_experiment("d", "b") == "2"
    assert paged.cursors == [None, "c"]
    assert _client(_Paged(_TWO_PAGES)).find_experiment("d", "zzz") is None


def test_upload_waits_for_the_dataset_with_sync() -> None:
    session = _Session(_Response(200, {"data": {"dataset_id": "d1"}}))
    assert _client(session).upload_dataset("create", "n", []) == "d1"
    assert session.kwargs[0]["params"] == {"sync": "true"}


def test_a_span_error_status_is_kept() -> None:
    assert to_readable_span(_row(status="error")).status.status_code is StatusCode.ERROR
    assert to_readable_span(_row(status="ok")).status.status_code is StatusCode.OK


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ChunkedEncodingError("cut"),
        requests.exceptions.TooManyRedirects("loop"),
        requests.exceptions.SSLError("tls"),
    ],
)
def test_any_requests_failure_is_unreachable(error: requests.RequestException) -> None:
    with pytest.raises(PhoenixUnreachableError):
        _client(_Session(error)).find_dataset("d")


_NOT_JSON = requests.exceptions.JSONDecodeError("x", "<html>", 0)


class _HtmlResponse(_Response):
    @override
    def json(self) -> object:
        raise _NOT_JSON


def test_reachability_tolerates_the_html_front_page() -> None:
    _client(_Session(_HtmlResponse(200, None, "<html>"))).check_reachable()


def test_a_non_json_reply_elsewhere_is_a_phoenix_error() -> None:
    session = _Session(_HtmlResponse(200, None, "<html>"))
    with pytest.raises(PhoenixReplyError):
        _client(session).find_dataset("d")
    with pytest.raises(PhoenixReplyError):
        _client(session).post_score("r", "n", 1.0, "match", _MOMENT, _MOMENT)


@pytest.mark.parametrize("body", [None, {}, {"data": None}, {"data": [{}]}])
def test_a_reply_of_the_wrong_shape_is_a_phoenix_error(body: object) -> None:
    client = _client(_Session(_Response(200, body)))
    with pytest.raises(PhoenixReplyError):
        client.find_dataset("d")
    with pytest.raises(PhoenixReplyError):
        client.upload_dataset("create", "n", [])
    with pytest.raises(PhoenixReplyError):
        client.dataset_examples("d")
    with pytest.raises(PhoenixReplyError):
        client.create_experiment("d", "n", {})
    with pytest.raises(PhoenixReplyError):
        client.experiment_runs("e")
    with pytest.raises(PhoenixReplyError):
        client.post_run("e", "x", {}, _MOMENT, _MOMENT, None)


def test_the_factory_builds_a_client_for_the_endpoint() -> None:
    client = phoenix_client(f"{_URL}/")
    assert isinstance(client, PhoenixClient)
    exporter = client._exporter
    assert isinstance(exporter, OTLPSpanExporter)
    assert exporter._endpoint == f"{_URL}/v1/traces"


def test_post_run_sends_the_trace_id_only_when_known() -> None:
    sent: list[Any] = []

    class Recording:
        def request(
            self,
            method: str,
            url: str,
            *,
            params: Mapping[str, str] | None = None,
            json: object = None,
            timeout: float | None = None,
        ) -> _Response:
            del method, url, params, timeout
            sent.append(json)
            return _Response(200, {"data": {"id": "r1"}})

    client = _client(Recording())
    moment = datetime(2026, 11, 1, tzinfo=UTC)
    client.post_run("e", "x", {"a": 1}, moment, moment, None)
    client.post_run("e", "x", {"a": 1}, moment, moment, _TRACE)
    assert "trace_id" not in sent[0]
    assert sent[1]["trace_id"] == _TRACE
    assert sent[0]["repetition_number"] == 1


def test_the_factory_ignores_proxy_settings_for_rest_and_spans() -> None:
    client = phoenix_client(_URL)
    exporter = client._exporter
    assert isinstance(exporter, OTLPSpanExporter)
    session: requests.Session = exporter._client._transport._session  # type: ignore[attr-defined]
    assert session.trust_env is False
    assert cast("object", client._session) is session
    client.close()


def test_close_shuts_the_exporter_and_the_session() -> None:
    closed: list[str] = []

    class Closing(_Exporter):
        @override
        def shutdown(self) -> None:
            closed.append("exporter")

    client = PhoenixClient(
        _URL,
        _Session(_Response()),
        Closing(),
        close_session=lambda: closed.append("session"),
    )
    client.close()
    assert closed == ["exporter", "session"]


def test_a_real_client_closes_its_session(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    monkeypatch.setattr(requests.Session, "close", lambda _self: closed.append(True))
    phoenix_client(_URL).close()
    assert closed


def test_a_non_hex_stored_id_is_a_phoenix_error() -> None:
    bad = SpanRow("not-hex", _SPAN, None, "n", 1, 2, "ok", {})
    with pytest.raises(PhoenixError, match="not hexadecimal"):
        to_readable_span(bad)
    with pytest.raises(PhoenixError):
        _client().send_spans([_row(span_id="zz")])


def test_the_exporter_log_is_quiet_during_a_send_and_restored_after(
    caplog: pytest.LogCaptureFixture,
) -> None:
    name = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    logger = logging.getLogger(name)

    class Noisy(_Exporter):
        @override
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            logger.warning("Transient error, retrying")
            return super().export(spans)

    with caplog.at_level(logging.WARNING):
        _client(exporter=Noisy()).send_spans([_row()])
        assert not caplog.records
        logger.warning("after")
    assert [record.getMessage() for record in caplog.records] == ["after"]


def test_the_exporter_log_is_restored_after_a_refused_batch() -> None:
    parent = logging.getLogger("opentelemetry.exporter.otlp.proto.http")
    before = parent.level
    with pytest.raises(SpanBatchRefusedError):
        _client(exporter=_Exporter(SpanExportResult.FAILURE)).send_spans([_row()])
    assert parent.level == before
