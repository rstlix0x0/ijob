import asyncio
import json
import socket
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, override

import dspy
import pytest
from dspy.lm15 import Request, Response
from dspy.utils.exceptions import AdapterParseError, LMError
from ijob_ai.errors import (
    CostUnknownError,
    ProviderFailedError,
    ProviderUnavailableError,
    TaskOutputError,
)
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import (
    ForgivingJSONAdapter,
    call_span,
    run_call,
    translate_failure,
)
from ijob_ai.llm.meter import open_meter
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.registry import prepare_environment
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import VerdictOut
from ijob_ai.tasks.signatures import JudgeSig, ScoreSig
from ijob_ai.tracing import attributes
from ijob_ai.tracing.setup import (
    OperationTrace,
    Tracing,
    current_operation_trace,
    operation_trace,
)
from ijob_ai.tracing.sqlite_exporter import SqliteSpanExporter
from ijob_core.model import PostingId
from ijob_core.traces import TraceStore
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from .fakes import (
    JUNK,
    VERDICT,
    FakeAsyncEngine,
    FakeEngine,
    Judge,
    KeyedCostEngine,
    ok_response,
    rate_limited,
    serve_openrouter,
)

OPENROUTER_MODEL = "openrouter/anthropic/claude-sonnet-4.5"


@pytest.fixture(autouse=True)
def prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    prepare_environment()


def claude_lm(engine: FakeAsyncEngine) -> dspy.LM:
    return dspy.LM(
        "claude-sdk/claude-haiku-5-5",
        engine=FakeEngine(),
        async_engine=engine,
        cache=False,
        num_retries=0,
    )


def openrouter_lm() -> dspy.LM:
    return dspy.LM(OPENROUTER_MODEL, cache=False, num_retries=0)


def call(
    lm: dspy.LM,
    budget: Budget,
    *,
    provider: Provider = Provider.CLAUDE_SDK,
    posting: str = "AAA",
    timeout_s: float = 5,
) -> Coroutine[Any, Any, dspy.Prediction]:
    return run_call(
        dspy.Predict(Judge),
        lm,
        inputs={"posting": posting},
        provider=provider,
        budget=budget,
        timeout_s=timeout_s,
        task=Task.JUDGE,
        task_version="v1",
        posting_id=PostingId(7),
    )


def test_run_call_success_charges_meter_cost() -> None:
    engine = FakeAsyncEngine(cost=0.004)
    budget = Budget(1.0)

    prediction = asyncio.run(call(claude_lm(engine), budget))

    assert prediction.keep is True
    assert budget.spent == pytest.approx(0.004)
    assert engine.meters_seen[0].cost_usd == 0.004


def test_run_call_parse_failure_is_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_openrouter(monkeypatch, [ok_response(JUNK, cost=0.003)])
    budget = Budget(1.0)

    with pytest.raises(TaskOutputError) as raised:
        asyncio.run(call(openrouter_lm(), budget, provider=Provider.OPENROUTER))

    assert raised.value.task == "judge"
    assert budget.spent == pytest.approx(0.003)


def test_junk_engine_reaches_cap() -> None:
    engine = FakeAsyncEngine([JUNK], cost=0.1)
    lm = claude_lm(engine)
    budget = Budget(0.25)

    async def loop() -> int:
        started = 0
        while budget.try_start():
            started += 1
            with pytest.raises(TaskOutputError):
                await call(lm, budget)
        return started

    assert asyncio.run(loop()) == 3
    assert budget.spent == pytest.approx(0.3)


def test_run_call_unwraps_llm_error_cause() -> None:
    failure = ProviderUnavailableError("claude is not installed")
    budget = Budget(1.0)

    with pytest.raises(ProviderUnavailableError) as raised:
        asyncio.run(call(claude_lm(FakeAsyncEngine(raises=failure)), budget))

    assert raised.value is failure


