"""The suites of `ijob eval run` that measure a model: judge, scorer, hostile, reasons.

`judge` and `scorer` run the task with the model under test on the cases of the
current label set and score the answers against the labels. `reasons` has a
grader model check the reasons in the model's latest judge and scorer runs.
`hostile` runs made-up pairs of clean and injected postings and counts the pairs
whose answer the injected text changed. The agent suites, `source-agent` and
`explore-agent`, are named in `Suite` and share this module's run row and report,
but run from `eval_agent_suites`.

Each suite has a plan, made before any call, and a run. A plan applies the rules
every suite shares: the model comes from configuration unless one is given, a
model on the set's jury is refused because it would be graded on its own answers,
a set with fewer than 10 labels is refused, and a posting whose text changed
since the set was drawn is skipped and counted. A labelled plan may also cover only
chosen postings, which makes it limited, and may run every call with a tuned program,
which sets the version of its row. A run writes its row through
`EvalSpend` before any call, stores each case's result as it ends and closes the
row however the run ends.

This module imports no `ijob_ai` task, model or DeepEval module at module level,
so a command that calls no model stays fast; those load inside the functions that
are about to call one.
"""

import json
import math
import statistics
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from ijob_ai.errors import (
    CapReachedError,
    LlmError,
    ProviderFailedError,
    TaskOutputError,
)
from ijob_ai.evals.hostile import HostilePair
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.reference import ModelRef
from ijob_ai.llm.settings import EvalSettings, LlmSettings, Task
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalRecords,
    EvalResult,
    EvalSet,
    EvalTask,
    RunStatus,
)
from ijob_core.eval_rules import Split, limit_split
from ijob_core.model import (
    Instructions,
    Posting,
    PostingId,
    ResumeProfile,
    Score,
    Verdict,
)
from ijob_core.store import Store
from ijob_core.tune_records import TunedProgram

from ijob_app.eval_ops import (
    EvalNeedsError,
    find_set,
    stored_profile,
    stored_rules,
)
from ijob_app.eval_spend import EvalSpend
from ijob_app.llm_ops import (
    ItemOutcome,
    ItemStatus,
    LlmRuntime,
    OperationReport,
    run_items,
)

MIN_LABELS: Final = 10
"""Labels of a task that a suite needs before it measures anything."""

REASONS_PER_TASK: Final = 10
"""Cases of each task `reasons` grades when no `--limit` is given."""

_TOO_FEW_FIX: Final = (
    "run `ijob eval review` to settle the open cases, or `ijob eval label` for more"
)
_ANSWER_KEEP: Final = "keep"
_ANSWER_REJECT: Final = "reject"
_P95: Final = 0.95

type Metrics = dict[str, float | int | None]
"""A run's metrics as stored: numbers, with `None` where a number is undefined."""


class Suite(StrEnum):
    """A suite that measures a model."""

    JUDGE = "judge"
    SCORER = "scorer"
    REASONS = "reasons"
    HOSTILE = "hostile"
    SOURCE_AGENT = "source-agent"
    EXPLORE_AGENT = "explore-agent"


AGENT_SUITES: Final = frozenset({Suite.SOURCE_AGENT, Suite.EXPLORE_AGENT})
"""The suites that run an agent: they use no labels and send no personal data."""

_DEFAULT_TASK: Final[Mapping[Suite, Task]] = {
    Suite.JUDGE: Task.JUDGE,
    Suite.SCORER: Task.SCORER,
    Suite.REASONS: Task.JUDGE,
    Suite.HOSTILE: Task.JUDGE,
    Suite.SOURCE_AGENT: Task.SOURCE_AGENT,
    Suite.EXPLORE_AGENT: Task.EXPLORE_AGENT,
}


@dataclass(frozen=True, slots=True)
class SuiteCase:
    """One labelled case to run.

    Attributes:
        posting: The posting as it was when the set was drawn.
        label: The decided answer for the case.
    """

    posting: Posting
    label: EvalLabel


@dataclass(frozen=True, slots=True)
class SuitePlan:
    """What a `judge` or `scorer` run will do, worked out before any call.

    Attributes:
        suite: `judge` or `scorer`.
        model: The model under test.
        eval_set: The label set.
        instructions: The stored instructions whose rules a judge run applies.
        profile: The stored profile a scorer run reads.
        cases: The labelled cases to run, by position.
        skipped: Cases left out because their posting changed or went since the
            set was drawn.
        limited: Whether `limit` or `only` narrowed the run.
        program: The tuned program every call runs with, or `None` for the base
            prompt.
        task_version: The version the run row and every call carry: the
            program's when there is one, else the task's.
    """

    suite: Suite
    model: ModelRef
    eval_set: EvalSet
    instructions: Instructions
    profile: ResumeProfile
    cases: tuple[SuiteCase, ...]
    skipped: int
    limited: bool
    program: TunedProgram | None
    task_version: str


@dataclass(frozen=True, slots=True)
class ReasonItem:
    """One reason to grade.

    Attributes:
        task: The task whose answer holds the reason.
        posting_id: The posting the answer is about.
        input_text: The numbered rules or the profile JSON, then the posting block.
        actual_output: The verdict's reason, or the score's reasons one per line.
    """

    task: EvalTask
    posting_id: PostingId
    input_text: str
    actual_output: str

    @property
    def key(self) -> str:
        """The case key the result is stored under."""
        return f"{self.task.value}:{self.posting_id}"


