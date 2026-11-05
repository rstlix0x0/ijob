"""Freezing the tuning split of a label set, reporting on it, and screening models.

`freeze_split` divides the labelled postings of a set into train, val, held-out
and unused once, and refuses while labels still wait for review or when too few
postings are kept to tune on. `split_report` reads what a frozen split holds and
what has happened since; it writes nothing.

`full_run` finds the run that already measures a model on every current label of
a task. `prepare_screen` works out which model and task pairs a screen still
needs, refusing before any spend what the screen could not run, and `run_screen`
freezes the split and runs those pairs one after another through a caller-given
runner, so each is an ordinary eval run with its own spend.

`program_status` says whether a tuned program is tested, accepted, or accepted
but not in use, and `program_in_force` returns a task's accepted program only
while it is in use.

`prepare_accept` refuses a program that cannot be accepted, `accept_program` runs
the `hostile` suite on it through a caller-given runner and accepts it only with
no attack and every pair answered, and `clear_program` goes back to the base
prompt.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, override

from ijob_ai.errors import LlmError, ProviderUnavailableError
from ijob_ai.llm.reference import ModelRef
from ijob_ai.llm.settings import EvalSettings, LlmSettings, Task, TuneSettings
from ijob_ai.tasks.versions import (
    JUDGE_VERSION,
    SCORER_VERSION,
    lacks_untrusted_sentence,
)
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalRun, EvalSet, EvalTask, RunStatus
from ijob_core.eval_rules import tune_split
from ijob_core.model import PostingId
from ijob_core.store import Store
from ijob_core.tune_records import SplitPart, TunedProgram, TuneSplitRow

from ijob_app.eval_estimate import Cost, sum_costs
from ijob_app.eval_ops import EvalNeedsError, review_items
from ijob_app.eval_run import RunOffer
from ijob_app.eval_spend import EvalBudgetSpentError
from ijob_app.eval_suites import (
    Suite,
    SuiteReport,
    prepare_suite,
    refuse_juror,
    unchanged_posting,
)

MIN_TUNING_KEEPS: Final = 20


class TooFewKeepsError(LlmError):
    """The labelled postings leave too few keeps and rejects to tune on.

    Attributes:
        keeps: How many labelled postings are kept.
        rejects: How many are rejected.
        n: How many keeps and rejects tuning would use of each.
    """

    def __init__(self, keeps: int, rejects: int, n: int) -> None:
        """Build the error from the labelled counts and the size tuning would get.

        Args:
            keeps: How many labelled postings are kept.
            rejects: How many are rejected.
            n: How many of each tuning would use.
        """
        super().__init__(
            f"{keeps} kept and {rejects} rejected postings leave {n} of each to "
            f"tune on, and tuning needs at least {MIN_TUNING_KEEPS}; "
            "label more with `ijob eval label --add`"
        )
        self.keeps = keeps
        self.rejects = rejects
        self.n = n


@dataclass(frozen=True, slots=True)
class SplitReport:
    """What a set's split holds, and what has happened since it was frozen.

    Attributes:
        frozen: Whether the set has a split.
        train: Postings in the train part.
        val: Postings in the val part.
        held_out: Postings in the held-out part.
        unused: Postings in no tuning part.
        added_later: Postings labelled after the freeze, left out of the split.
        changed: Labelled postings whose text changed since the draw or that are
            gone, whether the freeze left them out or they are in the split.
    """

    frozen: bool
    train: int
    val: int
    held_out: int
    unused: int
    added_later: int
    changed: int


def _awaiting_review(count: int) -> EvalNeedsError:
    """Build the refusal for labels that still wait for `ijob eval review`."""
    return EvalNeedsError(
        f"{count} labels still wait for review before the split is frozen",
        "run `ijob eval review`",
    )


def _not_current() -> EvalNeedsError:
    """Build the refusal for a set that is not the one for the stored inputs."""
    return EvalNeedsError(
        "the label set is not the one for the stored rules, CV profile and jury",
        "run `ijob eval label`",
    )


def _both_labelled(store: Store, eval_set: EvalSet) -> dict[PostingId, bool]:
    """Map each case with a judge and a scorer label to whether it is kept."""
    records = store.evals
    judged = {
        label.posting_id: bool(label.keep)
        for label in records.labels(eval_set.id, EvalTask.JUDGE)
    }
    scored = {
        label.posting_id for label in records.labels(eval_set.id, EvalTask.SCORER)
    }
    return {
        case.posting_id: judged[case.posting_id]
        for case in records.cases(eval_set.id)
        if case.posting_id in judged and case.posting_id in scored
    }


def _divide(
    store: Store,
    eval_set: EvalSet,
    settings: EvalSettings,
    now: Callable[[], datetime],
) -> dict[PostingId, SplitPart]:
    """Work out the split of a set that has none, refusing what could not be split.

    Raises:
        EvalNeedsError: The set is not the one for the stored rules, CV profile and
            jury, or labels still wait for `ijob eval review`.
        TooFewKeepsError: Fewer than 20 keeps and rejects are left to tune on.
        sqlite3.Error: The store cannot be read.
    """
    review = review_items(store, settings, now=now)
    if review.set_id != eval_set.id:
        raise _not_current()
    if review.items:
        raise _awaiting_review(len(review.items))
    keeps: list[PostingId] = []
    rejects: list[PostingId] = []
    cases = {case.posting_id: case for case in store.evals.cases(eval_set.id)}
    for posting_id, keep in _both_labelled(store, eval_set).items():
        if unchanged_posting(store, cases[posting_id]) is None:
            continue
        (keeps if keep else rejects).append(posting_id)
    parts, n = tune_split(
        eval_set.rules_sha256, eval_set.profile_sha256, keeps, rejects
    )
    if n < MIN_TUNING_KEEPS:
        raise TooFewKeepsError(len(keeps), len(rejects), n)
    return parts


def check_split(
    store: Store,
    eval_set: EvalSet,
    settings: EvalSettings,
    *,
    now: Callable[[], datetime],
) -> None:
    """Refuse what `freeze_split` would refuse, and freeze nothing.

    A set with a frozen split passes.

    Args:
        store: The open store.
        eval_set: The label set.
        settings: The eval settings, whose jury names the set the review covers.
        now: The wall clock, returning timezone-aware times.

    Raises:
        EvalNeedsError: The set is not the one for the stored rules, CV profile and
            jury, or labels still wait for `ijob eval review`.
        TooFewKeepsError: Fewer than 20 keeps and rejects are left to tune on.
        sqlite3.Error: The store cannot be read.
    """
    if not store.tunes.has_split(eval_set.id):
        _divide(store, eval_set, settings, now)


def freeze_split(
    store: Store,
    eval_set: EvalSet,
    settings: EvalSettings,
    *,
    now: Callable[[], datetime],
) -> list[TuneSplitRow]:
    """Freeze the set's split, or return the one already frozen.

    The split covers the postings that have both a judge and a scorer label and
    whose text is as it was when the set was drawn; others are skipped.

    Args:
        store: The open store.
        eval_set: The label set.
        settings: The eval settings, whose jury names the set the review covers.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The rows of the split, by posting.

    Raises:
        EvalNeedsError: The set is not the one for the stored rules, CV profile and
            jury, or labels still wait for `ijob eval review`; nothing is frozen.
        TooFewKeepsError: Fewer than 20 keeps and rejects are left to tune on;
            nothing is frozen.
        sqlite3.Error: The store cannot be read or written.
    """
    if not store.tunes.has_split(eval_set.id):
        store.tunes.put_split(eval_set.id, _divide(store, eval_set, settings, now))
    return store.tunes.split(eval_set.id)


def split_report(store: Store, eval_set: EvalSet) -> SplitReport:
    """Count the parts of the set's split and what it left out since the freeze.

    Every posting in the split is counted in its part, which never changes; one
    whose text changed since the draw or that is gone is counted as changed as
    well. Of the labelled postings outside the split, those whose text is as drawn
    count as added later and the rest as changed. A set with no split reports
    zeros and `frozen=False`.

    Args:
        store: The open store.
        eval_set: The label set.

    Returns:
        The counts. Nothing is written.
    """
    rows = store.tunes.split(eval_set.id)
    if not rows:
        return SplitReport(
            frozen=False, train=0, val=0, held_out=0, unused=0, added_later=0, changed=0
        )
    frozen = {row.posting_id for row in rows}
    cases = {case.posting_id: case for case in store.evals.cases(eval_set.id)}
    added = changed = 0
    for posting_id in frozen:
        if unchanged_posting(store, cases[posting_id]) is None:
            changed += 1
    for posting_id in _both_labelled(store, eval_set):
        if posting_id in frozen:
            continue
        if unchanged_posting(store, cases[posting_id]) is None:
            changed += 1
        else:
            added += 1
    counts = {part: sum(row.part is part for row in rows) for part in SplitPart}
    return SplitReport(
        frozen=True,
        train=counts[SplitPart.TRAIN],
        val=counts[SplitPart.VAL],
        held_out=counts[SplitPart.HELD_OUT],
        unused=counts[SplitPart.UNUSED],
        added_later=added,
        changed=changed,
    )


@dataclass(frozen=True, slots=True)
class ScreenPair:
    """One model measured on one task.

    Attributes:
        task: `judge` or `scorer`.
        model: The model to measure.
    """

    task: EvalTask
    model: ModelRef

    @property
    def suite(self) -> Suite:
        """The eval suite that runs the pair."""
        return Suite.JUDGE if self.task is EvalTask.JUDGE else Suite.SCORER


@dataclass(frozen=True, slots=True)
class ScreenPlan:
    """What a screen still has to run, worked out before any call.

    Attributes:
        eval_set: The label set the screen measures.
        pairs: The pairs with no full run yet, model by model, judge then scorer.
        skipped: Configured models left out because they are jurors of the set.
    """

    eval_set: EvalSet
    pairs: tuple[ScreenPair, ...]
    skipped: tuple[ModelRef, ...]


def _current_version(task: EvalTask) -> str:
    """Return the version of the task's current instructions."""
    from ijob_ai.tasks.signatures import JUDGE_VERSION, SCORER_VERSION  # noqa: PLC0415

    return JUDGE_VERSION if task is EvalTask.JUDGE else SCORER_VERSION


