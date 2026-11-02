import asyncio
import json
import re
from types import MappingProxyType

import dspy
import pytest
from ijob_ai.errors import NothingActionableError, TaskOutputError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.reference import Provider
from ijob_ai.tasks.instructions import ParsedInstructions, parse_instructions
from ijob_core.errors import InvalidValueError

from ..llm.fakes import JUNK, FakeAsyncEngine, FakeEngine


def _lm(engine: FakeAsyncEngine) -> dspy.LM:
    return dspy.LM(
        "claude-sdk/fake-model",
        engine=FakeEngine(),
        async_engine=engine,
        cache=False,
        num_retries=0,
    )


def _answer(
    filters: dict[str, object] | None = None, rules: list[str] | None = None
) -> str:
    return json.dumps({"parsed": {"filters": filters or {}, "rules": rules or []}})


def _parse(
    text: str, engine: FakeAsyncEngine, budget: Budget | None = None
) -> ParsedInstructions:
    return asyncio.run(
        parse_instructions(
            text,
            lm=_lm(engine),
            provider=Provider.CLAUDE_SDK,
            budget=budget or Budget(1.0),
            timeout_s=5,
        )
    )


def test_parse_instructions_returns_filters_and_rules() -> None:
    salary = {"amount": 5000, "currency": "USD", "period": "month"}
    engine = FakeAsyncEngine(
        [
            _answer(
                {"countries": ["ID"], "min_salary": salary},
                ["No crypto companies", "\x1b", "Prefer small teams\x00"],
            )
        ]
    )

    parsed = _parse("Remote in Indonesia, no crypto", engine)

    assert parsed.filters == {
        "countries": ["ID"],
        "min_salary": {"amount": 5000.0, "currency": "USD", "period": "month"},
    }
    assert parsed.rules == ("No crypto companies", "Prefer small teams")


@pytest.mark.parametrize("text", ["", "  \n\t ", "x" * 4001])
def test_parse_instructions_blank_refused_without_call(text: str) -> None:
    engine = FakeAsyncEngine()
    budget = Budget(1.0)

    with pytest.raises(InvalidValueError) as raised:
        _parse(text, engine, budget)

    assert raised.value.field == "instructions"
    assert engine.requests == []
    assert budget.spent == 0.0


def test_parse_instructions_limit_is_inclusive() -> None:
    engine = FakeAsyncEngine([_answer(rules=["a rule"])])

    assert _parse("x" * 4000, engine).rules == ("a rule",)


def test_parse_instructions_escapes_role_words() -> None:
    engine = FakeAsyncEngine(
        [_answer({"role": {"include": ["C++ (.*)"], "exclude": ["a.b", "\x00"]}})]
    )

    parsed = _parse("C++ roles only", engine)

    assert parsed.filters == {
        "role": {
            "include": [re.escape("C++ (.*)")],
            "exclude": [re.escape("a.b")],
        }
    }


def test_parse_instructions_sanitizes_companies() -> None:
    engine = FakeAsyncEngine(
        [_answer({"companies": ["  Acme\x00 ", "\x1b", " "], "countries": ["ID"]})]
    )

    parsed = _parse("Not Acme", engine)

    assert parsed.filters == {"companies": ["Acme"], "countries": ["ID"]}


def test_parse_instructions_blank_companies_alone_are_nothing() -> None:
    engine = FakeAsyncEngine([_answer({"companies": ["\x00", "  "]})])

    with pytest.raises(NothingActionableError):
        _parse("hello", engine)


def test_parse_instructions_filters_are_read_only() -> None:
    engine = FakeAsyncEngine([_answer({"countries": ["ID"]})])

    parsed = _parse("Indonesia", engine)

    assert isinstance(parsed.filters, MappingProxyType)


def test_parse_instructions_nothing_actionable() -> None:
    engine = FakeAsyncEngine([_answer(rules=["\x00", "  "])])

    with pytest.raises(NothingActionableError):
        _parse("hello", engine)


def test_parse_instructions_parse_failure_charged() -> None:
    engine = FakeAsyncEngine([JUNK], cost=0.002)
    budget = Budget(1.0)

    with pytest.raises(TaskOutputError) as raised:
        _parse("Remote only", engine, budget)

    assert raised.value.task == "instructions"
    assert budget.spent == pytest.approx(0.002)
