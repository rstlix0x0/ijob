"""The tuning report: untuned models and tuned programs side by side.

`build_tune_report` reads stored rows and makes no model call and no write. It
recomputes each number per case from the stored answers of eval runs, so a run's
own metrics text is never trusted. `Choosing` lists every untuned model on the
train and val postings only, so no held-out label or answer informs a choice;
only its cost per 100 is the whole run's rate, because a result stores no cost of
its own. `Held-out` appears once a tuned program of the report's set has a
complete held-out run, and lists every untuned model and every such program on
the held-out postings; a program of the set with no complete held-out run is
listed with dashes and its tuning spend. A posting whose text changed since the
draw counts as missing in every row, so all rows cover the same postings.

`report_markdown` renders the same tables as Markdown, and holds model,
optimizer and vendor names and numbers only. `choosing_cells`, `held_out_cells`
and `footer_lines` write the text both that and the terminal view show, so the
two never differ.
"""

import json
import statistics
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from ijob_ai.evals.metrics import (
    JudgeAnswer,
    JudgeCase,
    ScorerCase,
    judge_metrics,
    scorer_metrics,
    wilson,
)
from ijob_ai.llm.reference import ModelRef, Provider, parse_model_ref
from ijob_ai.llm.settings import LlmSettings, TuneSettings
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    EvalLabel,
    EvalRun,
    EvalSet,
    EvalTask,
    RunStatus,
)
from ijob_core.model import PostingId
from ijob_core.store import Store
from ijob_core.tune_records import SplitPart, TunedProgram

from ijob_app.eval_suites import unchanged_posting
from ijob_app.tune_ops import SplitReport, full_run, program_status, split_report

COST_SOURCE: Final = (
    "Cost comes from ijob's records, not Phoenix. Subscription cost is metered "
    "value, not money spent."
)
SCREEN_HINT: Final = "run `ijob tune screen` for any that never ran"
COST_RATE_NOTE: Final = (
    "$/100 is the whole run's rate, including held-out postings; "
    "cost is not split per posting"
)
MARGIN_NOTE: Final = (
    "± is the 95% interval half-width of keep recall for the judge, not of "
    "macro F1; the scorer has none."
)
_NO_SPLIT: Final = "No split yet: run `ijob tune screen` to freeze one."
_METERED: Final = "metered value"
_PER_100: Final = 100


@dataclass(frozen=True, slots=True)
class ChoosingRow:
    """One untuned model on one task, measured on the train and val postings.

    Attributes:
        task: `judge` or `scorer`.
        model: The model reference.
        vendor: Who makes the model.
        macro_f1: Judge macro F1, or `None`.
        recall: Judge keep recall, or `None`.
        ndcg: Scorer NDCG@10, or `None`.
        mean_error: Scorer mean absolute score error, or `None`.
        cost_per_100: The whole run's cost per 100 postings it holds a result
            for, in US dollars, or `None`. It is not limited to the train and val
            postings, because a result stores no cost of its own.
        metered: Whether the cost is claude-sdk metered value, not money spent.
        cases: Postings the numbers cover.
        missing: Train and val postings the run holds no result for.
        unusable: Covered postings whose stored answer is unusable. Each counts
            as wrong, but the scorer's mean error skips it.
    """

    task: EvalTask
    model: str
    vendor: str
    macro_f1: float | None
    recall: float | None
    ndcg: float | None
    mean_error: float | None
    cost_per_100: float | None
    metered: bool
    cases: int
    missing: int
    unusable: int


