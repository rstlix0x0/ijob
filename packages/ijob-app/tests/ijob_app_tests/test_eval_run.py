from collections.abc import Iterator
from pathlib import Path

import pytest
from ijob_ai.evals.hostile import load_hostile
from ijob_app.eval_estimate import CallKind
from ijob_app.eval_run import _hostile_calls, plan_run
from ijob_app.eval_suites import ReasonsPlan, Suite, SuitePlan
from ijob_core.errors import InvalidValueError
from ijob_core.store import Store

from .test_eval_suites import (
    _A,
    _B,
    _GRADER,
    _UNDER_TEST,
    _labelled,
    _llm,
    _prior_runs,
    _settings,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "ijob.db") as opened:
        yield opened


def test_hostile_calls_are_a_clean_and_a_hostile_call_per_pair() -> None:
    data = load_hostile()

    calls, pairs = _hostile_calls(_UNDER_TEST)

    assert pairs == len(data.judge) + len(data.scorer)
    assert [(call.kind, call.count) for call in calls] == [
        (CallKind.JUDGE, 2 * len(data.judge)),
        (CallKind.SCORER, 2 * len(data.scorer)),
    ]
    assert {call.ref for call in calls} == {_UNDER_TEST}


def test_hostile_plan_has_no_work_and_sends_no_personal_data(store: Store) -> None:
    plan = plan_run(
        store,
        _llm(judge=_UNDER_TEST),
        _settings(_A, _B),
        Suite.HOSTILE,
        model=None,
        limit=None,
    )

    assert plan.work is None
    assert plan.model == _UNDER_TEST
    assert plan.cases == sum(call.count for call in plan.calls) // 2
    assert plan.callers == (_UNDER_TEST,)
    assert plan.sends_personal_data is False


def test_hostile_plan_takes_no_limit(store: Store) -> None:
    with pytest.raises(InvalidValueError, match="limit"):
        plan_run(
            store,
            _llm(judge=_UNDER_TEST),
            _settings(_A, _B),
            Suite.HOSTILE,
            model=None,
            limit=3,
        )


def test_judge_plan_prices_one_call_per_case(store: Store) -> None:
    _labelled(store, 12)

    plan = plan_run(
        store,
        _llm(judge=_UNDER_TEST),
        _settings(_A, _B),
        Suite.JUDGE,
        model=None,
        limit=None,
    )

    assert isinstance(plan.work, SuitePlan)
    assert plan.cases == len(plan.work.cases) == 12
    assert [(call.ref, call.kind, call.count) for call in plan.calls] == [
        (_UNDER_TEST, CallKind.JUDGE, 12)
    ]
    assert plan.sends_personal_data is True


def test_reasons_plan_prices_one_grader_call_per_reason(store: Store) -> None:
    eval_set, postings = _labelled(store, 12)
    _prior_runs(store, eval_set, postings)

    plan = plan_run(
        store,
        _llm(judge=_UNDER_TEST, eval_grader=_GRADER),
        _settings(_A, _B),
        Suite.REASONS,
        model=None,
        limit=4,
    )

    assert isinstance(plan.work, ReasonsPlan)
    assert plan.cases == len(plan.work.items) == 4
    assert [(call.ref, call.kind, call.count) for call in plan.calls] == [
        (_GRADER, CallKind.GRADER, 4)
    ]
    assert plan.callers == (_GRADER,)
