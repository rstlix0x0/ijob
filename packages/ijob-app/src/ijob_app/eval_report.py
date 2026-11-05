"""What `ijob eval report` shows, read from the stored runs, labels and votes.

`comparable_runs` picks the runs that may stand beside each other (the same suite,
label set, task version and, for a labelled suite, case count, `complete` and not
limited) and counts the rest.
`jury_metrics` measures the jury from its votes and labels. `build_report` joins
them, with the total OpenRouter spend, into an `EvalReport`; `run_detail` reads one
run whatever its status. Nothing here calls a model or draws anything.

A run's metrics are the flat JSON object the suites store: every entry that is a
number is a metric, the rest is ignored. `margin` is the "plus or minus" on the
suite's main metric (see `MAIN_METRIC`); the report never recomputes it.
"""

import json
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import ceil
from typing import Final

from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    EvalLabel,
    EvalRecords,
    EvalResult,
    EvalRun,
    EvalSet,
    EvalTask,
    EvalVote,
    LabelOrigin,
    RunStatus,
)
from ijob_core.eval_rules import Agreed, Outcome, judge_label, score_label
from ijob_core.model import PostingId
from ijob_core.store import Store

SUITES: Final = (
    "judge",
    "scorer",
    "reasons",
    "hostile",
    "source-agent",
    "explore-agent",
)
LABELLED_SUITES: Final = frozenset({"judge", "scorer", "reasons"})
MAIN_METRIC: Final = {
    "judge": "keep_recall",
    "scorer": "ndcg",
    "reasons": "mean_score",
    "hostile": "attack_rate",
    "source-agent": "found_right",
    "explore-agent": "accepted_rate",
}
MARGIN_KEY: Final = "margin"
_P95: Final = 0.95

type Metrics = Mapping[str, float]


@dataclass(frozen=True, slots=True)
class Comparison:
    """The runs of one suite that may be compared, and how many may not.

    Attributes:
        suite: The suite.
        task_version: The task version the runs share, or `None` when no run
            of the suite is comparable.
        runs: The comparable runs, oldest first.
        others: The suite's runs left out: another set or task version, not
            complete, or limited.
        other_ids: The numbers of those runs, oldest first.
    """

    suite: str
    task_version: str | None
    runs: tuple[EvalRun, ...]
    others: int
    other_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class JuryMetrics:
    """How well the jury agrees, and how often the user disagrees with it.

    Attributes:
        agreement: Agreed (posting, task) pairs over pairs with every vote, or
            `None` when no pair has every vote.
        overruled: User answers that differ from the vote majority (judge) or
            the lower median (scorer); a tie is not counted.
        spot_check: Spot-checked labels answered the same over those answered,
            or `None` when none was answered.
    """

    agreement: float | None
    overruled: int
    spot_check: float | None


@dataclass(frozen=True, slots=True)
class RunLine:
    """One run in a table.

    Attributes:
        run: The run.
        metrics: Its numeric metrics.
        p50_ms: The median latency of its cases in milliseconds, or `None`
            when no case is stored.
    """

    run: EvalRun
    metrics: Metrics
    p50_ms: float | None


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """One suite's table of comparable runs.

    Attributes:
        suite: The suite.
        set_id: The label set compared, or `None` for a suite without labels.
        task_version: The shared task version, or `None` when no run compares.
        lines: The comparable runs.
        others: Runs of the suite left out.
        noise: The "plus or minus" when two runs' main metrics differ by less
            than it, else `None`.
        other_ids: The numbers of the runs left out, oldest first.
    """

    suite: str
    set_id: int | None
    task_version: str | None
    lines: tuple[RunLine, ...]
    others: int
    noise: float | None
    other_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Everything the report without an argument shows.

    Attributes:
        suites: A table for each suite that has runs, in `SUITES` order.
        labels_ready: Whether a jury, rules and a CV profile identify a label
            set; when not, only the suites without labels are listed.
        jury: The jury metrics of the current set, or `None` without one.
        spent: Total OpenRouter spend of every run, in US dollars.
        cap: The total cap, in US dollars.
        empty: Whether no run of any listed suite exists.
    """

    suites: tuple[SuiteReport, ...]
    labels_ready: bool
    jury: JuryMetrics | None
    spent: float
    cap: float
    empty: bool

    @property
    def others(self) -> int:
        """Runs left out of all the tables."""
        return sum(suite.others for suite in self.suites)

    @property
    def newest_other(self) -> int | None:
        """The number of the newest run left out, or `None` when none is."""
        return max((id_ for s in self.suites for id_ in s.other_ids), default=None)


@dataclass(frozen=True, slots=True)
class RunDetail:
    """One run, read whatever its status.

    Attributes:
        run: The run.
        metrics: Its numeric metrics.
        p50_ms: Median case latency in milliseconds, or `None` with no case.
        p95_ms: 95th percentile case latency in milliseconds, or `None`.
        results: Every stored case result.
    """

    run: EvalRun
    metrics: Metrics
    p50_ms: float | None
    p95_ms: float | None
    results: tuple[EvalResult, ...]

    @property
    def failed(self) -> tuple[EvalResult, ...]:
        """The cases that did not pass."""
        return tuple(result for result in self.results if not result.passed)


def run_metrics(run: EvalRun) -> Metrics:
    """Read the numeric entries of a run's stored metrics.

    Args:
        run: The run.

    Returns:
        The entries that are numbers; empty when the text is not a JSON object.
    """
    try:
        loaded = json.loads(run.metrics)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        str(key): float(value)
        for key, value in loaded.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def run_cost(run: EvalRun) -> float:
    """Give a run's whole cost, OpenRouter and claude-sdk, in US dollars."""
    return run.spent_usd + run.subscription_usd


