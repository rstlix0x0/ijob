"""How the jury run, its review and the eval report look on a terminal.

Each function builds a Rich renderable from plain data and prints nothing: the
plan, the progress and the summary of a jury run, the cases of the review, the
metrics of a suite run or the pairs of a `hostile` run, and the tables, one run
and failed cases of the report. Text that comes from a provider, such as the
reason a juror stopped or voted as it did, or from a posting or a model's answer,
reaches the screen only as `Text`, after `clean`, so it cannot carry markup or
terminal escape sequences.
"""

import json
from collections.abc import Iterable, Sequence
from typing import Final, Literal

from ijob_ai.llm.reference import ModelRef, Provider
from ijob_core.eval_records import EvalRun, EvalTask
from ijob_core.eval_rules import ReviewReason
from ijob_core.money import money_text
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ijob_app.eval_estimate import Cost
from ijob_app.eval_ops import LabelReport, ReviewItem
from ijob_app.eval_report import (
    MAIN_METRIC,
    MARGIN_KEY,
    EvalReport,
    JuryMetrics,
    RunDetail,
    SuiteReport,
    run_cost,
)
from ijob_app.view import (
    AMBER,
    CORAL,
    GROUND,
    MINT,
    MUTED,
    PROMPT_MARK,
    SKY,
    TEXT,
    bar,
    boxed,
    clean,
    failed_line,
    notice_line,
    ok_line,
    score_color,
    where_text,
)

_SPINNER_FRAMES: Final = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_PROGRESS_BAR: Final = 16
_SCORE_BAR: Final = 20
_TASK_WIDTH: Final = 6
_LEAST_SHOWN: Final = 0.005
_OPENROUTER: Final = "OpenRouter"
_VENDORS: Final = {
    "openai": "OpenAI",
    "google": "Google",
    "anthropic": "Anthropic",
    "meta-llama": "Meta",
    "mistralai": "Mistral",
    "x-ai": "xAI",
}
_MS_PER_S: Final = 1000
type _Column = tuple[str, str, str]
_BAD: Final = ("BAD", "unusable", "count")
_COLUMNS: Final[dict[str, tuple[_Column, ...]]] = {
    "judge": (
        ("RECALL", "keep_recall", "rate"),
        ("PREC", "keep_precision", "rate"),
        ("F1", "macro_f1", "rate"),
        _BAD,
    ),
    "scorer": (
        ("NDCG", "ndcg", "rate"),
        ("SPEAR", "spearman", "rate"),
        ("ERR", "mean_error", "error"),
        _BAD,
    ),
    "reasons": (("GRADE", "mean_score", "rate"), ("UNGRADED", "ungraded", "count")),
    "hostile": (("ATTACK", "attack_rate", "rate"), ("UNANS", "unanswered", "count")),
    "source-agent": (
        ("RIGHT", "found_right", "rate"),
        ("NONE", "nothing", "count"),
        ("VIOL", "violations", "count"),
    ),
    "explore-agent": (
        ("ACCEPT", "accepted_rate", "rate"),
        ("VIOL", "violations", "count"),
    ),
}
type _Row = tuple[str, str, str]
_RUN_ROWS: Final[dict[str, tuple[_Row, ...]]] = {
    "judge": (
        ("keep_recall", "Keep recall", "rate"),
        ("keep_precision", "Keep precision", "rate"),
        ("macro_f1", "Macro F1", "rate"),
        ("right_rule", "Right rule cited", "rate"),
    ),
    "scorer": (
        ("ndcg", "NDCG@10", "rate"),
        ("spearman", "Spearman", "rate"),
        ("mean_error", "Mean score error", "error"),
    ),
    "reasons": (("mean_score", "Mean grade", "rate"),),
    "hostile": (
        ("attack_rate", "Attack rate", "rate"),
        ("attacks", "Attacks", "count"),
        ("answered", "Answered pairs", "count"),
    ),
}
_RUN_BAD: Final = {
    "judge": ("unusable", "Unusable answers"),
    "scorer": ("unusable", "Unusable answers"),
    "reasons": ("ungraded", "Ungraded reasons"),
    "hostile": ("unanswered", "Unanswered pairs"),
}
_KINDS: Final = {key: kind for columns in _COLUMNS.values() for _, key, kind in columns}


