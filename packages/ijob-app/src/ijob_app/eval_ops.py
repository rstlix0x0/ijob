"""The jury run: draw a label set, collect the jurors' votes and derive labels.

`prepare_label_run` finds or draws the label set for the stored rules, CV profile
and jury, and works out which votes are still missing. `label_operation` requests
those votes in one `run_items` call under `EvalSpend`, stores each vote as soon as
it is cast, and derives the labels the jurors agree on. `derive_labels` does that
last step alone, for the commands that read votes a stopped run left behind.

`review_items` lists what `ijob eval review` offers: the cases whose votes give no
label, then a spot check of agreed labels. `record_review` stores the user's answer
to one of them.

A run never builds the CV profile: it uses the stored one or refuses.

This module imports no `ijob_ai` task or model module at module level, so a
command that calls no model stays fast; those load inside the functions that are
about to call one.
"""

import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final, assert_never

from ijob_ai.errors import (
    CostUnknownError,
    LlmError,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.reference import ModelRef
from ijob_ai.llm.settings import EvalSettings
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalRecords,
    EvalSet,
    EvalTask,
    EvalVote,
    LabelOrigin,
    RunStatus,
)
from ijob_core.eval_rules import (
    Agreed,
    NeedsReview,
    ReviewReason,
    Split,
    SpotCandidate,
    draw_cases,
    judge_label,
    limit_split,
    rejection_rule,
    score_label,
    spot_order,
)
from ijob_core.model import Instructions, Posting, PostingId, ResumeProfile
from ijob_core.store import Store

from ijob_app.eval_spend import EvalSpend
from ijob_app.llm_ops import (
    ItemOutcome,
    ItemStatus,
    LlmRuntime,
    OperationReport,
    run_items,
)

if TYPE_CHECKING:
    import dspy

_MIN_CASES: Final = 10
_SUITE: Final = "jury"
_OPERATION: Final = "eval-label"
_TASKS: Final = (EvalTask.JUDGE, EvalTask.SCORER)


class EvalNeedsError(LlmError):
    """The jury run or its review needs something the user has not stored yet.

    Attributes:
        what: What is missing.
        fix: What supplies it: a command to run, or a setting to make.
    """

    def __init__(self, what: str, fix: str) -> None:
        """Build the error from what is missing and the command that fixes it.

        Args:
            what: What is missing.
            fix: The command that supplies it.
        """
        super().__init__(f"{what}; {fix}")
        self.what = what
        self.fix = fix

    @classmethod
    def no_jury(cls) -> "EvalNeedsError":
        """Build the error for a config that sets no jury."""
        return cls(
            "no jury is set to identify the label set",
            "list two to five models as eval.jury under [eval] in the config",
        )

    @classmethod
    def no_rules(cls) -> "EvalNeedsError":
        """Build the error for instructions that are missing or hold no rule."""
        return cls(
            "no rules are stored for the jury to apply", "run `ijob instructions set`"
        )

    @classmethod
    def no_profile(cls) -> "EvalNeedsError":
        """Build the error for a missing CV profile."""
        return cls(
            "no CV profile is stored for the jury to score against",
            "run `ijob cv profile`",
        )

    @classmethod
    def stale_profile(cls) -> "EvalNeedsError":
        """Build the error for a CV profile read from an earlier CV."""
        return cls(
            "the stored CV profile was read from an earlier CV", "run `ijob cv profile`"
        )


class TooFewCasesError(LlmError):
    """The pools hold too few postings to draw a label set from.

    Attributes:
        found: How many postings the draw gave.
    """

    def __init__(self, found: int) -> None:
        """Build the error from the size of the draw.

        Args:
            found: How many postings the draw gave.
        """
        super().__init__(
            f"only {found} postings to draw from; a label set needs at least "
            f"{_MIN_CASES}; fetch and judge more postings first"
        )
        self.found = found


class LabelChange(StrEnum):
    """An input whose change makes a new label set."""

    RULES = "rules"
    CV = "CV profile"
    JURY = "jury"


@dataclass(frozen=True, slots=True)
class VoteItem:
    """One vote to request.

    Attributes:
        posting: The posting as it was when the set was drawn.
        task: The task the juror answers.
        juror: The juror's model.
    """

    posting: Posting
    task: EvalTask
    juror: ModelRef