@dataclass(frozen=True, slots=True)
class ReasonsPlan:
    """What a `reasons` run will do, worked out before any call.

    Attributes:
        model: The model whose reasons are graded; it is not called.
        grader: The model that grades.
        eval_set: The label set the graded runs used.
        items: The reasons to grade, judge first, each task by position.
        skipped: Reasons left out because their posting changed or went.
        limited: Whether `limit` narrowed the run.
    """

    model: ModelRef
    grader: ModelRef
    eval_set: EvalSet
    items: tuple[ReasonItem, ...]
    skipped: int
    limited: bool


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """What a suite run did.

    Attributes:
        run_id: The run row the results, spend and status were written to.
        suite: The suite run.
        model: The model under test.
        status: `complete` when every case ended with a result, else `incomplete`.
        operation: What `run_items` did and spent.
        metrics: The run's numbers as stored in the run row.
        skipped: Cases left out because their posting changed.
        stop_error: The error that stopped the run on a provider failure or an
            unknown cost, if any.
        subscription_spent: claude-sdk spend of the run, in US dollars.
    """

    run_id: int
    suite: Suite
    model: ModelRef
    status: RunStatus
    operation: OperationReport
    metrics: Metrics
    skipped: int
    stop_error: LlmError | None
    subscription_spent: float

    @property
    def exit_status(self) -> int:
        """The command's exit status: 1 for a failed run, else 0.

        A provider or cost-unknown stop fails a run. So does a `hostile` run that
        is incomplete, had any attack or had no answered pair, and so does a
        `source-agent` or `explore-agent` run that is incomplete or had any rule
        violation, because a target of 0 cannot be claimed from a partial run. A
        spending cap fails those suites by leaving them incomplete, and no other.
        """
        if self.operation.stopped_by in ("provider", "cost_unknown"):
            return 1
        if self.suite is Suite.HOSTILE:
            attacks = self.metrics.get("attacks") or 0
            answered = self.metrics.get("answered") or 0
            clean = self.status is RunStatus.COMPLETE and attacks == 0 and answered > 0
            return 0 if clean else 1
        if self.suite in AGENT_SUITES:
            violations = self.metrics.get("violations") or 0
            clean = self.status is RunStatus.COMPLETE and violations == 0
            return 0 if clean else 1
        return 0


@dataclass(frozen=True, slots=True, kw_only=True)
class _Finished[Payload]:
    """A case that ended with a result to store, and what its metrics need."""

    key: str
    passed: bool
    answer: str
    detail: str
    latency_ms: int
    payload: Payload


@dataclass(frozen=True, slots=True, kw_only=True)
class _Header:
    """The identifying fields of a run row."""

    suite: Suite
    model: ModelRef
    task_version: str
    set_id: int | None
    limited: bool
    skipped: int


@dataclass(frozen=True, slots=True)
class _Executed:
    """What `_execute` learned from a run."""

    run_id: int
    status: RunStatus
    operation: OperationReport
    metrics: Metrics
    stop_error: LlmError | None


def resolve_model(
    llm: LlmSettings, suite: Suite, override: ModelRef | None
) -> ModelRef:
    """Return the model a suite tests: the one given, else the suite's default.

    Args:
        llm: The model settings.
        suite: The suite to run.
        override: The model from `--model`, if any.

    Returns:
        `override`, or `[models].judge` for `judge`, `reasons` and `hostile`,
        `[models].scorer` for `scorer`, `[models].source_agent` for `source-agent`
        and `[models].explore_agent` for `explore-agent`.

    Raises:
        MissingModelError: No model is given or configured for the suite.
    """
    return override if override is not None else llm.model_for(_DEFAULT_TASK[suite])


def _task_version() -> str:
    """Return both task versions as stored for a suite that spans both tasks."""
    from ijob_ai.tasks.signatures import JUDGE_VERSION, SCORER_VERSION  # noqa: PLC0415

    return f"{JUDGE_VERSION}+{SCORER_VERSION}"


def _single_version(task: EvalTask) -> str:
    """Return the version of one task."""
    from ijob_ai.tasks.signatures import JUDGE_VERSION, SCORER_VERSION  # noqa: PLC0415

    return JUDGE_VERSION if task is EvalTask.JUDGE else SCORER_VERSION


def _no_set() -> EvalNeedsError:
    """Build the error for a jury, rules and profile that have no label set."""
    return EvalNeedsError(
        "no labels exist for the current rules, CV profile and jury",
        "run `ijob eval label`",
    )


def _too_few(found: int, task: EvalTask) -> EvalNeedsError:
    """Build the error for a task with fewer than `MIN_LABELS` labels."""
    return EvalNeedsError(
        f"only {found} {task.value} labels exist and a run needs {MIN_LABELS}",
        _TOO_FEW_FIX,
    )


def _no_run(task: EvalTask, model: ModelRef, version: str) -> EvalNeedsError:
    """Build the error for a model with no run of `task` to grade."""
    return EvalNeedsError(
        f"no complete, unlimited {task.value} run of {model} at {version} exists",
        f"run `ijob eval run {task.value} --model {model}`",
    )


def _none_remain(skipped: int) -> EvalNeedsError:
    """Build the error for a run in which every case was left out."""
    return EvalNeedsError(
        f"no case remains to run: {skipped} changed since the draw",
        "run `ijob eval label` to draw a new set",
    )


@dataclass(frozen=True, slots=True)
class _Context:
    """The stored inputs every labelled suite reads."""

    eval_set: EvalSet
    instructions: Instructions
    profile: ResumeProfile


def _inputs(
    store: Store, eval_settings: EvalSettings, model: ModelRef
) -> tuple[Instructions, ResumeProfile, tuple[str, ...]]:
    """Read the stored inputs and the jury, and refuse a juror as `model`.

    Returns:
        The stored instructions, the stored profile and the sorted jury.

    Raises:
        EvalNeedsError: No jury is set, or no rules or profile are stored.
        InvalidValueError: `model` is, or was, a juror of a set for these rules and
            profile.
    """
    if eval_settings.jury is None:
        raise EvalNeedsError.no_jury()
    instructions = stored_rules(store)
    profile = stored_profile(store)
    rules_sha256, profile_sha256 = instructions.rules_sha256, profile.sha256()
    jury = tuple(sorted(str(juror) for juror in eval_settings.jury))
    jurors = set(jury) | {
        juror
        for known in store.evals.sets()
        if (known.rules_sha256, known.profile_sha256) == (rules_sha256, profile_sha256)
        for juror in known.jury
    }
    if str(model) in jurors:
        raise InvalidValueError(
            "model",
            f"{model} is on the jury of the label set and would be graded on its "
            "own answers; pick another model",
        )
    return instructions, profile, jury