@dataclass(frozen=True, slots=True)
class HeldOutRow:
    """One untuned model or one tuned program on one task, on held-out postings.

    Attributes:
        task: `judge` or `scorer`.
        model: The model reference.
        program: The program's number, or `None` for an untuned model.
        optimizer: The optimizer that made the program, or `None`.
        main: Judge macro F1 or scorer NDCG@10, or `None`.
        margin: The 95% interval half-width of judge keep recall, or `None`.
        recall: Judge keep recall, or `None`.
        precision: Judge keep precision, or `None`.
        main_diff: `main` minus the baseline's, or `None` without both.
        cost_per_100: The run's cost per 100 postings in US dollars, or `None`.
        cost_diff: `cost_per_100` minus the baseline's, or `None` without both.
        cost_diff_metered: Whether either side of `cost_diff` is claude-sdk
            metered value, not money spent.
        p50_ms: Median latency in milliseconds, or `None`.
        tune_spend: Money spent tuning the program in US dollars, or `None`.
        tune_metered: claude-sdk metered value of tuning the program in US
            dollars, which is not money spent, or `None`.
        metered: Whether the cost is claude-sdk metered value, not money spent.
        cases: Postings the numbers cover.
        missing: Held-out postings the run holds no result for.
    """

    task: EvalTask
    model: str
    program: int | None
    optimizer: str | None
    main: float | None
    margin: float | None
    recall: float | None
    precision: float | None
    main_diff: float | None
    cost_per_100: float | None
    cost_diff: float | None
    p50_ms: float | None
    tune_spend: float | None
    tune_metered: float | None
    metered: bool
    cases: int
    missing: int
    cost_diff_metered: bool = False


@dataclass(frozen=True, slots=True)
class TuneReport:
    """What `ijob tune report` shows.

    Attributes:
        split: What the split holds and what happened since it was frozen.
        notice: Why the tables are empty, or `None` when a split exists.
        choosing: Every untuned model with a full run, by task and model.
        held_out: Empty until a tuned program has a complete held-out run.
        held_out_postings: Postings in the held-out part.
        held_out_keeps: Held-out postings labelled keep.
    """

    split: SplitReport
    notice: str | None
    choosing: tuple[ChoosingRow, ...]
    held_out: tuple[HeldOutRow, ...]
    held_out_postings: int
    held_out_keeps: int

    @property
    def missing(self) -> int:
        """The most postings any row leaves out because the run lacks them."""
        counts = [row.missing for row in self.choosing]
        counts += [row.missing for row in self.held_out]
        return max(counts, default=0)

    @property
    def missing_hint(self) -> str | None:
        """Say how many postings a row leaves out, or `None` when none."""
        if not self.missing:
            return None
        return (
            f"Up to {self.missing} postings have no stored answer, or changed "
            f"since the draw, and are left out; {SCREEN_HINT}."
        )

    @property
    def held_out_title(self) -> str:
        """The heading of the held-out table."""
        return (
            f"Held-out ({self.held_out_postings} postings, {self.held_out_keeps} keeps)"
        )


@dataclass(frozen=True, slots=True)
class _Scored:
    """The numbers recomputed from one run's stored answers."""

    cases: int
    missing: int
    unusable: int
    main: float | None
    margin: float | None
    recall: float | None
    precision: float | None
    mean_error: float | None
    p50_ms: float | None


def _vendor(model: str) -> str:
    """Name who makes a model reference: the provider's own, else its first part."""
    provider, _, name = model.partition("/")
    if provider == Provider.CLAUDE_SDK:
        return "anthropic"
    return name.partition("/")[0] if "/" in name else provider


def _metered(run: EvalRun) -> bool:
    """Tell whether the run's cost is claude-sdk subscription value."""
    return run.model.startswith(f"{Provider.CLAUDE_SDK}/")


def _cost_per_100(store: Store, run: EvalRun) -> float | None:
    """Give the run's whole cost per 100 of the postings it holds a result for.

    A result stores no cost of its own, so the rate covers every posting the run
    has a result for, held-out ones included.
    """
    results = len(store.evals.results(run.id))
    if not results:
        return None
    return (run.spent_usd + run.subscription_usd) / results * _PER_100


def _judge_answer(text: str) -> JudgeAnswer | None:
    """Read a stored judge answer; `None` for `null` or anything unusable."""
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("keep"), bool):
        return None
    rule = loaded.get("rule")
    return JudgeAnswer(loaded["keep"], rule if isinstance(rule, int) else None)


def _scorer_answer(text: str) -> int | None:
    """Read a stored scorer answer; `None` for `null` or anything unusable."""
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    score = loaded.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        return None
    return score