def full_run(
    store: Store, task: EvalTask, model: ModelRef, eval_set: EvalSet
) -> EvalRun | None:
    """Find the newest run that measures a model on every current case of a task.

    A run is full when it is complete, not limited, at the task's current version,
    on this set, and holds a result for every case that has a label for the task
    and a posting whose text is as drawn. A changed posting can never have a
    result, so it counts against no run. Runs that are limited, incomplete or at
    another version are passed over, however recent.

    Args:
        store: The open store.
        task: `judge` or `scorer`.
        model: The model under test.
        eval_set: The label set.

    Returns:
        The run, or `None` when there is none.
    """
    version = _current_version(task)
    labelled = {label.posting_id for label in store.evals.labels(eval_set.id, task)}
    needed = {
        str(case.posting_id)
        for case in store.evals.cases(eval_set.id)
        if case.posting_id in labelled and unchanged_posting(store, case) is not None
    }
    for run in reversed(store.evals.runs()):
        if (
            run.suite == task.value
            and run.model == str(model)
            and run.task_version == version
            and run.set_id == eval_set.id
            and run.status is RunStatus.COMPLETE
            and not run.limited
            and needed <= {result.case_key for result in store.evals.results(run.id)}
        ):
            return run
    return None


@dataclass(frozen=True, slots=True)
class ProgramStatus:
    """What `tune list` says about one tuned program.

    Attributes:
        accepted: Whether the program is the accepted one of its task.
        tested: Whether a held-out run of the program is recorded; read only for
            a program that is not accepted.
        not_in_use: Why an accepted program does not run, or `None` when it runs
            or is not accepted. A program that lacks the untrusted-data sentence is
            named here though search stops for it rather than use the base prompt.
    """

    accepted: bool
    tested: bool
    not_in_use: str | None = None

    @property
    def in_force(self) -> bool:
        """Whether the program is accepted and runs."""
        return self.accepted and self.not_in_use is None

    @override
    def __str__(self) -> str:
        """Render `tested`, `untested`, `accepted` or `accepted, not in use: ...`."""
        if not self.accepted:
            return "tested" if self.tested else "untested"
        if self.not_in_use is None:
            return "accepted"
        return f"accepted, not in use: {self.not_in_use}"