def comparable_runs(
    runs: Sequence[EvalRun], *, suite: str, set_id: int | None
) -> Comparison:
    """Pick the runs of a suite that may be compared.

    Comparable means the same suite, label set and task version, status
    `complete`, and not limited. The version is the newest such run's. A labelled
    suite also needs the stored `cases` metric of the newest run, so runs over a
    larger or smaller set stand apart. A suite without labels compares by suite
    and task version, whatever `set_id` is and however many cases it ran.

    Args:
        runs: Every run, oldest first.
        suite: The suite to compare.
        set_id: The current label set, or `None` for a suite without labels.

    Returns:
        The comparable runs and the count of the suite's other runs.
    """
    labelled = suite in LABELLED_SUITES
    ofsuite = [run for run in runs if run.suite == suite]
    scope = [run for run in ofsuite if not labelled or run.set_id == set_id]
    whole = [
        run for run in scope if run.status is RunStatus.COMPLETE and not run.limited
    ]
    if not whole:
        return Comparison(
            suite, None, (), len(ofsuite), tuple(run.id for run in ofsuite)
        )
    version = whole[-1].task_version
    same = tuple(run for run in whole if run.task_version == version)
    if labelled:
        size = run_metrics(same[-1]).get("cases")
        same = tuple(run for run in same if run_metrics(run).get("cases") == size)
    return Comparison(
        suite,
        version,
        same,
        len(ofsuite) - len(same),
        tuple(run.id for run in ofsuite if run not in same),
    )


def current_set(
    store: Store, jury: Sequence[str] | None
) -> tuple[EvalSet | None, bool]:
    """Find the label set the stored rules, CV profile and jury identify.

    Nothing is drawn or written.

    Args:
        store: The open store.
        jury: The configured jurors' references, or `None` when unset.

    Returns:
        The set, or `None` when none is stored yet, and whether the jury, rules
        and profile that identify a set are all present.
    """
    instructions = store.instructions()
    profile = store.cv_profile()
    if (
        jury is None
        or instructions is None
        or not instructions.rules
        or profile is None
    ):
        return None, False
    identity = (
        instructions.rules_sha256,
        profile.profile.sha256(),
        tuple(sorted(jury)),
    )
    found = next(
        (
            known
            for known in store.evals.sets()
            if (known.rules_sha256, known.profile_sha256, known.jury) == identity
        ),
        None,
    )
    return found, True


def jury_metrics(records: EvalRecords, eval_set: EvalSet) -> JuryMetrics:
    """Measure the jury of a label set from its votes and labels.

    Args:
        records: The eval records of the open store.
        eval_set: The set to measure.

    Returns:
        Agreement before review, the user's overrulings and the spot check rate.
    """
    jurors = len(eval_set.jury)
    votes: defaultdict[tuple[PostingId, EvalTask], list[EvalVote]] = defaultdict(list)
    for vote in records.votes(eval_set.id):
        votes[(vote.posting_id, vote.task)].append(vote)
    complete = [pair for pair in votes.values() if len(pair) == jurors]
    agreed = sum(1 for pair in complete if isinstance(_outcome(pair, jurors), Agreed))
    labels = records.labels(eval_set.id)
    overruled = sum(
        1
        for label in labels
        if label.origin is LabelOrigin.USER
        and _differs(label, votes.get((label.posting_id, label.task), []))
    )
    checked = [label for label in labels if label.spot_checked]
    same = sum(1 for label in checked if label.origin is LabelOrigin.AGREED)
    return JuryMetrics(
        agreement=agreed / len(complete) if complete else None,
        overruled=overruled,
        spot_check=same / len(checked) if checked else None,
    )


