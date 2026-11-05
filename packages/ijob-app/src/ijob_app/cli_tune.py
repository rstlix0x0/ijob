"""The command-line side of tuning.

`tune_app` builds the `tune` group. `tune screen [--yes]` measures every candidate
model, the baseline and the configured judge and scorer models on all current
labels, each pair of model and task as an ordinary `eval run` with its own
spending. It first shows the plan, with the estimated cost and who receives the
data, and asks when the label set has no split yet, because a frozen split can never
be undone, or when a model it calls is on OpenRouter; `--yes` answers the question,
and without a terminal and without `--yes` the command exits `1` before anything is
frozen or called. Only on a yes does it freeze the tuning split, once per label set,
and then run the pairs that have no full run yet. A rerun runs only what is still
missing and leaves the split as it was.

The screen ends with one line per pair. Its exit status is `0` when every pair is
complete, `1` when a pair is incomplete or did not run, when a provider failed,
a cost could not be known, the recorded spending is used up, the question was
declined or something the screen needs is missing, and `2` for a config setting
that cannot be used. Ctrl-C ends the screen; the runs already written stand, the
pair running at that moment is shown as interrupted with its run kept incomplete,
and the next screen runs the rest.

Of the LLM layer this module imports its error classes and the model reference
only, so loading it pulls in neither DSPy nor optuna.
"""

import asyncio
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Final

import typer
from ijob_ai.llm.reference import Provider

from ijob_app.cli_eval import AFFIRMATIVE, ask_question, eval_failures
from ijob_app.cli_llm import CONFIG_HELP, DB_HELP, SessionOpener
from ijob_app.cli_output import say_error, say_note
from ijob_app.cli_progress import ProgressLine
from ijob_app.eval_run import RunProgress
from ijob_app.eval_view import data_notice
from ijob_app.service import Service
from ijob_app.tune_ops import (
    PairOutcome,
    ScreenOffer,
    ScreenPair,
    ScreenReport,
    screen_report,
)
from ijob_app.tune_view import (
    screen_plan_lines,
    screen_plan_panel,
    screen_result_lines,
    screen_result_panel,
)
from ijob_app.view import is_terminal, make_console

_EXIT_REFUSED: Final = 1


def _show_plan(offer: ScreenOffer, notice: str | None) -> None:
    """Show the pairs, the estimate, the cap per run, the spending left and the data."""
    if is_terminal():
        make_console().print(screen_plan_panel(offer, notice))
        return
    for line in screen_plan_lines(offer):
        typer.echo(line, err=True)
    if notice is not None:
        say_note(f"note: {notice}")


def _confirm(
    offer: ScreenOffer, *, yes: bool, is_interactive: Callable[[], bool]
) -> None:
    """Ask before a screen that freezes the split or calls a model on OpenRouter.

    A screen on a label set with no split yet always asks, because the split can
    never be undone. Once the split exists, only OpenRouter calls ask.

    Raises:
        typer.Exit: With `1`, when the user must be asked and cannot be, or
            declines.
    """
    paid = any(model.provider is Provider.OPENROUTER for model in offer.callers)
    if yes or (offer.frozen and not paid):
        return
    if not is_interactive():
        say_error("confirmation needed; pass --yes")
        raise typer.Exit(code=_EXIT_REFUSED)
    if not offer.frozen:
        say_note("note: the screen freezes the tuning split; it is frozen for good")
    if ask_question("Start the screen?").strip().lower() not in AFFIRMATIVE:
        say_note("note: the screen was not started")
        raise typer.Exit(code=_EXIT_REFUSED)


def _run_screen(service: Service, offer: ScreenOffer) -> ScreenReport:
    """Run the screen with a progress line on a terminal's standard error.

    A Ctrl-C ends the screen early; the report then holds the pairs that ended, the
    one it cut short among them, and names the interruption.
    """
    line = ProgressLine(sys.stderr)
    outcomes: list[PairOutcome] = []

    def progress(pair: ScreenPair, update: RunProgress) -> None:
        line.show_live(
            f"{pair.task.value} {pair.model}: {update.done} of {update.total} cases, "
            f"${update.spent:.2f}"
        )

    report: ScreenReport | None = None
    try:
        with suppress(KeyboardInterrupt):
            report = asyncio.run(
                service.tune_screen(
                    offer, on_progress=progress, on_outcome=outcomes.append
                )
            )
    finally:
        line.clear()
    return report or screen_report(offer.plan, outcomes, "interrupted")


def _show_result(report: ScreenReport) -> None:
    """Show a pair per line: drawn on a terminal, plain lines in a pipe."""
    if is_terminal():
        make_console().print(screen_result_panel(report))
        return
    for line in screen_result_lines(report):
        typer.echo(line)
    if report.stopped is not None:
        say_note(f"note: stopped: {report.stopped}", literal=True)


def tune_app(
    open_session: SessionOpener, is_interactive: Callable[[], bool]
) -> typer.Typer:
    """Build the `tune` command group.

    Args:
        open_session: Opens the service for a command.
        is_interactive: Whether the user can be asked a question; `screen` asks
            only then.

    Returns:
        The group, with `screen`.
    """
    group = typer.Typer(
        add_completion=False,
        help="Measure models and tune the judge and scorer prompts.",
    )

    @group.command("screen")
    def screen(
        *,
        yes: Annotated[
            bool, typer.Option("--yes", help="Start without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Freeze the split and measure candidates, baseline and configured models."""
        with open_session(db, config, models=True) as session:
            service = session.service
            with eval_failures():
                offer = asyncio.run(service.tune_prepare_screen())
            _show_plan(offer, data_notice(offer.callers))
            if not offer.plan.pairs:
                say_note("note: every model already has a full run")
            _confirm(offer, yes=yes, is_interactive=is_interactive)
            with eval_failures():
                report = _run_screen(service, offer)
            _show_result(report)
            if report.exit_status:
                raise typer.Exit(code=report.exit_status)

    return group