def _task_key(task: EvalTask) -> Task:
    return Task.JUDGE if task is EvalTask.JUDGE else Task.SCORER


_RETUNE: Final = "tune again with `ijob tune screen` and `ijob tune run`"


def _unmet_and_fix(
    store: Store, llm: LlmSettings, program: TunedProgram
) -> tuple[str, str] | None:
    """Name the first of the three conditions a program fails and how to mend it.

    Returns:
        The reason and the fix, or `None` if every condition holds.
    """
    task = program.task
    configured = llm.models.get(_task_key(task))
    set_model = f"set [models].{task.value} to {program.model}"
    if configured is None:
        return f"no model is set for {task.value}", set_model
    if str(configured) != program.model:
        return f"the {task.value} model is {configured}, not {program.model}", set_model
    if task is EvalTask.JUDGE:
        instructions = store.instructions()
        current = None if instructions is None else instructions.rules_sha256
        what, base = "rules", JUDGE_VERSION
    else:
        stored = store.cv_profile()
        current = None if stored is None else stored.profile.sha256()
        what, base = "profile", SCORER_VERSION
    if current != program.bound_sha256:
        return f"the {what} changed since it was tuned", _RETUNE
    if program.base_version != base:
        return (
            f"it was tuned from {program.base_version}, the task is now {base}",
            _RETUNE,
        )
    return None


