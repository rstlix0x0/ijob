from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ijob_ai.errors import LlmError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import EvalSettings
from ijob_app.eval_spend import EvalBudgetSpentError, EvalSpend
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import RunStatus
from ijob_core.store import Store

_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_OPENROUTER = ModelRef(Provider.OPENROUTER, "made-up/model")
_CLAUDE = ModelRef(Provider.CLAUDE_SDK, "made-up-model")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "ijob.db") as opened:
        yield opened


def _record_earlier_spend(store: Store, spent: float) -> None:
    run = store.evals.start_run("judge", "made-up", "judge-1", None, started_at=_AT)
    store.evals.record_spend(run.id, spent_usd=spent, subscription_usd=0.0)


def _spend(store: Store, *, max_cost: float = 4, total_cost: float = 10) -> EvalSpend:
    settings = EvalSettings(max_cost_usd=max_cost, total_cost_usd=total_cost)
    return EvalSpend(store.evals, settings)


def test_cap_is_the_smaller_limit(store: Store) -> None:
    _record_earlier_spend(store, 7.5)
    assert _spend(store).cap == pytest.approx(2.5)


def test_cap_is_the_command_limit_when_the_total_is_far(store: Store) -> None:
    _record_earlier_spend(store, 1)
    assert _spend(store).cap == pytest.approx(4)


def test_cap_shrinks_as_the_command_spends(store: Store) -> None:
    _record_earlier_spend(store, 7.5)
    spend = _spend(store)

    spend.budget_for(_OPENROUTER, first := Budget(spend.cap)).charge(2.0)
    assert spend.cap == pytest.approx(0.5)  # only the remainder of the 2.50 limit
    spend.budget_for(_OPENROUTER, second := Budget(spend.cap)).charge(0.5)

    assert first.spent + second.spent == pytest.approx(2.5)
    assert spend.cap == 0
    assert not Budget(spend.cap).try_start()


def test_no_money_left_refuses(store: Store) -> None:
    _record_earlier_spend(store, 10)

    with pytest.raises(EvalBudgetSpentError, match=r"eval\.total_cost_usd") as raised:
        _spend(store)

    assert isinstance(raised.value, LlmError)


def test_openrouter_call_counts(store: Store) -> None:
    spend = _spend(store)
    operation = Budget(spend.cap)

    budget = spend.budget_for(_OPENROUTER, operation)
    budget.charge(0.5)

    assert budget is operation
    assert operation.spent == pytest.approx(0.5)
    assert spend.openrouter_spent == pytest.approx(0.5)
    assert spend.subscription_spent == 0


def test_subscription_call_does_not_count(store: Store) -> None:
    spend = _spend(store)
    operation = Budget(spend.cap)

    budget = spend.budget_for(_CLAUDE, operation)
    budget.charge(3.0)

    assert budget is not operation
    assert operation.spent == 0
    assert operation.try_start()
    assert spend.openrouter_spent == 0
    assert spend.subscription_spent == pytest.approx(3.0)


def test_subscription_budget_is_never_reached(store: Store) -> None:
    spend = _spend(store)

    budget = spend.budget_for(_CLAUDE, Budget(spend.cap))
    budget.charge(1_000_000.0)

    assert budget.try_start()
    assert spend.budget_for(_CLAUDE, Budget(spend.cap)) is budget


def test_begin_writes_the_run_row(store: Store) -> None:
    spend = _spend(store)

    run = spend.begin("judge", "made-up", "judge-1", None, started_at=_AT)

    stored = store.evals.run(run.id)
    assert stored is not None
    assert stored.suite == "judge"
    assert stored.status is RunStatus.INCOMPLETE  # a read shows `running` this way
    assert stored.spent_usd == 0
    assert stored.finished_at is None


def test_record_updates_spend(store: Store) -> None:
    spend = _spend(store)
    run = spend.begin("judge", "made-up", "judge-1", None, started_at=_AT)
    operation = Budget(spend.cap)

    spend.budget_for(_OPENROUTER, operation).charge(0.25)
    spend.budget_for(_CLAUDE, operation).charge(1.5)
    spend.record()

    stored = store.evals.run(run.id)
    assert stored is not None
    assert stored.spent_usd == pytest.approx(0.25)
    assert stored.subscription_usd == pytest.approx(1.5)
    assert store.evals.total_spend() == pytest.approx(0.25)


def test_record_follows_every_operation_of_the_command(store: Store) -> None:
    spend = _spend(store)
    run = spend.begin("jury", "made-up", "judge-1", None, started_at=_AT)

    spend.budget_for(_OPENROUTER, first := Budget(spend.cap)).charge(0.5)
    spend.budget_for(_OPENROUTER, second := Budget(spend.cap)).charge(0.25)
    spend.budget_for(_OPENROUTER, first).charge(0.25)
    spend.record()

    stored = store.evals.run(run.id)
    assert stored is not None
    assert stored.spent_usd == pytest.approx(1.0)
    assert second.spent == pytest.approx(0.25)


def test_finish_closes_the_run(store: Store) -> None:
    spend = _spend(store)
    run = spend.begin("judge", "made-up", "judge-1", None, started_at=_AT)
    spend.budget_for(_OPENROUTER, Budget(spend.cap)).charge(0.5)
    spend.record()

    spend.finish(RunStatus.COMPLETE, '{"accuracy": 1.0}', finished_at=_AT)

    stored = store.evals.run(run.id)
    assert stored is not None
    assert stored.status is RunStatus.COMPLETE
    assert stored.metrics == '{"accuracy": 1.0}'
    assert stored.finished_at == _AT


def test_finish_writes_the_spend_not_yet_recorded(store: Store) -> None:
    spend = _spend(store)
    run = spend.begin("judge", "made-up", "judge-1", None, started_at=_AT)
    spend.budget_for(_OPENROUTER, Budget(spend.cap)).charge(1.5)
    spend.budget_for(_CLAUDE, Budget(spend.cap)).charge(2.0)

    spend.finish(RunStatus.INCOMPLETE, "{}", finished_at=_AT)

    stored = store.evals.run(run.id)
    assert stored is not None
    assert stored.spent_usd == pytest.approx(1.5)
    assert stored.subscription_usd == pytest.approx(2.0)
    assert store.evals.total_spend() == pytest.approx(1.5)


def test_record_and_finish_need_a_begun_run(store: Store) -> None:
    spend = _spend(store)

    with pytest.raises(RuntimeError, match="begin"):
        spend.record()
    with pytest.raises(RuntimeError, match="begin"):
        spend.finish(RunStatus.COMPLETE, "{}", finished_at=_AT)


def test_begin_twice_is_refused(store: Store) -> None:
    spend = _spend(store)
    spend.begin("judge", "made-up", "judge-1", None, started_at=_AT)

    with pytest.raises(RuntimeError, match="already"):
        spend.begin("judge", "made-up", "judge-1", None, started_at=_AT)


def test_operation_cap_above_the_command_limit_is_refused(store: Store) -> None:
    spend = _spend(store, max_cost=4)

    with pytest.raises(InvalidValueError, match="cap"):
        spend.budget_for(_OPENROUTER, Budget(4.01))
    assert spend.openrouter_spent == 0


def test_operation_cap_is_checked_against_the_limit_not_what_is_left(
    store: Store,
) -> None:
    spend = _spend(store)
    first = Budget(spend.cap)
    spend.budget_for(_OPENROUTER, first).charge(3.0)

    # a worker that started before the charge still holds the full cap
    assert spend.budget_for(_OPENROUTER, first) is first
    spend.budget_for(_OPENROUTER, Budget(spend.cap))