def _macro_f1(judged: list[JudgeCase], defined: float | None) -> float | None:
    """Give macro F1, scoring a labelled class with no answers at 0.

    `judge_metrics` leaves macro F1 undefined when either class has no answers.
    Here a class with labels and no answers scores 0, so a judge that answers
    nothing, or only one class, reads as bad and not as unmeasured. A class with
    no labels in the covered postings stays undefined.
    """
    if defined is not None:
        return defined
    total = 0.0
    for keep in (True, False):
        labelled = sum(1 for case in judged if case.gold_keep is keep)
        if not labelled:
            return None
        answered = sum(
            1 for case in judged if case.answer is not None and case.answer.keep is keep
        )
        right = sum(
            1
            for case in judged
            if case.answer is not None
            and case.answer.keep is keep
            and case.gold_keep is keep
        )
        total += 2 * right / (labelled + answered)
    return total / 2


def _score_run(  # noqa: PLR0913, PLR0917  # one run and the postings it covers
    store: Store,
    run: EvalRun,
    task: EvalTask,
    labels: dict[PostingId, EvalLabel],
    keys: list[PostingId],
    changed: frozenset[PostingId],
) -> _Scored:
    """Recompute a run's numbers over the given postings from its stored answers.

    A posting in `changed` is left out and counted missing, whatever the run holds.
    """
    results = {result.case_key: result for result in store.evals.results(run.id)}
    present = [key for key in keys if str(key) in results and key not in changed]
    missing = len(keys) - len(present)
    latencies = [results[str(key)].latency_ms for key in present]
    p50 = statistics.median(latencies) if latencies else None
    if task is EvalTask.JUDGE:
        judged = [
            JudgeCase(
                labels[key].keep is True,
                labels[key].rule,
                _judge_answer(results[str(key)].answer),
            )
            for key in present
        ]
        numbers = judge_metrics(judged)
        unusable = sum(1 for case in judged if case.answer is None)
        labelled_keep = sum(1 for case in judged if case.gold_keep)
        right_keep = sum(
            1
            for case in judged
            if case.gold_keep and case.answer is not None and case.answer.keep
        )
        interval = wilson(right_keep, labelled_keep)
        return _Scored(
            len(present),
            missing,
            unusable,
            _macro_f1(judged, numbers.macro_f1),
            None if interval is None else interval.margin,
            numbers.keep_recall,
            numbers.keep_precision,
            None,
            p50,
        )
    scored = [
        ScorerCase(labels[key].score or 0, _scorer_answer(results[str(key)].answer))
        for key in present
    ]
    scorer = scorer_metrics(scored)
    unusable = sum(1 for case in scored if case.answer is None)
    return _Scored(
        len(present),
        missing,
        unusable,
        scorer.ndcg,
        None,
        None,
        None,
        scorer.mean_error,
        p50,
    )


def _split_keys(store: Store, eval_set: EvalSet, *parts: SplitPart) -> list[PostingId]:
    """List the postings of the given split parts by number."""
    return [
        row.posting_id for row in store.tunes.split(eval_set.id) if row.part in parts
    ]


def _changed(store: Store, eval_set: EvalSet) -> frozenset[PostingId]:
    """List the postings whose text changed since the draw or that are gone."""
    return frozenset(
        case.posting_id
        for case in store.evals.cases(eval_set.id)
        if unchanged_posting(store, case) is None
    )


def _labels(
    store: Store, eval_set: EvalSet, task: EvalTask
) -> dict[PostingId, EvalLabel]:
    return {label.posting_id: label for label in store.evals.labels(eval_set.id, task)}


def _stored_models(store: Store, eval_set: EvalSet, suites: set[str]) -> list[ModelRef]:
    """List the models any stored unlimited run of the set names, oldest first."""
    found: list[ModelRef] = []
    for run in store.evals.runs():
        if run.suite in suites and run.set_id == eval_set.id and not run.limited:
            try:
                found.append(parse_model_ref(run.model, "model"))
            except InvalidValueError:
                continue
    return found


def _untuned_runs(
    store: Store, eval_set: EvalSet, llm: LlmSettings, tune: TuneSettings
) -> list[tuple[EvalTask, ModelRef, EvalRun]]:
    """Find the full run of each model and task, where one exists.

    The models are the configured ones and every model the set's stored
    full-size runs name, so a model dropped from the config keeps its row.
    """
    suites = {task.value for task in EvalTask}
    models = dict.fromkeys(
        (
            *tune.candidates,
            *([tune.baseline] if tune.baseline else []),
            *llm.models.values(),
            *_stored_models(store, eval_set, suites),
        )
    )
    found = (
        (task, model, full_run(store, task, model, eval_set))
        for task in EvalTask
        for model in models
    )
    return [(task, model, run) for task, model, run in found if run is not None]