def _unmet(store: Store, llm: LlmSettings, program: TunedProgram) -> str | None:
    """Name the first of the three conditions a program fails, or `None` if it holds."""
    found = _unmet_and_fix(store, llm, program)
    return None if found is None else found[0]


def program_status(
    store: Store, llm: LlmSettings, program: TunedProgram
) -> ProgramStatus:
    """Say whether a program is tested, or whether an accepted one is in use.

    A program is in force only while its model is the task's configured model,
    its bound matches the stored rules (judge) or profile (scorer), and its base
    version is the task's current one, and a program that lacks the task's
    untrusted-data sentence is not in use either, since search stops for it. It is
    tested when a complete, limited run of its task on its model at its version,
    on its label set, is recorded. It never raises.

    Args:
        store: The open store.
        llm: The settings naming each task's model.
        program: The program.

    Returns:
        The status. Nothing is written.
    """
    accepted = program.accepted_at is not None
    tested = any(
        run.suite == program.task.value
        and run.model == program.model
        and run.task_version == program.version
        and run.set_id == program.set_id
        and run.status is RunStatus.COMPLETE
        and run.limited
        for run in store.evals.runs()
    )
    not_in_use = None
    if accepted:
        not_in_use = _unmet(store, llm, program)
        if not_in_use is None and lacks_untrusted_sentence(
            program.task, program.instructions
        ):
            not_in_use = (
                "its instructions lack the untrusted-data sentence, so search stops; "
                f"`ijob tune clear {program.task.value}` goes back to the base prompt"
            )
    return ProgramStatus(accepted=accepted, tested=tested, not_in_use=not_in_use)


def program_in_force(
    store: Store, llm: LlmSettings, task: EvalTask
) -> TunedProgram | None:
    """Return the accepted program of a task while it is in force.

    Args:
        store: The open store.
        llm: The settings naming each task's model.
        task: `judge` or `scorer`.

    Returns:
        The program, or `None` when none is accepted or the accepted one is not
        in use, so the base prompt runs.

    Raises:
        InvalidValueError: Naming the id, if the accepted row is malformed or,
            while it is in use, its instructions lack the task's untrusted-data
            sentence. Running with it would fail every call, so the task stops
            until `clear_program` unmarks it.
    """
    program = store.tunes.accepted(task)
    if program is None or _unmet(store, llm, program) is not None:
        return None
    if lacks_untrusted_sentence(task, program.instructions):
        reason = f"program {program.id} lacks the untrusted-data sentence"
        raise InvalidValueError("instructions", reason)
    return program


@dataclass(frozen=True, slots=True)
class AcceptOffer:
    """What accepting a program will run and cost, worked out before any call.

    Attributes:
        program: The program to accept.
        offer: The `hostile` run offer for the program's model and task.
    """

    program: TunedProgram
    offer: RunOffer


@dataclass(frozen=True, slots=True)
class AcceptReport:
    """How accepting a program ended.

    Attributes:
        program: The program that was tried.
        run_id: The `hostile` run row written for it.
        status: `complete` or `incomplete`.
        pairs: The pairs the run covered, a clean and a hostile posting each.
        attacks: Pairs whose answer the injected text changed.
        answered: Pairs with both answers usable.
        unanswered: Pairs with an unusable answer.
        accepted: Whether the program is now the accepted one of its task.
        stopped: Why the run stopped early, or `None`. It can hold text a
            provider sent, so clean it before showing it.
    """

    program: TunedProgram
    run_id: int
    status: RunStatus
    pairs: int
    attacks: int
    answered: int
    unanswered: int
    accepted: bool
    stopped: str | None

    @property
    def exit_status(self) -> int:
        """The command's exit status: 0 when the program was accepted, else 1."""
        return 0 if self.accepted else 1


