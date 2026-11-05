import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, override

import dspy
import pytest
from dspy.lm15 import Request, Response
from dspy.utils.exceptions import AdapterParseError
from ijob_ai.errors import (
    CapReachedError,
    CostUnknownError,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import ForgivingJSONAdapter
from ijob_ai.llm.meter import current_meter
from ijob_ai.llm.railed import RailedLM, RailGuard
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.registry import prepare_environment
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.programs import tuned_predict
from ijob_ai.tasks.signatures import JUDGE_UNTRUSTED, SCORER_UNTRUSTED, JudgeSig
from ijob_ai.tuning.engine import (
    TuneOutcome,
    TuneRequest,
    TuneStatus,
    tune_program,
)
from ijob_ai.tuning.optimizers import BuiltOptimizer, Optimizer
from ijob_core.eval_records import EvalTask
from ijob_core.tune_records import TunedProgram

from ..llm.fakes import FakeAsyncEngine, FakeEngine, Opaque, answer
from .sigint_child import IN_FLIGHT, STOPPED
from .test_optimizers import (
    MIPRO_PROPOSAL,
    SCORE_MARK,
    half_wrong,
    judge_examples,
    judge_reply,
    posting_of,
    request_text,
    scorer_examples,
)

COST: Final = 0.001
EXAMPLES: Final = 11
TRAIN: Final = 7
BOUND_S: Final = 30.0
CHILD_BOUND_S: Final = 60.0
TUNED_MARK: Final = "Decide the posting well"
GEPA_PROPOSAL: Final = json.dumps({"new_instruction": f"{TUNED_MARK}."})
REWRITE: Final = "Rewritten by the optimizer."
UNREADABLE: Final = "this is not json at all"
FAILURE: Final = "scripted failure"
ALL_OPTIMIZERS: Final = (Optimizer.GEPA, Optimizer.BOOTSTRAP, Optimizer.MIPRO)
TQDM: Final = "TQDM_DISABLE"

type Reply = Callable[[str], str]


class MeteredEngine(FakeAsyncEngine):
    """An engine that answers from a function of the request text and meters it.

    A reply function that raises fails the call before anything is metered.
    """

    def __init__(
        self, reply: Reply, *, cost: float | None = COST, delay_s: float = 0.0
    ) -> None:
        super().__init__(cost=cost, delay_s=delay_s)
        self._reply = reply

    @override
    async def complete(self, request: Request) -> Response:
        self.requests.append(request)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        text = self._reply(f"{request.system}\n{request_text(request)}")
        meter = current_meter()
        meter.completed = True
        meter.cost_usd = self._cost
        return answer(text)


def lm_on(engine: MeteredEngine) -> dspy.LM:
    return dspy.LM(
        "claude-sdk/claude-haiku-5-5",
        engine=FakeEngine(),
        async_engine=engine,
        cache=False,
        num_retries=0,
    )


def tuner_reply(optimizer: Optimizer) -> str:
    return MIPRO_PROPOSAL if optimizer is Optimizer.MIPRO else GEPA_PROPOSAL


@dataclass(frozen=True)
class Rig:
    """A request with the engines and the guard behind it."""

    request: TuneRequest
    guard: RailGuard
    engine: MeteredEngine
    tuner: MeteredEngine


def rig(  # noqa: PLR0913  # one knob per thing a test varies
    optimizer: Optimizer = Optimizer.BOOTSTRAP,
    task: Task = Task.JUDGE,
    reply: Reply = judge_reply,
    *,
    cap: float = 10.0,
    cost: float | None = COST,
    on_call: Callable[[RailGuard], None] | None = None,
    delay_s: float = 0.0,
    tuner_wrap: Callable[[Reply], Reply] = lambda reply: reply,
) -> Rig:
    """Build a request on the running loop; call it from inside a coroutine."""
    guard = RailGuard(on_call=on_call)
    loop = asyncio.get_running_loop()
    budget = Budget(cap)

    def railed(engine: MeteredEngine) -> RailedLM:
        return RailedLM(
            lm_on(engine),
            provider=Provider.CLAUDE_SDK,
            budgets=(budget,),
            timeout_s=BOUND_S,
            task=task,
            task_version="test-1",
            trace=None,
            loop=loop,
            guard=guard,
        )

    engine = MeteredEngine(reply, cost=cost, delay_s=delay_s)
    tuner = MeteredEngine(tuner_wrap(lambda _text: tuner_reply(optimizer)))
    examples = (
        judge_examples(EXAMPLES) if task is Task.JUDGE else scorer_examples(EXAMPLES)
    )
    request = TuneRequest(
        task=task,
        optimizer=optimizer,
        task_lm=railed(engine),
        tuner_lm=None if optimizer is Optimizer.BOOTSTRAP else railed(tuner),
        guard=guard,
        trainset=examples[:TRAIN],
        valset=examples[TRAIN:],
        num_threads=2,
    )
    return Rig(request, guard, engine, tuner)


def tune(  # noqa: PLR0913  # one knob per thing a test varies
    optimizer: Optimizer = Optimizer.BOOTSTRAP,
    task: Task = Task.JUDGE,
    reply: Reply = judge_reply,
    *,
    cap: float = 10.0,
    cost: float | None = COST,
    on_call: Callable[[RailGuard], None] | None = None,
    tuner_wrap: Callable[[Reply], Reply] = lambda reply: reply,
) -> tuple[TuneOutcome, Rig]:
    async def main() -> tuple[TuneOutcome, Rig]:
        built = rig(
            optimizer,
            task,
            reply,
            cap=cap,
            cost=cost,
            on_call=on_call,
            tuner_wrap=tuner_wrap,
        )
        return await tune_program(built.request), built

    return asyncio.run(main())


class ScriptedOptimizer:
    """Stands in for a DSPy optimizer, so a test fixes what compile does and returns."""

    def __init__(
        self,
        calls: int = 0,
        *,
        rewrite: str | None = None,
        demos: Sequence[dict[str, Opaque]] = (),
        raises: Exception | None = None,
        during: Callable[[], None] | None = None,
    ) -> None:
        self._calls = calls
        self._rewrite = rewrite
        self._demos = list(demos)
        self._raises = raises
        self._during = during

    def compile(
        self, student: Opaque, *, trainset: list[Opaque], valset: list[Opaque]
    ) -> Opaque:
        if self._during is not None:
            self._during()
        for example in (trainset + valset)[: self._calls]:
            # An optimizer scores a failed example 0 and goes on.
            with contextlib.suppress(Exception):
                student(**example.inputs())
        if self._raises is not None:
            raise self._raises
        program = student.deepcopy()
        if self._rewrite is not None:
            program.signature = program.signature.with_instructions(self._rewrite)
        program.demos = self._demos
        return program


def script(monkeypatch: pytest.MonkeyPatch, scripted: ScriptedOptimizer) -> None:
    def build(optimizer: Optimizer, _task: Task, **_options: Opaque) -> BuiltOptimizer:
        return BuiltOptimizer(optimizer, scripted)

    monkeypatch.setattr("ijob_ai.tuning.engine.build_optimizer", build)


def has_demos(text: str) -> bool:
    return len(re.findall(r"posting \d+", text)) > 1


def number_of(text: str) -> int:
    return int(posting_of(text).split("posting ")[1].split()[0])


def judge_wrongly(text: str) -> str:
    return (
        judge_reply(text.replace("KEEPME", "nope"))
        if "KEEPME" in posting_of(text)
        else judge_reply(f"{text} KEEPME")
    )


def improving(optimizer: Optimizer) -> Reply:
    """Answer so that this optimizer's own kind of change wins on val.

    GEPA changes only instructions, so it is right only under the tuner's words.
    The other two gather demos from right answers on train, and are right on val
    only with demos in the prompt.
    """

    def reply(text: str) -> str:
        if optimizer is Optimizer.GEPA:
            right = TUNED_MARK in text
        else:
            right = number_of(text) < TRAIN or has_demos(text)
        return judge_reply(text) if right else judge_wrongly(text)

    return reply


def right_only_under_tuner_words(text: str) -> str:
    right = number_of(text) < TRAIN or TUNED_MARK in text
    return judge_reply(text) if right else judge_wrongly(text)


def failing_on(indexes: set[int], period: int, reply: Reply) -> Reply:
    """Fail the calls whose running index falls on `indexes` of each `period`."""
    state = {"count": 0}

    def failing(text: str) -> str:
        index = state["count"] % period
        state["count"] += 1
        if index in indexes:
            raise ProviderFailedError(FAILURE)
        return reply(text)

    return failing


def score_20_low(text: str) -> str:
    value = int(re.findall(rf"{SCORE_MARK}(\d+)", posting_of(text))[0])
    return json.dumps({"score": {"score": abs(value - 20), "reasons": ["close"]}})


def stored(outcome: TuneOutcome) -> TunedProgram:
    """Put an outcome's program in the shape it reloads from."""
    assert outcome.program is not None
    return TunedProgram(
        id=1,
        task=EvalTask.JUDGE,
        model="claude-sdk/claude-haiku-5-5",
        optimizer="any",
        base_version="judge-2",
        set_id=1,
        bound_sha256="0" * 64,
        instructions=outcome.program.instructions,
        demos_json=outcome.program.demos_json,
        version="judge-2+00000000",
        tune_run_id=None,
        created_at=datetime.now(UTC),
        accepted_at=None,
    )


@dataclass(frozen=True)
class OutputState:
    """What the run must leave as it found it."""

    stdout: Opaque
    stderr: Opaque
    tqdm: str | None
    logging_level: int

    @classmethod
    def now(cls) -> "OutputState":
        return cls(
            sys.stdout, sys.stderr, os.environ.get(TQDM), logging.root.manager.disable
        )


@pytest.fixture(autouse=True)
def prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    prepare_environment()


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_all_three_save_a_program_that_survives_json(optimizer: Optimizer) -> None:
    outcome, _ = tune(optimizer, Task.JUDGE, improving(optimizer))
    assert outcome.status is TuneStatus.COMPLETE
    assert outcome.program is not None
    demos = json.loads(outcome.program.demos_json)
    if optimizer is Optimizer.GEPA:
        assert demos == []
        assert TUNED_MARK in outcome.program.instructions
    else:
        assert demos
    predict = tuned_predict(JudgeSig, stored(outcome))
    assert predict.signature.instructions == outcome.program.instructions
    assert predict.demos == demos
    messages = ForgivingJSONAdapter().format(
        predict.signature, predict.demos, {"rules": "1. no sales", "posting": "x"}
    )
    assert len(messages) == 2 + 2 * len(demos)
    for demo in demos:
        assert isinstance(demo["verdict"]["keep"], bool)


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_a_changed_program_ends_complete_with_best_val(optimizer: Optimizer) -> None:
    outcome, built = tune(optimizer, Task.JUDGE, improving(optimizer))
    assert built.guard.stopped
    assert not outcome.interrupted
    assert outcome.status is TuneStatus.COMPLETE
    assert outcome.program is not None
    assert not outcome.no_change
    assert outcome.best_val == 1.0


def test_budget_stop_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    script(monkeypatch, ScriptedOptimizer(calls=TRAIN, rewrite=REWRITE))
    outcome, built = tune(cap=3 * COST)
    assert built.guard.cap_reached
    assert outcome.cap_reached
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None
    assert outcome.best_val is None
    assert outcome.compile_error is None
    assert outcome.calls == 3


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_budget_stop_is_incomplete_on_each_optimizer(optimizer: Optimizer) -> None:
    outcome, built = tune(optimizer, Task.JUDGE, improving(optimizer), cap=10 * COST)
    assert built.guard.cap_reached
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None


def test_a_cap_reached_in_the_val_pass_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 4
    script(monkeypatch, ScriptedOptimizer(calls=calls, rewrite=REWRITE))
    outcome, built = tune(cap=(calls + 0.5) * COST)
    assert built.guard.cap_reached
    assert calls < outcome.calls <= calls + built.request.num_threads
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None
    assert outcome.best_val is None


def test_compile_raising_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    script(monkeypatch, ScriptedOptimizer(raises=RuntimeError("it broke")))
    outcome, _ = tune()
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.compile_error == "RuntimeError: it broke"
    assert outcome.program is None


def test_a_parse_error_keeps_only_its_type(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = "the text of a posting the model repeated"
    parse_error = AdapterParseError("JSONAdapter", JudgeSig, reply)
    assert reply in str(parse_error)
    script(monkeypatch, ScriptedOptimizer(raises=parse_error))
    outcome, _ = tune()
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.compile_error == "AdapterParseError"


def test_a_long_error_message_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    script(monkeypatch, ScriptedOptimizer(raises=RuntimeError("x" * 500)))
    outcome, _ = tune()
    assert outcome.compile_error == f"RuntimeError: {'x' * 200}"


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_thirty_percent_failures_finish(optimizer: Optimizer) -> None:
    def failing_thirty_percent(reply: Reply) -> Reply:
        return failing_on({0, 3, 6}, 10, reply)

    # On these tiny sets GEPA's tuner calls are 12.6% of its calls, and failing
    # them at the same rate pushes its share over a quarter, as the other two are.
    # A failed MIPROv2 proposal raises, which test_a_failed_mipro_proposal_is_incomplete
    # covers. The measured shares are 0.303 (GEPA), 0.306 (bootstrap) and 0.265 (MIPRO).
    outcome, _ = tune(
        optimizer,
        Task.JUDGE,
        failing_thirty_percent(improving(optimizer)),
        tuner_wrap=failing_thirty_percent
        if optimizer is Optimizer.GEPA
        else lambda reply: reply,
    )
    assert outcome.compile_error is None
    assert outcome.failed > 0
    assert outcome.first_error is not None
    assert outcome.status is TuneStatus.INCOMPLETE


def test_thirty_percent_of_calls_is_over_a_quarter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(monkeypatch, ScriptedOptimizer(calls=10, rewrite=REWRITE))
    outcome, _ = tune(reply=failing_on({0, 3, 6}, 10, judge_reply))
    assert outcome.calls == 10
    assert outcome.failed == 3
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None


def test_a_failed_mipro_proposal_is_incomplete() -> None:
    def failing_proposals(_reply: Reply) -> Reply:
        def fail(_text: str) -> str:
            raise ProviderFailedError(FAILURE)

        return fail

    outcome, _ = tune(
        Optimizer.MIPRO,
        Task.JUDGE,
        improving(Optimizer.MIPRO),
        tuner_wrap=failing_proposals,
    )
    assert outcome.compile_error is not None
    assert outcome.compile_error.startswith("ProviderFailedError")
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None


def test_a_mipro_val_over_the_limit_is_incomplete_and_spends_nothing() -> None:
    async def main() -> tuple[TuneOutcome, Rig]:
        built = rig(Optimizer.MIPRO)
        request = replace(built.request, valset=judge_examples(51))
        return await tune_program(request), built

    outcome, built = asyncio.run(main())
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.compile_error is not None
    assert outcome.compile_error.startswith("ValueError: ")
    assert "51" in outcome.compile_error
    assert outcome.calls == 0
    assert built.engine.requests == []
    assert built.tuner.requests == []


def test_a_raising_progress_changes_no_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_pipe(_guard: RailGuard) -> None:
        raise BrokenPipeError

    script(monkeypatch, ScriptedOptimizer(calls=TRAIN, rewrite=REWRITE))
    plain, _ = tune()
    broken, _ = tune(on_call=broken_pipe)
    assert plain.status is TuneStatus.COMPLETE
    assert plain.program is not None
    assert broken.status is plain.status
    assert broken.program == plain.program
    assert broken.best_val == plain.best_val
    assert plain.best_val is not None
    assert (broken.calls, broken.failed, broken.unusable) == (
        plain.calls,
        plain.failed,
        plain.unusable,
    )


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_failures_under_a_quarter_finish_complete(optimizer: Optimizer) -> None:
    reply = failing_on({0}, 5, improving(optimizer))
    outcome, _ = tune(optimizer, Task.JUDGE, reply)
    assert outcome.compile_error is None
    assert outcome.failed > 0
    assert outcome.failed * 4 <= outcome.calls
    assert outcome.status is TuneStatus.COMPLETE


def test_over_a_quarter_failed_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    script(monkeypatch, ScriptedOptimizer(calls=TRAIN, rewrite=REWRITE))
    outcome, _ = tune(reply=failing_on({0, 1}, 7, judge_reply))
    assert outcome.calls == TRAIN
    assert outcome.failed == 2
    assert outcome.first_error is not None
    assert FAILURE in outcome.first_error
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None
    assert outcome.best_val is None


def test_a_quarter_failed_exactly_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each pass makes four calls and fails one, which leaves exactly 2 of 8.
    script(monkeypatch, ScriptedOptimizer(calls=4, rewrite=REWRITE))
    outcome, _ = tune(reply=failing_on({0}, 4, judge_reply))
    assert outcome.calls == 8
    assert outcome.failed == 2
    assert outcome.status is TuneStatus.COMPLETE
    assert outcome.program is not None


def test_failures_in_the_val_pass_count_towards_the_quarter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fine_then_failing(text: str) -> str:
        if REWRITE in text:
            raise ProviderFailedError(FAILURE)
        return judge_reply(text)

    script(monkeypatch, ScriptedOptimizer(calls=4, rewrite=REWRITE))
    outcome, _ = tune(reply=fine_then_failing)
    assert outcome.calls == 8
    assert outcome.failed == 4
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None
    assert outcome.best_val is None


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_all_unreadable_gives_no_change(optimizer: Optimizer) -> None:
    outcome, _ = tune(optimizer, Task.JUDGE, lambda _text: UNREADABLE)
    assert outcome.status is TuneStatus.COMPLETE
    assert outcome.no_change
    assert outcome.program is None
    assert outcome.best_val is None
    assert outcome.unusable > 0
    assert outcome.failed == 0


def test_unreadable_gepa_reflections_are_not_counted() -> None:
    def unreadable_reflections(_reply: Reply) -> Reply:
        return lambda _text: UNREADABLE

    outcome, built = tune(
        Optimizer.GEPA,
        Task.JUDGE,
        improving(Optimizer.GEPA),
        tuner_wrap=unreadable_reflections,
    )
    assert built.tuner.requests
    assert outcome.unusable == 0
    assert outcome.no_change


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_a_removed_model_is_incomplete(optimizer: Optimizer) -> None:
    def removed(_text: str) -> str:
        raise ProviderFailedError(FAILURE)

    outcome, _ = tune(optimizer, Task.JUDGE, removed)
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None
    assert outcome.failed > 0


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_unknown_cost_is_fatal(optimizer: Optimizer) -> None:
    outcome, built = tune(optimizer, Task.JUDGE, improving(optimizer), cost=None)
    assert isinstance(outcome.fatal, CostUnknownError)
    assert built.guard.stopped
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None


def test_an_unusable_provider_is_fatal() -> None:
    def gone(_text: str) -> str:
        raise ProviderUnavailableError(FAILURE)

    outcome, _ = tune(reply=gone)
    assert isinstance(outcome.fatal, ProviderUnavailableError)
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None


def test_untrusted_sentence_is_restored() -> None:
    outcome, _ = tune(Optimizer.MIPRO, Task.JUDGE, right_only_under_tuner_words)
    assert outcome.status is TuneStatus.COMPLETE
    assert outcome.program is not None
    assert TUNED_MARK in outcome.program.instructions
    assert JUDGE_UNTRUSTED in outcome.program.instructions


@pytest.mark.parametrize(
    ("task", "sentence"),
    [(Task.JUDGE, JUDGE_UNTRUSTED), (Task.SCORER, SCORER_UNTRUSTED)],
)
def test_each_task_gets_its_own_sentence_back(
    monkeypatch: pytest.MonkeyPatch, task: Task, sentence: str
) -> None:
    script(monkeypatch, ScriptedOptimizer(rewrite=REWRITE))
    reply = judge_reply if task is Task.JUDGE else score_20_low
    outcome, _ = tune(task=task, reply=reply)
    assert outcome.program is not None
    assert outcome.program.instructions == f"{REWRITE}\n\n{sentence}"


def cancel_first(tasks: list[asyncio.Task[TuneOutcome]]) -> Callable[[], object]:
    """Build an action that cancels the run's task, which is added after this call."""

    def cancel() -> None:
        tasks[0].cancel()

    return cancel


class OnceAt:
    """An `on_call` that acts once, the first time enough calls were let through.

    Parallel calls start in step, so an exact count may never be seen.

    Attributes:
        calls_then: How many calls had been let through when it acted, or None.
    """

    def __init__(self, calls: int, act: Callable[[], object]) -> None:
        self._calls = calls
        self._act = act
        self.calls_then: int | None = None

    def __call__(self, guard: RailGuard) -> None:
        if self.calls_then is None and guard.calls >= self._calls:
            self.calls_then = guard.calls
            self._act()


def test_cancel_stops_spending() -> None:
    async def main() -> tuple[TuneOutcome, Rig, OnceAt, int, int]:
        tasks: list[asyncio.Task[TuneOutcome]] = []
        cancel = OnceAt(5, cancel_first(tasks))
        built = rig(on_call=cancel, delay_s=0.001)
        tasks.append(asyncio.create_task(tune_program(built.request)))
        done, _ = await asyncio.wait(tasks, timeout=BOUND_S)
        assert done
        at_end = len(built.engine.requests)
        await asyncio.sleep(0.05)
        return tasks[0].result(), built, cancel, at_end, len(built.engine.requests)

    outcome, built, cancel, at_end, later = asyncio.run(main())
    assert built.guard.stopped
    assert at_end == later
    assert cancel.calls_then is not None
    assert at_end <= cancel.calls_then + built.request.num_threads
    assert outcome.calls == at_end


def test_cancel_is_interrupted_not_only_stopped() -> None:
    async def main() -> tuple[TuneOutcome, asyncio.Task[TuneOutcome]]:
        tasks: list[asyncio.Task[TuneOutcome]] = []
        built = rig(
            Optimizer.BOOTSTRAP,
            on_call=OnceAt(3, cancel_first(tasks)),
        )
        tasks.append(asyncio.create_task(tune_program(built.request)))
        done, _ = await asyncio.wait(tasks, timeout=BOUND_S)
        assert done
        return tasks[0].result(), tasks[0]

    outcome, task = asyncio.run(main())
    assert outcome.interrupted
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None
    assert outcome.best_val is None
    assert not outcome.cap_reached
    assert outcome.fatal is None
    assert task.cancelling() == 0


def stop_directly(guard: RailGuard) -> None:
    guard.stop()


@pytest.mark.parametrize("rewrite", [REWRITE, None])
def test_a_direct_stop_makes_the_run_incomplete(
    monkeypatch: pytest.MonkeyPatch, rewrite: str | None
) -> None:
    script(monkeypatch, ScriptedOptimizer(calls=TRAIN, rewrite=rewrite))
    outcome, built = tune(on_call=stop_directly)
    assert built.guard.calls == 1
    assert outcome.status is TuneStatus.INCOMPLETE
    assert not outcome.no_change
    assert outcome.program is None
    assert outcome.best_val is None
    assert outcome.interrupted
    assert outcome.fatal is None


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_a_direct_stop_is_incomplete_on_each_optimizer(optimizer: Optimizer) -> None:
    outcome, _ = tune(
        optimizer, Task.JUDGE, improving(optimizer), on_call=stop_directly
    )
    assert outcome.status is TuneStatus.INCOMPLETE
    assert outcome.program is None
    assert outcome.compile_error is None


def test_a_cancelled_mipro_run_reads_no_compile_error() -> None:
    async def main() -> TuneOutcome:
        tasks: list[asyncio.Task[TuneOutcome]] = []
        built = rig(
            Optimizer.MIPRO,
            reply=improving(Optimizer.MIPRO),
            on_call=OnceAt(10, cancel_first(tasks)),
        )
        tasks.append(asyncio.create_task(tune_program(built.request)))
        done, _ = await asyncio.wait(tasks, timeout=BOUND_S)
        assert done
        return tasks[0].result()

    outcome = asyncio.run(main())
    assert outcome.interrupted
    assert not outcome.cap_reached
    assert outcome.compile_error is None
    assert outcome.status is TuneStatus.INCOMPLETE


def test_an_interrupt_keeps_a_compile_error_it_did_not_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(monkeypatch, ScriptedOptimizer(calls=2, raises=RuntimeError("it broke")))
    outcome, _ = tune(on_call=stop_directly)
    assert outcome.interrupted
    assert outcome.compile_error == "RuntimeError: it broke"


def test_a_refusal_with_no_interrupt_stays_a_compile_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(monkeypatch, ScriptedOptimizer(raises=CapReachedError()))
    outcome, _ = tune()
    assert not outcome.interrupted
    assert outcome.compile_error is not None
    assert outcome.compile_error.startswith("CapReachedError")


def test_ctrl_c_is_interrupted() -> None:
    outcome, built = tune(on_call=OnceAt(3, lambda: signal.raise_signal(signal.SIGINT)))
    assert outcome.interrupted
    assert outcome.status is TuneStatus.INCOMPLETE
    assert built.guard.stopped


def test_keyboard_interrupt_in_the_wait_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_wait = asyncio.wait
    raised: list[bool] = []

    async def wait_once_interrupted(*args: Opaque, **kwargs: Opaque) -> Opaque:
        if not raised:
            raised.append(True)
            raise KeyboardInterrupt
        return await real_wait(*args, **kwargs)

    script(monkeypatch, ScriptedOptimizer(calls=TRAIN, rewrite=REWRITE))
    monkeypatch.setattr("ijob_ai.tuning.engine.asyncio.wait", wait_once_interrupted)
    outcome, built = tune()
    assert outcome.interrupted
    assert outcome.status is TuneStatus.INCOMPLETE
    assert built.guard.stopped


def test_a_second_cancel_still_waits_for_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait(BOUND_S)

    script(monkeypatch, ScriptedOptimizer(during=block))

    async def main() -> tuple[bool, TuneOutcome, OutputState]:
        built = rig()
        task = asyncio.create_task(tune_program(built.request))
        await asyncio.to_thread(started.wait, BOUND_S)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        still_waiting = not task.done()
        release.set()
        done, _ = await asyncio.wait({task}, timeout=BOUND_S)
        assert done
        return still_waiting, task.result(), OutputState.now()

    still_waiting, outcome, state = asyncio.run(main())
    assert still_waiting
    assert outcome.interrupted
    assert state.logging_level == logging.NOTSET


def test_two_real_ctrl_c_stop_spending_restore_output_and_end_the_worker() -> None:
    tests_root = Path(__file__).parents[2]
    with subprocess.Popen(
        [sys.executable, "-m", "ijob_ai_tests.tuning.sigint_child"],
        cwd=tests_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        # Killing the child ends any read below, so a stuck run fails rather than hangs.
        bound = threading.Timer(CHILD_BOUND_S, child.kill)
        bound.start()
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == IN_FLIGHT
            child.send_signal(signal.SIGINT)
            assert child.stdout.readline().strip() == STOPPED
            child.send_signal(signal.SIGINT)
            output, errors = child.communicate()
        finally:
            bound.cancel()
            child.kill()
    assert child.returncode == 0, errors
    report = json.loads(output.strip().splitlines()[-1])
    assert report == {
        "left_with": "KeyboardInterrupt",
        "requests_at_exit": 1,
        "requests_later": 1,
        "calls": 1,
        "spent": 0.0,
        "stopped": True,
        "stdout_restored": True,
        "stderr_restored": True,
        "logging_disable": logging.NOTSET,
        "tqdm": None,
        "workers": [],
    }


@pytest.mark.parametrize("optimizer", ALL_OPTIMIZERS)
def test_compile_prints_nothing(
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    optimizer: Optimizer,
) -> None:
    monkeypatch.delenv(TQDM, raising=False)
    before = OutputState.now()
    tune(optimizer, Task.JUDGE, improving(optimizer))
    assert OutputState.now() == before
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_raising_compile_prints_nothing_and_restores(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[OutputState] = []

    def noisy() -> None:
        seen.append(OutputState.now())
        print("stray text")  # noqa: T201  # the output under test
        logging.getLogger("dspy").critical("a logged posting")

    monkeypatch.delenv(TQDM, raising=False)
    script(monkeypatch, ScriptedOptimizer(raises=RuntimeError("no"), during=noisy))
    before = OutputState.now()
    outcome, _ = tune()
    assert outcome.compile_error == "RuntimeError: no"
    assert OutputState.now() == before
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert seen[0].stdout is not before.stdout
    assert seen[0].tqdm == "1"
    assert seen[0].logging_level == logging.CRITICAL


def test_output_settings_held_before_the_run_come_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TQDM, "0")
    logging.disable(logging.WARNING)
    try:
        script(monkeypatch, ScriptedOptimizer())
        before = OutputState.now()
        tune()
        assert OutputState.now() == before
    finally:
        logging.disable(logging.NOTSET)


def test_progress_drawn_on_the_loop_reaches_stderr(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def draw(guard: RailGuard) -> None:
        sys.stderr.write(f"calls {guard.calls}\n")

    script(monkeypatch, ScriptedOptimizer(calls=2))
    tune(on_call=draw)
    assert capfd.readouterr().err == "calls 1\ncalls 2\n"


@pytest.mark.parametrize(
    ("task", "reply", "best_val"),
    [(Task.JUDGE, half_wrong, 0.75), (Task.SCORER, score_20_low, 0.8)],
)
def test_best_val_is_scored_through_the_rails(
    monkeypatch: pytest.MonkeyPatch, task: Task, reply: Reply, best_val: float
) -> None:
    calls = 2
    script(monkeypatch, ScriptedOptimizer(calls=calls, rewrite=REWRITE))
    outcome, built = tune(task=task, reply=reply)
    val_size = EXAMPLES - TRAIN
    assert built.guard.calls == calls + val_size
    assert outcome.calls == calls + val_size
    assert outcome.best_val == pytest.approx(best_val)
    assert outcome.status is TuneStatus.COMPLETE
    for request in built.engine.requests[calls:]:
        assert REWRITE in str(request.system)


def test_the_val_pass_runs_the_demos_it_saves(monkeypatch: pytest.MonkeyPatch) -> None:
    demo = {
        "rules": "1. no sales",
        "posting": "posting 99 KEEPME",
        "verdict": {"keep": True, "reason": "fits", "rule": None},
    }
    script(monkeypatch, ScriptedOptimizer(demos=[demo]))
    outcome, built = tune()
    assert outcome.program is not None
    assert json.loads(outcome.program.demos_json) == [demo]
    assert built.engine.requests
    for request in built.engine.requests:
        assert "posting 99" in request_text(request)


def test_demos_in_another_field_order_give_the_same_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {"keep": True, "reason": "fits", "rule": None}
    demo = {"rules": "1. no sales", "posting": "posting 99 KEEPME", "verdict": verdict}
    backwards = dict(reversed(demo.items()))
    backwards["verdict"] = dict(reversed(verdict.items()))
    script(monkeypatch, ScriptedOptimizer(demos=[demo]))
    first, _ = tune()
    script(monkeypatch, ScriptedOptimizer(demos=[backwards]))
    second, _ = tune()
    assert first.program is not None
    assert second.program is not None
    assert list(backwards) != list(demo)
    assert first.program.demos_json == second.program.demos_json


def test_no_change_makes_no_val_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 3
    script(monkeypatch, ScriptedOptimizer(calls=calls))
    outcome, built = tune()
    assert built.guard.calls == calls
    assert outcome.status is TuneStatus.COMPLETE
    assert outcome.no_change
    assert outcome.program is None
    assert outcome.best_val is None


def test_outcome_counts_calls_failures_and_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(monkeypatch, ScriptedOptimizer(calls=TRAIN))
    replies = iter(
        [
            UNREADABLE,
            UNREADABLE,
            "",
            "fail",
            judge_reply("posting 1 nope"),
            judge_reply("posting 1 nope"),
            judge_reply("posting 1 nope"),
        ]
    )

    def reply(_text: str) -> str:
        text = next(replies)
        if text == "fail":
            raise ProviderFailedError(FAILURE)
        return text

    async def main() -> tuple[TuneOutcome, Rig]:
        built = rig(reply=reply)
        return await tune_program(built.request), built

    outcome, _ = asyncio.run(main())
    assert outcome.calls == TRAIN
    assert outcome.failed == 1
    assert outcome.unusable == 3
    assert outcome.status is TuneStatus.COMPLETE


def test_a_request_needs_the_tuner_its_optimizer_uses() -> None:
    async def main() -> None:
        built = rig(Optimizer.GEPA)
        with pytest.raises(ValueError, match="tuner"):
            TuneRequest(
                task=Task.JUDGE,
                optimizer=Optimizer.GEPA,
                task_lm=built.request.task_lm,
                tuner_lm=None,
                guard=built.guard,
                trainset=built.request.trainset,
                valset=built.request.valset,
                num_threads=2,
            )

    asyncio.run(main())


def refuse(built: Rig, changes: dict[str, Opaque], message: str) -> None:
    """Assert that the rig's request, with `changes`, is refused before any spend."""
    fields: dict[str, Opaque] = {
        "task": built.request.task,
        "optimizer": built.request.optimizer,
        "task_lm": built.request.task_lm,
        "tuner_lm": built.request.tuner_lm,
        "guard": built.guard,
        "trainset": built.request.trainset,
        "valset": built.request.valset,
        "num_threads": built.request.num_threads,
    }
    with pytest.raises(ValueError, match=message):
        TuneRequest(**(fields | changes))
    assert built.engine.requests == []
    assert built.tuner.requests == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"task": Task.CV_PROFILE}, "tuned"),
        ({"trainset": []}, "train"),
        ({"valset": []}, "val"),
        ({"num_threads": 0}, "num_threads"),
    ],
)
def test_a_request_refuses_what_cannot_run(
    changes: dict[str, Opaque], message: str
) -> None:
    async def main() -> None:
        refuse(rig(), changes, message)

    asyncio.run(main())