def refuse_juror(store: Store, eval_settings: EvalSettings, model: ModelRef) -> None:
    """Refuse a model that is, or was, a juror of the label set for the stored inputs.

    Args:
        store: The open store.
        eval_settings: The eval settings, whose jury identifies the set.
        model: The model that would be graded.

    Raises:
        EvalNeedsError: No jury is set, or no rules or profile are stored.
        InvalidValueError: `model` is, or was, a juror of a set for these rules and
            profile.
    """
    _inputs(store, eval_settings, model)


def _context(store: Store, eval_settings: EvalSettings, model: ModelRef) -> _Context:
    """Find the label set for the stored inputs and refuse a juror as the model.

    Raises:
        EvalNeedsError: No jury is set, no rules or profile are stored, or no set
            exists for them.
        InvalidValueError: `model` is, or was, a juror of a set for these rules and
            profile.
    """
    instructions, profile, jury = _inputs(store, eval_settings, model)
    eval_set = find_set(store.evals, instructions.rules_sha256, profile.sha256(), jury)
    if eval_set is None:
        raise _no_set()
    return _Context(eval_set, instructions, profile)


def _labelled(
    records: EvalRecords, eval_set: EvalSet, task: EvalTask
) -> list[tuple[EvalCase, EvalLabel]]:
    """Return the set's cases that have a label for `task`, by position.

    Raises:
        EvalNeedsError: Fewer than `MIN_LABELS` cases are labelled.
    """
    labels = {label.posting_id: label for label in records.labels(eval_set.id, task)}
    pairs = [
        (case, labels[case.posting_id])
        for case in records.cases(eval_set.id)
        if case.posting_id in labels
    ]
    if len(pairs) < MIN_LABELS:
        raise _too_few(len(pairs), task)
    return pairs


def _check_limit(limit: int, high: int = 100) -> None:
    """Raise unless `limit` is from 1 to `high`.

    Raises:
        InvalidValueError: `limit` is outside the range.
    """
    if not 1 <= limit <= high:
        raise InvalidValueError("limit", f"{limit} is outside 1 to {high}")


def _limited[Row](
    rows: Sequence[Row], bucket: Callable[[Row], Bucket], limit: int | None
) -> list[Row]:
    """Take the rows a `--limit` covers: the first of each pool by position."""
    if limit is None:
        return list(rows)
    pools = {pool: [row for row in rows if bucket(row) is pool] for pool in Bucket}
    split = limit_split(
        limit,
        Split(
            len(pools[Bucket.KEPT]),
            len(pools[Bucket.REJECTED]),
            len(pools[Bucket.UNJUDGED]),
        ),
    )
    taken = (
        pools[Bucket.KEPT][: split.kept]
        + pools[Bucket.REJECTED][: split.rejected]
        + pools[Bucket.UNJUDGED][: split.unjudged]
    )
    return [row for row in rows if any(row is chosen for chosen in taken)]


def unchanged_posting(store: Store, case: EvalCase) -> Posting | None:
    """Return the case's posting, or `None` if its text changed or it is gone."""
    from ijob_ai.tasks.text import input_sha256, posting_block  # noqa: PLC0415

    posting = store.posting(case.posting_id)
    if posting is None or input_sha256(posting_block(posting)) != case.input_sha256:
        return None
    return posting


def prepare_suite(  # noqa: PLR0913  # the plan's inputs, one each
    store: Store,
    llm: LlmSettings,
    eval_settings: EvalSettings,
    suite: Suite,
    *,
    model: ModelRef | None,
    limit: int | None,
    only: Collection[PostingId] | None = None,
    program: TunedProgram | None = None,
) -> SuitePlan:
    """Work out a `judge` or `scorer` run before any call.

    The run covers the labelled cases of the set for the stored rules, CV profile
    and jury, by position. With `only` it covers just the cases whose posting is
    named, and with `limit` the first of each pool as `limit_split` divides them;
    either one makes the run limited. A case whose posting text differs from the
    one the set was drawn under, or whose posting is gone, is skipped and counted.
    With a `program` every call runs with it and the run is stored under its
    version.

    Args:
        store: The open store.
        llm: The model settings.
        eval_settings: The eval settings, whose jury identifies the set.
        suite: `judge` or `scorer`.
        model: The model under test, or `None` for the suite's default.
        limit: Cover only this many cases, from 1 to 100; all when `None`.
        only: Cover only the cases of these postings; all when `None`.
        program: A tuned program of the suite's task to run with; the base prompt
            when `None`.

    Returns:
        The plan.

    Raises:
        InvalidValueError: `suite` is not `judge` or `scorer`, `limit` is outside
            1 to 100, the model is on the set's jury, or `program` is for the
            other task.
        MissingModelError: No model is given or configured.
        EvalNeedsError: No jury, rules, profile or set exists, fewer than 10
            cases are labelled, or every case changed since the draw.
        sqlite3.Error: The store cannot be read.
    """
    if suite not in (Suite.JUDGE, Suite.SCORER):
        raise InvalidValueError("suite", f"{suite.value} has no labelled cases")
    chosen = resolve_model(llm, suite, model)
    if limit is not None:
        _check_limit(limit)
    context = _context(store, eval_settings, chosen)
    task = EvalTask.JUDGE if suite is Suite.JUDGE else EvalTask.SCORER
    if program is not None and program.task is not task:
        raise InvalidValueError(
            "program",
            f"program {program.id} is for {program.task.value}, not {task.value}",
        )
    labelled = _labelled(store.evals, context.eval_set, task)
    if only is not None:
        named = set(only)
        labelled = [pair for pair in labelled if pair[0].posting_id in named]
    pairs = _limited(labelled, lambda pair: pair[0].bucket, limit)
    cases: list[SuiteCase] = []
    skipped = 0
    for case, label in pairs:
        posting = unchanged_posting(store, case)
        if posting is None:
            skipped += 1
        else:
            cases.append(SuiteCase(posting, label))
    if not cases:
        raise _none_remain(skipped)
    return SuitePlan(
        suite=suite,
        model=chosen,
        eval_set=context.eval_set,
        instructions=context.instructions,
        profile=context.profile,
        cases=tuple(cases),
        skipped=skipped,
        limited=limit is not None or only is not None,
        program=program,
        task_version=_single_version(task) if program is None else program.version,
    )


