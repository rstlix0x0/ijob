import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from ijob_ai.evals.agent_cases import load_hostile_pages
from ijob_ai.evals.hostile import load_hostile
from ijob_app.eval_agent_suites import ExplorePlan, SourcePlan
from ijob_app.eval_estimate import CallKind
from ijob_app.eval_run import _hostile_calls, plan_run, run_plan
from ijob_app.eval_spend import EvalSpend
from ijob_app.eval_suites import ReasonsPlan, Suite, SuitePlan
from ijob_app.home import IjobHome
from ijob_app.llm_ops import LlmOptions, LlmRuntime
from ijob_core.errors import InvalidValueError
from ijob_core.store import Store

from .eval_fakes import JurorEngines
from .llm_fakes import MADE_UP_CLAUDE, write_fake_cli, write_llm_config
from .test_eval_suites import (
    _A,
    _B,
    _GRADER,
    _ROUTED,
    _UNDER_TEST,
    _labelled,
    _llm,
    _now,
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


def test_source_agent_plan_covers_the_companies_and_the_hostile_pages(
    store: Store,
) -> None:
    plan = plan_run(
        store,
        _llm(source_agent=_UNDER_TEST, source_recipe=_ROUTED),
        _settings(),
        Suite.SOURCE_AGENT,
        model=None,
        limit=2,
    )

    assert isinstance(plan.work, SourcePlan)
    assert plan.model == _UNDER_TEST
    assert len(plan.work.cases) == 2
    assert plan.work.limited is True
    assert plan.cases == 2 + len(load_hostile_pages())
    assert [(call.ref, call.kind) for call in plan.calls] == [
        (_UNDER_TEST, CallKind.AGENT),
        (_ROUTED, CallKind.AGENT),
    ]
    assert plan.callers == (_UNDER_TEST, _ROUTED)


def test_explore_agent_plan_names_the_grader_among_its_callers(store: Store) -> None:
    plan = plan_run(
        store,
        _llm(explore_agent=_UNDER_TEST, eval_grader=_GRADER),
        _settings(),
        Suite.EXPLORE_AGENT,
        model=None,
        limit=None,
    )

    assert isinstance(plan.work, ExplorePlan)
    assert plan.cases == len(plan.work.scenarios) == 5
    assert plan.work.limited is False
    assert plan.callers == (_UNDER_TEST, _GRADER)
    assert {call.kind for call in plan.calls} == {CallKind.AGENT}


@pytest.mark.parametrize("suite", [Suite.SOURCE_AGENT, Suite.EXPLORE_AGENT])
def test_agent_plans_use_no_labels_and_send_no_personal_data(
    store: Store, suite: Suite
) -> None:
    plan = plan_run(
        store,
        _llm(source_agent=_UNDER_TEST, explore_agent=_UNDER_TEST, eval_grader=_GRADER),
        _settings(_UNDER_TEST),
        suite,
        model=None,
        limit=1,
    )

    assert plan.sends_personal_data is False


@pytest.mark.parametrize(
    ("suite", "limit"),
    [
        (Suite.SOURCE_AGENT, 0),
        (Suite.SOURCE_AGENT, 151),
        (Suite.EXPLORE_AGENT, 0),
        (Suite.EXPLORE_AGENT, 6),
    ],
)
def test_agent_plan_limit_out_of_range_is_refused(
    store: Store, suite: Suite, limit: int
) -> None:
    llm = _llm(source_agent=_UNDER_TEST, explore_agent=_UNDER_TEST, eval_grader=_GRADER)

    with pytest.raises(InvalidValueError, match="limit"):
        plan_run(store, llm, _settings(), suite, model=None, limit=limit)


def test_agent_plan_limits_are_accepted_at_the_edges(store: Store) -> None:
    llm = _llm(source_agent=_UNDER_TEST, explore_agent=_UNDER_TEST, eval_grader=_GRADER)

    source = plan_run(
        store, llm, _settings(), Suite.SOURCE_AGENT, model=None, limit=150
    )
    explore = plan_run(
        store, llm, _settings(), Suite.EXPLORE_AGENT, model=None, limit=5
    )

    assert isinstance(source.work, SourcePlan)
    assert len(source.work.cases) == 150
    assert explore.cases == 5


@pytest.fixture
def runtime(home: IjobHome, tmp_path: Path) -> LlmRuntime:
    cli = write_fake_cli(tmp_path / "bin")
    write_llm_config(home.config, judge=MADE_UP_CLAUDE, cli_path=cli.path)
    options = LlmOptions(config_path=home.config, engines=JurorEngines())
    return LlmRuntime.create(home, options)


def test_run_plan_leaves_an_agent_plan_to_run_agent_plan(
    store: Store, runtime: LlmRuntime
) -> None:
    plan = plan_run(
        store,
        _llm(source_agent=_UNDER_TEST),
        _settings(),
        Suite.SOURCE_AGENT,
        model=None,
        limit=1,
    )

    with pytest.raises(InvalidValueError, match="source-agent"):
        asyncio.run(
            run_plan(
                runtime, store, plan, EvalSpend(store.evals, _settings()), now=_now
            )
        )

    assert store.evals.runs() == []