def prepare_accept(store: Store, llm: LlmSettings, program_id: int) -> TunedProgram:
    """Find a program and refuse what could not be accepted, before any spend.

    Args:
        store: The open store.
        llm: The settings naming each task's model.
        program_id: The program's number.

    Returns:
        The program.

    Raises:
        InvalidValueError: No program has that number, a stored row is malformed
            (the message names its id), or the program's instructions lack the
            task's untrusted-data sentence.
        EvalNeedsError: The program has no complete held-out run, or its model,
            bound or base version is not the task's now; the fix says what to
            change.
        sqlite3.Error: The store cannot be read.
    """
    program = store.tunes.program(program_id)
    if program is None:
        msg = f"program {program_id} is unknown; `ijob tune list` shows the programs"
        raise InvalidValueError("program", msg)
    label = f"program {program.id}"
    if not program_status(store, llm, program).tested:
        missing = f"{label} has no complete held-out run"
        raise EvalNeedsError(missing, "run `ijob tune run`")
    unmet = _unmet_and_fix(store, llm, program)
    if unmet is not None:
        reason, fix = unmet
        what = f"{label} would not be used in search: {reason}"
        raise EvalNeedsError(what, fix)
    if lacks_untrusted_sentence(program.task, program.instructions):
        reason = f"{label} lacks the untrusted-data sentence"
        raise InvalidValueError("instructions", reason)
    return program


def _count(report: SuiteReport, key: str) -> int:
    """Read one count of a `hostile` run's metrics; an absent one reads as 0."""
    return int(report.metrics.get(key) or 0)


async def accept_program(
    store: Store,
    program: TunedProgram,
    run: Callable[[], Awaitable[SuiteReport]],
    *,
    now: Callable[[], datetime],
) -> AcceptReport:
    """Run the hostile suite on a program, and accept it if nothing went wrong.

    The program is accepted only when the run is complete, has no attack and every
    pair of the program's task is answered; accepting it unmarks any other accepted
    program of the task. Otherwise nothing changes.

    Args:
        store: The open store.
        program: The program from `prepare_accept`.
        run: Runs the `hostile` suite with the program and returns its report.
        now: The wall clock, returning timezone-aware times.

    Returns:
        What the run found and whether the program was accepted.

    Raises:
        EvalBudgetSpentError: The recorded spending is used up; nothing ran.
        ProviderUnavailableError: A variable that redirects model calls is set.
        asyncio.CancelledError: The call was cancelled; the run row stands.
        sqlite3.Error: The store cannot be read or written.
    """
    report = await run()
    pairs = _count(report, "pairs")
    attacks = _count(report, "attacks")
    answered = _count(report, "answered")
    unanswered = _count(report, "unanswered")
    accepted = (
        report.status is RunStatus.COMPLETE
        and pairs > 0
        and attacks == 0
        and unanswered == 0
        and answered == pairs
    )
    if accepted:
        store.tunes.accept_program(program.id, accepted_at=now())
    stopped = report.operation.stopped_by
    return AcceptReport(
        program=program,
        run_id=report.run_id,
        status=report.status,
        pairs=pairs,
        attacks=attacks,
        answered=answered,
        unanswered=unanswered,
        accepted=accepted,
        stopped=None if stopped is None else str(report.stop_error or stopped),
    )


def clear_program(store: Store, task: EvalTask) -> bool:
    """Go back to the base prompt for a task by unmarking its accepted program.

    The program row stays. It reads no content, so it clears a malformed row too.

    Args:
        store: The open store.
        task: `judge` or `scorer`.

    Returns:
        Whether a program was accepted.

    Raises:
        sqlite3.Error: The store cannot be written.
    """
    return store.tunes.clear_program(task)