def _reason_text(task: EvalTask, answer: str) -> str | None:
    """Read the reason out of a stored answer, or `None` when it holds none."""
    stored = json.loads(answer)
    if not isinstance(stored, dict):
        return None
    if task is EvalTask.JUDGE:
        reason = stored.get("reason")
        return reason if isinstance(reason, str) and reason else None
    reasons = stored.get("reasons")
    if not isinstance(reasons, list) or not reasons:
        return None
    return "\n".join(str(reason) for reason in reasons)


def _latest_run(
    records: EvalRecords, task: EvalTask, model: ModelRef, eval_set: EvalSet
) -> int:
    """Return the id of the model's latest complete, unlimited run of `task`.

    Raises:
        EvalNeedsError: There is none at the current task version for this set.
    """
    version = _single_version(task)
    matching = [
        run
        for run in records.runs()
        if run.suite == task.value
        and run.model == str(model)
        and run.task_version == version
        and run.set_id == eval_set.id
        and run.status is RunStatus.COMPLETE
        and not run.limited
    ]
    if not matching:
        raise _no_run(task, model, version)
    return max(run.id for run in matching)


def _shares(limit: int | None) -> tuple[int, int]:
    """Return how many judge and scorer reasons a run grades.

    With no `limit` it is 10 and 10; with one the cases split evenly and the odd
    one goes to the judge.
    """
    if limit is None:
        return REASONS_PER_TASK, REASONS_PER_TASK
    return limit - limit // 2, limit // 2


def prepare_reasons(
    store: Store,
    llm: LlmSettings,
    eval_settings: EvalSettings,
    *,
    model: ModelRef | None,
    limit: int | None,
) -> ReasonsPlan:
    """Work out a `reasons` run before any call.

    The reasons come from the model's latest complete, unlimited `judge` and
    `scorer` runs at the current task versions on the current set: the first cases
    by position that hold a reason. A case whose posting changed since the set was
    drawn is skipped and counted.

    Args:
        store: The open store.
        llm: The model settings, whose `[models].eval_grader` is the grader.
        eval_settings: The eval settings, whose jury identifies the set.
        model: The model whose reasons are graded, or `None` for `[models].judge`.
        limit: Grade this many reasons, from 1 to 100, split evenly with the odd
            one to the judge; 10 of each task when `None`.

    Returns:
        The plan.

    Raises:
        InvalidValueError: `limit` is outside 1 to 100, the model is the grader,
            or the model is on the set's jury.
        MissingModelError: No model or grader is given or configured.
        EvalNeedsError: No jury, rules, profile or set exists, fewer than 10 cases
            are labelled, a run to grade is missing, or no reason is left to grade.
        sqlite3.Error: The store cannot be read.
    """
    chosen = resolve_model(llm, Suite.REASONS, model)
    grader = llm.model_for(Task.EVAL_GRADER)
    if limit is not None:
        _check_limit(limit)
    if grader == chosen:
        raise InvalidValueError(
            "model", f"{chosen} is the grader and may not grade its own answers"
        )
    context = _context(store, eval_settings, chosen)
    for task in EvalTask:
        _labelled(store.evals, context.eval_set, task)
    runs = {
        task: _latest_run(store.evals, task, chosen, context.eval_set)
        for task in EvalTask
    }
    cases = store.evals.cases(context.eval_set.id)
    judge_share, scorer_share = _shares(limit)
    shares = {EvalTask.JUDGE: judge_share, EvalTask.SCORER: scorer_share}
    items: list[ReasonItem] = []
    skipped = 0
    for task in EvalTask:
        results = {r.case_key: r for r in store.evals.results(runs[task])}
        taken = 0
        for case in cases:
            result = results.get(str(case.posting_id))
            reason = None if result is None else _reason_text(task, result.answer)
            if reason is None:
                continue
            posting = unchanged_posting(store, case)
            if posting is None:
                skipped += 1
                continue
            if taken < shares[task]:
                items.append(
                    ReasonItem(
                        task=task,
                        posting_id=case.posting_id,
                        input_text=_reason_input(task, context, posting),
                        actual_output=reason,
                    )
                )
                taken += 1
    if not items:
        raise _none_remain(skipped)
    return ReasonsPlan(
        model=chosen,
        grader=grader,
        eval_set=context.eval_set,
        items=tuple(items),
        skipped=skipped,
        limited=limit is not None,
    )


def _reason_input(task: EvalTask, context: _Context, posting: Posting) -> str:
    """Write the grader's input: the rules or profile, then the posting block.

    The posting block has any field marker broken, so a posting cannot pose as
    a section of the grader's prompt.
    """
    from ijob_ai.tasks.text import (  # noqa: PLC0415
        neutralize_markers,
        numbered_rules,
        posting_block,
    )

    head = (
        numbered_rules(context.instructions.rules)
        if task is EvalTask.JUDGE
        else context.profile.to_json()
    )
    return f"{head}\n\n{neutralize_markers(posting_block(posting))}"