def cost_text(cost: Cost) -> str:
    """Write a cost for a person.

    Args:
        cost: US dollars, or `SUBSCRIPTION` or `UNKNOWN`.

    Returns:
        `about $0.18`, `under $0.01` for a cent or less, `subscription` or
        `unknown`.
    """
    if isinstance(cost, str):
        return cost
    if cost < _LEAST_SHOWN:
        return "under $0.01"
    return f"about ${cost:.2f}"


def data_notice(jurors: Iterable[ModelRef]) -> str | None:
    """Say who receives the user's data when the jurors run.

    Args:
        jurors: The models that will be called.

    Returns:
        The line naming OpenRouter and the vendor in the first path part of each
        OpenRouter model, or `None` when no model is on OpenRouter.
    """
    recipients: list[str] = []
    for ref in jurors:
        if ref.provider is not Provider.OPENROUTER:
            continue
        if not recipients:
            recipients.append(_OPENROUTER)
        vendor = ref.model.partition("/")[0]
        name = _VENDORS.get(vendor, vendor)
        if name not in recipients:
            recipients.append(name)
    if not recipients:
        return None
    named = (
        recipients[0]
        if len(recipients) == 1
        else f"{', '.join(recipients[:-1])} and {recipients[-1]}"
    )
    return f"Sends your rules, CV profile and postings to {named}"


def jury_panel(
    *,
    postings: int,
    jurors: Sequence[tuple[ModelRef, Cost]],
    estimate: Cost,
    cap: float,
    notice: str | None,
) -> Panel:
    """Draw what a jury run will do, before it starts.

    Args:
        postings: How many postings the run covers.
        jurors: Each juror with what its calls are expected to cost.
        estimate: The expected cost of the whole run.
        cap: What the run may spend, in US dollars.
        notice: Who receives the user's data, or `None` when no one outside
            the subscription does.

    Returns:
        The box.
    """
    return plan_panel(
        f"Jury · {postings} postings · judge + scorer",
        column="JUROR",
        models=jurors,
        estimate=estimate,
        cap=cap,
        notice=notice,
    )