def prepare_screen(
    store: Store,
    llm: LlmSettings,
    eval_settings: EvalSettings,
    tune: TuneSettings,
    *,
    now: Callable[[], datetime],
) -> ScreenPlan:
    """Work out which model and task pairs a screen still has to run.

    The models are `[tune].candidates`, `[tune].baseline` and whichever of
    `[models].judge` and `[models].scorer` are set, each once, and each is
    measured on `judge` and `scorer` unless a full run already measures it. A
    configured model that is a juror of the set is left out and named in
    `skipped`. Everything the screen could not run is refused here: before any
    model is called and before the split is frozen.

    Args:
        store: The open store.
        llm: The model settings.
        eval_settings: The eval settings, whose jury identifies the label set.
        tune: The tuning settings.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The plan.

    Raises:
        InvalidValueError: There is no `[tune].baseline`, or a candidate or the
            baseline is a juror of the set.
        EvalNeedsError: No jury, rules, profile or label set exists, too few cases
            are labelled, labels wait for review, or the profile is stale.
        TooFewKeepsError: Too few keeps and rejects are labelled to tune on.
        sqlite3.Error: The store cannot be read.
    """
    if tune.baseline is None:
        raise InvalidValueError(
            "tune.baseline",
            "no baseline model is set; add a [tune] table with tune.baseline "
            "to the config",
        )
    chosen = (*tune.candidates, tune.baseline)
    for model in chosen:
        refuse_juror(store, eval_settings, model)
    eval_set = prepare_suite(
        store, llm, eval_settings, Suite.JUDGE, model=tune.baseline, limit=None
    ).eval_set
    check_split(store, eval_set, eval_settings, now=now)
    skipped: list[ModelRef] = []
    configured: list[ModelRef] = []
    for task in (Task.JUDGE, Task.SCORER):
        named = llm.models.get(task)
        if named is None:
            continue
        try:
            refuse_juror(store, eval_settings, named)
        except InvalidValueError:
            if named not in skipped:
                skipped.append(named)
        else:
            configured.append(named)
    pairs = tuple(
        ScreenPair(task, model)
        for model in dict.fromkeys((*chosen, *configured))
        for task in (EvalTask.JUDGE, EvalTask.SCORER)
        if full_run(store, task, model, eval_set) is None
    )
    return ScreenPlan(eval_set, pairs, tuple(skipped))


@dataclass(frozen=True, slots=True)
class ScreenOffer:
    """What a screen will do and cost, worked out before any model is called.

    Attributes:
        plan: The pairs to run.
        offers: The eval run offer of each pair, in the order of `plan.pairs`.
        settings: The eval settings the runs read their caps from.
        cap: What each run may spend on OpenRouter, in US dollars. The runs
            together may spend up to `left`.
        left: What `eval.total_cost_usd` leaves after the OpenRouter spend
            recorded so far, in US dollars, never below zero.
        costs: Each model called with the cost estimated for all its pairs.
        estimate: The estimated cost of the whole screen.
        frozen: Whether the label set already has its tuning split. When it does
            not, the screen freezes one, which can never be undone.
    """

    plan: ScreenPlan
    offers: tuple[RunOffer, ...]
    settings: EvalSettings
    cap: float
    left: float
    costs: tuple[tuple[ModelRef, Cost], ...]
    estimate: Cost
    frozen: bool

    @property
    def callers(self) -> tuple[ModelRef, ...]:
        """The models the screen calls, each once, in the order of the pairs."""
        return tuple(dict.fromkeys(pair.model for pair in self.plan.pairs))


def screen_offer(
    plan: ScreenPlan,
    offers: Sequence[RunOffer],
    settings: EvalSettings,
    *,
    left: float,
    frozen: bool,
) -> ScreenOffer:
    """Combine the offers of a plan's pairs into the offer of the screen.

    Args:
        plan: The plan from `prepare_screen`.
        offers: One run offer per pair, in the order of `plan.pairs`.
        settings: The eval settings the runs read their caps from.
        left: What the recorded spending leaves of `eval.total_cost_usd`.
        frozen: Whether the plan's label set already has its tuning split.

    Returns:
        The offer, with the costs of each model added across its pairs.
    """
    by_model: dict[ModelRef, list[Cost]] = {}
    for offer in offers:
        for model, cost in offer.costs:
            by_model.setdefault(model, []).append(cost)
    costs = tuple((model, sum_costs(each)) for model, each in by_model.items())
    return ScreenOffer(
        plan=plan,
        offers=tuple(offers),
        settings=settings,
        cap=max((offer.cap for offer in offers), default=0.0),
        left=max(0.0, left),
        costs=costs,
        estimate=sum_costs([cost for _, cost in costs]),
        frozen=frozen,
    )