def test_run_call_other_engine_error_is_provider_failed() -> None:
    engine = FakeAsyncEngine(raises=RuntimeError("x"))
    budget = Budget(1.0)

    with pytest.raises(ProviderFailedError):
        asyncio.run(call(claude_lm(engine), budget))

    assert budget.spent == 0
    assert engine.meters_seen[0].completed is False


def test_run_call_openrouter_billed_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_openrouter(monkeypatch, [ok_response(VERDICT, cost=0.000123)])
    budget = Budget(1.0)

    prediction = asyncio.run(
        call(openrouter_lm(), budget, provider=Provider.OPENROUTER)
    )

    assert prediction.keep is True
    assert budget.spent == pytest.approx(0.000123)


def test_run_call_openrouter_estimate_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_openrouter(monkeypatch, [ok_response(VERDICT, cost=None)])
    budget = Budget(1.0)

    asyncio.run(call(openrouter_lm(), budget, provider=Provider.OPENROUTER))

    # 11 prompt and 4 completion tokens at the bundled Sonnet 4.5 list price.
    assert budget.spent > 0
    assert budget.spent == pytest.approx(11 * 3e-6 + 4 * 15e-6)


def test_run_call_openrouter_429_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = serve_openrouter(monkeypatch, [rate_limited()] * 5)
    budget = Budget(1.0)

    with pytest.raises(ProviderFailedError, match="LMRateLimitError"):
        asyncio.run(call(openrouter_lm(), budget, provider=Provider.OPENROUTER))

    assert len(transport.requests) == 1
    assert budget.spent == 0


def test_run_call_completed_without_cost_raises_cost_unknown() -> None:
    engine = FakeAsyncEngine(cost=None)
    budget = Budget(1.0)

    with pytest.raises(CostUnknownError, match="claude-sdk/claude-haiku-5-5"):
        asyncio.run(call(claude_lm(engine), budget))

    assert budget.spent == 0


def test_run_call_cost_unknown_wins_over_parse_failure() -> None:
    engine = FakeAsyncEngine([JUNK], cost=None)

    with pytest.raises(CostUnknownError):
        asyncio.run(call(claude_lm(engine), Budget(1.0)))


def test_run_call_timeout() -> None:
    engine = FakeAsyncEngine(delay_s=1.0)
    budget = Budget(1.0)

    with pytest.raises(ProviderFailedError, match=r"timed out after 0\.1 s"):
        asyncio.run(call(claude_lm(engine), budget, timeout_s=0.1))

    assert budget.spent == 0


def test_run_call_makes_no_network_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[object] = []

    def refuse(*args: object, **_kwargs: object) -> None:
        attempts.append(args[:2])
        message = "network blocked by the test"
        raise OSError(message)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    prepare_environment()
    serve_openrouter(monkeypatch, [ok_response(VERDICT, cost=0.000123)])

    asyncio.run(call(openrouter_lm(), Budget(1.0), provider=Provider.OPENROUTER))

    assert attempts == []


def test_concurrent_calls_isolated() -> None:
    engine = KeyedCostEngine({"AAA": 0.001, "BBB": 0.002}, {"AAA": 0.2, "BBB": 0.05})
    lm = claude_lm(engine)
    budget = Budget(1.0)

    async def both() -> list[dspy.Prediction]:
        return list(
            await asyncio.gather(
                call(lm, budget, posting="AAA"), call(lm, budget, posting="BBB")
            )
        )

    results = asyncio.run(both())

    assert [result.keep for result in results] == [True, True]
    assert budget.spent == pytest.approx(0.003)
    assert len({id(meter) for meter in engine.meters_seen}) == 2
    assert sorted(meter.cost_usd or 0 for meter in engine.meters_seen) == [
        0.001,
        0.002,
    ]
    assert lm.history == []