def _percentile(sorted_values: Sequence[int], fraction: float) -> int:
    """Return the nearest-rank percentile of non-empty, ascending values."""
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[rank - 1]


def _latency(finished: Sequence[_Finished[object]]) -> Metrics:
    """Return the median and 95th-percentile latency in milliseconds."""
    values = sorted(case.latency_ms for case in finished)
    if not values:
        return {"latency_p50_ms": None, "latency_p95_ms": None}
    return {
        "latency_p50_ms": int(statistics.median(values)),
        "latency_p95_ms": _percentile(values, _P95),
    }


def _elapsed_ms(started: float) -> int:
    """Return the milliseconds since `started` from `time.perf_counter`."""
    return round((time.perf_counter() - started) * 1000)


async def _execute[Item, Payload](  # noqa: PLR0913  # one run, its inputs one each
    runtime: LlmRuntime,
    store: Store,
    spend: EvalSpend,
    *,
    header: _Header,
    items: Sequence[Item],
    work: Callable[[Item, Budget], Awaitable[_Finished[Payload]]],
    summarize: Callable[[Sequence[_Finished[Payload]]], Metrics],
    now: Callable[[], datetime],
    on_outcome: Callable[[ItemOutcome[object, object]], None] | None,
    trusted: Callable[[Sequence[_Finished[Payload]]], bool] | None = None,
) -> _Executed:
    """Run the items under one budget and write the run row around them.

    The row is written, `running`, before any call. Each finished case's result is
    stored and the spend written as the case ends, so a cancelled run keeps both.
    The row is closed, with the metrics of the cases that finished, however the run
    ends; it is `complete` only when every item ended with a result and, when
    `trusted` is given, it accepts the finished cases.
    """
    cap = spend.cap
    run = spend.begin(
        header.suite.value,
        str(header.model),
        header.task_version,
        header.set_id,
        started_at=now(),
        limited=header.limited,
    )
    finished: list[_Finished[Payload]] = []

    def handle(outcome: ItemOutcome[Item, _Finished[Payload]]) -> None:
        if outcome.result is not None:
            done = outcome.result
            finished.append(done)
            store.evals.put_result(
                EvalResult(
                    run.id,
                    done.key,
                    done.passed,
                    done.answer,
                    done.detail,
                    done.latency_ms,
                )
            )
        spend.record()
        if on_outcome is not None:
            on_outcome(outcome)

    status = RunStatus.INCOMPLETE
    operation = OperationReport(done=0, not_done=0, spent=0.0, cap=cap, stopped_by=None)
    stop_error: LlmError | None = None
    metrics: Metrics = {}
    try:
        if items:
            result = await run_items(
                runtime,
                f"eval-{header.suite.value}",
                items,
                work,
                on_outcome=handle,
                cap=cap,
            )
            operation = result.report
            stop_error = next(
                (
                    outcome.error
                    for outcome in result.outcomes
                    if outcome.status in (ItemStatus.PROVIDER_ERROR, ItemStatus.STOPPED)
                ),
                None,
            )
            if all(outcome.status is ItemStatus.DONE for outcome in result.outcomes):
                status = RunStatus.COMPLETE
        else:
            status = RunStatus.COMPLETE
    finally:
        if (
            status is RunStatus.COMPLETE
            and trusted is not None
            and not trusted(finished)
        ):
            status = RunStatus.INCOMPLETE
        metrics = {**summarize(finished), "skipped": header.skipped}
        spend.finish(status, json.dumps(metrics), finished_at=now())
    return _Executed(run.id, status, operation, metrics, stop_error)


def _report(header: _Header, executed: _Executed, spend: EvalSpend) -> SuiteReport:
    """Build the report of a run from what `_execute` learned."""
    return SuiteReport(
        run_id=executed.run_id,
        suite=header.suite,
        model=header.model,
        status=executed.status,
        operation=executed.operation,
        metrics=executed.metrics,
        skipped=header.skipped,
        stop_error=executed.stop_error,
        subscription_spent=spend.subscription_spent,
    )


_TIMEOUT_TRIES: Final = 3


class _Asker:
    """Has the model under test judge or score a posting, charging the right budget."""

    def __init__(
        self,
        runtime: LlmRuntime,
        spend: EvalSpend,
        model: ModelRef,
        program: TunedProgram | None = None,
    ) -> None:
        self._runtime = runtime
        self._spend = spend
        self._model = model
        self._program = program

    def _charged(self, budget: Budget) -> Budget:
        """Return the budget this call charges, once it has room for a new call.

        Raises:
            CapReachedError: The budget has no room for another call.
        """
        charged = self._spend.budget_for(self._model, budget)
        if not charged.try_start():
            raise CapReachedError
        return charged

    @staticmethod
    async def _asked[Answer](ask: Callable[[], Awaitable[Answer]]) -> Answer:
        """Ask, and ask again when the call times out.

        A call that stalls is rare but ends a long run, so it gets more tries. Any
        other failure is not retried.

        Raises:
            ProviderFailedError: The last try timed out, or a try failed otherwise.
        """
        tries_left = _TIMEOUT_TRIES
        while True:
            try:
                return await ask()
            except ProviderFailedError as error:
                tries_left -= 1
                if not tries_left or not isinstance(error.__cause__, TimeoutError):
                    raise

    async def judge(
        self, posting: Posting, rules: Sequence[str], budget: Budget
    ) -> Verdict | TaskOutputError:
        """Judge `posting`; an unusable answer comes back as the error.

        Raises:
            LlmError: The call fails for any reason but an unusable answer.
        """
        from ijob_ai.tasks.judge import judge_posting  # noqa: PLC0415

        async def ask() -> Verdict:
            charged = self._charged(budget)
            registry = await self._runtime.registry_for_ref(self._model)
            return await judge_posting(
                posting,
                rules,
                lm=registry.lm_for_ref(self._model),
                provider=self._model.provider,
                budget=charged,
                timeout_s=self._runtime.settings.timeout_s,
                program=self._program,
            )

        try:
            return await self._asked(ask)
        except TaskOutputError as error:
            return error

    async def score(
        self, posting: Posting, profile: ResumeProfile, budget: Budget
    ) -> Score | TaskOutputError:
        """Score `posting`; an unusable answer comes back as the error.

        Raises:
            LlmError: The call fails for any reason but an unusable answer.
        """
        from ijob_ai.tasks.scorer import score_posting  # noqa: PLC0415

        async def ask() -> Score:
            charged = self._charged(budget)
            registry = await self._runtime.registry_for_ref(self._model)
            return await score_posting(
                posting,
                profile,
                lm=registry.lm_for_ref(self._model),
                provider=self._model.provider,
                budget=charged,
                timeout_s=self._runtime.settings.timeout_s,
                program=self._program,
            )

        try:
            return await self._asked(ask)
        except TaskOutputError as error:
            return error


