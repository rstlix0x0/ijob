import asyncio
import json
from datetime import UTC, datetime

import dspy
import pytest
from ijob_ai.errors import TaskOutputError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.registry import prepare_environment
from ijob_ai.tasks.scorer import score_posting
from ijob_ai.tasks.text import posting_block
from ijob_core.model import Posting, PostingId, ResumeProfile, Score, Seniority

from ..llm.fakes import FakeAsyncEngine, FakeEngine

_SEEN = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    prepare_environment()


def _posting() -> Posting:
    return Posting(
        id=PostingId(7),
        title="Staff Engineer",
        company="Acme",
        location="Berlin",
        url="https://jobs.example.test/7",
        posted_at=None,
        first_seen=_SEEN,
        last_seen=_SEEN,
        description="Build things.",
    )


def _profile() -> ResumeProfile:
    return ResumeProfile(
        summary="Backend engineer",
        years_experience=9,
        seniority=Seniority.SENIOR,
        roles=("backend",),
        spoken_languages=("English",),
        locations=("Jakarta",),
        work_authorization=("ID",),
        skills=("python", "sql"),
    )


def _answer(score: int, reasons: list[str]) -> str:
    return json.dumps({"score": {"score": score, "reasons": reasons}})


def _lm(engine: FakeAsyncEngine) -> dspy.LM:
    return dspy.LM(
        "claude-sdk/claude-haiku-5-5",
        engine=FakeEngine(),
        async_engine=engine,
        cache=False,
        num_retries=0,
    )


def _score(engine: FakeAsyncEngine, budget: Budget | None = None) -> Score:
    return asyncio.run(
        score_posting(
            _posting(),
            _profile(),
            lm=_lm(engine),
            provider=Provider.CLAUDE_SDK,
            budget=budget or Budget(1.0),
            timeout_s=5,
        )
    )


def _user_text(engine: FakeAsyncEngine) -> str:
    return "".join(
        part.text
        for message in engine.requests[0].messages
        for part in message.parts
        if hasattr(part, "text")
    )


def test_scorer_returns_bounded_score() -> None:
    engine = FakeAsyncEngine([_answer(87, ["strong python", "same city"])])
    budget = Budget(1.0)

    assert _score(engine, budget) == Score(87, ("strong python", "same city"))
    assert budget.spent == pytest.approx(0.001)


def test_scorer_sends_profile_json_as_input() -> None:
    engine = FakeAsyncEngine([_answer(50, ["ok"])])

    _score(engine)

    user = _user_text(engine)
    assert "[[ ## profile ## ]]" in user
    assert _profile().to_json() in user
    assert "[[ ## posting ## ]]" in user
    assert posting_block(_posting()) in user
    assert user.index("[[ ## posting ## ]]") < user.index(posting_block(_posting()))


def test_scorer_sanitizes_reasons_and_drops_empty_ones() -> None:
    engine = FakeAsyncEngine([_answer(60, ["  fits\x00 ", "\x1b\x00", "ok"])])

    assert _score(engine) == Score(60, ("fits", "ok"))


def test_scorer_no_reasons_left_rejected() -> None:
    engine = FakeAsyncEngine([_answer(60, ["\x00", " "])])

    with pytest.raises(TaskOutputError) as raised:
        _score(engine)

    assert raised.value.task == "scorer"
