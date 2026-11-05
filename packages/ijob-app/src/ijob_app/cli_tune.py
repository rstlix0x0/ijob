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

`tune run [--model M] [--optimizer O] [--task T] [--again] [--yes]` tunes every
combination of `[tune].models`, the three optimizers and the judge and scorer that is
not done yet, then tests each program it makes on the held-out postings. It shows the
plan with the estimated cost, who receives the data and the caps, and always asks
first; `--yes` answers the question, and without a terminal and without `--yes` the
command exits `1` before anything is written. Progress goes to standard error and the
table of every combination and its state to standard output. A combination that
reached a cap, failed too often, whose optimizer raised or whose held-out test lost a
call is shown as incomplete and the next one runs; a spent ledger, an unavailable
provider, an unknown cost or Ctrl-C ends the run, and Ctrl-C shows the combination it
cut short as incomplete, with its spend where known. Postings left out of tuning
because their text changed since the draw are counted in a note. The exit status is
`0` when every combination is done, tuned or unchanged, `1` when one is incomplete or
did not run or the run was refused, and `2` for a config setting or option that cannot
be used. A rerun plans an incomplete combination again and leaves the done ones.

`tune list` shows each tuned program with its status and writes nothing.

`tune report [--out PATH]` shows the untuned models on the train and val postings and,
once a tuned program has a complete held-out run, every model and program on the
held-out postings. It reads the store, calls no model and never freezes a split;
`--out` also writes the same tables as Markdown over that file. The exit status is `0`,
`1` when the file cannot be written and `2` for a config setting that cannot be used.

`tune accept ID [--yes]` runs the `hostile` suite on a tested program, for its task
only, and accepts the program only with no attack and every pair answered, so search
then uses it while its model, rules or profile and base version are current. A program
that cannot be accepted is refused before anything runs. For an OpenRouter model it
shows that the program's instructions and demos may hold the user's rules and
postings, and always asks first; `--yes` answers the question, and without a terminal
and without `--yes` the command exits `1` before anything runs. The exit status is
`0` when the program is accepted, `1` when it is not or was refused, and `2` for an
unknown program or a config setting that cannot be used.

`tune clear TASK` goes back to the base prompt for `judge` or `scorer`; the program
row stays.

