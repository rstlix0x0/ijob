"""The tuning grid: which model, optimizer and task need a program, and running them.

The grid is `[tune].models` times the optimizers times `judge` and `scorer`,
narrowed by the command's options. `prepare_grid` works out, before any call and
before any spend, which combinations still have to run, which are done, and what
the run would take; it refuses what could not run. `planned_calls` turns a plan
into the calls to price, from the calls each optimizer measured to make.

`run_combination` tunes one combination under the rails and, when it made a
program, saves it and tests it on the held-out postings through the normal suite.
Each combination writes one `tune` row to `eval_runs`, with its own `EvalSpend`, so
its OpenRouter spend joins the ledger as it is made. `run_grid` runs the plan's
combinations one after another, going on after a failure that is the
combination's own and stopping after one that would repeat.

This module renders nothing; `tune_view` draws what it works out. The DSPy side
of tuning loads only inside the functions that are about to tune.
"""

import asyncio
import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal

from ijob_ai.errors import CostUnknownError, ProviderUnavailableError
from ijob_ai.llm.reference import ModelRef
from ijob_ai.llm.settings import EvalSettings, LlmSettings, Task, TuneSettings
from ijob_ai.tasks.versions import JUDGE_VERSION, SCORER_VERSION
from ijob_ai.tuning.names import Optimizer
from ijob_core.eval_records import EvalLabel, EvalRun, EvalSet, EvalTask, RunStatus
from ijob_core.store import Store
from ijob_core.tune_records import SplitPart, TunedProgram

from ijob_app.eval_estimate import (
    CallEstimate,
    CallKind,
    Cost,
    PlannedCall,
    cost_by_model,
    total_cost,
)
from ijob_app.eval_ops import EvalNeedsError, stored_profile, stored_rules
from ijob_app.eval_run import plan_run, run_plan
from ijob_app.eval_spend import EvalBudgetSpentError, EvalSpend
from ijob_app.eval_suites import (
    Suite,
    SuiteReport,
    prepare_suite,
    refuse_juror,
    unchanged_posting,
)
from ijob_app.llm_ops import ItemOutcome, LlmRuntime
from ijob_app.tune_ops import ProgramStatus, full_run, program_status

if TYPE_CHECKING:
    import dspy
    from ijob_ai.tracing.setup import OperationTrace
    from ijob_ai.tuning.engine import ProgramText, TuneOutcome

TUNE_SUITE: Final = "tune"
"""The suite name of a tuning run's row."""

_DEMO_FACTOR: Final = 2.5
"""How much dearer a prompt with two demos is than one without, by the same prices."""

_TUNING_CALLS: Final[Mapping[Optimizer, int]] = {
    Optimizer.GEPA: 477 + 28,
    Optimizer.BOOTSTRAP: 240,
    Optimizer.MIPRO: 275 + 14,
}
"""The calls each optimizer measured to make, its helper calls included."""

_WITH_DEMOS: Final = frozenset({Optimizer.BOOTSTRAP, Optimizer.MIPRO})
_TASKS: Final = (EvalTask.JUDGE, EvalTask.SCORER)
_NEEDS_TUNER: Final = frozenset({Optimizer.GEPA, Optimizer.MIPRO})
_FAILED_SHARE_DIVISOR: Final = 4
"""A run is unsound when over one call in this many failed."""


def base_version(task: EvalTask) -> str:
    """Return the version of the task's current instructions."""
    return JUDGE_VERSION if task is EvalTask.JUDGE else SCORER_VERSION


@dataclass(frozen=True, slots=True)
class Combination:
    """One task, model and optimizer of the grid.

    Attributes:
        task: `judge` or `scorer`.
        model: The model to tune for.
        optimizer: The optimizer to tune with.
        retry: A saved program of this combination that has no complete held-out
            run yet; the combination then only tests it, tuning nothing.
    """

    task: EvalTask
    model: ModelRef
    optimizer: Optimizer
    retry: TunedProgram | None = None

    @property
    def suite(self) -> Suite:
        """The eval suite that runs the combination's task."""
        return Suite.JUDGE if self.task is EvalTask.JUDGE else Suite.SCORER

    @property
    def base_version(self) -> str:
        """The version of the task's current instructions."""
        return base_version(self.task)


@dataclass(frozen=True, slots=True)
class GridSizes:
    """How big the frozen split is, for pricing the grid.

    Attributes:
        train: Postings in the train part.
        val: Postings in the val part.
        held_out: Postings in the held-out part.
        characters: Mean length of a posting block over the three parts.
    """

    train: int
    val: int
    held_out: int
    characters: float


@dataclass(frozen=True, slots=True)
class GridPlan:
    """What a grid will do, worked out before any call.

    Attributes:
        eval_set: The label set the grid tunes on.
        combinations: The combinations to run, model by model, optimizer by
            optimizer, judge then scorer.
        done: The combinations left out because a complete row already covers
            them and nothing is left to test.
        tuner: The model that reflects and proposes, or `None` when no
            combination to run tunes with an optimizer that needs one.
        sizes: The sizes of the frozen split.
        settings: The eval settings the runs read their caps from.
        tune: The tuning settings.
    """

    eval_set: EvalSet
    combinations: tuple[Combination, ...]
    done: tuple[Combination, ...]
    tuner: ModelRef | None
    sizes: GridSizes
    settings: EvalSettings
    tune: TuneSettings


@dataclass(frozen=True, slots=True)
class TuneRow:
    """A `tune` run row with the numbers it holds.

    Attributes:
        run: The run row.
        task: The task tuned.
        optimizer: The optimizer used.
        calls: Model calls let through.
        failures: Calls that failed.
        unusable: Replies that could not be read.
        baseline_val: The untuned model's score on the val part, or `None`.
        best_val: The tuned program's score on the val part, or `None` when no
            program came of the run.
        no_change: Whether the run was sound and left the base program as it was.
        program_id: The saved program, or `None`.
    """

    run: EvalRun
    task: EvalTask
    optimizer: Optimizer
    calls: int
    failures: int
    unusable: int
    baseline_val: float | None
    best_val: float | None
    no_change: bool
    program_id: int | None