def _word(*, keep: bool) -> str:
    """Name a keep or reject answer for the keep-or-reject metric."""
    return _ANSWER_KEEP if keep else _ANSWER_REJECT


async def run_judge_suite(  # noqa: PLR0913  # the run's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    plan: SuitePlan,
    spend: EvalSpend,
    *,
    now: Callable[[], datetime],
    on_outcome: Callable[[ItemOutcome[object, object]], None] | None = None,
) -> SuiteReport:
    """Run the judge on each labelled case and score its answers against the labels.

    Each case's answer, whether it matched the label's keep or reject, and its
    latency are stored as the case ends. An unusable answer is stored as `null`
    and counts as wrong. A provider failure or an unknown cost stops the run, and
    so does the cap; the run is then `incomplete`, and so is a run in which any case
    ended on the cap. The row is closed, with its spend, however the run ends. A
    plan with a program runs every call with it and stores the row under its
    version.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        plan: The plan from `prepare_suite` for `judge`.
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
    from ijob_ai.evals.deepeval_metrics import keep_match, measure_case  # noqa: PLC0415
    from ijob_ai.evals.metrics import (  # noqa: PLC0415
        JudgeAnswer,
        JudgeCase,
        judge_metrics,
        wilson,
    )

    asker = _Asker(runtime, spend, plan.model, plan.program)

    async def work(case: SuiteCase, budget: Budget) -> _Finished[JudgeCase]:
        started = time.perf_counter()
        answer = await asker.judge(case.posting, plan.instructions.rules, budget)
        latency_ms = _elapsed_ms(started)
        gold = case.label.keep is True
        gold_rule = case.label.rule
        if isinstance(answer, TaskOutputError):
            return _Finished(
                key=str(case.posting.id),
                passed=False,
                answer="null",
                detail=str(answer),
                latency_ms=latency_ms,
                payload=JudgeCase(gold, gold_rule, None),
            )
        measured = await measure_case(
            keep_match(),
            input=str(case.posting.id),
            actual_output=_word(keep=answer.keep),
            expected_output=_word(keep=gold),
        )
        stored = {"keep": answer.keep, "rule": answer.rule, "reason": answer.reason}
        return _Finished(
            key=str(case.posting.id),
            passed=measured.passed,
            answer=json.dumps(stored),
            detail="",
            latency_ms=latency_ms,
            payload=JudgeCase(gold, gold_rule, JudgeAnswer(answer.keep, answer.rule)),
        )

    def summarize(finished: Sequence[_Finished[JudgeCase]]) -> Metrics:
        cases = [case.payload for case in finished]
        numbers = judge_metrics(cases)
        labelled_keep = sum(1 for case in cases if case.gold_keep)
        right_keep = sum(
            1
            for case in cases
            if case.gold_keep and case.answer is not None and case.answer.keep
        )
        interval = wilson(right_keep, labelled_keep)
        return {
            "keep_recall": numbers.keep_recall,
            "margin": None if interval is None else interval.margin,
            "keep_precision": numbers.keep_precision,
            "macro_f1": numbers.macro_f1,
            "right_rule": numbers.right_rule,
            "cases": len(cases),
            "unusable": sum(1 for case in cases if case.answer is None),
            "passed": sum(1 for case in finished if case.passed),
            **_latency(finished),
        }

    header = _Header(
        suite=Suite.JUDGE,
        model=plan.model,
        task_version=plan.task_version,
        set_id=plan.eval_set.id,
        limited=plan.limited,
        skipped=plan.skipped,
    )
    executed = await _execute(
        runtime,
        store,
        spend,
        header=header,
        items=plan.cases,
        work=work,
        summarize=summarize,
        now=now,
        on_outcome=on_outcome,
    )
    return _report(header, executed, spend)


async def run_scorer_suite(  # noqa: PLR0913  # the run's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    plan: SuitePlan,
    spend: EvalSpend,
    *,
    now: Callable[[], datetime],
    on_outcome: Callable[[ItemOutcome[object, object]], None] | None = None,
) -> SuiteReport:
    """Run the scorer on each labelled case and compare its scores with the labels.

    Each case's answer, whether it came within 20 points of the label, and its
    latency are stored as the case ends. An unusable answer is stored as `null`
    and counts as wrong. The run ends, its row closes and a program applies, as
    `run_judge_suite` describes.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        plan: The plan from `prepare_suite` for `scorer`.
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
    from ijob_ai.evals.deepeval_metrics import measure_case, score_close  # noqa: PLC0415
    from ijob_ai.evals.metrics import ScorerCase, scorer_metrics  # noqa: PLC0415

    asker = _Asker(runtime, spend, plan.model, plan.program)

    async def work(case: SuiteCase, budget: Budget) -> _Finished[ScorerCase]:
        started = time.perf_counter()
        answer = await asker.score(case.posting, plan.profile, budget)
        latency_ms = _elapsed_ms(started)
        gold = case.label.score or 0  # a scorer label always holds a score
        if isinstance(answer, TaskOutputError):
            return _Finished(
                key=str(case.posting.id),
                passed=False,
                answer="null",
                detail=str(answer),
                latency_ms=latency_ms,
                payload=ScorerCase(gold, None),
            )
        measured = await measure_case(
            score_close(),
            input=str(case.posting.id),
            actual_output=str(answer.score),
            expected_output=str(gold),
        )
        stored = {"score": answer.score, "reasons": list(answer.reasons)}
        return _Finished(
            key=str(case.posting.id),
            passed=measured.passed,
            answer=json.dumps(stored),
            detail="",
            latency_ms=latency_ms,
            payload=ScorerCase(gold, answer.score),
        )

    def summarize(finished: Sequence[_Finished[ScorerCase]]) -> Metrics:
        cases = [case.payload for case in finished]
        numbers = scorer_metrics(cases)
        return {
            "ndcg": numbers.ndcg,
            "margin": None,
            "spearman": numbers.spearman,
            "mean_error": numbers.mean_error,
            "cases": len(cases),
            "unusable": sum(1 for case in cases if case.answer is None),
            "passed": sum(1 for case in finished if case.passed),
            **_latency(finished),
        }

    header = _Header(
        suite=Suite.SCORER,
        model=plan.model,
        task_version=plan.task_version,
        set_id=plan.eval_set.id,
        limited=plan.limited,
        skipped=plan.skipped,
    )
    executed = await _execute(
        runtime,
        store,
        spend,
        header=header,
        items=plan.cases,
        work=work,
        summarize=summarize,
        now=now,
        on_outcome=on_outcome,
    )
    return _report(header, executed, spend)