Of the LLM layer this module imports its error classes, the model reference and the
optimizer names only, so loading it pulls in neither DSPy nor optuna.
"""

import asyncio
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Final

import typer
from ijob_ai.llm.reference import Provider, parse_model_ref
from ijob_ai.tuning.names import Optimizer
from ijob_core.eval_records import EvalTask

from ijob_app.cli_eval import AFFIRMATIVE, ask_question, eval_failures
from ijob_app.cli_llm import CONFIG_HELP, DB_HELP, SessionOpener
from ijob_app.cli_output import say_error, say_note
from ijob_app.cli_progress import ProgressLine
from ijob_app.eval_run import RunProgress
from ijob_app.eval_view import data_notice
from ijob_app.service import Service
from ijob_app.tune_grid import (
    INTERRUPTED,
    Combination,
    CombinationResult,
    GridOffer,
    GridReport,
    Phase,
    TuneProgress,
    grid_report,
    interrupted_result,
)
from ijob_app.tune_ops import (
    AcceptOffer,
    AcceptReport,
    PairOutcome,
    ScreenOffer,
    ScreenPair,
    ScreenReport,
    screen_report,
)
from ijob_app.tune_report import TuneReport, write_report_markdown
from ijob_app.tune_view import (
    accept_data_notice,
    accept_failure_text,
    accept_plan_lines,
    accept_plan_panel,
    accept_result_lines,
    accept_result_panel,
    combination_text,
    grid_plan_lines,
    grid_plan_panel,
    grid_result_lines,
    grid_result_panel,
    program_lines,
    program_panel,
    progress_text,
    report_lines,
    report_panel,
    screen_plan_lines,
    screen_plan_panel,
    screen_result_lines,
    screen_result_panel,
)
from ijob_app.view import clean, is_terminal, make_console

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


def _show_grid_plan(offer: GridOffer, notice: str | None) -> None:
    """Show the runs, the estimate, the caps, the spending left and the data."""
    if is_terminal():
        make_console().print(grid_plan_panel(offer, notice))
        return
    for line in grid_plan_lines(offer):
        typer.echo(line, err=True)
    if notice is not None:
        say_note(f"note: {notice}")


def _confirm_run(*, yes: bool, is_interactive: Callable[[], bool]) -> None:
    """Ask before a run that spends; no is the default.

    Raises:
        typer.Exit: With `1`, when the user must be asked and cannot be, or
            declines.
    """
    if yes:
        return
    if not is_interactive():
        say_error("confirmation needed; pass --yes")
        raise typer.Exit(code=_EXIT_REFUSED)
    if ask_question("Start the run?").strip().lower() not in AFFIRMATIVE:
        say_note("note: the run was not started")
        raise typer.Exit(code=_EXIT_REFUSED)


def _run_grid(service: Service, offer: GridOffer) -> GridReport:
    """Run the grid with a progress line on standard error.

    A Ctrl-C ends the grid early; the report then holds the combinations that
    ended and the one it cut short, shown incomplete with what is known of its
    spend, and names the interruption.
    """
    line = ProgressLine(sys.stderr)
    results: list[CombinationResult] = []
    begun: list[Combination] = []
    latest: dict[Phase, TuneProgress] = {}

    def started(combination: Combination) -> None:
        begun.append(combination)
        latest.clear()
        line.show(f"tuning {combination_text(combination)}")

    def progress(update: TuneProgress) -> None:
        latest[update.phase] = update
        line.show_live(progress_text(begun[-1], update))

    report: GridReport | None = None
    try:
        with suppress(KeyboardInterrupt):
            report = asyncio.run(
                service.tune_run(
                    offer,
                    on_start=started,
                    on_progress=progress,
                    on_result=results.append,
                )
            )
    finally:
        line.clear()
    if report is not None:
        return report
    running = begun[len(results) :]
    return grid_report(
        offer.plan,
        results,
        INTERRUPTED,
        in_flight=interrupted_result(running[0], latest.values()) if running else None,
    )


def _show_grid_result(report: GridReport) -> None:
    """Show a combination per line: drawn on a terminal, plain lines in a pipe."""
    if is_terminal():
        make_console().print(grid_result_panel(report))
    else:
        for line in grid_result_lines(report):
            typer.echo(line)
    if report.changed:
        say_note(f"note: {_left_out(report.changed)}")
    if report.stopped is not None and not is_terminal():
        say_note(f"note: stopped: {clean(report.stopped)}", literal=True)


def _left_out(count: int) -> str:
    """Say how many postings tuning left out because their text changed."""
    which = "posting was" if count == 1 else "postings were"
    return f"{count} {which} left out of tuning: the text changed since the draw"


def _show_report(report: TuneReport) -> None:
    """Show the report: boxes on a terminal, the Markdown lines in a pipe."""
    if is_terminal():
        make_console().print(report_panel(report))
        return
    for line in report_lines(report):
        typer.echo(line)


def _write_report(path: Path, report: TuneReport) -> None:
    """Write the report's Markdown over a file and say where it went.

    Raises:
        typer.Exit: With `1`, when the file cannot be written.
    """
    try:
        write_report_markdown(path, report)
    except OSError as error:
        say_error(f"cannot write {path}: {error.strerror or error}", literal=True)
        raise typer.Exit(code=_EXIT_REFUSED) from error
    say_note(f"note: wrote {clean(str(path))}", literal=True)


def _show_accept_plan(offer: AcceptOffer, notice: str | None) -> None:
    """Show the program, the pairs, the estimate, the cap and who gets its text."""
    if is_terminal():
        make_console().print(accept_plan_panel(offer, notice))
        return
    for line in accept_plan_lines(offer):
        typer.echo(line, err=True)
    if notice is not None:
        say_note(f"note: {notice}")


def _run_accept(service: Service, offer: AcceptOffer) -> AcceptReport | None:
    """Run the hostile suite with a progress line on standard error.

    Returns:
        What the run found, or `None` when a Ctrl-C ended it; the run row it left
        stands and the program is not accepted.
    """
    line = ProgressLine(sys.stderr)

    def progress(update: RunProgress) -> None:
        line.show_live(
            f"program {offer.program.id}: {update.done} of {update.total} pairs, "
            f"${update.spent:.2f}"
        )

    report: AcceptReport | None = None
    try:
        with suppress(KeyboardInterrupt):
            report = asyncio.run(service.tune_accept(offer, on_progress=progress))
    finally:
        line.clear()
    return report


def _show_accept_result(report: AcceptReport) -> None:
    """Show the counts and the verdict: drawn on a terminal, plain lines in a pipe."""
    if is_terminal():
        make_console().print(accept_result_panel(report))
        return
    for line in accept_result_lines(report):
        typer.echo(line)


def tune_app(  # noqa: PLR0915  # one registration per command
    open_session: SessionOpener, is_interactive: Callable[[], bool]
) -> typer.Typer:
    """Build the `tune` command group.

    Args:
        open_session: Opens the service for a command.
        is_interactive: Whether the user can be asked a question; `screen` and
            `run` ask only then.

    Returns:
        The group, with `screen`, `run`, `list`, `report`, `accept` and `clear`.
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

    @group.command("run")
    def run(  # noqa: PLR0913  # one option of the command per argument
        *,
        model: Annotated[
            str | None,
            typer.Option("--model", help="Tune only this model, listed or not."),
        ] = None,
        optimizer: Annotated[
            Optimizer | None,
            typer.Option("--optimizer", help="Tune only with this one."),
        ] = None,
        task: Annotated[
            EvalTask | None, typer.Option("--task", help="Tune only this task.")
        ] = None,
        again: Annotated[
            bool, typer.Option("--again", help="Tune the combinations that are done.")
        ] = False,
        yes: Annotated[
            bool, typer.Option("--yes", help="Start without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Tune each model, optimizer and task, then test the programs on held-out."""
        with open_session(db, config, models=True) as session:
            service = session.service
            with eval_failures():
                ref = None if model is None else parse_model_ref(model, "--model")
                offer = asyncio.run(
                    service.tune_prepare_run(
                        model=ref, optimizer=optimizer, task=task, again=again
                    )
                )
            if not offer.plan.combinations:
                say_note("note: every combination is already done")
                _show_grid_result(grid_report(offer.plan, (), None))
                return
            _show_grid_plan(offer, data_notice(offer.callers))
            _confirm_run(yes=yes, is_interactive=is_interactive)
            with eval_failures():
                report = _run_grid(service, offer)
            _show_grid_result(report)
            if report.exit_status:
                raise typer.Exit(code=report.exit_status)

    @group.command("list")
    def list_programs(
        *,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """List the tuned programs and whether each is tested, accepted or in use."""
        with open_session(db, config, models=True) as session:
            with eval_failures():
                programs = session.service.tune_list()
            if not programs:
                say_note("note: no tuned programs yet; run `ijob tune run`")
                return
            if is_terminal():
                make_console().print(program_panel(programs))
                return
            for line in program_lines(programs):
                typer.echo(line)

    @group.command("report")
    def report(
        *,
        out: Annotated[
            Path | None,
            typer.Option(
                "--out",
                help="Also write the tables to this Markdown file, replacing it.",
            ),
        ] = None,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Compare the untuned models and the tuned programs on held-out postings."""
        with open_session(db, config, models=True) as session:
            with eval_failures():
                built = session.service.tune_report()
            _show_report(built)
            if out is not None:
                _write_report(out, built)

    @group.command("accept")
    def accept(
        program_id: Annotated[
            int,
            typer.Argument(
                metavar="ID", help="The program, as `tune list` numbers it."
            ),
        ],
        *,
        yes: Annotated[
            bool, typer.Option("--yes", help="Start without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Test a program against hostile postings and use it in search if it passes."""
        with open_session(db, config, models=True) as session:
            service = session.service
            with eval_failures():
                offer = asyncio.run(service.tune_prepare_accept(program_id))
            model = parse_model_ref(offer.program.model, "model")
            _show_accept_plan(offer, accept_data_notice(model))
            if model.provider is Provider.OPENROUTER:
                _confirm_run(yes=yes, is_interactive=is_interactive)
            with eval_failures():
                report = _run_accept(service, offer)
            if report is None:
                say_error("the run was interrupted; no program was accepted")
                raise typer.Exit(code=_EXIT_REFUSED)
            _show_accept_result(report)
            if report.exit_status:
                say_error(
                    accept_failure_text(report),
                    f"ijob eval report {report.run_id} --failures",
                    literal=True,
                )
                raise typer.Exit(code=report.exit_status)

    @group.command("clear")
    def clear(
        task: Annotated[
            EvalTask,
            typer.Argument(metavar="TASK", help="The task: judge or scorer."),
        ],
        *,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """Go back to the base prompt for a task."""
        with open_session(db, None, models=False) as session:
            cleared = session.service.tune_clear(task)
        if cleared:
            say_note(f"note: {task.value} goes back to the base prompt")
        else:
            say_note(f"note: no program is accepted for {task.value}")

    return group
