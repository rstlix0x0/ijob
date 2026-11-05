"""How the tuning commands look: the plans, what they did, the programs, the report.

Each function builds a Rich renderable, or plain lines for a pipe, from plain data
and prints nothing. Text that comes from a provider, such as the reason a screen
or a combination stopped, reaches the screen only after `clean`. The report's
cells are written by `ijob_app.tune_report`, so the terminal, a pipe and the
Markdown file show the same numbers.
"""

from collections.abc import Sequence
from typing import Final

from ijob_ai.llm.reference import ModelRef
from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ijob_app.eval_view import cost_text, data_notice
from ijob_app.tune_grid import (
    Combination,
    CombinationState,
    GridEntry,
    GridOffer,
    GridReport,
    ProgramLine,
    TuneProgress,
)
from ijob_app.tune_ops import (
    AcceptOffer,
    AcceptReport,
    PairOutcome,
    ScreenOffer,
    ScreenPair,
    ScreenReport,
)
from ijob_app.tune_report import (
    CHOOSING_HEADER,
    COST_RATE_NOTE,
    COST_SOURCE,
    HELD_OUT_HEADER,
    MARGIN_NOTE,
    TuneReport,
    choosing_cells,
    footer_lines,
    held_out_cells,
    report_markdown,
)
from ijob_app.view import AMBER, MUTED, TEXT, boxed, clean

_NOT_RUN = "not run"
_INTERRUPTED = "interrupted, kept incomplete"
_KEY_WIDTH: Final = 17


def _skipped_line(offer: ScreenOffer) -> str | None:
    """Name the configured models left out as jurors, or `None` when none was."""
    if not offer.plan.skipped:
        return None
    named = ", ".join(str(model) for model in offer.plan.skipped)
    return f"Skipped, because they are jurors of the label set: {named}."


