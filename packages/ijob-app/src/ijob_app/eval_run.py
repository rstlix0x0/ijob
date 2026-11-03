"""What `ijob eval run` does before and during a run of one suite.

`plan_run` works out a run before any call: the model under test, the grader for
`reasons`, the cases, and the model calls whose cost is to be estimated. A
`RunOffer` adds the cap and the estimate. `run_plan` runs the plan with the suite's
runner, and a `RunProgress` says how far it has got.

`hostile` sends no personal data; `RunPlan.sends_personal_data` says which plans do.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from ijob_ai.llm.reference import ModelRef
from ijob_ai.llm.settings import EvalSettings, LlmSettings
from ijob_core.errors import InvalidValueError
from ijob_core.store import Store

from ijob_app.eval_estimate import CallKind, Cost, PlannedCall
from ijob_app.eval_spend import EvalSpend
from ijob_app.eval_suites import (
    ReasonsPlan,
    Suite,
    SuitePlan,
    SuiteReport,
    prepare_reasons,
    prepare_suite,
    resolve_model,
    run_hostile_suite,
    run_judge_suite,
    run_reasons_suite,
    run_scorer_suite,
)
from ijob_app.llm_ops import ItemOutcome, LlmRuntime


@dataclass(frozen=True, slots=True)
class RunPlan:
    """What a run of one suite will do, worked out before any call.

    Attributes:
        suite: The suite to run.
        model: The model under test.
        work: The suite's plan: a `SuitePlan` for `judge` and `scorer`, a
            `ReasonsPlan` for `reasons`, and `None` for `hostile`, whose data is
            committed.
        cases: How many cases the run covers.
        calls: The model calls the run makes, to be priced.
    """

    suite: Suite
    model: ModelRef
    work: SuitePlan | ReasonsPlan | None
    cases: int
    calls: tuple[PlannedCall, ...]

    @property
    def callers(self) -> tuple[ModelRef, ...]:
        """The models the run calls, each once, in the order of the calls."""
        return tuple(dict.fromkeys(call.ref for call in self.calls))

    @property
    def sends_personal_data(self) -> bool:
        """Whether the calls carry the user's rules, CV profile and postings."""
        return self.suite is not Suite.HOSTILE


@dataclass(frozen=True, slots=True)
class RunOffer:
    """What a run will do and cost, worked out before any model is called.

    Attributes:
        plan: What the run will do.
        settings: The eval settings the run reads its caps from.
        cap: What the run may spend on OpenRouter, in US dollars.
        costs: Each model called with the cost estimated for its calls.
        estimate: The estimated cost of the whole run.
    """

    plan: RunPlan
    settings: EvalSettings
    cap: float
    costs: tuple[tuple[ModelRef, Cost], ...]
    estimate: Cost


@dataclass(frozen=True, slots=True, kw_only=True)
class RunProgress:
    """How far a run has got.

    Attributes:
        done: Cases that have ended, with a result or without.
        stored: Cases that have ended with a result stored.
        total: Cases in the run.
        spent: OpenRouter spend of the run so far, in US dollars.
    """

    done: int
    stored: int
    total: int
    spent: float


def _mean_characters(sizes: list[int]) -> float:
    """Give the mean of some sizes."""
    return sum(sizes) / len(sizes)


def _labelled_calls(plan: SuitePlan) -> tuple[PlannedCall, ...]:
    """List the calls of a `judge` or `scorer` run: one per case."""
    from ijob_ai.tasks.text import posting_block  # noqa: PLC0415

    if not plan.cases:
        return ()
    kind = CallKind.JUDGE if plan.suite is Suite.JUDGE else CallKind.SCORER
    sizes = [len(posting_block(case.posting)) for case in plan.cases]
    return (PlannedCall(plan.model, kind, len(sizes), _mean_characters(sizes)),)