@dataclass(frozen=True, slots=True)
class _HostileItem:
    """One pair to run, with the task it tests."""

    task: EvalTask
    pair: HostilePair


def _hostile_posting(pair: HostilePair, *, hostile: bool, at: datetime) -> Posting:
    """Build the made-up posting of a pair, clean or with the injected text."""
    return Posting(
        id=PostingId(0),
        title=pair.title,
        company=pair.company,
        location=pair.location,
        url=f"https://example.com/hostile/{pair.name}",
        posted_at=None,
        first_seen=at,
        last_seen=at,
        description=pair.hostile_description if hostile else pair.description,
    )


async def run_hostile_suite(  # noqa: PLR0913  # the run's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    model: ModelRef,
    spend: EvalSpend,
    *,
    now: Callable[[], datetime],
    on_outcome: Callable[[ItemOutcome[object, object]], None] | None = None,
    program: TunedProgram | None = None,
    task: EvalTask | None = None,
) -> SuiteReport:
    """Run the made-up pairs and count those whose answer the injected text changed.

    Each pair is run clean and hostile with the model under test, for the judge and
    the scorer. A keep or reject that flips, or a score that moves by more than 20
    points, is an attack; a pair with an unusable answer is counted apart and is no
    attack. The suite uses no labels, so any model may be tested, a juror included.
    It fails, with `SuiteReport.exit_status` 1, on any attack, on no answered pair
    and on any incomplete run. The run's task version holds both versions.

    With a `program`, only the pairs of its task run, every call runs with it, and
    the row is limited and stored under the program's version, so it is never the
    current run of `eval report`. The rules and profile are the suite's made-up
    ones, so the program's bound rule does not apply. A `task` without a program
    runs that task's pairs on the base prompt, under its version, also limited.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        model: The model under test.
        spend: The command's spending; it must not have begun a run.
        now: The wall clock, returning timezone-aware times.
        on_outcome: Called, on the event loop, with each pair's outcome.
        program: A tuned program to run every call with, or `None` for the base
            prompt.
        task: Run only this task's pairs; the program's task when `None` and a
            program is given, else both tasks.

    Returns:
        What the run did.

    Raises:
        InvalidValueError: `task` is not the task of `program`.
        asyncio.CancelledError: The call was cancelled; the results already stored
            and the run row stand.
        ExceptionGroup: A call raised something other than an `LlmError`.
        sqlite3.Error: The store cannot be written.
    """
    from ijob_ai.evals.hostile import load_hostile  # noqa: PLC0415
    from ijob_ai.evals.metrics import (  # noqa: PLC0415
        attack_rate,
        keep_attacked,
        score_attacked,
        wilson,
    )

    if program is not None:
        if task is not None and task is not program.task:
            raise InvalidValueError(
                "task",
                f"program {program.id} is for {program.task.value}, not {task.value}",
            )
        task = program.task
    data = load_hostile()
    asker = _Asker(runtime, spend, model, program)
    items = [
        _HostileItem(EvalTask.JUDGE, pair)
        for pair in data.judge
        if task in (None, EvalTask.JUDGE)
    ] + [
        _HostileItem(EvalTask.SCORER, pair)
        for pair in data.scorer
        if task in (None, EvalTask.SCORER)
    ]

    async def judge_pair(
        pair: HostilePair, budget: Budget, at: datetime
    ) -> tuple[bool | None, bool | None]:
        answers: list[bool | None] = []
        for hostile in (False, True):
            posting = _hostile_posting(pair, hostile=hostile, at=at)
            verdict = await asker.judge(posting, data.rules, budget)
            answers.append(
                None if isinstance(verdict, TaskOutputError) else verdict.keep
            )
        return answers[0], answers[1]

    async def score_pair(
        pair: HostilePair, budget: Budget, at: datetime
    ) -> tuple[int | None, int | None]:
        answers: list[int | None] = []
        for hostile in (False, True):
            posting = _hostile_posting(pair, hostile=hostile, at=at)
            score = await asker.score(posting, data.profile, budget)
            answers.append(None if isinstance(score, TaskOutputError) else score.score)
        return answers[0], answers[1]

    async def work(item: _HostileItem, budget: Budget) -> _Finished[bool | None]:
        started = time.perf_counter()
        shown: tuple[str | int | None, str | int | None]
        if item.task is EvalTask.JUDGE:
            clean_keep, hostile_keep = await judge_pair(item.pair, budget, now())
            attacked = keep_attacked(clean=clean_keep, hostile=hostile_keep)
            shown = tuple(  # type: ignore[assignment]  # two answers, as a pair
                None if keep is None else _word(keep=keep)
                for keep in (clean_keep, hostile_keep)
            )
        else:
            clean_score, hostile_score = await score_pair(item.pair, budget, now())
            attacked = score_attacked(clean=clean_score, hostile=hostile_score)
            shown = (clean_score, hostile_score)
        details = [
            f"{name} answer unusable"
            for name, answer in zip(("clean", "hostile"), shown, strict=True)
            if answer is None
        ]
        if attacked:
            details.append("changed")
        return _Finished(
            key=f"{item.task.value}:{item.pair.name}",
            passed=attacked is False,
            answer=json.dumps({"clean": shown[0], "hostile": shown[1]}),
            detail="; ".join(details),
            latency_ms=_elapsed_ms(started),
            payload=attacked,
        )

    def summarize(finished: Sequence[_Finished[bool | None]]) -> Metrics:
        counted = attack_rate([case.payload for case in finished])
        interval = wilson(counted.attacks, counted.answered)
        return {
            "attack_rate": counted.rate,
            "margin": None if interval is None else interval.margin,
            "attacks": counted.attacks,
            "answered": counted.answered,
            "unanswered": counted.unanswered,
            "pairs": len(finished),
            **_latency(finished),
        }

    header = _Header(
        suite=Suite.HOSTILE,
        model=model,
        task_version=(
            _task_version()
            if task is None
            else program.version
            if program is not None
            else _single_version(task)
        ),
        set_id=None,
        limited=task is not None,
        skipped=0,
    )
    executed = await _execute(
        runtime,
        store,
        spend,
        header=header,
        items=items,
        work=work,
        summarize=summarize,
        now=now,
        on_outcome=on_outcome,
    )
    return _report(header, executed, spend)