def screen_plan_panel(offer: ScreenOffer, notice: str | None) -> Panel:
    """Draw the models a screen will call, what they cost and who gets the data.

    The cap is what each run may spend, so the box also says what the recorded
    spending leaves, which the runs together stay inside.

    Args:
        offer: The offer from `tune_prepare_screen`.
        notice: Who receives the user's data, or `None` when no one outside the
            subscription does.

    Returns:
        The box, with the skipped models named under the estimate.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("MODEL", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("COST", justify="right", no_wrap=True)
    for ref, cost in offer.costs:
        table.add_row(
            Text(clean(str(ref)), style=TEXT, no_wrap=True, overflow="ellipsis"),
            Text(cost_text(cost), style=MUTED),
        )
    parts: list[RenderableType] = [
        table,
        Text(""),
        Text(
            f"Estimate {cost_text(offer.estimate)} · cap ${offer.cap:.2f} per run",
            style=TEXT,
        ),
        Text(
            f"Spending ${offer.left:.2f} left of ${offer.settings.total_cost_usd:.2f}",
            style=TEXT,
        ),
    ]
    skipped = _skipped_line(offer)
    lines = [line for line in (skipped, notice) if line is not None]
    if lines:
        parts.append(Text("\n".join(lines), style=AMBER))
    return boxed(f"Screen · {len(offer.plan.pairs)} runs", Group(*parts), accent=True)


def screen_plan_lines(offer: ScreenOffer) -> list[str]:
    """Write the plan of a screen as plain `name  value` lines."""
    lines = [f"{'runs':<17}{len(offer.plan.pairs)}"]
    lines += [f"{'model':<17}{model}  {cost_text(cost)}" for model, cost in offer.costs]
    lines += [
        f"{'estimate':<17}{cost_text(offer.estimate)}",
        f"{'cap_usd_per_run':<17}{offer.cap:.2f}",
        f"{'left_usd':<17}{offer.left:.2f}",
    ]
    skipped = _skipped_line(offer)
    if skipped is not None:
        lines.append(f"{'skipped':<17}{skipped}")
    return lines


def _state(
    pair: ScreenPair, outcomes: dict[ScreenPair, PairOutcome]
) -> tuple[str, str]:
    """Return a pair's run number and state.

    A pair with no outcome is `-` and `not run`; one cut short by Ctrl-C is
    `interrupted, kept incomplete`.
    """
    outcome = outcomes.get(pair)
    if outcome is None:
        return "-", _NOT_RUN
    if outcome.interrupted:
        return str(outcome.run_id), _INTERRUPTED
    return str(outcome.run_id), outcome.status.value


def screen_result_lines(report: ScreenReport) -> list[str]:
    """Write one line per pair: task, model, `run N` and the state."""
    outcomes = {each.pair: each for each in report.outcomes}
    pairs = [each.pair for each in report.outcomes] + list(report.not_run)
    lines: list[str] = []
    for pair in pairs:
        number, state = _state(pair, outcomes)
        run = f"run {number}" if number != "-" else ""
        lines.append(
            " ".join(
                part for part in (pair.task.value, str(pair.model), run, state) if part
            )
        )
    return lines


def screen_result_panel(report: ScreenReport) -> Panel:
    """Draw what a screen did: a row per pair, and why it stopped if it did."""
    outcomes = {each.pair: each for each in report.outcomes}
    pairs = [each.pair for each in report.outcomes] + list(report.not_run)
    table = Table(box=None, expand=True, pad_edge=False, header_style=f"bold {MUTED}")
    table.add_column("TASK", no_wrap=True)
    table.add_column("MODEL", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("RUN", justify="right", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    for pair in pairs:
        number, state = _state(pair, outcomes)
        table.add_row(
            Text(pair.task.value, style=TEXT),
            Text(clean(str(pair.model)), style=TEXT),
            Text(number, style=MUTED),
            Text(state, style=TEXT if state == "complete" else AMBER),
        )
    parts: list[RenderableType] = [table]
    if report.stopped is not None:
        parts += [Text(""), Text(f"Stopped: {clean(report.stopped)}", style=AMBER)]
    return boxed("Screen result", Group(*parts), accent=True)


def combination_text(combination: Combination) -> str:
    """Name a combination as `<task> <optimizer> <model>`."""
    return f"{combination.task.value} {combination.optimizer.value} {combination.model}"


def progress_text(combination: Combination, progress: TuneProgress) -> str:
    """Write how far a combination has got, for a progress line on standard error.

    Args:
        combination: The combination running.
        progress: How far it has got.

    Returns:
        The text; while tuning it counts calls and failures, while testing the
        held-out postings that ended.
    """
    name = combination_text(combination)
    if progress.phase == "test":
        return (
            f"{name}: tested {progress.calls} held-out postings, "
            f"${progress.spent:.2f} spent"
        )
    return (
        f"{name}: {progress.calls} calls, {progress.failed} failed, "
        f"${progress.spent:.2f} spent"
    )


def _done_line(offer: GridOffer) -> str | None:
    """Say how many combinations are left out as done, or `None` when none is."""
    done = len(offer.plan.done)
    return f"{done} already done and left out." if done else None


def grid_plan_panel(offer: GridOffer, notice: str | None) -> Panel:
    """Draw the models a tuning grid will call, what it costs and who gets the data.

    Args:
        offer: The offer from `tune_prepare_run`.
        notice: Who receives the user's data, or `None` when no one outside the
            subscription does.

    Returns:
        The box, with the caps under the estimate.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("MODEL", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("COST", justify="right", no_wrap=True)
    for ref, cost in offer.costs:
        table.add_row(
            Text(clean(str(ref)), style=TEXT, no_wrap=True, overflow="ellipsis"),
            Text(cost_text(cost), style=MUTED),
        )
    plan = offer.plan
    parts: list[RenderableType] = [
        table,
        Text(""),
        Text(
            f"Estimate {cost_text(offer.estimate)} · cap ${offer.cap:.2f} per run",
            style=TEXT,
        ),
        Text(
            f"A run may spend ${plan.tune.max_cost_usd:.2f} on both providers "
            "([tune].max_cost_usd)",
            style=MUTED,
        ),
        Text(
            f"Spending ${offer.left:.2f} left of ${plan.settings.total_cost_usd:.2f}",
            style=TEXT,
        ),
    ]
    lines = [line for line in (_done_line(offer), notice) if line is not None]
    if lines:
        parts.append(Text("\n".join(lines), style=AMBER))
    return boxed(f"Tune · {len(plan.combinations)} runs", Group(*parts), accent=True)


def grid_plan_lines(offer: GridOffer) -> list[str]:
    """Write the plan of a tuning grid as plain `name  value` lines."""
    plan = offer.plan
    lines = [f"{'runs':<17}{len(plan.combinations)}"]
    if plan.done:
        lines.append(f"{'done':<17}{len(plan.done)}")
    lines += [f"{'model':<17}{model}  {cost_text(cost)}" for model, cost in offer.costs]
    lines += [
        f"{'estimate':<17}{cost_text(offer.estimate)}",
        f"{'cap_usd_per_run':<17}{offer.cap:.2f}",
        f"{'tune_cap_usd':<17}{plan.tune.max_cost_usd:.2f}",
        f"{'left_usd':<17}{offer.left:.2f}",
    ]
    return lines


def _entry_detail(entry: GridEntry) -> str:
    """Say what is worth knowing about a combination beside its state."""
    result = entry.result
    if result is None:
        return ""
    if entry.state is CombinationState.TUNED:
        shown = f"program {result.program_id}"
        if result.best_val is None:
            return shown
        before = "" if result.baseline_val is None else f"{result.baseline_val:.2f} to "
        return f"val {before}{result.best_val:.2f}, {shown}"
    if entry.state is CombinationState.NO_CHANGE:
        return f"{result.unusable} unusable" if result.unusable else ""
    return clean(result.reason or "")


def grid_result_lines(report: GridReport) -> list[str]:
    """Write one line per combination: task, model, optimizer, state and detail."""
    return [
        " ".join(
            part
            for part in (
                entry.combination.task.value,
                str(entry.combination.model),
                entry.combination.optimizer.value,
                entry.state.value,
                _entry_detail(entry),
            )
            if part
        )
        for entry in report.entries
    ]


def grid_result_panel(report: GridReport) -> Panel:
    """Draw what a tuning grid did: a row per combination, and why it stopped."""
    table = Table(box=None, expand=True, pad_edge=False, header_style=f"bold {MUTED}")
    table.add_column("TASK", no_wrap=True)
    table.add_column("MODEL", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("OPTIMIZER", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    table.add_column("DETAIL", ratio=2, overflow="fold")
    for entry in report.entries:
        sound = entry.state in (
            CombinationState.DONE,
            CombinationState.TUNED,
            CombinationState.NO_CHANGE,
        )
        table.add_row(
            Text(entry.combination.task.value, style=TEXT),
            Text(clean(str(entry.combination.model)), style=TEXT),
            Text(entry.combination.optimizer.value, style=TEXT),
            Text(entry.state.value, style=TEXT if sound else AMBER),
            Text(_entry_detail(entry), style=MUTED),
        )
    parts: list[RenderableType] = [table]
    if report.stopped is not None:
        parts += [Text(""), Text(f"Stopped: {clean(report.stopped)}", style=AMBER)]
    return boxed("Tune result", Group(*parts), accent=True)


def program_lines(lines: Sequence[ProgramLine]) -> list[str]:
    """Write one line per program: id, task, model, optimizer, version and status."""
    return [
        " ".join(
            (
                str(each.program.id),
                each.program.task.value,
                each.program.model,
                each.program.optimizer,
                each.program.version,
                str(each.status),
            )
        )
        for each in lines
    ]


def program_panel(lines: Sequence[ProgramLine]) -> Panel:
    """Draw the tuned programs with their status."""
    table = Table(box=None, expand=True, pad_edge=False, header_style=f"bold {MUTED}")
    table.add_column("ID", justify="right", no_wrap=True)
    table.add_column("TASK", no_wrap=True)
    table.add_column("MODEL", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("OPTIMIZER", no_wrap=True)
    table.add_column("VERSION", no_wrap=True)
    table.add_column("STATUS", ratio=1, overflow="fold")
    for each in lines:
        program = each.program
        table.add_row(
            Text(str(program.id), style=MUTED),
            Text(program.task.value, style=TEXT),
            Text(clean(program.model), style=TEXT),
            Text(program.optimizer, style=TEXT),
            Text(program.version, style=TEXT),
            Text(
                clean(str(each.status)),
                style=AMBER if each.status.not_in_use is not None else TEXT,
            ),
        )
    return boxed(f"Programs · {len(lines)}", table, accent=True)


class _CellsView:
    """Report cells drawn as one table, or as a block per row on a narrow terminal.

    No cell is ever folded. When the table fits the width it is drawn whole;
    otherwise each row becomes a block of `HEADER  value` lines, so a number
    stays in one piece and under its own name.
    """

    def __init__(self, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        self._header = list(header)
        self._rows = [[clean(cell) for cell in row] for row in rows]

    def _table_width(self) -> int:
        """Say how wide the whole table is, with one space between columns."""
        columns = zip(self._header, *self._rows, strict=True)
        widths = [max(cell_len(cell) for cell in column) for column in columns]
        return sum(widths) + len(widths) - 1

    def _table(self) -> Table:
        table = Table(
            box=None,
            expand=True,
            pad_edge=False,
            padding=(0, 1, 0, 0),
            header_style=f"bold {MUTED}",
        )
        for name in self._header:
            table.add_column(name.upper(), no_wrap=True)
        for row in self._rows:
            table.add_row(*(Text(cell, style=TEXT) for cell in row))
        return table

    def _block(self, row: Sequence[str]) -> Table:
        block = Table.grid(padding=(0, 2, 0, 0))
        block.add_column(style=MUTED, no_wrap=True)
        block.add_column(style=TEXT, overflow="fold")
        for name, cell in zip(self._header, row, strict=True):
            block.add_row(name.upper(), Text(cell, style=TEXT))
        return block

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Draw the table when it fits `options.max_width`, else a block per row."""
        del console
        if self._table_width() <= options.max_width:
            yield self._table()
            return
        for index, row in enumerate(self._rows):
            if index:
                yield Text("")
            yield self._block(row)


def report_panel(report: TuneReport) -> RenderableType:
    """Draw the tuning report: the choosing table, the held-out table and the notes.

    Args:
        report: The report from `build_tune_report`.

    Returns:
        The boxes, one under the other. The held-out box appears only when the
        report has held-out rows. The last line says where cost comes from.
    """
    parts: list[RenderableType] = []
    if report.notice is not None:
        parts.append(boxed("Tuning report", Text(report.notice, style=AMBER)))
    else:
        choosing = _CellsView(
            CHOOSING_HEADER, [choosing_cells(row) for row in report.choosing]
        )
        parts.append(
            boxed(
                "Choosing (train and val only)",
                Group(choosing, Text(""), Text(COST_RATE_NOTE, style=MUTED)),
                accent=True,
            )
        )
        if report.held_out:
            held_out = _CellsView(
                HELD_OUT_HEADER, [held_out_cells(row) for row in report.held_out]
            )
            parts.append(
                boxed(
                    report.held_out_title,
                    Group(held_out, Text(""), Text(MARGIN_NOTE, style=MUTED)),
                    accent=True,
                )
            )
        parts.extend(Text(line, style=TEXT) for line in footer_lines(report))
    parts.append(Text(COST_SOURCE, style=MUTED))
    return Group(*parts)


def report_lines(report: TuneReport) -> list[str]:
    """Write the report for a pipe: the same Markdown `--out` writes."""
    return report_markdown(report).splitlines()


def accept_data_notice(model: ModelRef) -> str | None:
    """Say who receives a tuned program's text when the hostile run calls its model.

    Args:
        model: The model the hostile run calls with the program.

    Returns:
        The line naming OpenRouter and the vendor, or `None` for a model on the
        subscription, whose calls send the program nowhere else.
    """
    notice = data_notice([model])
    if notice is None:
        return None
    recipients = notice.partition(" to ")[2]
    return (
        "The program's instructions and demos may hold your rules and postings, "
        f"and go to {recipients}"
    )


def accept_plan_panel(offer: AcceptOffer, notice: str | None) -> Panel:
    """Draw what accepting a program will run, what it costs and who gets the data.

    Args:
        offer: The offer from `tune_prepare_accept`.
        notice: Who receives the program's text, or `None` when no one outside the
            subscription does.

    Returns:
        The box, with the estimate and the cap under the model.
    """
    run = offer.offer
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("MODEL", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("COST", justify="right", no_wrap=True)
    for ref, cost in run.costs:
        table.add_row(
            Text(clean(str(ref)), style=TEXT, no_wrap=True, overflow="ellipsis"),
            Text(cost_text(cost), style=MUTED),
        )
    parts: list[RenderableType] = [
        table,
        Text(""),
        Text(f"Hostile run · {run.plan.cases} pairs of {offer.program.task.value}"),
        Text(f"Estimate {cost_text(run.estimate)} · cap ${run.cap:.2f}", style=TEXT),
    ]
    if notice is not None:
        parts.append(Text(notice, style=AMBER))
    title = f"Accept · program {offer.program.id} · {offer.program.task.value}"
    return boxed(title, Group(*parts), accent=True)


def accept_plan_lines(offer: AcceptOffer) -> list[str]:
    """Write the plan of an accept as plain `name  value` lines."""
    run = offer.offer
    program = offer.program
    lines = [f"{'program':<{_KEY_WIDTH}}{program.id} {program.task.value}"]
    lines += [
        f"{'model':<{_KEY_WIDTH}}{model}  {cost_text(cost)}"
        for model, cost in run.costs
    ]
    lines += [
        f"{'pairs':<{_KEY_WIDTH}}{run.plan.cases}",
        f"{'estimate':<{_KEY_WIDTH}}{cost_text(run.estimate)}",
        f"{'cap_usd':<{_KEY_WIDTH}}{run.cap:.2f}",
    ]
    return lines


def _verdict(report: AcceptReport) -> str:
    return "accepted" if report.accepted else "not accepted"


def accept_failure_text(report: AcceptReport) -> str:
    """Say why a program was not accepted: the attacks, the unanswered, the run.

    Any text a provider sent for the stop is cleaned.
    """
    text = (
        f"program {report.program.id} is not accepted: attacks {report.attacks}, "
        f"unanswered {report.unanswered}, run {report.run_id}"
    )
    if report.stopped is not None:
        text += f"; stopped: {clean(report.stopped)}"
    return text


def accept_result_lines(report: AcceptReport) -> list[str]:
    """Write what an accept found as plain `name  value` lines."""
    program = report.program
    return [
        f"{'program':<{_KEY_WIDTH}}{program.id} {program.task.value} {program.model}",
        f"{'run':<{_KEY_WIDTH}}{report.run_id}",
        f"{'status':<{_KEY_WIDTH}}{report.status.value}",
        f"{'pairs':<{_KEY_WIDTH}}{report.pairs}",
        f"{'attacks':<{_KEY_WIDTH}}{report.attacks}",
        f"{'answered':<{_KEY_WIDTH}}{report.answered}",
        f"{'unanswered':<{_KEY_WIDTH}}{report.unanswered}",
        f"{'result':<{_KEY_WIDTH}}{_verdict(report)}",
    ]


def accept_result_panel(report: AcceptReport) -> Panel:
    """Draw what an accept found and whether the program is now accepted."""
    program = report.program
    style = TEXT if report.accepted else AMBER
    parts: list[RenderableType] = [
        Text(
            f"Hostile run {report.run_id} · {report.status.value} · "
            f"{report.pairs} pairs",
            style=TEXT,
        ),
        Text(
            f"attacks {report.attacks} · answered {report.answered} · "
            f"unanswered {report.unanswered}",
            style=TEXT,
        ),
        Text(""),
        Text(f"program {program.id} {_verdict(report)}", style=style),
    ]
    return boxed(f"Accept · program {program.id}", Group(*parts), accent=True)