def _choosing(
    store: Store,
    eval_set: EvalSet,
    runs: list[tuple[EvalTask, ModelRef, EvalRun]],
    changed: frozenset[PostingId],
) -> tuple[ChoosingRow, ...]:
    keys = _split_keys(store, eval_set, SplitPart.TRAIN, SplitPart.VAL)
    labels = {task: _labels(store, eval_set, task) for task in EvalTask}
    rows: list[ChoosingRow] = []
    for task, model, run in runs:
        scored = _score_run(store, run, task, labels[task], keys, changed)
        judge = task is EvalTask.JUDGE
        rows.append(
            ChoosingRow(
                task=task,
                model=str(model),
                vendor=_vendor(str(model)),
                macro_f1=scored.main if judge else None,
                recall=scored.recall,
                ndcg=None if judge else scored.main,
                mean_error=scored.mean_error,
                cost_per_100=_cost_per_100(store, run),
                metered=_metered(run),
                cases=scored.cases,
                missing=scored.missing,
                unusable=scored.unusable,
            )
        )
    return tuple(rows)


def _held_out_run(store: Store, program: TunedProgram) -> EvalRun | None:
    """Find the newest complete limited run of a program's version on its set."""
    for run in reversed(store.evals.runs()):
        if (
            run.suite == program.task.value
            and run.model == program.model
            and run.task_version == program.version
            and run.set_id == program.set_id
            and run.status is RunStatus.COMPLETE
            and run.limited
        ):
            return run
    return None


def _tune_cost(
    store: Store, program: TunedProgram | None
) -> tuple[float | None, float | None]:
    """Give the money and the metered value spent tuning a program, if stored."""
    if program is None or program.tune_run_id is None:
        return None, None
    tune_run = store.evals.run(program.tune_run_id)
    if tune_run is None:
        return None, None
    return tune_run.spent_usd, tune_run.subscription_usd


def _waiting_row(store: Store, program: TunedProgram) -> HeldOutRow:
    """Make the row of a program with no complete held-out run: dashes and spend."""
    spend, metered_value = _tune_cost(store, program)
    return HeldOutRow(
        task=program.task,
        model=program.model,
        program=program.id,
        optimizer=program.optimizer,
        main=None,
        margin=None,
        recall=None,
        precision=None,
        main_diff=None,
        cost_per_100=None,
        cost_diff=None,
        p50_ms=None,
        tune_spend=spend,
        tune_metered=metered_value,
        metered=program.model.startswith(f"{Provider.CLAUDE_SDK}/"),
        cases=0,
        missing=0,
    )


def _held_out_row(  # noqa: PLR0913  # the row's whole inputs, one each
    store: Store,
    run: EvalRun,
    task: EvalTask,
    *,
    labels: dict[PostingId, EvalLabel],
    keys: list[PostingId],
    changed: frozenset[PostingId],
    program: TunedProgram | None,
) -> HeldOutRow:
    scored = _score_run(store, run, task, labels, keys, changed)
    spend, metered_value = _tune_cost(store, program)
    return HeldOutRow(
        task=task,
        model=run.model,
        program=None if program is None else program.id,
        optimizer=None if program is None else program.optimizer,
        main=scored.main,
        margin=scored.margin,
        recall=scored.recall,
        precision=scored.precision,
        main_diff=None,
        cost_per_100=_cost_per_100(store, run),
        cost_diff=None,
        p50_ms=scored.p50_ms,
        tune_spend=spend,
        tune_metered=metered_value,
        metered=_metered(run),
        cases=scored.cases,
        missing=scored.missing,
    )


def _against(row: HeldOutRow, base: HeldOutRow | None) -> HeldOutRow:
    """Fill a row's differences from the baseline's row of the same task."""
    if base is None:
        return row
    main = None if row.main is None or base.main is None else row.main - base.main
    cost = (
        None
        if row.cost_per_100 is None or base.cost_per_100 is None
        else row.cost_per_100 - base.cost_per_100
    )
    return replace(
        row,
        main_diff=main,
        cost_diff=cost,
        cost_diff_metered=row.metered or base.metered,
    )