def _number(value: object) -> float | None:
    """Read a stored number, or `None` when it is absent or not one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _tune_row(run: EvalRun) -> TuneRow | None:
    """Read a `tune` run's metrics, or `None` when the run holds none to read."""
    try:
        metrics = json.loads(run.metrics)
        program_id = metrics["program_id"]
        return TuneRow(
            run=run,
            task=EvalTask(metrics["task"]),
            optimizer=Optimizer(metrics["optimizer"]),
            calls=int(metrics["calls"]),
            failures=int(metrics["failures"]),
            unusable=int(metrics["unusable"]),
            baseline_val=_number(metrics["baseline_val"]),
            best_val=_number(metrics["best_val"]),
            no_change=bool(metrics["no_change"]),
            program_id=None if program_id is None else int(program_id),
        )
    except (ValueError, KeyError, TypeError):
        return None


def tune_rows(store: Store, *, set_id: int | None = None) -> list[TuneRow]:
    """List the `tune` run rows, oldest first.

    A row whose metrics cannot be read, such as one left by a process that was
    killed before it could close the row, is left out.

    Args:
        store: The open store.
        set_id: Keep only the rows of this label set; all when `None`.

    Returns:
        The rows.

    Raises:
        sqlite3.Error: The store cannot be read.
    """
    rows = (
        _tune_row(run)
        for run in store.evals.runs()
        if run.suite == TUNE_SUITE and (set_id is None or run.set_id == set_id)
    )
    return [row for row in rows if row is not None]


def _covers(row: TuneRow, eval_set: EvalSet, combination: Combination) -> bool:
    """Say whether a row is a complete run of the combination on this set."""
    return (
        row.run.status is RunStatus.COMPLETE
        and row.run.set_id == eval_set.id
        and row.run.model == str(combination.model)
        and row.run.task_version == combination.base_version
        and row.task is combination.task
        and row.optimizer is combination.optimizer
    )


def combination_done(store: Store, eval_set: EvalSet, combination: Combination) -> bool:
    """Say whether a complete `tune` row covers the combination, `no change` included.

    Args:
        store: The open store.
        eval_set: The label set.
        combination: The combination to look for.

    Returns:
        Whether a complete row for its task, model, optimizer, base version and
        set exists.

    Raises:
        sqlite3.Error: The store cannot be read.
    """
    return any(
        _covers(row, eval_set, combination)
        for row in tune_rows(store, set_id=eval_set.id)
    )


def tuning_equivalents(optimizer: Optimizer, *, val: int) -> float:
    """Count the calls tuning makes, as calls of a prompt without demos.

    The optimizer's measured calls and the val pass of the program it returns,
    each dearer by the demo factor for an optimizer that gathers demos.

    Args:
        optimizer: The optimizer.
        val: Postings in the val part.

    Returns:
        The call-equivalents.
    """
    return _factor(optimizer) * (_TUNING_CALLS[optimizer] + val)


def held_out_equivalents(optimizer: Optimizer, *, held_out: int) -> float:
    """Count the calls testing a program on the held-out part makes.

    Args:
        optimizer: The optimizer that made the program.
        held_out: Postings in the held-out part.

    Returns:
        The call-equivalents.
    """
    return _factor(optimizer) * held_out


def _factor(optimizer: Optimizer) -> float:
    """Return how dear a call of the optimizer's program is against a plain one."""
    return _DEMO_FACTOR if optimizer in _WITH_DEMOS else 1.0


def planned_calls(plan: GridPlan) -> tuple[PlannedCall, ...]:
    """List the calls of a plan to price: one entry per model and task.

    A combination that tunes is priced for its tuning and its held-out test, one
    that only retries a program for its test alone. The tuner's calls are on the
    subscription and not priced.

    Args:
        plan: The plan.

    Returns:
        The calls, judge before scorer for each model, models in plan order.
    """
    totals: defaultdict[tuple[ModelRef, CallKind], float] = defaultdict(float)
    for each in plan.combinations:
        kind = CallKind.JUDGE if each.task is EvalTask.JUDGE else CallKind.SCORER
        tuning = (
            0.0
            if each.retry is not None
            else tuning_equivalents(each.optimizer, val=plan.sizes.val)
        )
        totals[each.model, kind] += tuning + held_out_equivalents(
            each.optimizer, held_out=plan.sizes.held_out
        )
    models = dict.fromkeys(each.model for each in plan.combinations)
    return tuple(
        PlannedCall(model, kind, math.ceil(totals[model, kind]), plan.sizes.characters)
        for model in models
        for kind in (CallKind.JUDGE, CallKind.SCORER)
        if (model, kind) in totals
    )


def _sizes(store: Store, eval_set: EvalSet) -> GridSizes:
    """Count the parts of the set's frozen split and measure its postings."""
    from ijob_ai.tasks.text import posting_block  # noqa: PLC0415

    rows = store.tunes.split(eval_set.id)
    lengths = [
        len(posting_block(posting))
        for row in rows
        if (posting := store.posting(row.posting_id)) is not None
    ]
    counts = {part: sum(row.part is part for row in rows) for part in SplitPart}
    return GridSizes(
        train=counts[SplitPart.TRAIN],
        val=counts[SplitPart.VAL],
        held_out=counts[SplitPart.HELD_OUT],
        characters=sum(lengths) / len(lengths) if lengths else 0.0,
    )


def _no_models() -> EvalNeedsError:
    """Build the refusal for a config that lists no model to tune."""
    return EvalNeedsError(
        "no models are chosen to tune",
        "read `ijob tune report`, then list the models as tune.models under "
        "[tune] in the config",
    )