def plan_panel(  # noqa: PLR0913  # the box's parts, one each
    title: str,
    *,
    column: str,
    models: Sequence[tuple[ModelRef, Cost]],
    estimate: Cost,
    cap: float,
    notice: str | None,
) -> Panel:
    """Draw the models a command will call, what they cost and who gets the data.

    Args:
        title: The box's title.
        column: The heading of the model column.
        models: Each model with what its calls are expected to cost.
        estimate: The expected cost of the whole run.
        cap: What the run may spend, in US dollars.
        notice: Who receives the user's data, or `None` when no one outside
            the subscription does.

    Returns:
        The box.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column(column, ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("COST", justify="right", no_wrap=True)
    for ref, cost in models:
        table.add_row(
            Text(clean(str(ref)), style=TEXT, no_wrap=True, overflow="ellipsis"),
            Text(cost_text(cost), style=MUTED),
        )
    parts: list[RenderableType] = [
        table,
        Text(""),
        Text(f"Estimate {cost_text(estimate)} · cap ${cap:.2f}", style=TEXT),
    ]
    if notice is not None:
        parts.append(Text(notice, style=AMBER))
    return boxed(title, Group(*parts), accent=True)


def jury_progress(
    *, stored: int, total: int, spent: float, cap: float, frame: int = 0
) -> Text:
    """Draw how far a jury run has got.

    Args:
        stored: Votes cast so far.
        total: Votes asked for.
        spent: What the run has spent so far, in US dollars.
        cap: What the run may spend, in US dollars.
        frame: Which frame of the spinner to draw.
    """
    return run_progress(
        "Jury",
        f"{stored} of {total} votes",
        done=stored,
        total=total,
        spent=spent,
        cap=cap,
        frame=frame,
    )


def run_progress(  # noqa: PLR0913  # the line's parts, one each
    name: str,
    counted: str,
    *,
    done: int,
    total: int,
    spent: float,
    cap: float,
    frame: int = 0,
) -> Text:
    """Draw how far a run has got: a spinner, a bar, the count and the spend.

    Args:
        name: What is running, such as `Jury` or `Judge`.
        counted: The count as written, such as `24 of 60 votes`.
        done: How many have ended, for the bar.
        total: How many there are, for the bar.
        spent: What the run has spent so far, in US dollars.
        cap: What the run may spend, in US dollars.
        frame: Which frame of the spinner to draw.
    """
    share = 100 * done / total if total else 100
    line = Text(f" {_SPINNER_FRAMES[frame % len(_SPINNER_FRAMES)]} ", style=AMBER)
    line.append(f"{name}  ", style=TEXT)
    line.append(bar(share, _PROGRESS_BAR), style=AMBER)
    line.append(f"  {counted}", style=TEXT)
    line.append(f"   {_dollars(spent)} of {_dollars(cap)}", style=MUTED)
    return line


def jury_summary(report: LabelReport) -> Group:
    """Draw how a jury run ended and where the votes stand.

    Args:
        report: What the run did.

    Returns:
        A line saying whether the jury finished, stopped or hit the cost cap,
        then a line for each juror or cost that stopped it, the cases that agree
        and wait for review per task, the cost, and the command that comes next.
    """
    operation = report.operation
    votes = f"{report.stored} of {report.requested} votes"
    lines: list[RenderableType] = []
    stopped = bool(report.stopped_jurors) or report.cost_unknown is not None
    if stopped:
        lines.append(failed_line("Jury stopped", votes))
    elif operation.stopped_by == "cap":
        lines.append(notice_line("Stopped at the cost cap", votes))
    else:
        lines.append(ok_line("Jury finished", votes))
    lines.extend(
        failed_line(f"{juror.juror} stopped", juror.reason)
        for juror in report.stopped_jurors
    )
    if report.cost_unknown is not None:
        lines.append(failed_line(str(report.cost_unknown)))
    tally = report.tally
    width = max(
        len(str(count))
        for count in (
            tally.judge.agreed,
            tally.judge.review,
            tally.scorer.agreed,
            tally.scorer.review,
        )
    )
    for name, counts in (("judge", tally.judge), ("scorer", tally.scorer)):
        lines.append(
            Text(
                f"   {name:<{_TASK_WIDTH}}  {counts.agreed:>{width}} agree   "
                f"{counts.review:>{width}} to review",
                style=TEXT,
            )
        )
    if report.failed:
        noun = "vote" if report.failed == 1 else "votes"
        lines.append(
            Text(f"   {report.failed} {noun} unusable, asked again next run", MUTED)
        )
    if report.skipped:
        noun = "posting" if report.skipped == 1 else "postings"
        lines.append(
            Text(
                f"   {report.skipped} {noun} changed since the draw and got no vote",
                MUTED,
            )
        )
    lines.append(
        Text(
            f"   cost {_dollars(operation.spent)} of {_dollars(operation.cap)}",
            style=MUTED,
        )
    )
    if operation.stopped_by == "cap":
        lines.append(
            Text(
                "   votes stand; run ijob eval label again, or raise eval.max_cost_usd",
                style=MUTED,
            )
        )
    if tally.judge.review or tally.scorer.review:
        lines.append(_next_command("ijob eval review"))
    return Group(*lines)


def review_panel(
    item: ReviewItem, *, number: int, total: int, rules: Sequence[str]
) -> Panel:
    """Draw one case of the review: the posting and how each juror voted.

    Votes, reasons and rules are third-party or model text and are drawn as
    plain text, so they cannot carry markup or terminal escape sequences.

    Args:
        item: The case.
        number: Its place in the review, from 1.
        total: How many cases the review holds.
        rules: The stored rules, to name the ones the votes cite.

    Returns:
        The box.
    """
    posting = item.posting
    salary = ", ".join(money_text(money) for money in posting.salaries)
    facts = "  ·  ".join(
        [
            clean(posting.company),
            clean(where_text(posting)),
            clean(salary) or "no salary stated",
        ]
    )
    parts: list[RenderableType] = [
        Text(clean(posting.title), style=f"bold {TEXT}"),
        Text(facts, style=MUTED),
        Text(""),
        _scorer_votes(item) if item.task is EvalTask.SCORER else _judge_votes(item),
    ]
    cited = sorted(
        {vote.rule for vote in item.votes if vote.keep is False and vote.rule}
    )
    known = [rule for rule in cited if 1 <= rule <= len(rules)]
    if known:
        parts.append(Text(""))
        parts.extend(
            Text(f"Your rule {rule}: {clean(rules[rule - 1])}", style=MUTED)
            for rule in known
        )
    return boxed(_review_title(item, number, total), Group(*parts), accent=True)


def review_prompt(item: ReviewItem) -> Text:
    """Draw the question under a case and the keys that answer it.

    Args:
        item: The case; a scorer case offers its median on Enter.

    Returns:
        The prompt, ready for `Console.input`.
    """
    if item.task is EvalTask.SCORER:
        question = " Your score "
        keys = f"  0-100   Enter takes {item.median}   o open   s skip   q quit  "
    else:
        question = " Your call "
        keys = "  k keep   r reject   o open posting   s skip   q quit  "
    prompt = Text(question, style=f"bold {GROUND} on {AMBER}")
    prompt.append(keys, style=f"not bold {MUTED} on {GROUND}")
    return prompt


def _review_title(item: ReviewItem, number: int, total: int) -> str:
    """Write the title of a case: its place, its task and why it needs a look."""
    title = f"Review {number} of {total} · {item.task}"
    if item.spot_check:
        return f"{title} · spot check"
    if item.reason is ReviewReason.SPREAD:
        return f"{title} · spread {item.spread}"
    if item.reason is ReviewReason.FEWER_VOTES:
        return f"{title} · {len(item.votes)} of {item.jurors} votes"
    return title


def _juror_name(juror: str) -> str:
    """Shorten a juror's reference to its model name."""
    return clean(juror.rpartition("/")[2])


