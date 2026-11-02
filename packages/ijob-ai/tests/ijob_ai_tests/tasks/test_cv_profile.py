import asyncio
import json

import dspy
import pytest
from ijob_ai.errors import TaskOutputError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.reference import Provider
from ijob_ai.tasks.cv_profile import build_profile
from ijob_core.model import ResumeProfile, Seniority

from ..llm.fakes import JUNK, FakeAsyncEngine, FakeEngine


def _lm(engine: FakeAsyncEngine) -> dspy.LM:
    return dspy.LM(
        "claude-sdk/fake-model",
        engine=FakeEngine(),
        async_engine=engine,
        cache=False,
        num_retries=0,
    )


def _answer(**overrides: object) -> str:
    profile: dict[str, object] = {
        "summary": "Backend engineer",
        "years_experience": 7,
        "seniority": "senior",
        "roles": ["Backend Engineer"],
        "skills": ["Python"],
        "spoken_languages": ["English"],
        "locations": ["Jakarta"],
        "work_authorization": ["Indonesia"],
    }
    profile.update(overrides)
    return json.dumps({"profile": profile})


def _build(engine: FakeAsyncEngine, budget: Budget | None = None) -> ResumeProfile:
    return asyncio.run(
        build_profile(
            "CV text",
            lm=_lm(engine),
            provider=Provider.CLAUDE_SDK,
            budget=budget or Budget(1.0),
            timeout_s=5,
        )
    )


def test_build_profile_returns_sanitized_profile() -> None:
    engine = FakeAsyncEngine([_answer(skills=["Rust\x1b", "Go"])])

    profile = _build(engine)

    assert profile.skills == ("Rust", "Go")
    assert profile.summary == "Backend engineer"
    assert profile.years_experience == 7
    assert profile.seniority is Seniority.SENIOR
    assert profile.roles == ("Backend Engineer",)
    assert profile.locations == ("Jakarta",)
    assert len(engine.requests) == 1


def test_build_profile_drops_items_empty_after_sanitizing() -> None:
    engine = FakeAsyncEngine([_answer(skills=["\x00", "Rust", " \x1b "])])

    assert _build(engine).skills == ("Rust",)


def test_build_profile_blank_summary_is_task_output_error() -> None:
    engine = FakeAsyncEngine([_answer(summary="\x00")])

    with pytest.raises(TaskOutputError) as raised:
        _build(engine)

    assert raised.value.task == "cv_profile"


def test_build_profile_parse_failure_charged() -> None:
    engine = FakeAsyncEngine([JUNK], cost=0.002)
    budget = Budget(1.0)

    with pytest.raises(TaskOutputError):
        _build(engine, budget)

    assert budget.spent == pytest.approx(0.002)


def test_build_profile_charges_budget_on_success() -> None:
    engine = FakeAsyncEngine([_answer()], cost=0.004)
    budget = Budget(1.0)

    _build(engine, budget)

    assert budget.spent == pytest.approx(0.004)