def _no_split() -> EvalNeedsError:
    """Build the refusal for a label set whose split is not frozen."""
    return EvalNeedsError(
        "the label set has no frozen tuning split", "run `ijob tune screen`"
    )


def _nothing_to_learn_from() -> EvalNeedsError:
    """Build the refusal for a train or val part with no unchanged posting left."""
    return EvalNeedsError(
        "the train or val part has no posting left whose text is as drawn",
        "run `ijob eval label` to draw a new set",
    )


def _screen_hint(model: ModelRef, task: EvalTask) -> EvalNeedsError:
    """Build the refusal for a model with no full run of a task."""
    return EvalNeedsError(
        f"{model} has no full {task.value} run; `tune screen` measures "
        "tune.candidates, tune.baseline and the configured models",
        "run `ijob tune screen`",
    )


def _planned(  # noqa: PLR0913  # the grid's axes, one each
    store: Store,
    llm: LlmSettings,
    eval_set: EvalSet,
    *,
    models: Sequence[ModelRef],
    optimizers: Sequence[Optimizer],
    tasks: Sequence[EvalTask],
    again: bool,
) -> tuple[list[Combination], list[Combination]]:
    """Split the grid into the combinations to run and the ones already done."""
    rows = tune_rows(store, set_id=eval_set.id)
    todo: list[Combination] = []
    done: list[Combination] = []
    for model in models:
        for optimizer in optimizers:
            for task in tasks:
                each = Combination(task, model, optimizer)
                covering = [row for row in rows if _covers(row, eval_set, each)]
                if again or not covering:
                    todo.append(each)
                    continue
                untested = _untested_programs(store, llm, covering)
                if untested:
                    todo.extend(
                        Combination(task, model, optimizer, retry=program)
                        for program in untested
                    )
                else:
                    done.append(each)
    return todo, done


def _untested_programs(
    store: Store, llm: LlmSettings, covering: Sequence[TuneRow]
) -> list[TunedProgram]:
    """List the programs the rows saved that have no complete held-out run.

    Every program counts, not only the newest, and one that several rows share
    counts once; the oldest comes first.
    """
    saved = dict.fromkeys(
        row.program_id for row in covering if row.program_id is not None
    )
    programs = (store.tunes.program(program_id) for program_id in saved)
    return [
        program
        for program in programs
        if program is not None and not program_status(store, llm, program).tested
    ]


def prepare_grid(  # noqa: PLR0913  # the plan's inputs, one each
    store: Store,
    llm: LlmSettings,
    eval_settings: EvalSettings,
    tune: TuneSettings,
    *,
    model: ModelRef | None = None,
    optimizer: Optimizer | None = None,
    task: EvalTask | None = None,
    again: bool = False,
) -> GridPlan:
    """Work out which combinations of the grid still have to run.

    The grid is `[tune].models` times the three optimizers times `judge` and
    `scorer`; `model`, `optimizer` and `task` narrow it, and a `model` outside
    `[tune].models` is allowed. A combination is done when a complete `tune` row
    covers its task, model, optimizer, base version and set, `no change`
    included; `again` plans it anyway. Each program a done combination saved that
    has no complete held-out run is planned as a retry of the test alone, the
    older ones too. Everything the grid could not run is refused here, before any
    call and before anything is written.

    Args:
        store: The open store.
        llm: The model settings, whose `[models].tuner` GEPA and MIPROv2 need.
        eval_settings: The eval settings, whose jury identifies the label set.
        tune: The tuning settings.
        model: Run only this model, whether or not `[tune].models` lists it.
        optimizer: Run only this optimizer.
        task: Run only this task.
        again: Plan combinations that are done as well.

    Returns:
        The plan.

    Raises:
        EvalNeedsError: `[tune].models` is empty, the label set has no frozen
            split, a model has no full run of a task, or the jury, rules, profile
            or labels are missing or stale.
        InvalidValueError: A model is a juror of the label set.
        MissingModelError: GEPA or MIPROv2 would tune and `[models].tuner` is
            not set.
        sqlite3.Error: The store cannot be read.
    """
    if not tune.models:
        raise _no_models()
    models = [model] if model is not None else list(dict.fromkeys(tune.models))
    for each in models:
        refuse_juror(store, eval_settings, each)
    eval_set = prepare_suite(
        store, llm, eval_settings, Suite.JUDGE, model=models[0], limit=None
    ).eval_set
    if not store.tunes.has_split(eval_set.id):
        raise _no_split()
    tasks = [task] if task is not None else list(_TASKS)
    for each in models:
        for chosen in tasks:
            if full_run(store, chosen, each, eval_set) is None:
                raise _screen_hint(each, chosen)
    optimizers = [optimizer] if optimizer is not None else list(Optimizer)
    todo, done = _planned(
        store,
        llm,
        eval_set,
        models=models,
        optimizers=optimizers,
        tasks=tasks,
        again=again,
    )
    needs_tuner = any(
        each.retry is None and each.optimizer in _NEEDS_TUNER for each in todo
    )
    return GridPlan(
        eval_set=eval_set,
        combinations=tuple(todo),
        done=tuple(done),
        tuner=llm.model_for(Task.TUNER) if needs_tuner else None,
        sizes=_sizes(store, eval_set),
        settings=eval_settings,
        tune=tune,
    )


@dataclass(frozen=True, slots=True)
class TuningExamples:
    """The examples a tuning run learns from and chooses on.

    Attributes:
        train: The train part's examples, each with its inputs marked and a `label`.
        val: The val part's examples, in the same form.
        changed: Postings of the two parts left out because their text changed
            since the draw or they are gone.
    """

    train: tuple[Any, ...]
    val: tuple[Any, ...]
    changed: int