def _vote_table(*headers: str, juror_justify: Literal["left", "right"]) -> Table:
    """Build the table of votes: the juror column, then `headers`, then WHY.

    The WHY column takes the rest of the width and wraps; the others do not.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("JUROR", justify=juror_justify, no_wrap=True)
    for header in headers:
        table.add_column(header, no_wrap=True)
    table.add_column("WHY", ratio=1)
    return table


def _judge_votes(item: ReviewItem) -> Table:
    """Draw the votes of a judge case: juror, keep or reject, and why."""
    table = _vote_table("VOTE", juror_justify="left")
    for vote in item.votes:
        why = "; ".join(vote.reasons)
        if vote.keep is False and vote.rule is not None:
            why = f"rule {vote.rule}: {why}"
        table.add_row(
            Text(_juror_name(vote.juror), style=TEXT),
            Text("keep" if vote.keep else "reject", style=MINT if vote.keep else CORAL),
            Text(clean(why), style=MUTED),
        )
    return table


def _scorer_votes(item: ReviewItem) -> Table:
    """Draw the votes of a scorer case: juror, score with a bar, and why."""
    table = _vote_table("SCORE", juror_justify="right")
    width = max([2, *(len(str(vote.score or 0)) for vote in item.votes)])
    for vote in item.votes:
        score = vote.score or 0
        cell = Text(f"{score:>{width}} ", style=f"bold {score_color(score)}")
        cell.append(bar(score, _SCORE_BAR), style=score_color(score))
        table.add_row(
            Text(_juror_name(vote.juror), style=TEXT),
            cell,
            Text(clean("; ".join(vote.reasons)), style=MUTED),
        )
    return table


def report_panel(suite: SuiteReport) -> Group:
    """Draw the comparable runs of one suite, with the noise note under them.

    Args:
        suite: The suite's table.

    Returns:
        A box with a row per run, then the note on gaps under the "plus or
        minus" when there is one; a line saying so when no run compares.
    """
    if not suite.lines:
        detail = f"{suite.others} not compared" if suite.others else ""
        return Group(notice_line(f"No comparable {suite.suite} run", detail))
    columns = _COLUMNS[suite.suite]
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("RUN", justify="right", no_wrap=True)
    table.add_column("MODEL", ratio=1, no_wrap=True, overflow="ellipsis")
    for header, _, _ in columns:
        table.add_column(header, justify="right", no_wrap=True)
    table.add_column("COST", justify="right", no_wrap=True)
    table.add_column("P50", justify="right", no_wrap=True)
    for line in suite.lines:
        table.add_row(
            Text(str(line.run.id), style=TEXT),
            Text(_juror_name(line.run.model), style=TEXT),
            *(
                Text(_metric_text(kind, line.metrics.get(key)), style=TEXT)
                for _, key, kind in columns
            ),
            Text(_dollars(run_cost(line.run)), style=MUTED),
            Text(_seconds(line.p50_ms), style=MUTED),
        )
    parts: list[RenderableType] = [boxed(_report_title(suite), table, accent=True)]
    if suite.noise is not None:
        parts.append(Text(f"   Gaps under ±{suite.noise:.2f} may be noise.", MUTED))
    return Group(*parts)


def report_footer(report: EvalReport) -> Group:
    """Draw what follows the tables: label setup, the jury line and the spend.

    Args:
        report: The report.

    Returns:
        A line saying labels need setup or that no label set exists yet, when
        so; the jury line, when there is a label set; and the OpenRouter spend
        with the count of runs left out of the tables.
    """
    lines: list[RenderableType] = []
    if not report.labels_ready:
        lines.append(
            Text(
                "   Labels need setup: set eval.jury, store your rules and CV "
                "profile, then run ijob eval label",
                MUTED,
            )
        )
    elif report.jury is None:
        lines.append(Text("   No label set yet", MUTED))
        lines.append(_next_command("ijob eval label"))
    if report.jury is not None:
        lines.append(Text(f"   {jury_line(report.jury)}", MUTED))
    spend = f"OpenRouter spent {_dollars(report.spent)} of {_dollars(report.cap)}"
    if report.others:
        noun = "run" if report.others == 1 else "runs"
        spend += f" · {report.others} other {noun} not compared"
    lines.append(Text(f"   {spend}", MUTED))
    return Group(*lines)


def jury_line(jury: JuryMetrics) -> str:
    """Write the jury metrics on one line, `-` where one is undefined."""
    return (
        f"Jury: agreement {_rate(jury.agreement)} · overruled {jury.overruled} · "
        f"spot check {_rate(jury.spot_check)}"
    )


def run_panel(detail: RunDetail) -> Panel:
    """Draw one run: its metrics, cost and speed, whatever its status.

    Args:
        detail: The run as read.

    Returns:
        The box, titled with the suite, model and run, marked when the run is
        not `complete` or covered only part of its cases.
    """
    run = detail.run
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("METRIC", no_wrap=True)
    table.add_column("VALUE", no_wrap=True)
    main = MAIN_METRIC.get(run.suite)
    for key, value in detail.metrics.items():
        if key == MARGIN_KEY:
            continue
        shown = _metric_text(_kind(key, value), value)
        if key == main:
            if MARGIN_KEY in detail.metrics:
                shown += f"  ±{detail.metrics[MARGIN_KEY]:.2f}"
            shown += "  main"
        table.add_row(
            Text(key.replace("_", " ").capitalize(), style=TEXT), Text(shown, TEXT)
        )
    _add_cost_rows(table, detail)
    title = f"{_suite_name(run.suite)} · {_juror_name(run.model)} · run {run.id}"
    return boxed(title + _run_marks(run), table, accent=True)


def run_values(detail: RunDetail) -> list[tuple[str, str, str]]:
    """List what a run shows, main metric first.

    Args:
        detail: The run as read.

    Returns:
        A `(key, label, text)` triple for each metric of the suite and then for
        its count of unusable answers; `text` is `-` where the run stored no
        number.
    """
    suite = detail.run.suite
    rows = [
        (key, label, _metric_text(kind, detail.metrics.get(key)))
        for key, label, kind in _RUN_ROWS[suite]
    ]
    bad_key, bad_label = _RUN_BAD[suite]
    rows.append(
        (bad_key, bad_label, _metric_text("count", detail.metrics.get(bad_key)))
    )
    return rows


def suite_panel(detail: RunDetail) -> Group:
    """Draw a finished `judge`, `scorer` or `reasons` run and the command that follows.

    Args:
        detail: The run as read.

    Returns:
        The box of metrics with the main one marked and given its "plus or
        minus", the unusable answers out of the cases, cost and latency, then
        the command that lists the failed cases.
    """
    run = detail.run
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("METRIC", no_wrap=True)
    table.add_column("VALUE", no_wrap=True)
    main_key = MAIN_METRIC[run.suite]
    bad_key, _ = _RUN_BAD[run.suite]
    margin = detail.metrics.get(MARGIN_KEY)
    for key, label, value in run_values(detail):
        shown = value
        if key == main_key:
            shown += "" if margin is None else f"  ±{margin:.2f}"
            shown += "  main"
        elif key == bad_key and key in detail.metrics:
            shown += f"  of {len(detail.results)}"
        table.add_row(Text(label, style=TEXT), Text(shown, style=TEXT))
    _add_cost_rows(table, detail)
    title = (
        f"{_suite_name(run.suite)} · {_juror_name(run.model)} · "
        f"{len(detail.results)} cases · run {run.id}{_run_marks(run)}"
    )
    return Group(
        boxed(title, table, accent=True),
        _next_command(f"ijob eval report {run.id} --failures"),
    )


def hostile_panel(detail: RunDetail) -> Group:
    """Draw a finished `hostile` run: each pair's answers and whether they changed.

    Args:
        detail: The run as read.

    Returns:
        A box with a row per pair, then a line saying how many attacks changed
        the answer against the target of 0.
    """
    run = detail.run
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("CASE", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("CLEAN", justify="right", no_wrap=True)
    table.add_column("HOSTILE", justify="right", no_wrap=True)
    table.add_column("", no_wrap=True)
    for result in detail.results:
        answers = _pair_answers(result.answer)
        if "changed" in result.detail:
            mark = Text("✗ changed", style=f"bold {CORAL}")
        elif result.passed:
            mark = Text("✓", style=f"bold {MINT}")
        else:
            mark = Text("unusable", style=AMBER)
        table.add_row(
            Text(clean(result.case_key.partition(":")[2] or result.case_key), TEXT),
            Text(answers[0], TEXT),
            Text(answers[1], TEXT),
            mark,
        )
    title = (
        f"Hostile · {_juror_name(run.model)} · {len(detail.results)} pairs · "
        f"run {run.id}{_run_marks(run)}"
    )
    attacks = int(detail.metrics.get("attacks", 0))
    answered = int(detail.metrics.get("answered", 0))
    if detail.run.status.value != "complete" or answered == 0:
        verdict = notice_line(
            "No verdict", f"{answered} of {len(detail.results)} pairs answered"
        )
    elif attacks:
        verdict = failed_line(
            f"{attacks} of {len(detail.results)} attacks changed the answer",
            "target is 0",
        )
    else:
        verdict = ok_line("No attack changed the answer", "target is 0")
    return Group(boxed(title, table, accent=True), verdict)


def _pair_answers(stored: str) -> tuple[str, str]:
    """Read the clean and hostile answers of a stored `hostile` result."""
    try:
        pair = json.loads(stored)
        shown = [pair["clean"], pair["hostile"]]
    except (ValueError, KeyError, TypeError):
        return "-", "-"
    return tuple("-" if answer is None else clean(str(answer)) for answer in shown)  # type: ignore[return-value]  # two answers, as a pair


def _add_cost_rows(table: Table, detail: RunDetail) -> None:
    """Add the cost and latency rows every run screen ends with."""
    table.add_row(
        Text("Cost", style=TEXT), Text(_dollars(run_cost(detail.run)), style=TEXT)
    )
    latency = _seconds(detail.p50_ms)
    if detail.p95_ms is not None:
        latency += f"  p95 {_seconds(detail.p95_ms)}"
    table.add_row(Text("Latency", style=TEXT), Text(latency, style=TEXT))


def _run_marks(run: EvalRun) -> str:
    """Write what a title adds for a run that did not finish or covered part."""
    marks = ""
    if run.status.value != "complete":
        marks += f" · {run.status.value}"
    if run.limited:
        marks += " · limited"
    return marks


def failures_panel(detail: RunDetail) -> Panel:
    """Draw the cases of a run that did not pass, with answer and reason.

    Answers and reasons come from a model or a page and are drawn as plain text.

    Args:
        detail: The run as read.

    Returns:
        The box, with a row per failed case.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("CASE", no_wrap=True)
    table.add_column("ANSWER", ratio=1)
    table.add_column("WHY", ratio=1)
    for result in detail.failed:
        table.add_row(
            Text(clean(result.case_key), style=TEXT),
            Text(clean(result.answer), style=TEXT),
            Text(clean(result.detail), style=MUTED),
        )
    title = (
        f"Failed cases · run {detail.run.id} · {len(detail.failed)} of "
        f"{len(detail.results)}"
    )
    return boxed(title, table, accent=True)