def _held_out(  # noqa: PLR0913, PLR0917  # the report's inputs, one each
    store: Store,
    eval_set: EvalSet,
    llm: LlmSettings,
    tune: TuneSettings,
    runs: list[tuple[EvalTask, ModelRef, EvalRun]],
    changed: frozenset[PostingId],
) -> tuple[HeldOutRow, ...]:
    tested: list[tuple[TunedProgram, EvalRun]] = []
    waiting: list[TunedProgram] = []
    for task in EvalTask:
        for program in store.tunes.programs(task):
            if program.set_id != eval_set.id:
                continue
            run = _held_out_run(store, program)
            if run is not None and program_status(store, llm, program).tested:
                tested.append((program, run))
            else:
                waiting.append(program)
    if not tested:
        return ()
    keys = _split_keys(store, eval_set, SplitPart.HELD_OUT)
    labels = {task: _labels(store, eval_set, task) for task in EvalTask}
    rows = [
        _held_out_row(
            store,
            run,
            task,
            labels=labels[task],
            keys=keys,
            changed=changed,
            program=None,
        )
        for task, _, run in runs
    ]
    rows += [
        _held_out_row(
            store,
            run,
            program.task,
            labels=labels[program.task],
            keys=keys,
            changed=changed,
            program=program,
        )
        for program, run in tested
    ]
    rows += [_waiting_row(store, program) for program in waiting]
    baseline = None if tune.baseline is None else str(tune.baseline)
    bases = {
        row.task: row for row in rows if row.program is None and row.model == baseline
    }
    return tuple(_against(row, bases.get(row.task)) for row in rows)


def build_tune_report(
    store: Store, eval_set: EvalSet | None, llm: LlmSettings, tune: TuneSettings
) -> TuneReport:
    """Build the report from stored rows, calling no model and writing nothing.

    Args:
        store: The open store.
        eval_set: The label set, or `None` when none exists.
        llm: The settings naming each task's model.
        tune: The tuning settings.

    Returns:
        The report. While the set has no split, both tables are empty and
        `notice` says to run `ijob tune screen`.

    Raises:
        InvalidValueError: Naming `tune.baseline`, if it is not set.
        sqlite3.Error: The store cannot be read.
    """
    if tune.baseline is None:
        raise InvalidValueError(
            "tune.baseline",
            "no baseline model is set; add a [tune] table with tune.baseline "
            "to the config",
        )
    if eval_set is None:
        empty = SplitReport(
            frozen=False, train=0, val=0, held_out=0, unused=0, added_later=0, changed=0
        )
        return TuneReport(empty, _NO_SPLIT, (), (), 0, 0)
    split = split_report(store, eval_set)
    if not split.frozen:
        return TuneReport(split, _NO_SPLIT, (), (), 0, 0)
    runs = _untuned_runs(store, eval_set, llm, tune)
    changed = _changed(store, eval_set)
    held = set(_split_keys(store, eval_set, SplitPart.HELD_OUT))
    keeps = sum(
        1
        for label in store.evals.labels(eval_set.id, EvalTask.JUDGE)
        if label.posting_id in held and label.keep
    )
    return TuneReport(
        split,
        None,
        _choosing(store, eval_set, runs, changed),
        _held_out(store, eval_set, llm, tune, runs, changed),
        split.held_out,
        keeps,
    )


def _num(value: float | None, places: int = 3) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _diff(value: float | None, places: int = 3) -> str:
    return "-" if value is None else f"{value:+.{places}f}"


def _money(value: float | None, *, metered: bool) -> str:
    if value is None:
        return "-"
    return f"${value:.2f} ({_METERED})" if metered else f"${value:.2f}"


def _cost_diff(row: HeldOutRow) -> str:
    """Show the cost difference, labelled when either side is metered value."""
    text = _diff(row.cost_diff, 2)
    if row.cost_diff is not None and row.cost_diff_metered:
        return f"{text} ({_METERED})"
    return text