def tuning_examples(store: Store, eval_set: EvalSet, task: EvalTask) -> TuningExamples:
    """Build the train and val examples of a task from the frozen split.

    The inputs are the ones the task sends, with the posting's field-marker form
    broken, since a demo holds the posting text and reaches prompts. Each label is
    the decided answer: `keep` for the judge, the score for the scorer. Only the
    train and val parts are read; no other posting reaches an optimizer.

    Args:
        store: The open store.
        eval_set: The label set, whose split is frozen.
        task: `judge` or `scorer`.

    Returns:
        The examples, with the number of postings left out.

    Raises:
        EvalNeedsError: No rules or CV profile is stored, or the train or val part
            has no unchanged posting left.
        sqlite3.Error: The store cannot be read.
    """
    import dspy  # noqa: PLC0415
    from ijob_ai.tasks.text import (  # noqa: PLC0415
        neutralize_markers,
        numbered_rules,
        posting_block,
    )

    judge = task is EvalTask.JUDGE
    shared = (
        numbered_rules(stored_rules(store).rules)
        if judge
        else stored_profile(store).to_json()
    )
    labels = {
        label.posting_id: label for label in store.evals.labels(eval_set.id, task)
    }
    cases = {case.posting_id: case for case in store.evals.cases(eval_set.id)}
    parts: dict[SplitPart, list[Any]] = {SplitPart.TRAIN: [], SplitPart.VAL: []}
    changed = 0
    for row in store.tunes.split(eval_set.id):
        if row.part not in parts:
            continue
        case, label = cases.get(row.posting_id), labels.get(row.posting_id)
        posting = None if case is None else unchanged_posting(store, case)
        if posting is None or label is None:
            changed += 1
            continue
        text = neutralize_markers(posting_block(posting))
        parts[row.part].append(
            dspy.Example(
                rules=shared, posting=text, label=label.keep is True
            ).with_inputs("rules", "posting")
            if judge
            else dspy.Example(
                posting=text, profile=shared, label=label.score or 0
            ).with_inputs("posting", "profile")
        )
    if not parts[SplitPart.TRAIN] or not parts[SplitPart.VAL]:
        raise _nothing_to_learn_from()
    return TuningExamples(
        tuple(parts[SplitPart.TRAIN]), tuple(parts[SplitPart.VAL]), changed
    )


def _stored_score(task: EvalTask, answer: str, label: EvalLabel) -> float:
    """Score a stored answer with the tuning metric; an unusable answer scores 0."""
    from ijob_ai.tasks.outputs import ScoreOut, VerdictOut  # noqa: PLC0415
    from ijob_ai.tuning.metrics import judge_score, scorer_score  # noqa: PLC0415

    try:
        stored = json.loads(answer)
    except ValueError:
        return 0.0
    if not isinstance(stored, dict):
        return 0.0
    if task is EvalTask.JUDGE:
        keep = stored.get("keep")
        if not isinstance(keep, bool):
            return 0.0
        verdict = VerdictOut.model_construct(keep=keep, reason="", rule=None)
        return judge_score(verdict, label.keep is True)
    score = stored.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        return 0.0
    return scorer_score(
        ScoreOut.model_construct(score=score, reasons=[]), label.score or 0
    )


def baseline_val(
    store: Store, eval_set: EvalSet, task: EvalTask, model: ModelRef
) -> float | None:
    """Score the untuned model on the val part from its stored full run.

    Each val posting is scored with the tuning metric from the answer the full run
    stored for it, so no model is called and nothing is spent. A posting with no
    stored answer, such as one whose text changed, is left out.

    Args:
        store: The open store.
        eval_set: The label set.
        task: `judge` or `scorer`.
        model: The untuned model.

    Returns:
        The mean score, or `None` when the model has no full run or no val
        posting has a stored answer.

    Raises:
        sqlite3.Error: The store cannot be read.
    """
    run = full_run(store, task, model, eval_set)
    if run is None:
        return None
    answers = {result.case_key: result.answer for result in store.evals.results(run.id)}
    labels = {
        label.posting_id: label for label in store.evals.labels(eval_set.id, task)
    }
    scores = [
        _stored_score(task, answers[str(row.posting_id)], labels[row.posting_id])
        for row in store.tunes.split(eval_set.id)
        if row.part is SplitPart.VAL
        and str(row.posting_id) in answers
        and row.posting_id in labels
    ]
    return sum(scores) / len(scores) if scores else None


type Phase = Literal["tune", "test"]
"""What a combination is doing: tuning, or testing a program on the held-out part."""

INTERRUPTED: Final = "interrupted"
"""The reason a grid gives when the user ended it."""


class CombinationState(StrEnum):
    """How a combination stands after the grid has been through it."""

    DONE = "done"
    TUNED = "tuned"
    NO_CHANGE = "no change"
    INCOMPLETE = "incomplete"
    NOT_RUN = "not run"


@dataclass(frozen=True, slots=True)
class TuneProgress:
    """How far a combination has got.

    Attributes:
        phase: `tune` while the optimizer and the val pass call the model, `test`
            while the program runs on the held-out part.
        calls: Model calls made in the phase; for `test`, cases ended.
        failed: Calls that failed; for `test`, cases that ended with no result.
        spent: What the phase has spent on both providers, in US dollars.
        openrouter_spent: OpenRouter spend of the command so far, in US dollars.
    """

    phase: Phase
    calls: int
    failed: int
    spent: float
    openrouter_spent: float