async def run_reasons_suite(  # noqa: PLR0913  # the run's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    plan: ReasonsPlan,
    spend: EvalSpend,
    *,
    now: Callable[[], datetime],
    on_outcome: Callable[[ItemOutcome[object, object]], None] | None = None,
) -> SuiteReport:
    """Have the grader check each reason against the facts in its input.

    One grader call per reason; the model under test is not called. The grade is a
    score from 0.0 to 1.0, and a reason passes at 0.5 or above. A reason whose
    grade is unusable is stored as ungraded and counted, and is left out of the
    mean. A grader provider failure or unknown cost stops the run, as the cap does.
    Only the grader's spend counts toward the cap, if it is on OpenRouter.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        plan: The plan from `prepare_reasons`.
        spend: The command's spending; it must not have begun a run.
        now: The wall clock, returning timezone-aware times.
        on_outcome: Called, on the event loop, with each reason's outcome.

    Returns:
        What the run did.

    Raises:
        asyncio.CancelledError: The call was cancelled; the results already stored
            and the run row stand.
        ExceptionGroup: A call raised something other than an `LlmError`.
        sqlite3.Error: The store cannot be written.
    """
    from ijob_ai.evals.deepeval_metrics import grade_reason, make_grader  # noqa: PLC0415

    async def work(item: ReasonItem, budget: Budget) -> _Finished[float | None]:
        started = time.perf_counter()
        registry = await runtime.registry_for_ref(plan.grader)
        grader = make_grader(
            lm=registry.lm_for_ref(plan.grader),
            provider=plan.grader.provider,
            budget=spend.budget_for(plan.grader, budget),
            timeout_s=runtime.settings.timeout_s,
            model_name=str(plan.grader),
        )
        try:
            graded = await grade_reason(
                grader, input=item.input_text, actual_output=item.actual_output
            )
        except TaskOutputError as error:
            return _Finished(
                key=item.key,
                passed=False,
                answer=json.dumps({"score": None, "reason": None}),
                detail=f"ungraded: {error}",
                latency_ms=_elapsed_ms(started),
                payload=None,
            )
        return _Finished(
            key=item.key,
            passed=graded.passed,
            answer=json.dumps({"score": graded.score, "reason": graded.reason}),
            detail="",
            latency_ms=_elapsed_ms(started),
            payload=graded.score,
        )

    def summarize(finished: Sequence[_Finished[float | None]]) -> Metrics:
        scores = [case.payload for case in finished if case.payload is not None]
        return {
            "mean_score": statistics.fmean(scores) if scores else None,
            "margin": None,
            "graded": len(scores),
            "ungraded": len(finished) - len(scores),
            "passed": sum(1 for case in finished if case.passed),
            "cases": len(plan.items),
            **_latency(finished),
        }

    header = _Header(
        suite=Suite.REASONS,
        model=plan.model,
        task_version=_task_version(),
        set_id=plan.eval_set.id,
        limited=plan.limited,
        skipped=plan.skipped,
    )
    executed = await _execute(
        runtime,
        store,
        spend,
        header=header,
        items=plan.items,
        work=work,
        summarize=summarize,
        now=now,
        on_outcome=on_outcome,
    )
    return _report(header, executed, spend)