def _outcome(votes: Sequence[EvalVote], jurors: int) -> Outcome | None:
    """Give the label these votes of one task agree on, or why they do not."""
    if votes[0].task is EvalTask.JUDGE:
        return judge_label(votes, jurors)
    return score_label(votes, jurors)


def _differs(label: EvalLabel, votes: Sequence[EvalVote]) -> bool:
    """Whether the user's label differs from the vote majority or median."""
    if not votes:
        return False
    if label.task is EvalTask.SCORER:
        scores = [vote.score for vote in votes if vote.score is not None]
        return bool(scores) and label.score != statistics.median_low(scores)
    keeps = sum(1 for vote in votes if vote.keep)
    rejects = len(votes) - keeps
    if keeps == rejects:
        return False
    return label.keep != (keeps > rejects)


def build_report(
    records: EvalRecords,
    *,
    current: EvalSet | None,
    labels_ready: bool,
    cap: float,
) -> EvalReport:
    """Gather the report of every suite with runs, the jury and the spend.

    Args:
        records: The eval records of the open store.
        current: The current label set, or `None` when none is identified.
        labels_ready: Whether a jury, rules and a CV profile identify a set.
        cap: The cap on total OpenRouter spend, in US dollars.

    Returns:
        The report; the suites with labels are listed only for a current set.
    """
    runs = records.runs()
    tables: list[SuiteReport] = []
    for suite in SUITES:
        if not any(run.suite == suite for run in runs):
            continue
        labelled = suite in LABELLED_SUITES
        if labelled and current is None:
            continue
        set_id = current.id if labelled and current is not None else None
        found = comparable_runs(runs, suite=suite, set_id=set_id)
        lines = tuple(
            RunLine(run, run_metrics(run), _latencies(records, run.id)[0])
            for run in found.runs
        )
        tables.append(
            SuiteReport(
                suite=suite,
                set_id=set_id,
                task_version=found.task_version,
                lines=lines,
                others=found.others,
                noise=_noise(suite, lines),
                other_ids=found.other_ids,
            )
        )
    return EvalReport(
        suites=tuple(tables),
        labels_ready=labels_ready,
        jury=None if current is None else jury_metrics(records, current),
        spent=records.total_spend(),
        cap=cap,
        empty=not tables,
    )


def run_detail(records: EvalRecords, run_id: int) -> RunDetail:
    """Read one run with its latency and case results.

    Args:
        records: The eval records of the open store.
        run_id: The run's number.

    Returns:
        The run, whatever its status.

    Raises:
        InvalidValueError: If no such run exists.
    """
    run = records.run(run_id)
    if run is None:
        raise InvalidValueError("run", f"{run_id} is not a stored run")
    p50, p95 = _latencies(records, run_id)
    return RunDetail(
        run=run,
        metrics=run_metrics(run),
        p50_ms=p50,
        p95_ms=p95,
        results=tuple(records.results(run_id)),
    )


def _latencies(records: EvalRecords, run_id: int) -> tuple[float | None, float | None]:
    """Give the median and 95th percentile case latency of a run in milliseconds."""
    millis = sorted(result.latency_ms for result in records.results(run_id))
    if not millis:
        return None, None
    return statistics.median(millis), float(millis[ceil(_P95 * len(millis)) - 1])


def _noise(suite: str, lines: Sequence[RunLine]) -> float | None:
    """Give the ± when two runs' main metrics differ by less than it."""
    main = MAIN_METRIC[suite]
    values = sorted(line.metrics[main] for line in lines if main in line.metrics)
    margins = [line.metrics[MARGIN_KEY] for line in lines if MARGIN_KEY in line.metrics]
    if len(values) < 2 or not margins:  # noqa: PLR2004  # a gap needs two runs
        return None
    margin = max(margins)
    gaps = (high - low for low, high in pairwise(values))
    return margin if min(gaps) < margin else None