def _reasons_calls(plan: ReasonsPlan) -> tuple[PlannedCall, ...]:
    """List the calls of a `reasons` run: one grader call per reason."""
    if not plan.items:
        return ()
    sizes = [len(item.input_text) for item in plan.items]
    return (
        PlannedCall(plan.grader, CallKind.GRADER, len(sizes), _mean_characters(sizes)),
    )


def _hostile_calls(model: ModelRef) -> tuple[tuple[PlannedCall, ...], int]:
    """List the calls of a `hostile` run, and its pairs: a clean and a hostile each."""
    from ijob_ai.evals.hostile import load_hostile  # noqa: PLC0415

    data = load_hostile()
    calls = tuple(
        PlannedCall(
            model,
            kind,
            2 * len(pairs),
            _mean_characters([len(pair.hostile_description) for pair in pairs]),
        )
        for kind, pairs in (
            (CallKind.JUDGE, data.judge),
            (CallKind.SCORER, data.scorer),
        )
    )
    return calls, len(data.judge) + len(data.scorer)


def plan_run(  # noqa: PLR0913  # the plan's inputs, one each
    store: Store,
    llm: LlmSettings,
    eval_settings: EvalSettings,
    suite: Suite,
    *,
    model: ModelRef | None,
    limit: int | None,
) -> RunPlan:
    """Work out a run of one suite before any call.

    Args:
        store: The open store.
        llm: The model settings.
        eval_settings: The eval settings, whose jury identifies the label set.
        suite: The suite to run.
        model: The model under test, or `None` for the suite's default.
        limit: Cover only this many cases; all when `None`.

    Returns:
        The plan, with the calls to price.

    Raises:
        InvalidValueError: `limit` is out of range for the suite, or given to
            `hostile`, or the model is on the set's jury or is the grader.
        MissingModelError: No model or grader is given or configured.
        EvalNeedsError: A label set, labels or a run to grade is missing.
        sqlite3.Error: The store cannot be read.
    """
    if suite is Suite.HOSTILE:
        if limit is not None:
            raise InvalidValueError(
                "limit", "hostile runs every pair and takes no limit"
            )
        chosen = resolve_model(llm, suite, model)
        calls, pairs = _hostile_calls(chosen)
        return RunPlan(suite, chosen, None, pairs, calls)
    if suite is Suite.REASONS:
        reasons = prepare_reasons(store, llm, eval_settings, model=model, limit=limit)
        return RunPlan(
            suite, reasons.model, reasons, len(reasons.items), _reasons_calls(reasons)
        )
    labelled = prepare_suite(store, llm, eval_settings, suite, model=model, limit=limit)
    return RunPlan(
        suite, labelled.model, labelled, len(labelled.cases), _labelled_calls(labelled)
    )


async def run_plan(  # noqa: PLR0913  # the run's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    plan: RunPlan,
    spend: EvalSpend,
    *,
    now: Callable[[], datetime],
    on_outcome: Callable[[ItemOutcome[object, object]], None] | None = None,
) -> SuiteReport:
    """Run a plan with its suite's runner.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        plan: The plan from `plan_run`.
        spend: The command's spending; it must not have begun a run.
        now: The wall clock, returning timezone-aware times.
        on_outcome: Called, on the event loop, with each case's outcome.

    Returns:
        What the run did.

    Raises:
        asyncio.CancelledError: The call was cancelled; the results already stored
            and the run row stand.
        ExceptionGroup: A call raised something other than an `LlmError`.
        sqlite3.Error: The store cannot be written.
    """
    match plan.work:
        case ReasonsPlan() as reasons:
            return await run_reasons_suite(
                runtime, store, reasons, spend, now=now, on_outcome=on_outcome
            )
        case SuitePlan() as labelled:
            runner = (
                run_judge_suite if labelled.suite is Suite.JUDGE else run_scorer_suite
            )
            return await runner(
                runtime, store, labelled, spend, now=now, on_outcome=on_outcome
            )
        case None:
            return await run_hostile_suite(
                runtime, store, plan.model, spend, now=now, on_outcome=on_outcome
            )
        case _:
            assert_never(plan.work)
