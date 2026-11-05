"""How a tuning screen looks: the plan it will run and what it did.

Each function builds a Rich renderable, or plain lines for a pipe, from plain data
and prints nothing. Text that comes from a provider, such as the reason a screen
stopped, reaches the screen only as `Text`, after `clean`.
"""

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ijob_app.eval_view import cost_text
from ijob_app.tune_ops import PairOutcome, ScreenOffer, ScreenPair, ScreenReport
from ijob_app.view import AMBER, MUTED, TEXT, boxed, clean

_NOT_RUN = "not run"
_INTERRUPTED = "interrupted, kept incomplete"


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