class MeteringEngine(FakeAsyncEngine):
    """A fake engine that also meters the model and tokens, like the claude engine."""

    @override
    async def complete(self, request: Request) -> Response:
        response = await super().complete(request)
        meter = self.meters_seen[-1]
        meter.model = "claude-haiku-5-5-20260101"
        meter.input_tokens = 12
        meter.output_tokens = 3
        return response


def make_tracing(tmp_path: Path) -> tuple[Tracing, InMemorySpanExporter]:
    """Build a tracing on a local provider whose spans an in-memory exporter keeps."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    store = TraceStore.open(tmp_path / "traces.db")
    tracing = Tracing(
        provider=provider,
        store=store,
        exporter=SqliteSpanExporter(store),
        max_size_mb=1,
        warnings=[],
    )
    return tracing, exporter


@contextmanager
def traced(tmp_path: Path) -> Iterator[InMemorySpanExporter]:
    """Run the block as the operation `search`; its root span ends on exit."""
    tracing, exporter = make_tracing(tmp_path)
    with operation_trace(tracing, "search"):
        yield exporter
    tracing.finish()


def span_named(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    [span] = [span for span in exporter.get_finished_spans() if span.name == name]
    return span


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def test_call_span_name_and_genai_attributes(tmp_path: Path) -> None:
    engine = MeteringEngine(cost=0.004)
    lm = dspy.LM(
        "claude-sdk/m",
        engine=FakeEngine(),
        async_engine=engine,
        cache=False,
        num_retries=0,
    )
    with traced(tmp_path) as exporter:
        asyncio.run(call(lm, Budget(1.0)))

    values = attrs(span_named(exporter, "chat m"))
    assert values[attributes.PROVIDER_NAME] == "anthropic"
    assert values[attributes.OPERATION_NAME] == "chat"
    assert values[attributes.REQUEST_MODEL] == "m"
    assert values[attributes.RESPONSE_MODEL] == "claude-haiku-5-5-20260101"
    assert values[attributes.INPUT_TOKENS] == 12
    assert values[attributes.OUTPUT_TOKENS] == 3
    assert values[attributes.COST] == 0.004
    assert values[attributes.TASK] == "judge"
    assert values[attributes.TASK_VERSION] == "v1"
    assert values[attributes.POSTING_ID] == 7
    assert values[attributes.OUTCOME] == "ok"
    assert "AAA" in values[attributes.PROMPT]
    assert values[attributes.COMPLETION] == VERDICT


def open_call_span(trace: OperationTrace | None) -> None:
    """Enter and leave a `call_span` on a fresh meter, passing `trace` as given."""
    with (
        open_meter() as meter,
        call_span(
            provider=Provider.CLAUDE_SDK,
            model="m",
            meter=meter,
            history=[],
            inputs={"posting": "AAA"},
            task=Task.JUDGE,
            task_version="v1",
            posting_id=None,
            trace=trace,
        ),
    ):
        pass


def test_call_span_uses_the_trace_it_is_given(tmp_path: Path) -> None:
    tracing, exporter = make_tracing(tmp_path)

    open_call_span(OperationTrace(tracing, "tune"))
    tracing.finish()

    values = attrs(span_named(exporter, "chat m"))
    assert values[attributes.OUTCOME] == "ok"
    assert attributes.POSTING_ID not in values
    assert span_named(exporter, "ijob tune")


def test_call_span_prefers_the_given_trace_to_the_current_one(tmp_path: Path) -> None:
    current_dir, given_dir = tmp_path / "current", tmp_path / "given"
    current_dir.mkdir()
    given_dir.mkdir()
    current, current_exporter = make_tracing(current_dir)
    given, given_exporter = make_tracing(given_dir)

    with operation_trace(current, "search"):
        open_call_span(OperationTrace(given, "tune"))
    current.finish()
    given.finish()

    assert current_exporter.get_finished_spans() == ()
    span = span_named(given_exporter, "chat m")
    assert span.parent is not None
    assert (
        span.parent.span_id == span_named(given_exporter, "ijob tune").context.span_id
    )


def test_call_span_without_a_trace_opens_none() -> None:
    open_call_span(None)

    assert current_operation_trace() is None


def test_call_span_omits_posting_id_when_none(tmp_path: Path) -> None:
    with traced(tmp_path) as exporter:
        asyncio.run(
            run_call(
                dspy.Predict(Judge),
                claude_lm(FakeAsyncEngine()),
                inputs={"posting": "AAA"},
                provider=Provider.CLAUDE_SDK,
                budget=Budget(1.0),
                timeout_s=5,
                task=Task.JUDGE,
                task_version="v1",
                posting_id=None,
            )
        )

    values = attrs(span_named(exporter, "chat claude-haiku-5-5"))
    assert attributes.POSTING_ID not in values


def test_call_span_records_cost_and_outcome_on_parse_failure(tmp_path: Path) -> None:
    engine = FakeAsyncEngine([JUNK], cost=0.002)
    with traced(tmp_path) as exporter, pytest.raises(TaskOutputError):
        asyncio.run(call(claude_lm(engine), Budget(1.0)))

    values = attrs(span_named(exporter, "chat claude-haiku-5-5"))
    assert values[attributes.OUTCOME] == "task_output_error"
    assert values[attributes.COST] == 0.002
    assert values[attributes.COMPLETION] == JUNK


def test_call_span_provider_unavailable_outcome(tmp_path: Path) -> None:
    engine = FakeAsyncEngine(raises=ProviderUnavailableError("no claude"))
    with traced(tmp_path) as exporter, pytest.raises(ProviderUnavailableError):
        asyncio.run(call(claude_lm(engine), Budget(1.0)))

    values = attrs(span_named(exporter, "chat claude-haiku-5-5"))
    assert values[attributes.OUTCOME] == "provider_unavailable"
    assert attributes.COST not in values
    assert attributes.COMPLETION not in values
    assert json.loads(values[attributes.PROMPT]) == {"posting": "AAA"}


@pytest.mark.parametrize(
    ("engine", "timeout_s", "error", "outcome"),
    [
        (FakeAsyncEngine(delay_s=1.0), 0.1, ProviderFailedError, "timeout"),
        (FakeAsyncEngine(cost=None), 5, CostUnknownError, "cost_unknown"),
        (
            FakeAsyncEngine(raises=RuntimeError("x")),
            5,
            ProviderFailedError,
            "provider_failed",
        ),
    ],
)
def test_call_span_failure_outcomes(
    tmp_path: Path,
    engine: FakeAsyncEngine,
    timeout_s: float,
    error: type[Exception],
    outcome: str,
) -> None:
    with traced(tmp_path) as exporter, pytest.raises(error):
        asyncio.run(call(claude_lm(engine), Budget(1.0), timeout_s=timeout_s))

    values = attrs(span_named(exporter, "chat claude-haiku-5-5"))
    assert values[attributes.OUTCOME] == outcome
    assert "AAA" in values[attributes.PROMPT]


def test_call_span_truncates_prompt(tmp_path: Path) -> None:
    with traced(tmp_path) as exporter:
        asyncio.run(
            call(claude_lm(FakeAsyncEngine()), Budget(1.0), posting="x" * 20_000)
        )

    prompt = attrs(span_named(exporter, "chat claude-haiku-5-5"))[attributes.PROMPT]
    assert len(prompt.encode()) == 16 * 1024
    assert prompt.endswith("[truncated]")


def test_call_span_nested_under_operation_root(tmp_path: Path) -> None:
    with traced(tmp_path) as exporter:
        asyncio.run(call(claude_lm(FakeAsyncEngine()), Budget(1.0)))

    root = span_named(exporter, "ijob search")
    child = span_named(exporter, "chat claude-haiku-5-5")
    assert root.parent is None
    assert child.parent is not None
    assert child.parent.span_id == root.context.span_id


def test_call_without_operation_trace_opens_no_span(tmp_path: Path) -> None:
    tracing, exporter = make_tracing(tmp_path)

    asyncio.run(call(claude_lm(FakeAsyncEngine()), Budget(1.0)))
    tracing.finish()

    assert exporter.get_finished_spans() == ()


def test_call_span_prompt_fallback_serializes_inputs_sorted(tmp_path: Path) -> None:
    engine = FakeAsyncEngine(raises=RuntimeError("x"))
    with traced(tmp_path) as exporter, pytest.raises(ProviderFailedError):
        asyncio.run(
            run_call(
                dspy.Predict(Judge),
                claude_lm(engine),
                inputs={"posting": "AAA", "extra": Path("/x")},
                provider=Provider.CLAUDE_SDK,
                budget=Budget(1.0),
                timeout_s=5,
                task=Task.JUDGE,
                task_version="v1",
                posting_id=None,
            )
        )

    prompt = attrs(span_named(exporter, "chat claude-haiku-5-5"))[attributes.PROMPT]
    assert prompt == '{"extra": "/x", "posting": "AAA"}'


def test_call_span_prompt_fallback_is_clipped(tmp_path: Path) -> None:
    engine = FakeAsyncEngine(raises=RuntimeError("x"))
    with traced(tmp_path) as exporter, pytest.raises(ProviderFailedError):
        asyncio.run(call(claude_lm(engine), Budget(1.0), posting="x" * 20_000))

    prompt = attrs(span_named(exporter, "chat claude-haiku-5-5"))[attributes.PROMPT]
    assert len(prompt.encode()) == 16 * 1024
    assert prompt.endswith("[truncated]")


def test_call_span_openrouter_records_response_model_and_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    serve_openrouter(monkeypatch, [ok_response(VERDICT, cost=0.000123)])
    with traced(tmp_path) as exporter:
        asyncio.run(call(openrouter_lm(), Budget(1.0), provider=Provider.OPENROUTER))

    values = attrs(span_named(exporter, "chat anthropic/claude-sonnet-4.5"))
    assert values[attributes.PROVIDER_NAME] == "openrouter"
    assert values[attributes.REQUEST_MODEL] == "anthropic/claude-sonnet-4.5"
    assert values[attributes.RESPONSE_MODEL] == "anthropic/claude-sonnet-4.5"
    assert values[attributes.INPUT_TOKENS] == 11
    assert values[attributes.OUTPUT_TOKENS] == 4
    assert values[attributes.COST] == 0.000123
    assert values[attributes.OUTCOME] == "ok"


def test_run_call_claude_lm_with_openrouter_provider_raises_before_any_call() -> None:
    engine = FakeAsyncEngine()
    budget = Budget(1.0)

    with pytest.raises(ValueError, match="provider"):
        asyncio.run(call(claude_lm(engine), budget, provider=Provider.OPENROUTER))

    assert engine.requests == []
    assert budget.spent == 0


def test_run_call_openrouter_lm_with_claude_provider_raises_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = serve_openrouter(monkeypatch, [ok_response(VERDICT, cost=0.5)])
    budget = Budget(1.0)

    with pytest.raises(ValueError, match="provider"):
        asyncio.run(call(openrouter_lm(), budget, provider=Provider.CLAUDE_SDK))

    assert transport.requests == []
    assert budget.spent == 0


def test_run_call_mismatch_opens_no_span(tmp_path: Path) -> None:
    lm = claude_lm(FakeAsyncEngine())
    with traced(tmp_path) as exporter, suppress(ValueError):
        asyncio.run(call(lm, Budget(1.0), provider=Provider.OPENROUTER))

    assert exporter.get_finished_spans() == ()


def test_run_call_translated_errors_chain_the_failure() -> None:
    with pytest.raises(TaskOutputError) as parse:
        asyncio.run(call(claude_lm(FakeAsyncEngine([JUNK])), Budget(1.0)))
    with pytest.raises(ProviderFailedError) as failed:
        asyncio.run(
            call(claude_lm(FakeAsyncEngine(raises=RuntimeError("x"))), Budget(1.0))
        )
    with pytest.raises(ProviderFailedError) as timeout:
        asyncio.run(
            call(claude_lm(FakeAsyncEngine(delay_s=1.0)), Budget(1.0), timeout_s=0.1)
        )

    assert isinstance(parse.value.__cause__, AdapterParseError)
    assert isinstance(failed.value.__cause__, LMError)
    assert isinstance(timeout.value.__cause__, TimeoutError)


def test_translate_unrecognised_failure_is_not_reported_as_timeout() -> None:
    translated = translate_failure(KeyError("k"), 5, Task.JUDGE)

    assert isinstance(translated, ProviderFailedError)
    assert "timed out" not in str(translated)
    assert "KeyError" in str(translated)


_FLAT_VERDICT = '{"keep": true, "reason": "fits", "rule": null}'
_FLAT_SCORE = '{"score": 70, "reasons": ["fits"]}'


@pytest.mark.parametrize(
    "reply",
    [
        _FLAT_VERDICT,
        f"```json\n{_FLAT_VERDICT}\n```",
        f"Here is the verdict.\n\n{_FLAT_VERDICT}",
        f'{{"verdict": {_FLAT_VERDICT}}}',
    ],
)
def test_a_verdict_reads_with_or_without_its_field_name(reply: str) -> None:
    parsed = ForgivingJSONAdapter().parse(JudgeSig, reply)

    assert parsed["verdict"].keep is True
    assert parsed["verdict"].reason == "fits"


@pytest.mark.parametrize("reply", [_FLAT_SCORE, f'{{"score": {_FLAT_SCORE}}}'])
def test_a_score_reads_with_or_without_its_field_name(reply: str) -> None:
    parsed = ForgivingJSONAdapter().parse(ScoreSig, reply)

    assert parsed["score"].score == 70
    assert parsed["score"].reasons == ["fits"]


@pytest.mark.parametrize(
    "reply",
    [
        JUNK,
        '{"reason": "fits"}',
        '{"keep": true, "reason": "fits", "rule": 0}',
        '{"keep": true, "reason": "fits", "rule": null, "extra": 1}',
        "[1, 2]",
    ],
)
def test_a_flat_answer_that_does_not_fit_is_still_refused(reply: str) -> None:
    with pytest.raises(AdapterParseError):
        ForgivingJSONAdapter().parse(JudgeSig, reply)


def test_a_signature_of_several_answers_needs_their_field_names() -> None:
    class _Two(dspy.Signature):  # type: ignore[misc]
        """Answer twice."""

        first: VerdictOut = dspy.OutputField()
        second: VerdictOut = dspy.OutputField()

    with pytest.raises(AdapterParseError):
        ForgivingJSONAdapter().parse(_Two, _FLAT_VERDICT)


def test_an_answer_of_plain_text_needs_its_field_name() -> None:
    class _Text(dspy.Signature):  # type: ignore[misc]
        """Answer in words."""

        text: str = dspy.OutputField()

    with pytest.raises(AdapterParseError):
        ForgivingJSONAdapter().parse(_Text, '{"other": 1}')


def test_a_flat_answer_passes_the_call_boundary() -> None:
    engine = FakeAsyncEngine([_FLAT_VERDICT])

    prediction = asyncio.run(
        run_call(
            dspy.Predict(JudgeSig),
            claude_lm(engine),
            inputs={"posting": "AAA"},
            provider=Provider.CLAUDE_SDK,
            budget=Budget(1.0),
            timeout_s=5,
            task=Task.JUDGE,
            task_version="v1",
            posting_id=PostingId(7),
        )
    )

    assert prediction.verdict.keep is True
