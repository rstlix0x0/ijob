import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime

import dspy
import pytest
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.registry import prepare_environment
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import VerdictOut
from ijob_ai.tasks.programs import (
    program_version,
    tuned_predict,
    with_untrusted_statement,
)
from ijob_ai.tasks.signatures import (
    JUDGE_UNTRUSTED,
    SCORER_UNTRUSTED,
    JudgeSig,
    ScoreSig,
)
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalTask
from ijob_core.tune_records import TunedProgram

from ..llm.fakes import FakeAsyncEngine, FakeEngine

_AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_DEMO = {
    "rules": "1. no crypto",
    "posting": "Posting 99999 at a company that is gone",
    "verdict": {"keep": False, "reason": "crypto", "rule": 1},
}


@pytest.fixture(autouse=True)
def prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    prepare_environment()


def make_program(
    *,
    task: EvalTask = EvalTask.JUDGE,
    instructions: str | None = None,
    demos: Sequence[Mapping[str, object]] | None = None,
) -> TunedProgram:
    """Build a stored program; the default instructions hold the task's sentence."""
    sentence = JUDGE_UNTRUSTED if task is EvalTask.JUDGE else SCORER_UNTRUSTED
    if instructions is None:
        instructions = f"Tuned instructions.\n\n{sentence}"
    demos_json = json.dumps([_DEMO] if demos is None else demos)
    base = "judge-2" if task is EvalTask.JUDGE else "scorer-1"
    return TunedProgram(
        id=1,
        task=task,
        model="claude-sdk/claude-haiku-5-5",
        optimizer="gepa",
        base_version=base,
        set_id=1,
        bound_sha256="a" * 64,
        instructions=instructions,
        demos_json=demos_json,
        version=program_version(base, instructions, demos_json),
        tune_run_id=None,
        created_at=_AT,
        accepted_at=None,
    )


def test_program_version_is_stable() -> None:
    first = program_version("judge-2", "Text.", '[{"a": 1}]')

    assert first == program_version("judge-2", "Text.", '[{"a": 1}]')
    assert re.fullmatch(r"judge-2\+[0-9a-f]{8}", first)


def test_program_version_changes_with_demos() -> None:
    assert program_version("judge-2", "Text.", "[]") != program_version(
        "judge-2", "Text.", '[{"a": 1}]'
    )
    assert program_version("judge-2", "Text.", "[]") != program_version(
        "judge-2", "Other.", "[]"
    )


def test_statement_is_appended_when_missing() -> None:
    result = with_untrusted_statement("Rewritten 9.", JUDGE_UNTRUSTED)

    assert result.startswith("Rewritten 9.")
    assert result.endswith(JUDGE_UNTRUSTED)


def test_statement_is_kept_when_present() -> None:
    text = f"Rewritten 9. {SCORER_UNTRUSTED} More."

    assert with_untrusted_statement(text, SCORER_UNTRUSTED) == text


def test_untrusted_constants_match_the_signatures() -> None:
    assert JUDGE_UNTRUSTED in JudgeSig.instructions.splitlines()
    assert "\n" not in JUDGE_UNTRUSTED
    assert SCORER_UNTRUSTED in " ".join(ScoreSig.instructions.split())
    assert any(SCORER_UNTRUSTED in line for line in ScoreSig.instructions.splitlines())
    assert "\n" not in SCORER_UNTRUSTED


def test_versions_load_no_dspy() -> None:
    code = (
        "import sys\n"
        "import ijob_ai.tasks.versions\n"
        "heavy = ('dspy', 'claude_agent_sdk', 'pypdf', 'litellm', 'optuna')\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def _lm(engine: FakeAsyncEngine) -> dspy.LM:
    return dspy.LM(
        "claude-sdk/claude-haiku-5-5",
        engine=FakeEngine(),
        async_engine=engine,
        cache=False,
        num_retries=0,
    )


def _run(predict: dspy.Predict, engine: FakeAsyncEngine) -> dspy.Prediction:
    return asyncio.run(
        run_call(
            predict,
            _lm(engine),
            inputs={"rules": "1. no ads", "posting": "A posting."},
            provider=Provider.CLAUDE_SDK,
            budget=Budget(1.0),
            timeout_s=5,
            task=Task.JUDGE,
            task_version="judge-2",
            posting_id=None,
        )
    )


def test_a_demo_naming_a_gone_posting_still_runs() -> None:
    answer = json.dumps({"verdict": {"keep": True, "reason": "fits", "rule": None}})
    engine = FakeAsyncEngine([answer])

    prediction = _run(tuned_predict(JudgeSig, make_program()), engine)

    assert prediction.verdict.keep is True
    user = "".join(
        part.text
        for message in engine.requests[0].messages
        for part in message.parts
        if hasattr(part, "text")
    )
    assert "Posting 99999 at a company that is gone" in user


def test_tuned_predict_survives_json() -> None:
    program = make_program()
    original = dspy.Predict(JudgeSig.with_instructions(program.instructions))
    original.demos = [
        dspy.Example(
            rules="1. no crypto",
            posting="Posting 99999 at a company that is gone",
            verdict=VerdictOut(keep=False, reason="crypto", rule=1),
        )
    ]
    state = original.dump_state()
    stored = json.dumps(state["demos"])
    program = make_program(demos=json.loads(stored))

    reloaded = tuned_predict(JudgeSig, program)

    adapter = dspy.JSONAdapter()
    inputs = {"rules": "1. x", "posting": "p"}
    assert adapter.format(reloaded.signature, reloaded.demos, inputs) == (
        adapter.format(original.signature, original.demos, inputs)
    )
    assert len(reloaded.demos) == 1
    assert reloaded.signature.instructions == program.instructions


@pytest.mark.parametrize(
    ("task", "signature"),
    [(EvalTask.JUDGE, JudgeSig), (EvalTask.SCORER, ScoreSig)],
)
def test_a_program_without_the_untrusted_sentence_is_refused(
    task: EvalTask, signature: type[dspy.Signature]
) -> None:
    program = replace(
        make_program(task=task, instructions="Tuned instructions."), id=41
    )

    with pytest.raises(InvalidValueError, match="program 41"):
        tuned_predict(signature, program)


def test_a_program_holding_only_the_other_tasks_sentence_is_refused() -> None:
    program = make_program(
        task=EvalTask.SCORER, instructions=f"Text.\n\n{JUDGE_UNTRUSTED}"
    )

    with pytest.raises(InvalidValueError):
        tuned_predict(ScoreSig, program)
