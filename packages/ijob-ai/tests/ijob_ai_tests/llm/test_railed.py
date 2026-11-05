import asyncio
import threading
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any, override

import dspy
import pytest
from dspy.lm15 import Request, Response
from ijob_ai.errors import (
    CapReachedError,
    CostUnknownError,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import ForgivingJSONAdapter, run_call
from ijob_ai.llm.railed import RailedLM, RailGuard
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.registry import prepare_environment
from ijob_ai.llm.settings import Task
from ijob_ai.tracing import attributes
from ijob_ai.tracing.setup import OperationTrace, Tracing
from ijob_ai.tracing.sqlite_exporter import SqliteSpanExporter
from ijob_core.traces import TraceStore
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from .fakes import (
    VERDICT,
    FakeAsyncEngine,
    FakeEngine,
    Judge,
    ok_response,
    serve_openrouter,
)

OPENROUTER_MODEL = "openrouter/anthropic/claude-sonnet-4.5"
MESSAGES = [{"role": "user", "content": "AAA"}]


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


def railed(
    lm: dspy.LM,
    *budgets: Budget,
    guard: RailGuard | None = None,
    provider: Provider = Provider.CLAUDE_SDK,
    timeout_s: float = 5,
    trace: OperationTrace | None = None,
) -> RailedLM:
    """Wrap `lm`; call from inside a running loop, which the rail then uses."""
    return RailedLM(
        lm,
        provider=provider,
        budgets=budgets or (Budget(1.0),),
        timeout_s=timeout_s,
        task=Task.JUDGE,
        task_version="v1",
        trace=trace,
        loop=asyncio.get_running_loop(),
        guard=guard if guard is not None else RailGuard(),
    )


class ThreadEngine(FakeAsyncEngine):
    """A fake engine that remembers the thread each request ran on."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[int] = []

    @override
    async def complete(self, request: Request) -> Response:
        self.threads.append(threading.get_ident())
        return await super().complete(request)


def test_messages_shape_reaches_the_model() -> None:
    engine = FakeAsyncEngine()

    async def main() -> list[Any]:
        lm = railed(claude_lm(engine))
        return await asyncio.to_thread(lambda: lm(messages=MESSAGES))

    assert asyncio.run(main()) == [VERDICT]
    assert len(engine.requests) == 1


def test_prompt_shape_reaches_the_model() -> None:
    engine = FakeAsyncEngine()

    async def main() -> list[Any]:
        lm = railed(claude_lm(engine))
        return await asyncio.to_thread(lambda: lm("text"))

    assert asyncio.run(main()) == [VERDICT]
    assert len(engine.requests) == 1


def test_calls_run_on_the_given_loop() -> None:
    engine = ThreadEngine()
    seen: dict[str, int] = {}

    async def main() -> None:
        seen["loop"] = threading.get_ident()
        lm = railed(claude_lm(engine))

        def work() -> None:
            seen["worker"] = threading.get_ident()
            lm("text")

        await asyncio.to_thread(work)

    asyncio.run(main())

    assert engine.threads == [seen["loop"]]
    assert seen["worker"] != seen["loop"]


def test_every_budget_is_checked_before_a_call() -> None:
    engine = FakeAsyncEngine()
    roomy, full = Budget(1.0), Budget(0.0)
    guard = RailGuard()

    async def main() -> None:
        lm = railed(claude_lm(engine), roomy, full, guard=guard)
        await asyncio.to_thread(lambda: lm("text"))

    with pytest.raises(CapReachedError):
        asyncio.run(main())

    assert engine.requests == []


def test_every_budget_is_charged_after_a_call() -> None:
    engine = FakeAsyncEngine(cost=0.004)
    first, second = Budget(1.0), Budget(2.0)

    async def main() -> None:
        lm = railed(claude_lm(engine), first, second)
        await asyncio.to_thread(lambda: lm("text"))

    asyncio.run(main())

    assert first.spent == pytest.approx(0.004)
    assert second.spent == pytest.approx(0.004)


def test_a_refused_call_spends_nothing() -> None:
    engine = FakeAsyncEngine(cost=0.5)
    first, second = Budget(1.0), Budget(0.4)
    guard = RailGuard()

    async def main() -> None:
        lm = railed(claude_lm(engine), first, second, guard=guard)
        await asyncio.to_thread(lambda: lm("text"))
        assert not guard.cap_reached
        await asyncio.to_thread(lambda: lm("text"))

    with pytest.raises(CapReachedError):
        asyncio.run(main())

    assert guard.cap_reached
    assert guard.calls == 1
    assert first.spent == pytest.approx(0.5)
    assert second.spent == pytest.approx(0.5)
    assert len(engine.requests) == 1


def test_stop_refuses_every_later_call() -> None:
    engine = FakeAsyncEngine()
    guard = RailGuard()

    async def main() -> None:
        lm = railed(claude_lm(engine), guard=guard)
        await asyncio.to_thread(lambda: lm("text"))
        guard.stop()
        for _ in range(2):
            with pytest.raises(CapReachedError):
                await asyncio.to_thread(lambda: lm("text"))

    asyncio.run(main())

    assert guard.stopped
    assert not guard.cap_reached
    assert len(engine.requests) == 1
    assert guard.calls == 1


def test_timeout_counts_as_a_failure() -> None:
    engine = FakeAsyncEngine(delay_s=1.0)
    guard = RailGuard()

    async def main() -> None:
        lm = railed(claude_lm(engine), guard=guard, timeout_s=0.1)
        await asyncio.to_thread(lambda: lm("text"))

    with pytest.raises(ProviderFailedError, match="timed out"):
        asyncio.run(main())

    assert guard.failed == 1
    assert guard.calls == 1
    assert guard.first_error is not None
    assert "timed out" in guard.first_error
    assert guard.fatal is None


def _call_with(lm: RailedLM, text: str) -> list[Any]:
    return lm(text)


def test_provider_failure_keeps_the_first_error() -> None:
    guard = RailGuard()

    async def main() -> None:
        for text in ("first", "second"):
            lm = railed(
                claude_lm(FakeAsyncEngine(raises=RuntimeError(text))), guard=guard
            )
            with pytest.raises(ProviderFailedError):
                await asyncio.to_thread(_call_with, lm, "text")

    asyncio.run(main())

    assert guard.failed == 2
    assert guard.first_error is not None
    assert "first" in guard.first_error
    assert "second" not in guard.first_error


def test_unknown_cost_raises_and_stops() -> None:
    engine = FakeAsyncEngine(cost=None)
    budget = Budget(1.0)
    guard = RailGuard()

    async def main() -> None:
        lm = railed(claude_lm(engine), budget, guard=guard)
        with pytest.raises(CostUnknownError):
            await asyncio.to_thread(lambda: lm("text"))
        with pytest.raises(CapReachedError):
            await asyncio.to_thread(lambda: lm("text"))

    asyncio.run(main())

    assert isinstance(guard.fatal, CostUnknownError)
    assert guard.stopped
    assert not guard.cap_reached
    assert len(engine.requests) == 1
    assert budget.spent == 0


def test_provider_unavailable_stops() -> None:
    engine = FakeAsyncEngine(raises=ProviderUnavailableError("no claude"))
    guard = RailGuard()

    async def main() -> None:
        lm = railed(claude_lm(engine), guard=guard)
        with pytest.raises(ProviderUnavailableError):
            await asyncio.to_thread(lambda: lm("text"))
        with pytest.raises(CapReachedError):
            await asyncio.to_thread(lambda: lm("text"))

    asyncio.run(main())

    assert isinstance(guard.fatal, ProviderUnavailableError)
    assert guard.stopped
    assert len(engine.requests) == 1


def test_provider_mismatch_is_refused() -> None:
    engine = FakeAsyncEngine()
    budget = Budget(1.0)

    async def main() -> None:
        lm = railed(claude_lm(engine), budget, provider=Provider.OPENROUTER)
        await asyncio.to_thread(lambda: lm("text"))

    with pytest.raises(ValueError, match="provider"):
        asyncio.run(main())

    assert engine.requests == []
    assert budget.spent == 0


def test_openrouter_is_metered_from_history(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_openrouter(monkeypatch, [ok_response(VERDICT, cost=0.25)])
    first, second = Budget(1.0), Budget(1.0)

    async def main() -> list[Any]:
        lm = railed(openrouter_lm(), first, second, provider=Provider.OPENROUTER)
        return await asyncio.to_thread(lambda: lm(messages=MESSAGES))

    assert asyncio.run(main()) == [VERDICT]
    assert first.spent == pytest.approx(0.25)
    assert second.spent == pytest.approx(0.25)


def test_claude_is_metered_by_its_engine() -> None:
    engine = FakeAsyncEngine(cost=0.0125)
    budget = Budget(1.0)

    async def main() -> None:
        lm = railed(claude_lm(engine), budget)
        await asyncio.to_thread(lambda: lm("text"))

    asyncio.run(main())

    assert engine.meters_seen[0].cost_usd == 0.0125
    assert budget.spent == pytest.approx(0.0125)


def make_tracing(tmp_path: Path) -> tuple[Tracing, InMemorySpanExporter]:
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


def test_each_call_records_one_span(tmp_path: Path) -> None:
    tracing, exporter = make_tracing(tmp_path)
    trace = OperationTrace(tracing, "tune")

    async def main() -> None:
        lm = railed(claude_lm(FakeAsyncEngine(cost=0.004)), trace=trace)
        await asyncio.to_thread(lambda: lm("text"))

    asyncio.run(main())
    tracing.finish()

    [span] = [s for s in exporter.get_finished_spans() if s.name.startswith("chat")]
    values = dict(span.attributes or {})
    assert span.name == "chat claude-haiku-5-5"
    assert values[attributes.TASK] == "judge"
    assert values[attributes.TASK_VERSION] == "v1"
    assert values[attributes.COST] == 0.004
    assert values[attributes.OUTCOME] == "ok"
    assert attributes.POSTING_ID not in values
    roots = [s for s in exporter.get_finished_spans() if s.name == "ijob tune"]
    assert _is_child(span, roots)


def _is_child(span: ReadableSpan, roots: Sequence[ReadableSpan]) -> bool:
    return (
        len(roots) == 1
        and span.parent is not None
        and span.parent.span_id == roots[0].context.span_id
    )


def test_each_call_uses_its_own_copy() -> None:
    engine = FakeAsyncEngine()
    inner = claude_lm(engine)

    async def main() -> None:
        lm = railed(inner)
        await asyncio.to_thread(lambda: lm("one"))
        await asyncio.to_thread(lambda: lm("two"))

    asyncio.run(main())

    assert len(engine.requests) == 2
    assert inner.history == []


def test_failed_call_is_not_retried() -> None:
    engine = FakeAsyncEngine(raises=RuntimeError("boom"))
    guard = RailGuard()

    async def main() -> None:
        lm = railed(claude_lm(engine), guard=guard)
        await asyncio.to_thread(lambda: lm("text"))

    with pytest.raises(ProviderFailedError):
        asyncio.run(main())

    assert len(engine.requests) == 1
    assert guard.calls == 1


def test_copy_keeps_the_rails() -> None:
    engine = FakeAsyncEngine(cost=0.01)
    budget = Budget(1.0)
    guard = RailGuard()

    async def main() -> RailedLM:
        lm = railed(claude_lm(engine), budget, guard=guard)
        copy = lm.copy(temperature=0.7)
        await asyncio.to_thread(lambda: copy("text"))
        return copy

    copy = asyncio.run(main())

    assert isinstance(copy, RailedLM)
    assert budget.spent == pytest.approx(0.01)
    assert guard.calls == 1
    assert engine.requests[0].config.temperature == 0.7


def test_on_call_runs_after_each_call() -> None:
    engine = FakeAsyncEngine()
    seen: list[tuple[int, int, int]] = []
    loop_thread: list[int] = []

    def on_call(guard: RailGuard) -> None:
        seen.append((guard.calls, guard.failed, threading.get_ident()))

    async def main() -> None:
        loop_thread.append(threading.get_ident())
        guard = RailGuard(on_call=on_call)
        lm = railed(claude_lm(engine), guard=guard)
        await asyncio.to_thread(lambda: lm("text"))
        await asyncio.to_thread(lambda: lm("text"))

    asyncio.run(main())

    assert seen == [(1, 0, loop_thread[0]), (2, 0, loop_thread[0])]


def test_messages_call_gives_no_warning() -> None:
    engine = FakeAsyncEngine()

    async def main() -> None:
        lm = railed(claude_lm(engine))
        await asyncio.to_thread(lambda: lm(messages=MESSAGES))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(main())

    assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []
    assert len(engine.requests) == 1


def test_an_openrouter_task_sends_the_same_request_through_the_rail_and_run_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = serve_openrouter(
        monkeypatch, [ok_response(VERDICT, cost=0.25), ok_response(VERDICT, cost=0.25)]
    )
    predict = dspy.Predict(Judge)

    async def main() -> None:
        await run_call(
            predict,
            openrouter_lm(),
            inputs={"posting": "AAA"},
            provider=Provider.OPENROUTER,
            budget=Budget(1.0),
            timeout_s=5,
            task=Task.JUDGE,
            task_version="v1",
            posting_id=None,
        )
        rail = railed(openrouter_lm(), provider=Provider.OPENROUTER)

        def through_the_rail() -> None:
            with dspy.context(lm=rail, adapter=ForgivingJSONAdapter()):
                predict(posting="AAA")

        await asyncio.to_thread(through_the_rail)

    asyncio.run(main())

    [direct, via_rail] = [request.body for request in transport.requests]
    assert b"response_format" in direct
    assert via_rail == direct
