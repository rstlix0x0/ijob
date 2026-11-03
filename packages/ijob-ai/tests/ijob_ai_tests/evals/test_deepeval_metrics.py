import asyncio
import inspect
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import dspy
import pytest
from ijob_ai.errors import CapReachedError, TaskOutputError
from ijob_ai.evals.deepeval_metrics import (
    FAITHFUL_STEPS,
    GRADER_VERSION,
    GraderModel,
    grade_reason,
    keep_match,
    load_deepeval,
    make_grader,
    measure_case,
    score_close,
)
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.registry import prepare_environment
from ijob_ai.tracing import attributes
from ijob_ai.tracing.setup import Tracing, operation_trace
from ijob_ai.tracing.sqlite_exporter import SqliteSpanExporter
from ijob_core.traces import TraceStore
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from ..llm.fakes import JUNK, FakeAsyncEngine, FakeEngine

GRADE = '{"reason": "all facts appear", "score": 7}'


@pytest.fixture(autouse=True)
def prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    prepare_environment()


def grader_on(engine: FakeAsyncEngine, budget: Budget) -> GraderModel:
    lm = dspy.LM(
        "claude-sdk/claude-haiku-5-5",
        engine=FakeEngine(),
        async_engine=engine,
        cache=False,
        num_retries=0,
    )
    return make_grader(
        lm=lm,
        provider=Provider.CLAUDE_SDK,
        budget=budget,
        timeout_s=5,
        model_name="claude-sdk/claude-haiku-5-5",
    )


def test_keep_match_passes_on_the_same_answer() -> None:
    same = asyncio.run(
        measure_case(
            keep_match(), input="posting", actual_output="keep", expected_output="keep"
        )
    )
    different = asyncio.run(
        measure_case(
            keep_match(),
            input="posting",
            actual_output="keep",
            expected_output="reject",
        )
    )

    assert (same.score, same.passed) == (1.0, True)
    assert (different.score, different.passed) == (0.0, False)


def test_keep_match_fails_on_an_unusable_answer() -> None:
    result = asyncio.run(
        measure_case(
            keep_match(),
            input="posting",
            actual_output="maybe",
            expected_output="keep",
        )
    )

    assert result.passed is False


def test_score_close_within_twenty() -> None:
    inside = asyncio.run(
        measure_case(score_close(), input="p", actual_output="60", expected_output="40")
    )
    outside = asyncio.run(
        measure_case(score_close(), input="p", actual_output="61", expected_output="40")
    )
    below = asyncio.run(
        measure_case(score_close(), input="p", actual_output="20", expected_output="40")
    )

    assert inside.passed is True
    assert outside.passed is False
    assert below.passed is True
    assert outside.score == pytest.approx(0.0)


def test_grader_charges_one_call() -> None:
    engine = FakeAsyncEngine([GRADE], cost=0.005)
    budget = Budget(1.0)

    asyncio.run(
        grade_reason(
            grader_on(engine, budget), input="rules and posting", actual_output="fits"
        )
    )

    assert len(engine.requests) == 1
    assert budget.spent == pytest.approx(0.005)


def test_grade_reason_scores_zero_to_one() -> None:
    engine = FakeAsyncEngine([GRADE], cost=0.005)

    result = asyncio.run(
        grade_reason(
            grader_on(engine, Budget(1.0)),
            input="rules and posting",
            actual_output="fits",
        )
    )

    assert result.score == pytest.approx(0.7)
    assert result.passed is True
    assert result.reason == "all facts appear"


def test_grade_reason_fails_below_half() -> None:
    engine = FakeAsyncEngine(['{"reason": "invented", "score": 4}'])

    result = asyncio.run(
        grade_reason(grader_on(engine, Budget(1.0)), input="p", actual_output="x")
    )

    assert result.score == pytest.approx(0.4)
    assert result.passed is False


def test_grade_prompt_carries_the_steps_and_the_case() -> None:
    engine = FakeAsyncEngine([GRADE])

    asyncio.run(
        grade_reason(
            grader_on(engine, Budget(1.0)),
            input="THE-INPUT-TEXT",
            actual_output="THE-OUTPUT-TEXT",
        )
    )

    sent = repr(engine.requests[0])
    assert "THE-INPUT-TEXT" in sent
    assert "THE-OUTPUT-TEXT" in sent
    assert all(step in sent for step in FAITHFUL_STEPS)


def test_faithful_steps_are_the_three_fixed_steps() -> None:
    assert len(FAITHFUL_STEPS) == 3
    assert FAITHFUL_STEPS[0].startswith("List each fact")