@dataclass(frozen=True, slots=True, kw_only=True)
class CombinationResult:
    """How one combination ended.

    Attributes:
        combination: The combination.
        state: `tuned`, `no change` or `incomplete`.
        reason: Why it is incomplete, or what is left to do; `None` otherwise. It
            can hold text a provider sent, so clean it before showing it.
        run_id: The `tune` row written, or `None` for a retry, which tunes nothing.
        program_id: The program saved, or `None`.
        held_out_run_id: The held-out run written for the program, or `None`.
        calls: Model calls let through.
        failures: Calls that failed.
        unusable: Replies no adapter form could read.
        baseline_val: The untuned model's score on the val part, or `None`.
        best_val: The tuned program's score on the val part, or `None`.
        run_spent: What the run budget was charged, both providers, in US dollars.
        changed: Train and val postings left out because their text changed.
        stop: Why the grid must stop after this combination, or `None`.
        interrupted: Whether the user ended the combination.
    """

    combination: Combination
    state: CombinationState
    reason: str | None = None
    run_id: int | None = None
    program_id: int | None = None
    held_out_run_id: int | None = None
    calls: int = 0
    failures: int = 0
    unusable: int = 0
    baseline_val: float | None = None
    best_val: float | None = None
    run_spent: float = 0.0
    changed: int = 0
    stop: str | None = None
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class _Tested:
    """What the held-out test of a program came to.

    Attributes:
        report: The held-out run, or `None` when none was made.
        reason: Why the test did not finish, or `None`.
        stop: Why the grid must stop, or `None`.
    """

    report: SuiteReport | None
    reason: str | None
    stop: str | None


def _held_out_reason(program: TunedProgram, report: SuiteReport) -> str:
    """Say why a held-out run did not finish."""
    stopped = report.operation.stopped_by
    if stopped == "cap":
        why = "OpenRouter spending reached [eval].max_cost_usd"
    elif stopped is None:
        why = f"{report.operation.not_done} cases ended with no result"
    else:
        why = str(report.stop_error or stopped)
    return f"program {program.id} is saved, but its held-out test did not finish: {why}"


async def _test_program(  # noqa: PLR0913  # one run's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    plan: GridPlan,
    combination: Combination,
    program: TunedProgram,
    *,
    now: Callable[[], datetime],
    on_progress: Callable[[TuneProgress], None] | None,
) -> _Tested:
    """Run a saved program on the held-out postings, through the normal suite.

    The run is limited, under the program's version, with a fresh `EvalSpend`. The
    test ends the grid when the ledger is spent, a provider cannot be used or a
    cost is unknown, since the next combination would meet the same thing; one
    call that failed only leaves this test incomplete.
    """
    held_out = {
        row.posting_id
        for row in store.tunes.split(plan.eval_set.id)
        if row.part is SplitPart.HELD_OUT
    }
    skipped = f"program {program.id} is saved, but its held-out test did not run: "
    try:
        spend = EvalSpend(store.evals, plan.settings)
        planned = plan_run(
            store,
            runtime.settings,
            plan.settings,
            combination.suite,
            model=combination.model,
            limit=None,
            only=held_out,
            program=program,
        )
    except EvalBudgetSpentError as error:
        return _Tested(None, f"{skipped}{error}", str(error))
    except EvalNeedsError as error:
        return _Tested(None, f"{skipped}{error}", None)
    ended = 0
    failed = 0

    def progress(outcome: ItemOutcome[object, object]) -> None:
        nonlocal ended, failed
        ended += 1
        failed += outcome.result is None
        if on_progress is not None:
            on_progress(
                TuneProgress(
                    "test",
                    ended,
                    failed,
                    spend.openrouter_spent + spend.subscription_spent,
                    spend.openrouter_spent,
                )
            )

    report = await run_plan(
        runtime, store, planned, spend, now=now, on_outcome=progress
    )
    stop = None
    if isinstance(report.stop_error, ProviderUnavailableError | CostUnknownError):
        stop = str(report.stop_error)
    reason = (
        None
        if report.status is RunStatus.COMPLETE
        else _held_out_reason(program, report)
    )
    return _Tested(report, reason, stop)


@dataclass(frozen=True, slots=True)
class _Tuned:
    """What tuning one combination left behind, before any test.

    Attributes:
        run_id: The `tune` row.
        outcome: What the engine returned.
        program: The program saved, or `None`.
        baseline: The untuned model's score on the val part.
        run_spent: What the run budget was charged.
        changed: Train and val postings left out.
        spend_cap: What the command's OpenRouter spend was capped at when it began.
    """

    run_id: int
    outcome: "TuneOutcome"
    program: TunedProgram | None
    baseline: float | None
    run_spent: float
    changed: int
    spend_cap: float


def _metrics(  # noqa: PLR0913  # the row's whole metrics, one argument each
    combination: Combination,
    *,
    calls: int,
    failures: int,
    outcome: "TuneOutcome | None",
    baseline: float | None,
    program: TunedProgram | None,
) -> dict[str, object]:
    """Write the metrics of a `tune` row; its keys are a contract with its readers."""
    return {
        "task": combination.task.value,
        "optimizer": combination.optimizer.value,
        "calls": calls,
        "failures": failures,
        "unusable": 0 if outcome is None else outcome.unusable,
        "baseline_val": baseline,
        "best_val": outcome.best_val if outcome is not None and program else None,
        "no_change": outcome is not None and outcome.no_change,
        "program_id": None if program is None else program.id,
    }


def _save_program(  # noqa: PLR0913  # a program's whole row, one argument each
    store: Store,
    plan: GridPlan,
    combination: Combination,
    text: "ProgramText",
    *,
    run_id: int,
    at: datetime,
) -> TunedProgram:
    """Save the program a run made, bound to the rules or profile its demos hold."""
    from ijob_ai.tasks.programs import program_version  # noqa: PLC0415

    judge = combination.task is EvalTask.JUDGE
    return store.tunes.save_program(
        task=combination.task,
        model=str(combination.model),
        optimizer=combination.optimizer.value,
        base_version=combination.base_version,
        set_id=plan.eval_set.id,
        bound_sha256=(
            plan.eval_set.rules_sha256 if judge else plan.eval_set.profile_sha256
        ),
        instructions=text.instructions,
        demos_json=text.demos_json,
        version=program_version(
            combination.base_version, text.instructions, text.demos_json
        ),
        tune_run_id=run_id,
        created_at=at,
    )