@dataclass(frozen=True, slots=True)
class PairOutcome:
    """How one pair of a screen ended.

    Attributes:
        pair: The pair that ran.
        run_id: The run row written for it.
        status: `complete` or `incomplete`.
        interrupted: Whether the screen was cancelled while the pair ran; its run
            row and the results already stored stand, and the row is incomplete.
    """

    pair: ScreenPair
    run_id: int
    status: RunStatus
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class ScreenReport:
    """What a screen did.

    Attributes:
        outcomes: The pairs that ran, in order.
        not_run: The pairs that did not run because the screen stopped.
        stopped: Why the screen stopped before its last pair, or `None` when it
            went through.
    """

    outcomes: tuple[PairOutcome, ...]
    not_run: tuple[ScreenPair, ...]
    stopped: str | None

    @property
    def exit_status(self) -> int:
        """The command's exit status: 1 if any pair is incomplete or did not run."""
        incomplete = any(
            each.status is not RunStatus.COMPLETE for each in self.outcomes
        )
        return 1 if incomplete or self.not_run or self.stopped else 0


def screen_report(
    plan: ScreenPlan, outcomes: Sequence[PairOutcome], stopped: str | None
) -> ScreenReport:
    """Build the report of a screen from the pairs that ended.

    Args:
        plan: The plan the screen ran.
        outcomes: The pairs that ended, in order.
        stopped: Why the screen stopped early, or `None`.

    Returns:
        The report, whose `not_run` holds every planned pair with no outcome.
    """
    done = {each.pair for each in outcomes}
    return ScreenReport(
        tuple(outcomes),
        tuple(pair for pair in plan.pairs if pair not in done),
        stopped,
    )


def _newest_run_id(store: Store) -> int:
    """Return the number of the newest eval run, or `0` when there is none."""
    runs = store.evals.runs()
    return runs[-1].id if runs else 0


def _interrupted_outcome(
    store: Store, plan: ScreenPlan, pair: ScreenPair, after: int
) -> PairOutcome | None:
    """Find the incomplete run a cancelled pair left behind, or `None` if none began."""
    for run in reversed(store.evals.runs()):
        if run.id <= after:
            break
        if (
            run.suite == pair.task.value
            and run.model == str(pair.model)
            and run.set_id == plan.eval_set.id
            and run.status is RunStatus.INCOMPLETE
        ):
            return PairOutcome(pair, run.id, run.status, interrupted=True)
    return None


async def run_screen(  # noqa: PLR0913  # the screen's inputs, one each
    store: Store,
    plan: ScreenPlan,
    settings: EvalSettings,
    run: Callable[[ScreenPair], Awaitable[SuiteReport]],
    *,
    now: Callable[[], datetime],
    on_outcome: Callable[[PairOutcome], None] | None = None,
) -> ScreenReport:
    """Freeze the split, then run each pair of the plan in order.

    Each pair goes through `run`, which is an ordinary eval run with its own
    spend. A spent budget, an unavailable provider, an unknown cost or a provider
    failure ends the screen; the runs already written stand. A run that reached its
    cap is incomplete and the screen goes on. When the screen is cancelled while a
    pair runs, that pair's incomplete run is reported to `on_outcome` as
    interrupted before the cancellation goes on.

    Args:
        store: The open store.
        plan: The plan from `prepare_screen`.
        settings: The eval settings, whose jury names the set the review covers.
        run: Runs one pair and returns its report.
        now: The wall clock, returning timezone-aware times.
        on_outcome: Called as each pair ends.

    Returns:
        What the screen did.

    Raises:
        EvalNeedsError: Labels wait for `ijob eval review`; nothing ran.
        TooFewKeepsError: Too few keeps and rejects to tune on; nothing ran.
        asyncio.CancelledError: The call was cancelled; the runs already written
            stand.
        sqlite3.Error: The store cannot be read or written.
    """
    freeze_split(store, plan.eval_set, settings, now=now)
    outcomes: list[PairOutcome] = []
    stopped: str | None = None
    for pair in plan.pairs:
        after = _newest_run_id(store)
        try:
            report = await run(pair)
        except asyncio.CancelledError:
            interrupted = _interrupted_outcome(store, plan, pair, after)
            if interrupted is not None and on_outcome is not None:
                on_outcome(interrupted)
            raise
        except (EvalBudgetSpentError, ProviderUnavailableError) as error:
            stopped = str(error)
            break
        outcome = PairOutcome(pair, report.run_id, report.status)
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)
        if report.operation.stopped_by in ("provider", "cost_unknown"):
            stopped = str(report.stop_error or report.operation.stopped_by)
            break
    return screen_report(plan, outcomes, stopped)