@dataclass(frozen=True, slots=True)
class LabelPlan:
    """What a jury run will do, worked out before any call.

    Attributes:
        eval_set: The label set, drawn and stored.
        instructions: The stored instructions whose rules the judge votes apply.
        profile: The stored CV profile the scorer votes read.
        jurors: The jurors, in the order their votes are requested.
        changed: What differs from the last set that has votes; empty when the
            set is the one in use or there is no earlier set.
        cases: How many cases the run covers, after `limit`.
        skipped: Cases among them whose posting changed or went since the draw;
            they get no new vote.
        items: The votes still missing, case by case, judge then scorer, jurors
            in order.
        limited: Whether `limit` narrowed the run.
    """

    eval_set: EvalSet
    instructions: Instructions
    profile: ResumeProfile
    jurors: tuple[ModelRef, ...]
    changed: tuple[LabelChange, ...]
    cases: int
    skipped: int
    items: tuple[VoteItem, ...]
    limited: bool


@dataclass(frozen=True, slots=True)
class TaskTally:
    """How the votes on one task stand.

    Attributes:
        agreed: Cases whose votes agree on a label.
        review: Cases whose votes do not agree and that no label settles yet.
    """

    agreed: int
    review: int


@dataclass(frozen=True, slots=True)
class LabelTally:
    """How the votes of a set stand, per task.

    Attributes:
        judge: The judge votes.
        scorer: The scorer votes.
    """

    judge: TaskTally
    scorer: TaskTally


@dataclass(frozen=True, slots=True)
class StoppedJuror:
    """A juror that made no further call after its provider failed.

    Attributes:
        juror: The juror's model reference.
        reason: Why the provider failed.
    """

    juror: str
    reason: str