async def _tune(  # noqa: PLR0913  # one run's inputs and steps, in order
    runtime: LlmRuntime,
    store: Store,
    plan: GridPlan,
    combination: Combination,
    *,
    now: Callable[[], datetime],
    on_progress: Callable[[TuneProgress], None] | None,
) -> _Tuned:
    """Tune one combination under the rails and write its `tune` row.

    The row is written before any call, its spend after every call, and it is
    closed however the run ends. A program a complete run made is saved before the
    row closes, so the row names it.
    """
    from ijob_ai.llm.budget import Budget  # noqa: PLC0415
    from ijob_ai.llm.railed import RailedLM, RailGuard  # noqa: PLC0415
    from ijob_ai.tracing.setup import operation_trace, start_tracing  # noqa: PLC0415
    from ijob_ai.tuning.engine import (  # noqa: PLC0415
        TuneRequest,
        TuneStatus,
        tune_program,
    )

    task = combination.task
    examples = tuning_examples(store, plan.eval_set, task)
    baseline = baseline_val(store, plan.eval_set, task, combination.model)
    spend = EvalSpend(store.evals, plan.settings)
    registry = await runtime.registry_for_ref(combination.model)
    task_lm = registry.lm_for_ref(combination.model)
    tuner_lm = None
    if combination.optimizer in _NEEDS_TUNER:
        if plan.tuner is None:
            msg = f"{combination.optimizer.value} needs a tuner and the plan has none"
            raise ValueError(msg)
        tuner_lm = (await runtime.registry_for_ref(plan.tuner)).lm_for_ref(plan.tuner)
    run_budget = Budget(plan.tune.max_cost_usd)
    operation = Budget(spend.cap)

    def called(guard: RailGuard) -> None:
        spend.record()
        if on_progress is not None:
            on_progress(
                TuneProgress(
                    "tune",
                    guard.calls,
                    guard.failed,
                    run_budget.spent,
                    spend.openrouter_spent,
                )
            )

    guard = RailGuard(on_call=called)

    def rail(
        ref: ModelRef, lm: "dspy.LM", kind: Task, trace: "OperationTrace"
    ) -> RailedLM:
        return RailedLM(
            lm,
            provider=ref.provider,
            budgets=(run_budget, spend.budget_for(ref, operation)),
            timeout_s=runtime.settings.timeout_s,
            task=kind,
            task_version=combination.base_version,
            trace=trace,
            loop=asyncio.get_running_loop(),
            guard=guard,
        )

    run = spend.begin(
        TUNE_SUITE,
        str(combination.model),
        combination.base_version,
        plan.eval_set.id,
        started_at=now(),
    )
    tracing = start_tracing(runtime.home.traces, runtime.tracing_settings)
    outcome: TuneOutcome | None = None
    program: TunedProgram | None = None
    status = RunStatus.INCOMPLETE
    try:
        with operation_trace(tracing, f"tune-{task.value}") as trace:
            task_key = Task.JUDGE if task is EvalTask.JUDGE else Task.SCORER
            request = TuneRequest(
                task=task_key,
                optimizer=combination.optimizer,
                task_lm=rail(combination.model, task_lm, task_key, trace),
                tuner_lm=None
                if tuner_lm is None or plan.tuner is None
                else rail(plan.tuner, tuner_lm, Task.TUNER, trace),
                guard=guard,
                trainset=examples.train,
                valset=examples.val,
                num_threads=runtime.settings.concurrency,
            )
            outcome = await tune_program(request)
        if outcome.status is TuneStatus.COMPLETE and outcome.program is not None:
            program = _save_program(
                store, plan, combination, outcome.program, run_id=run.id, at=now()
            )
        if outcome.status is TuneStatus.COMPLETE:
            status = RunStatus.COMPLETE
    finally:
        metrics = _metrics(
            combination,
            calls=guard.calls,
            failures=guard.failed,
            outcome=outcome,
            baseline=baseline,
            program=program,
        )
        spend.finish(status, json.dumps(metrics), finished_at=now())
        await asyncio.to_thread(tracing.finish)
    return _Tuned(
        run.id,
        outcome,
        program,
        baseline,
        run_budget.spent,
        examples.changed,
        operation.cap,
    )


def _cap_reason(plan: GridPlan, tuned: _Tuned) -> str:
    """Say that a spending cap ended the run, and which settings set the caps."""
    return (
        f"a spending cap was reached after ${tuned.run_spent:.4f}: a run may spend "
        f"${plan.tune.max_cost_usd:.2f} ([tune].max_cost_usd) and OpenRouter spend "
        f"may reach ${tuned.spend_cap:.2f} ([eval].max_cost_usd, or what "
        "[eval].total_cost_usd leaves)"
    )


def _incomplete_reason(plan: GridPlan, tuned: _Tuned) -> str:
    """Say why a sound run could not be had, from what the engine recorded.

    The causes are told in the order of the spec: the cap, over a quarter of calls
    failed, `compile` raised.
    """
    outcome = tuned.outcome
    if outcome.cap_reached:
        return _cap_reason(plan, tuned)
    if outcome.failed * _FAILED_SHARE_DIVISOR > outcome.calls:
        return (
            f"{outcome.failed} of {outcome.calls} calls failed; the first error was: "
            f"{outcome.first_error}; run `ijob traces` to read the calls"
        )
    return f"compile raised: {outcome.compile_error}"