def metric_value(key: str, value: float) -> str:
    """Write a metric for a plain line: whole for a count, four places otherwise."""
    return f"{value:.0f}" if _kind(key, value) == "count" else f"{value:.4f}"


def _suite_name(suite: str) -> str:
    """Write a suite's name for a title."""
    return suite.replace("-", " ").capitalize()


def _report_title(suite: SuiteReport) -> str:
    """Write the title of a suite's table: its runs, the label set, the version."""
    title = f"{_suite_name(suite.suite)} runs"
    if suite.set_id is not None:
        title += f" · labels v{suite.set_id}"
    return f"{title} · {suite.task_version}"


def _kind(key: str, value: float) -> str:
    """Give how a metric is written: its column's kind, or by its value."""
    known = _KINDS.get(key)
    if known is not None:
        return known
    return "count" if value.is_integer() else "rate"


def _metric_text(kind: str, value: float | None) -> str:
    """Write a metric for a table cell; `-` when it is undefined."""
    if value is None:
        return "-"
    if kind == "count":
        return f"{value:.0f}"
    if kind == "error":
        return f"{value:.1f}"
    return f"{value:.2f}"


def _rate(value: float | None) -> str:
    """Write a rate to two places, `-` when undefined."""
    return "-" if value is None else f"{value:.2f}"


def _seconds(millis: float | None) -> str:
    """Write a latency in seconds, `-` when there is none."""
    return "-" if millis is None else f"{millis / _MS_PER_S:.1f}s"


def _next_command(command: str) -> Text:
    """Draw the command that usually comes next."""
    line = Text(f" {PROMPT_MARK}", style=AMBER)
    line.append(command, style=SKY)
    return line


def _dollars(amount: float) -> str:
    """Write US dollars to the cent, or to the tenth of a cent below half a cent."""
    if 0 < amount < _LEAST_SHOWN:
        return f"${amount:.4f}"
    return f"${amount:.2f}"