def _spend(money: float | None, metered_value: float | None) -> str:
    """Show tune spend: money, metered value, or both when a tuner mixed them."""
    if money is None or metered_value is None:
        return "-"
    if not metered_value:
        return f"${money:.2f}"
    if not money:
        return _money(metered_value, metered=True)
    return f"${money:.2f} + {_money(metered_value, metered=True)}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


CHOOSING_HEADER: Final = (
    "task", "model", "vendor", "macro F1", "recall", "NDCG@10", "mean error",
    "cost / 100 postings", "cases", "missing", "unusable",
)  # fmt: skip
HELD_OUT_HEADER: Final = (
    "task", "model", "program", "main", "margin", "main vs baseline",
    "recall", "precision", "cost / 100 postings", "cost vs baseline",
    "P50 ms", "tune spend", "cases", "missing",
)  # fmt: skip


def choosing_cells(row: ChoosingRow) -> list[str]:
    """Write a `Choosing` row as the text of its cells, in `CHOOSING_HEADER` order.

    A number the row does not have reads `-`, and a metered cost says so.
    """
    return [
        row.task.value,
        row.model,
        row.vendor,
        _num(row.macro_f1),
        _num(row.recall),
        _num(row.ndcg),
        _num(row.mean_error, 1),
        _money(row.cost_per_100, metered=row.metered),
        str(row.cases),
        str(row.missing),
        str(row.unusable),
    ]


def held_out_cells(row: HeldOutRow) -> list[str]:
    """Write a `Held-out` row as the text of its cells, in `HELD_OUT_HEADER` order.

    A number the row does not have reads `-`; a metered cost and a cost difference
    with a metered side say so, and tune spend and tune metered value stay apart.
    """
    return [
        row.task.value,
        row.model,
        "untuned"
        if row.program is None
        else f"program {row.program} ({row.optimizer})",
        _num(row.main),
        "-" if row.margin is None else f"±{row.margin:.3f}",
        _diff(row.main_diff),
        _num(row.recall),
        _num(row.precision),
        _money(row.cost_per_100, metered=row.metered),
        _cost_diff(row),
        _num(row.p50_ms, 0),
        _spend(row.tune_spend, row.tune_metered),
        str(row.cases),
        str(row.missing),
    ]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def footer_lines(report: TuneReport) -> list[str]:
    """Write what happened since the freeze, and how many postings a row left out.

    Returns:
        The line counting labels added after the freeze and changed postings,
        then the missing-postings hint when there is one.
    """
    split = report.split
    later = f"{_plural(split.added_later, 'label')} added after the freeze"
    changed = f"{_plural(split.changed, 'posting')} changed"
    lines = [f"{later}; {changed}."]
    if report.missing_hint is not None:
        lines.append(report.missing_hint)
    return lines


def _choosing_lines(report: TuneReport) -> list[str]:
    rows = [choosing_cells(row) for row in report.choosing]
    return ["## Choosing", "", *_table(list(CHOOSING_HEADER), rows), "", COST_RATE_NOTE]


def _held_out_lines(report: TuneReport) -> list[str]:
    rows = [held_out_cells(row) for row in report.held_out]
    return [
        f"## {report.held_out_title}",
        "",
        *_table(list(HELD_OUT_HEADER), rows),
        "",
        MARGIN_NOTE,
    ]


def report_markdown(report: TuneReport) -> str:
    """Render the report as Markdown: names and numbers only.

    The text holds model, optimizer and vendor names and numbers. It holds no
    prompt text, rule, profile or posting, because the report never reads them.

    Args:
        report: The report from `build_tune_report`.

    Returns:
        The Markdown text, ending in a newline.
    """
    lines = ["# Tuning report", ""]
    if report.notice is not None:
        lines += [report.notice, ""]
    else:
        lines += [*_choosing_lines(report), ""]
        if report.held_out:
            lines += [*_held_out_lines(report), ""]
        for line in footer_lines(report):
            lines += [line, ""]
    lines += [COST_SOURCE, ""]
    return "\n".join(lines)


def write_report_markdown(path: Path, report: TuneReport) -> None:
    """Write the report's Markdown to a file, replacing what was there.

    Args:
        path: The file to write.
        report: The report from `build_tune_report`.

    Raises:
        OSError: The file cannot be written.
    """
    path.write_text(report_markdown(report), encoding="utf-8")