@dataclass(frozen=True, slots=True)
class LabelReport:
    """What a jury run did.

    Attributes:
        run_id: The run row the spend and status were written to.
        set_id: The label set.
        operation: What `run_items` did and spent; an `OperationReport` with no
            calls when no vote was missing.
        requested: Votes asked for.
        stored: Votes cast and stored.
        failed: Votes whose answer was unusable or that failed otherwise; a re-run
            asks for them again.
        skipped: Cases that got no vote because their posting changed.
        stopped_jurors: Jurors whose provider failed.
        cost_unknown: The error that stopped the run for an unknown cost, if any.
        subscription_spent: claude-sdk spend of the run, in US dollars.
        tally: How the votes of the whole set stand after the run.
    """

    run_id: int
    set_id: int
    operation: OperationReport
    requested: int
    stored: int
    failed: int
    skipped: int
    stopped_jurors: tuple[StoppedJuror, ...]
    cost_unknown: CostUnknownError | None
    subscription_spent: float
    tally: LabelTally


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One case the user is asked about.

    Attributes:
        set_id: The label set.
        posting: The posting, with the text the jurors read.
        task: The task the jurors answered.
        votes: Their votes, by juror.
        jurors: How many jurors the set has.
        reason: Why the votes give no label; `None` for a spot check.
        agreed: The label the jurors agreed on, for a spot check; `None` for a
            case that waits for the user.
    """

    set_id: int
    posting: Posting
    task: EvalTask
    votes: tuple[EvalVote, ...]
    jurors: int
    reason: ReviewReason | None
    agreed: EvalLabel | None

    @property
    def spot_check(self) -> bool:
        """Whether the jurors agreed and the user only checks."""
        return self.agreed is not None

    @property
    def spread(self) -> int | None:
        """The points between the highest and lowest score; `None` for a judge case."""
        scores = self._scores()
        return max(scores) - min(scores) if scores else None

    @property
    def median(self) -> int | None:
        """The lower median of the scores; `None` for a judge case."""
        scores = self._scores()
        return statistics.median_low(scores) if scores else None

    def _scores(self) -> list[int]:
        """Return the scores of the votes."""
        return [vote.score for vote in self.votes if vote.score is not None]


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """What a review offers.

    Attributes:
        has_set: Whether a label set exists for the stored rules, CV profile and
            jury.
        rules: The stored rules, whose numbers the judge votes cite.
        items: The cases to show, in order: the ones waiting for the user, then
            the spot check.
        skipped: Postings left out because their text changed or they are gone.
    """

    has_set: bool
    rules: tuple[str, ...]
    items: tuple[ReviewItem, ...]
    skipped: int


@dataclass(frozen=True, slots=True)
class _Row:
    """A case of the set with the posting hash it was drawn under."""

    posting_id: PostingId
    bucket: Bucket
    digest: str


def prepare_label_run(
    store: Store,
    settings: EvalSettings,
    *,
    limit: int | None = None,
    now: Callable[[], datetime],
) -> LabelPlan:
    """Find or draw the label set and list the votes it still needs.

    The set is the one for the stored rules, the stored CV profile and the jury.
    A set that has cases keeps them: the draw is made once. With `limit`, the run
    covers the first cases of each pool by position, as `limit_split` divides
    them. A case whose posting text differs from the one the set was drawn under,
    or whose posting is gone, is skipped and counted.

    Args:
        store: The open store.
        settings: The eval settings, whose jury votes.
        limit: Cover only this many cases, from 1 to 100; all when `None`.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The plan, with the votes still missing.

    Raises:
        InvalidValueError: No jury is set, or `limit` is outside 1 to 100.
        EvalNeedsError: No rules are stored, or no CV profile read from the
            stored CV.
        TooFewCasesError: A new set would hold fewer than 10 cases.
        sqlite3.Error: The store cannot be read or written.
    """
    from ijob_ai.tasks.text import input_sha256, posting_block  # noqa: PLC0415

    if settings.jury is None:
        raise InvalidValueError(
            "eval.jury",
            "is not set; list two to five models under [eval] in the config",
        )
    if limit is not None:
        limit_split(limit, Split(0, 0, 0))  # raises for a limit outside 1 to 100
    instructions = stored_rules(store)
    profile = stored_profile(store)
    jurors = tuple(sorted(settings.jury, key=str))
    eval_set, rows = _set_and_rows(
        store,
        instructions.rules_sha256,
        profile.sha256(),
        tuple(str(juror) for juror in jurors),
        now=now,
    )
    have = {
        (vote.posting_id, vote.task, vote.juror)
        for vote in store.evals.votes(eval_set.id)
    }
    covered = _limited(rows, limit)
    items: list[VoteItem] = []
    skipped = 0
    for row in covered:
        posting = store.posting(row.posting_id)
        if posting is None or input_sha256(posting_block(posting)) != row.digest:
            skipped += 1
            continue
        items.extend(
            VoteItem(posting, task, juror)
            for task in _TASKS
            for juror in jurors
            if (row.posting_id, task, str(juror)) not in have
        )
    return LabelPlan(
        eval_set=eval_set,
        instructions=instructions,
        profile=profile,
        jurors=jurors,
        changed=_changes(store.evals, eval_set),
        cases=len(covered),
        skipped=skipped,
        items=tuple(items),
        limited=limit is not None,
    )


def _set_and_rows(
    store: Store,
    rules_sha256: str,
    profile_sha256: str,
    jury: tuple[str, ...],
    *,
    now: Callable[[], datetime],
) -> tuple[EvalSet, list[_Row]]:
    """Return the set for these inputs with its cases, drawing and storing it if new."""
    from ijob_ai.tasks.text import input_sha256, posting_block  # noqa: PLC0415

    records = store.evals
    existing = find_set(records, rules_sha256, profile_sha256, jury)
    if existing is not None and (stored := records.cases(existing.id)):
        return existing, [
            _Row(case.posting_id, case.bucket, case.input_sha256) for case in stored
        ]
    drawn = draw_cases(rules_sha256, profile_sha256, records.pools(rules_sha256))
    if len(drawn) < _MIN_CASES:
        raise TooFewCasesError(len(drawn))
    rows: list[_Row] = []
    for posting_id, bucket in drawn:
        posting = store.posting(posting_id)
        if posting is not None:
            rows.append(_Row(posting_id, bucket, input_sha256(posting_block(posting))))
    eval_set = records.find_or_create_set(
        rules_sha256, profile_sha256, jury, created_at=now()
    )
    records.put_cases(
        eval_set.id,
        [
            EvalCase(eval_set.id, row.posting_id, position, row.bucket, row.digest)
            for position, row in enumerate(rows)
        ],
    )
    return eval_set, rows


def find_set(
    records: EvalRecords, rules_sha256: str, profile_sha256: str, jury: tuple[str, ...]
) -> EvalSet | None:
    """Return the stored set for these inputs, or `None`."""
    return next(
        (
            known
            for known in records.sets()
            if (known.rules_sha256, known.profile_sha256, known.jury)
            == (rules_sha256, profile_sha256, jury)
        ),
        None,
    )


def stored_rules(store: Store) -> Instructions:
    """Return the stored instructions, which must hold at least one rule."""
    instructions = store.instructions()
    if instructions is None or not instructions.rules:
        raise EvalNeedsError.no_rules()
    return instructions


def stored_profile(store: Store) -> ResumeProfile:
    """Return the stored CV profile, which must be read from the stored CV."""
    stored = store.cv_profile()
    if stored is None:
        raise EvalNeedsError.no_profile()
    cv = store.cv()
    if cv is not None and stored.cv_sha256 != cv.sha256:
        raise EvalNeedsError.stale_profile()
    return stored.profile


def _limited(rows: Sequence[_Row], limit: int | None) -> list[_Row]:
    """Take the rows a `--limit` covers: the first of each pool by position."""
    if limit is None:
        return list(rows)
    by_bucket = {
        bucket: [row for row in rows if row.bucket is bucket] for bucket in Bucket
    }
    split = limit_split(
        limit,
        Split(
            len(by_bucket[Bucket.KEPT]),
            len(by_bucket[Bucket.REJECTED]),
            len(by_bucket[Bucket.UNJUDGED]),
        ),
    )
    taken = (
        by_bucket[Bucket.KEPT][: split.kept]
        + by_bucket[Bucket.REJECTED][: split.rejected]
        + by_bucket[Bucket.UNJUDGED][: split.unjudged]
    )
    chosen = {row.posting_id for row in taken}
    return [row for row in rows if row.posting_id in chosen]


def _changes(records: EvalRecords, current: EvalSet) -> tuple[LabelChange, ...]:
    """Name what differs between `current` and the last other set with votes.

    A set that already has votes is in use, and a first set has nothing to differ
    from; both give no change.
    """
    if records.votes(current.id):
        return ()
    voted = [
        other
        for other in records.sets()
        if other.id != current.id and records.votes(other.id)
    ]
    if not voted:
        return ()
    previous = voted[-1]
    return tuple(
        change
        for change, differs in (
            (LabelChange.RULES, previous.rules_sha256 != current.rules_sha256),
            (LabelChange.CV, previous.profile_sha256 != current.profile_sha256),
            (LabelChange.JURY, previous.jury != current.jury),
        )
        if differs
    )


async def label_operation(  # noqa: PLR0913  # the operation's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    plan: LabelPlan,
    spend: EvalSpend,
    *,
    now: Callable[[], datetime],
    on_outcome: Callable[[ItemOutcome[VoteItem, EvalVote | None]], None] | None = None,
) -> LabelReport:
    """Request the missing votes and derive the labels they give.

    All votes go through one `run_items` call, capped at what `spend` allows when
    the call starts. Each vote is stored as soon as it is cast and the run row is
    updated after every item, so a cancelled run keeps both. A provider failure
    stops that juror alone: its later items make no call and the other jurors go
    on. An unusable answer leaves no vote, to be asked for again. An unknown cost
    stops the run, and so does the cap. Labels are derived, and the run row closed,
    however the run ends.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        plan: The plan from `prepare_label_run`.
        spend: The command's spending; it must not have begun a run.
        now: The wall clock, returning timezone-aware times.
        on_outcome: Called, on the event loop, with each item's outcome.

    Returns:
        What the run did.

    Raises:
        asyncio.CancelledError: The call was cancelled; the votes already cast and
            the run row stand.
        ExceptionGroup: A call raised something other than an `LlmError`.
        sqlite3.Error: The store cannot be read or written.
    """
    from ijob_ai.tasks.signatures import JUDGE_VERSION, SCORER_VERSION  # noqa: PLC0415

    stopped: dict[ModelRef, LlmError] = {}
    cap = spend.cap
    run = spend.begin(
        _SUITE,
        "+".join(plan.eval_set.jury),
        f"{JUDGE_VERSION}+{SCORER_VERSION}",
        plan.eval_set.id,
        started_at=now(),
        limited=plan.limited,
    )

    async def cast(item: VoteItem, budget: Budget) -> EvalVote | None:
        juror = item.juror
        if juror in stopped:
            return None
        try:
            registry = await runtime.registry_for_ref(juror)
            vote = await _ask(
                item,
                plan,
                registry.lm_for_ref(juror),
                spend.budget_for(juror, budget),
                timeout_s=runtime.settings.timeout_s,
                now=now,
            )
        except (ProviderFailedError, ProviderUnavailableError) as error:
            stopped.setdefault(juror, error)
            return None
        store.evals.put_vote(vote)
        return vote

    def handle(outcome: ItemOutcome[VoteItem, EvalVote | None]) -> None:
        spend.record()
        if on_outcome is not None:
            on_outcome(outcome)

    status = RunStatus.INCOMPLETE
    metrics = "{}"
    try:
        if plan.items:
            result = await run_items(
                runtime,
                _OPERATION,
                plan.items,
                cast,
                on_outcome=handle,
                cap=cap,
            )
            report, outcomes = result.report, result.outcomes
        else:
            report = OperationReport(
                done=0, not_done=0, spent=0.0, cap=cap, stopped_by=None
            )
            outcomes = ()
        stored = sum(1 for outcome in outcomes if outcome.result is not None)
        failed = sum(1 for outcome in outcomes if outcome.status is ItemStatus.FAILED)
        stopped_jurors = tuple(
            StoppedJuror(str(juror), str(error)) for juror, error in stopped.items()
        )
        cost_unknown = next(
            (
                outcome.error
                for outcome in outcomes
                if isinstance(outcome.error, CostUnknownError)
            ),
            None,
        )
        if stored == len(plan.items):
            status = RunStatus.COMPLETE
        metrics = json.dumps(
            {
                "requested": len(plan.items),
                "stored": stored,
                "failed": failed,
                "skipped": plan.skipped,
                "stopped_jurors": [stop.juror for stop in stopped_jurors],
                "stopped_by": report.stopped_by,
            }
        )
    finally:
        tally = derive_labels(store.evals, plan.eval_set, at=now())
        spend.finish(status, metrics, finished_at=now())
    return LabelReport(
        run_id=run.id,
        set_id=plan.eval_set.id,
        operation=report,
        requested=len(plan.items),
        stored=stored,
        failed=failed,
        skipped=plan.skipped,
        stopped_jurors=stopped_jurors,
        cost_unknown=cost_unknown,
        subscription_spent=spend.subscription_spent,
        tally=tally,
    )


async def _ask(  # noqa: PLR0913  # the call's whole contract, one argument each
    item: VoteItem,
    plan: LabelPlan,
    lm: "dspy.LM",
    budget: Budget,
    *,
    timeout_s: float,
    now: Callable[[], datetime],
) -> EvalVote:
    """Have the juror's LM judge or score the posting and wrap the answer as a vote.

    Raises:
        LlmError: The call fails or its answer is unusable.
    """
    from ijob_ai.tasks.judge import judge_posting  # noqa: PLC0415
    from ijob_ai.tasks.scorer import score_posting  # noqa: PLC0415
    from ijob_ai.tasks.signatures import JUDGE_VERSION, SCORER_VERSION  # noqa: PLC0415

    posting, juror = item.posting, item.juror
    if item.task is EvalTask.JUDGE:
        verdict = await judge_posting(
            posting,
            plan.instructions.rules,
            lm=lm,
            provider=juror.provider,
            budget=budget,
            timeout_s=timeout_s,
        )
        return EvalVote(
            set_id=plan.eval_set.id,
            posting_id=posting.id,
            task=EvalTask.JUDGE,
            juror=str(juror),
            task_version=JUDGE_VERSION,
            keep=verdict.keep,
            rule=verdict.rule,
            score=None,
            reasons=(verdict.reason,),
            made_at=now(),
        )
    score = await score_posting(
        posting,
        plan.profile,
        lm=lm,
        provider=juror.provider,
        budget=budget,
        timeout_s=timeout_s,
    )
    return EvalVote(
        set_id=plan.eval_set.id,
        posting_id=posting.id,
        task=EvalTask.SCORER,
        juror=str(juror),
        task_version=SCORER_VERSION,
        keep=None,
        rule=None,
        score=score.score,
        reasons=score.reasons,
        made_at=now(),
    )


def derive_labels(
    records: EvalRecords, eval_set: EvalSet, *, at: datetime
) -> LabelTally:
    """Label every case whose votes agree, leaving existing labels as they are.

    A case is labelled once every juror has voted and the votes agree (see
    `judge_label` and `score_label`). A case with no vote stays unlabelled and
    out of the tally.

    Args:
        records: The eval records of the open store.
        eval_set: The set whose votes to read.
        at: When the labels are decided, timezone-aware.

    Returns:
        Per task, how many cases agree and how many wait for review.

    Raises:
        InvalidValueError: `at` has no timezone.
        sqlite3.Error: The store cannot be read or written.
    """
    jurors = len(eval_set.jury)
    labelled = {(label.posting_id, label.task) for label in records.labels(eval_set.id)}
    groups: defaultdict[tuple[PostingId, EvalTask], list[EvalVote]] = defaultdict(list)
    for vote in records.votes(eval_set.id):
        groups[vote.posting_id, vote.task].append(vote)
    agreed = dict.fromkeys(_TASKS, 0)
    review = dict.fromkeys(_TASKS, 0)
    for (posting_id, task), votes in groups.items():
        rule = judge_label if task is EvalTask.JUDGE else score_label
        outcome = rule(votes, jurors)
        match outcome:
            case None:
                continue
            case Agreed():
                agreed[task] += 1
                if (posting_id, task) not in labelled:
                    records.put_label(
                        EvalLabel(
                            set_id=eval_set.id,
                            posting_id=posting_id,
                            task=task,
                            keep=outcome.keep,
                            rule=outcome.rule,
                            score=outcome.score,
                            origin=LabelOrigin.AGREED,
                            spot_checked=False,
                            decided_at=at,
                        )
                    )
            case NeedsReview():
                if (posting_id, task) not in labelled:
                    review[task] += 1
            case _:
                assert_never(outcome)
    return LabelTally(
        judge=TaskTally(agreed[EvalTask.JUDGE], review[EvalTask.JUDGE]),
        scorer=TaskTally(agreed[EvalTask.SCORER], review[EvalTask.SCORER]),
    )


def review_items(
    store: Store, settings: EvalSettings, *, now: Callable[[], datetime]
) -> ReviewPlan:
    """List what `ijob eval review` offers, after labelling what the votes agree on.

    The set is the one for the stored rules, the stored CV profile and the jury;
    none is made here. Labels are derived first, so votes a stopped run left
    behind reach the review. Cases come first, in set order, judge before scorer:
    those whose votes disagree or are fewer than the jurors and that no label
    settles yet. Then comes the spot check of agreed labels (see `spot_order`).
    A case with no vote is not offered. A posting whose text changed since the
    draw, or that is gone, is left out and counted.

    Args:
        store: The open store.
        settings: The eval settings, whose jury names the set.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The plan; with no set it holds no item.

    Raises:
        EvalNeedsError: No jury is set, no rules are stored, or no CV profile
            read from the stored CV.
        sqlite3.Error: The store cannot be read or written.
    """
    if settings.jury is None:
        raise EvalNeedsError.no_jury()
    instructions = stored_rules(store)
    profile = stored_profile(store)
    records = store.evals
    jury = tuple(sorted(str(juror) for juror in settings.jury))
    eval_set = find_set(records, instructions.rules_sha256, profile.sha256(), jury)
    if eval_set is None:
        return ReviewPlan(has_set=False, rules=instructions.rules, items=(), skipped=0)
    derive_labels(records, eval_set, at=now())
    postings = _CurrentPostings(
        store,
        {case.posting_id: case.input_sha256 for case in records.cases(eval_set.id)},
    )
    groups: defaultdict[tuple[PostingId, EvalTask], list[EvalVote]] = defaultdict(list)
    for vote in records.votes(eval_set.id):
        groups[vote.posting_id, vote.task].append(vote)
    labels = {
        (label.posting_id, label.task): label for label in records.labels(eval_set.id)
    }
    waiting: list[ReviewItem] = []
    for posting_id in postings.drawn:
        for task in _TASKS:
            votes = groups.get((posting_id, task))
            if not votes or (posting_id, task) in labels:
                continue
            rule = judge_label if task is EvalTask.JUDGE else score_label
            outcome = rule(votes, len(jury))
            if not isinstance(outcome, NeedsReview):
                continue
            if (posting := postings.get(posting_id)) is not None:
                waiting.append(
                    ReviewItem(
                        eval_set.id,
                        posting,
                        task,
                        tuple(votes),
                        len(jury),
                        outcome.reason,
                        None,
                    )
                )
    candidates = [
        SpotCandidate(label.posting_id, label.task)
        for label in labels.values()
        if label.origin is LabelOrigin.AGREED
        and not label.spot_checked
        and postings.get(label.posting_id) is not None
    ]
    checked = [label.task for label in labels.values() if label.spot_checked]
    seed = eval_set.rules_sha256 + eval_set.profile_sha256
    checks: list[ReviewItem] = []
    for candidate in spot_order(seed, candidates, checked=checked):
        posting = postings.get(candidate.posting_id)
        key = (candidate.posting_id, candidate.task)
        if posting is not None:
            checks.append(
                ReviewItem(
                    eval_set.id,
                    posting,
                    candidate.task,
                    tuple(groups[key]),
                    len(jury),
                    None,
                    labels[key],
                )
            )
    return ReviewPlan(
        has_set=True,
        rules=instructions.rules,
        items=(*waiting, *checks),
        skipped=len(postings.unusable),
    )


class _CurrentPostings:
    """The postings of a set as they are now, when they read as when it was drawn.

    Attributes:
        drawn: The set's postings, in sample order.
        unusable: The postings asked for whose text changed or that are gone.
    """

    def __init__(self, store: Store, digests: dict[PostingId, str]) -> None:
        """Hold the store and the hash each posting had when the set was drawn."""
        self._store = store
        self._digests = digests
        self._found: dict[PostingId, Posting | None] = {}
        self.drawn = tuple(digests)
        self.unusable: set[PostingId] = set()

    def get(self, posting_id: PostingId) -> Posting | None:
        """Return the posting if it still has the drawn text, else note it unusable."""
        from ijob_ai.tasks.text import input_sha256, posting_block  # noqa: PLC0415

        if posting_id not in self._found:
            posting = self._store.posting(posting_id)
            same = (
                posting is not None
                and input_sha256(posting_block(posting)) == self._digests[posting_id]
            )
            self._found[posting_id] = posting if same else None
        found = self._found[posting_id]
        if found is None:
            self.unusable.add(posting_id)
        return found


def record_review(
    store: Store,
    item: ReviewItem,
    *,
    keep: bool | None = None,
    score: int | None = None,
    now: Callable[[], datetime],
) -> EvalLabel:
    """Store the user's answer to a review item as a label.

    A judge answer that rejects carries the rule more than half of the rejecting
    votes name, if any. The answer replaces any label for the case. On a spot
    check, an answer equal to the agreed label leaves it with origin `agreed` and
    sets the spot check mark; a different one is stored with origin `user` and the
    mark. Any other answer is stored with origin `user`, unmarked.

    Args:
        store: The open store.
        item: The case answered.
        keep: Keep or reject, for a judge case.
        score: The fit from 0 to 100, for a scorer case.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The label as stored.

    Raises:
        InvalidValueError: The answer does not fit the task or is out of range.
        sqlite3.Error: The store cannot be written.
    """
    rule = None
    if keep is False:
        rule = rejection_rule(
            [
                vote.rule
                for vote in item.votes
                if vote.keep is False and vote.rule is not None
            ]
        )
    answer = EvalLabel(
        set_id=item.set_id,
        posting_id=item.posting.id,
        task=item.task,
        keep=keep,
        rule=rule,
        score=score,
        origin=LabelOrigin.USER,
        spot_checked=item.spot_check,
        decided_at=now(),
    )
    agreed = item.agreed
    if agreed is not None and (agreed.keep, agreed.rule, agreed.score) == (
        answer.keep,
        answer.rule,
        answer.score,
    ):
        store.evals.mark_spot_checked(item.set_id, item.posting.id, item.task)
        return replace(agreed, spot_checked=True)
    store.evals.put_label(answer)
    return answer