def _tuned_result(
    plan: GridPlan,
    combination: Combination,
    tuned: _Tuned,
    tested: _Tested | None,
) -> CombinationResult:
    """Build a combination's result from its tuning and, if it made one, its test."""
    from ijob_ai.tuning.engine import TuneStatus  # noqa: PLC0415

    outcome = tuned.outcome
    base = CombinationResult(
        combination=combination,
        state=CombinationState.NO_CHANGE,
        run_id=tuned.run_id,
        program_id=None if tuned.program is None else tuned.program.id,
        calls=outcome.calls,
        failures=outcome.failed,
        unusable=outcome.unusable,
        baseline_val=tuned.baseline,
        best_val=outcome.best_val,
        run_spent=tuned.run_spent,
        changed=tuned.changed,
    )
    incomplete = CombinationState.INCOMPLETE
    if outcome.interrupted:
        reason = f"{INTERRUPTED}; the spend so far is kept"
        return replace(
            base, state=incomplete, reason=reason, stop=INTERRUPTED, interrupted=True
        )
    if outcome.fatal is not None:
        text = str(outcome.fatal)
        return replace(base, state=incomplete, reason=text, stop=text)
    if outcome.status is not TuneStatus.COMPLETE:
        return replace(base, state=incomplete, reason=_incomplete_reason(plan, tuned))
    if tested is None:
        return base
    return replace(
        base,
        state=incomplete if tested.reason is not None else CombinationState.TUNED,
        reason=tested.reason,
        held_out_run_id=None if tested.report is None else tested.report.run_id,
        stop=tested.stop,
    )


async def run_combination(  # noqa: PLR0913  # one run's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    plan: GridPlan,
    combination: Combination,
    *,
    now: Callable[[], datetime],
    on_progress: Callable[[TuneProgress], None] | None = None,
) -> CombinationResult:
    """Tune one combination, then test the program it made on the held-out postings.

    The run is one `tune` row with its own `EvalSpend`, a fresh guard, and a trace
    whose root span is `ijob tune-<task>`. Every call is charged to the run budget,
    `[tune].max_cost_usd`; OpenRouter calls are also charged to the command's
    OpenRouter limit and claude-sdk calls to the subscription. Train and val come
    only from the frozen split. A complete run that changed the program saves it,
    bound to the rules or profile its demos hold, and the held-out postings run
    through the normal suite with it. A run that changed nothing saves and tests
    nothing. A combination planned as a retry tunes nothing and only tests its
    saved program.

    What ended the combination is in the result; a cancel or Ctrl-C reaching the
    tuning is absorbed, and the result is `interrupted`.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        plan: The plan from `prepare_grid`.
        combination: The combination to run.
        now: The wall clock, returning timezone-aware times.
        on_progress: Called on the loop after each call, and after each case of
            the held-out test.

    Returns:
        How the combination ended.

    Raises:
        EvalBudgetSpentError: The recorded eval spending has used up
            `eval.total_cost_usd`; nothing was written.
        ProviderUnavailableError: A provider cannot be used; nothing was written.
        EvalNeedsError: The train or val part has no unchanged posting left.
        asyncio.CancelledError: The call was cancelled outside the tuning; the
            rows already written stand.
        sqlite3.Error: The store cannot be read or written.
    """
    if combination.retry is not None:
        tested = await _test_program(
            runtime,
            store,
            plan,
            combination,
            combination.retry,
            now=now,
            on_progress=on_progress,
        )
        return CombinationResult(
            combination=combination,
            state=CombinationState.INCOMPLETE
            if tested.reason is not None
            else CombinationState.TUNED,
            reason=tested.reason,
            program_id=combination.retry.id,
            held_out_run_id=None if tested.report is None else tested.report.run_id,
            stop=tested.stop,
        )
    tuned = await _tune(
        runtime, store, plan, combination, now=now, on_progress=on_progress
    )
    if tuned.program is None:
        return _tuned_result(plan, combination, tuned, None)
    held_out = await _test_program(
        runtime,
        store,
        plan,
        combination,
        tuned.program,
        now=now,
        on_progress=on_progress,
    )
    return _tuned_result(plan, combination, tuned, held_out)


def interrupted_result(
    combination: Combination, progress: Iterable[TuneProgress]
) -> CombinationResult:
    """Build the result of a combination that Ctrl-C ended before it could report.

    A second Ctrl-C leaves the loop, and a first one during a held-out test is
    raised out of `run_grid`, so neither gives a result. The command keeps the
    latest progress of the combination, which this reads for what is known of its
    spend; its `tune` row stays incomplete, or complete when it was only the test
    that was cut short, and a rerun plans it again.

    Args:
        combination: The combination that was running.
        progress: Its latest progress, one update per phase it reached.

    Returns:
        An incomplete, interrupted result, whose reason names the spend if any
        progress was seen.
    """
    updates = list(progress)
    tuning = next((each for each in updates if each.phase == "tune"), None)
    spent = sum(each.spent for each in updates)
    kept = (
        f"the spend so far, ${spent:.4f}, is kept"
        if updates
        else "the spend so far is kept"
    )
    return CombinationResult(
        combination=combination,
        state=CombinationState.INCOMPLETE,
        reason=f"{INTERRUPTED}; {kept}",
        calls=0 if tuning is None else tuning.calls,
        failures=0 if tuning is None else tuning.failed,
        run_spent=0.0 if tuning is None else tuning.spent,
        stop=INTERRUPTED,
        interrupted=True,
    )


@dataclass(frozen=True, slots=True)
class GridOffer:
    """What a grid will do and cost, worked out before any model is called.

    Attributes:
        plan: The combinations to run.
        cap: What each combination's OpenRouter spend may reach, in US dollars.
        left: What `eval.total_cost_usd` leaves after the OpenRouter spend
            recorded so far, in US dollars, never below zero.
        costs: Each model called with the cost estimated for all its combinations.
        estimate: The estimated cost of the whole grid.
    """

    plan: GridPlan
    cap: float
    left: float
    costs: tuple[tuple[ModelRef, Cost], ...]
    estimate: Cost

    @property
    def callers(self) -> tuple[ModelRef, ...]:
        """The models the grid calls to tune, each once, in plan order."""
        return tuple(dict.fromkeys(each.model for each in self.plan.combinations))