def test_grader_unusable_answer_is_a_task_output_error() -> None:
    engine = FakeAsyncEngine([JUNK], cost=0.002)
    budget = Budget(1.0)

    with pytest.raises(TaskOutputError):
        asyncio.run(
            grade_reason(grader_on(engine, budget), input="p", actual_output="x")
        )

    assert budget.spent == pytest.approx(0.002)


@pytest.mark.parametrize("score", [70, 11, -1])
def test_grade_score_outside_range_is_a_task_output_error(score: int) -> None:
    engine = FakeAsyncEngine([f'{{"reason": "invented", "score": {score}}}'])

    with pytest.raises(TaskOutputError) as caught:
        asyncio.run(
            grade_reason(grader_on(engine, Budget(1.0)), input="p", actual_output="x")
        )

    assert caught.value.task == "eval_grader"


@pytest.mark.parametrize("empty", ["", "  \n"])
def test_grade_reason_fails_an_empty_reason_without_a_call(empty: str) -> None:
    engine = FakeAsyncEngine([GRADE], cost=0.005)
    budget = Budget(1.0)

    result = asyncio.run(
        grade_reason(grader_on(engine, budget), input="p", actual_output=empty)
    )

    assert (result.score, result.passed) == (0.0, False)
    assert result.reason is not None
    assert engine.requests == []
    assert budget.spent == 0.0


def test_grader_refuses_when_the_cap_is_reached() -> None:
    engine = FakeAsyncEngine([GRADE])
    budget = Budget(0.01)
    budget.charge(0.01)

    with pytest.raises(CapReachedError):
        asyncio.run(
            grade_reason(grader_on(engine, budget), input="p", actual_output="x")
        )

    assert engine.requests == []


def test_grader_generate_raises() -> None:
    grader = grader_on(FakeAsyncEngine(), Budget(1.0))

    with pytest.raises(NotImplementedError):
        grader.generate("prompt")


def test_measure_prints_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    engine = FakeAsyncEngine([GRADE])

    async def run() -> None:
        await measure_case(
            keep_match(), input="p", actual_output="keep", expected_output="keep"
        )
        await grade_reason(
            grader_on(engine, Budget(1.0)), input="p", actual_output="fits"
        )

    asyncio.run(run())

    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert list(tmp_path.iterdir()) == []


def test_import_and_measure_print_and_write_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    probe = (
        "import asyncio\n"
        "from ijob_ai.evals import deepeval_metrics as m\n"
        "m.load_deepeval()\n"
        "asyncio.run(m.measure_case(m.keep_match(), input='p',\n"
        "            actual_output='keep', expected_output='keep'))\n"
    )
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"DEEPEVAL_TELEMETRY_OPT_OUT", "DEEPEVAL_DISABLE_DOTENV"}
    }
    env["HOME"] = str(home)

    result = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=work,
    )

    assert result.returncode == 0, result.stderr
    assert (result.stdout, result.stderr) == ("", "")
    assert list(work.iterdir()) == []
    assert list(home.iterdir()) == []


def test_measure_keeps_the_private_indicator_argument() -> None:
    parameters = inspect.signature(load_deepeval().g_eval_class.a_measure).parameters

    assert "_show_indicator" in parameters


def test_loader_is_cached() -> None:
    assert load_deepeval() is load_deepeval()


def test_loader_sets_opt_outs_before_import() -> None:
    probe = (
        "import os, sys\n"
        "seen = []\n"
        "class Spy:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'deepeval':\n"
        "            seen.append((os.environ.get('DEEPEVAL_TELEMETRY_OPT_OUT'),\n"
        "                         os.environ.get('DEEPEVAL_DISABLE_DOTENV')))\n"
        "sys.meta_path.insert(0, Spy())\n"
        "from ijob_ai.evals import deepeval_metrics\n"
        "assert 'deepeval' not in sys.modules, 'imported with the module'\n"
        "deepeval_metrics.load_deepeval()\n"
        "assert seen[0] == ('1', '1'), seen\n"
    )
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"DEEPEVAL_TELEMETRY_OPT_OUT", "DEEPEVAL_DISABLE_DOTENV"}
    }

    result = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr


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


@contextmanager
def traced(tmp_path: Path) -> Iterator[InMemorySpanExporter]:
    tracing, exporter = make_tracing(tmp_path)
    with operation_trace(tracing, "eval"):
        yield exporter
    tracing.finish()


def test_grader_span_records_task_and_grader_version(tmp_path: Path) -> None:
    engine = FakeAsyncEngine([GRADE], cost=0.005)

    with traced(tmp_path) as exporter:
        asyncio.run(
            grade_reason(grader_on(engine, Budget(1.0)), input="p", actual_output="x")
        )

    [span] = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    recorded = dict(span.attributes or {})
    assert recorded[attributes.TASK] == "eval_grader"
    assert recorded[attributes.TASK_VERSION] == GRADER_VERSION