@pytest.mark.parametrize(
    ("field", "message"),
    [("trainset", "train example.*label"), ("valset", "val example.*label")],
)
def test_a_request_refuses_an_example_with_no_label(field: str, message: str) -> None:
    labelled = judge_examples(2)
    unlabelled = dspy.Example(rules="1. no sales", posting="posting 9").with_inputs(
        "rules", "posting"
    )

    async def main() -> None:
        refuse(rig(), {field: [*labelled, unlabelled]}, message)

    asyncio.run(main())


def rail_on(engine: MeteredEngine, guard: RailGuard) -> RailedLM:
    return RailedLM(
        lm_on(engine),
        provider=Provider.CLAUDE_SDK,
        budgets=(Budget(1.0),),
        timeout_s=BOUND_S,
        task=Task.JUDGE,
        task_version="test-1",
        trace=None,
        loop=asyncio.get_running_loop(),
        guard=guard,
    )


@pytest.mark.parametrize(
    ("field", "message"), [("task_lm", "task_lm"), ("tuner_lm", "tuner_lm")]
)
def test_a_request_refuses_a_plain_lm(field: str, message: str) -> None:
    async def main() -> None:
        built = rig(Optimizer.GEPA)
        refuse(built, {field: lm_on(built.engine)}, message)

    asyncio.run(main())


@pytest.mark.parametrize(
    ("field", "message"), [("task_lm", "task_lm"), ("tuner_lm", "tuner_lm")]
)
def test_a_request_refuses_a_rail_on_another_guard(field: str, message: str) -> None:
    async def main() -> None:
        built = rig(Optimizer.GEPA)
        refuse(built, {field: rail_on(built.engine, RailGuard())}, message)

    asyncio.run(main())


def test_a_used_guard_is_refused() -> None:
    async def main() -> None:
        built = rig()
        built.guard.stop()
        with pytest.raises(ValueError, match="guard"):
            await tune_program(built.request)

    asyncio.run(main())