def grid_offer(
    plan: GridPlan, estimates: Sequence[CallEstimate], *, cap: float, left: float
) -> GridOffer:
    """Combine a plan with the estimates of its planned calls.

    Args:
        plan: The plan from `prepare_grid`.
        estimates: The estimates of `planned_calls(plan)`.
        cap: What a combination's OpenRouter spend may reach.
        left: What the recorded spending leaves of `eval.total_cost_usd`.

    Returns:
        The offer.
    """
    return GridOffer(
        plan=plan,
        cap=cap,
        left=max(0.0, left),
        costs=tuple(cost_by_model(estimates)),
        estimate=total_cost(estimates),
    )


@dataclass(frozen=True, slots=True)
class GridEntry:
    """One combination and how the grid left it.

    Attributes:
        combination: The combination.
        state: Its state.
        result: How it ended, or `None` for one that was done before or never run.
    """

    combination: Combination
    state: CombinationState
    result: CombinationResult | None


@dataclass(frozen=True, slots=True)
class GridReport:
    """What a grid did.

    Attributes:
        done: The combinations left out because a complete row covered them.
        results: The combinations that ran, in order.
        not_run: The combinations that did not run because the grid stopped.
        stopped: Why the grid stopped before its last combination, or `None`.
    """

    done: tuple[Combination, ...]
    results: tuple[CombinationResult, ...]
    not_run: tuple[Combination, ...]
    stopped: str | None

    @property
    def entries(self) -> tuple[GridEntry, ...]:
        """Every combination with its state: the done ones, then those that ran."""
        return (
            *(GridEntry(each, CombinationState.DONE, None) for each in self.done),
            *(GridEntry(each.combination, each.state, each) for each in self.results),
            *(GridEntry(each, CombinationState.NOT_RUN, None) for each in self.not_run),
        )

    @property
    def changed(self) -> int:
        """Train and val postings the combinations that ran left out as changed."""
        return sum(each.changed for each in self.results)

    @property
    def exit_status(self) -> int:
        """The command's exit status: 1 if any combination is incomplete or not run."""
        incomplete = any(
            each.state is CombinationState.INCOMPLETE for each in self.results
        )
        return 1 if incomplete or self.not_run or self.stopped else 0


def grid_report(
    plan: GridPlan,
    results: Sequence[CombinationResult],
    stopped: str | None,
    *,
    in_flight: CombinationResult | None = None,
) -> GridReport:
    """Build the report of a grid from the combinations that ended.

    Args:
        plan: The plan the grid ran.
        results: The combinations that ended, in order.
        stopped: Why the grid stopped early, or `None`.
        in_flight: The result of the combination that was running when the grid
            was cut short, which comes after `results`; `None` when none was.

    Returns:
        The report, whose `not_run` holds every planned combination after those
        that ended or were running.
    """
    ran = (*results, in_flight) if in_flight is not None else tuple(results)
    return GridReport(
        done=plan.done,
        results=ran,
        not_run=plan.combinations[len(ran) :],
        stopped=stopped,
    )


async def run_grid(  # noqa: PLR0913  # the grid's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    plan: GridPlan,
    *,
    now: Callable[[], datetime],
    on_start: Callable[[Combination], None] | None = None,
    on_progress: Callable[[TuneProgress], None] | None = None,
    on_result: Callable[[CombinationResult], None] | None = None,
) -> GridReport:
    """Run each combination of the plan in order.

    A combination that reached its cap, had over a quarter of its calls fail, whose
    `compile` raised, whose held-out test lost a call or whose train or val part
    has no unchanged posting left is incomplete and the grid goes on. A spent
    ledger, an unavailable provider, an unknown cost or the user's Ctrl-C ends the
    grid, since the next combination would meet the same thing.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        plan: The plan from `prepare_grid`.
        now: The wall clock, returning timezone-aware times.
        on_start: Called as each combination begins.
        on_progress: Called on the loop after each call of a combination.
        on_result: Called as each combination ends.

    Returns:
        What the grid did.

    Raises:
        asyncio.CancelledError: The call was cancelled outside the tuning; the rows
            already written stand.
        sqlite3.Error: The store cannot be read or written.
    """
    results: list[CombinationResult] = []
    stopped: str | None = None
    for combination in plan.combinations:
        if on_start is not None:
            on_start(combination)
        try:
            result = await run_combination(
                runtime,
                store,
                plan,
                combination,
                now=now,
                on_progress=on_progress,
            )
        except (
            EvalBudgetSpentError,
            ProviderUnavailableError,
            CostUnknownError,
        ) as error:
            stopped = str(error)
            break
        except EvalNeedsError as error:
            result = CombinationResult(
                combination=combination,
                state=CombinationState.INCOMPLETE,
                reason=str(error),
            )
        results.append(result)
        if on_result is not None:
            on_result(result)
        if result.stop is not None:
            stopped = result.stop
            break
    return grid_report(plan, results, stopped)


@dataclass(frozen=True, slots=True)
class ProgramLine:
    """A tuned program with what `tune list` says about it.

    Attributes:
        program: The program.
        status: Whether it is tested, untested, accepted or accepted but not in use.
    """

    program: TunedProgram
    status: ProgramStatus


def list_programs(store: Store, llm: LlmSettings) -> list[ProgramLine]:
    """List every tuned program with its status, judge programs first, by id.

    Args:
        store: The open store.
        llm: The settings naming each task's model.

    Returns:
        The programs. Nothing is written.

    Raises:
        InvalidValueError: Naming the id of a malformed program.
        sqlite3.Error: The store cannot be read.
    """
    return [
        ProgramLine(program, program_status(store, llm, program))
        for task in EvalTask
        for program in store.tunes.programs(task)
    ]
