"""The command-line side of the evals.

`eval_app` builds the `eval` group, with `label`, `review`, `run` and `report`.
`eval label [--limit N] [--yes]` has a jury of models vote on a sample of postings,
with their rules and your CV profile, and labels the cases the jurors agree on.
`eval review` has you settle the rest.

On a terminal it draws the plan (the jurors, the estimated cost, the cap and, for
OpenRouter jurors, who receives your data), asks before it starts, shows the
votes arriving and ends with a summary. The question is asked when a juror with
votes to cast is on OpenRouter, or when the rules, CV profile or jury changed since
the last run, which makes a new label set; `--yes` answers it, and without a
terminal and without `--yes` the command exits `1` before any call. A jury of
claude-sdk models alone asks nothing.

In a pipe the plan goes to stderr as `name  value` lines, as does the question
when stdin is a terminal, and stdout gets the result as `name  value` lines:
`votes`, `requested`, `failed`, `skipped`, the
cases that `*_agree` and are left for `*_review` per task, and `cost_usd`. The
exit status is `0` when the run ended or reached the cost cap, `1` when a juror's
provider failed, a cost could not be known, the question was declined, something
the run needs is missing or the recorded spending is used up, and `2` for a
`--limit` or a config setting that cannot be used, such as an absent
`[eval].jury`. Ctrl-C on a terminal ends the run with `Stopped` and exits `0`; the
votes already cast stand and the next run asks for the rest.

`eval run SUITE [--model PROVIDER/MODEL] [--limit N] [--yes]` runs `judge`,
`scorer`, `reasons`, `hostile`, `source-agent` or `explore-agent` on the model,
which comes from configuration unless given, and shows what it measured. The plan,
with the estimated cost and the cap, is shown first. The question is asked when a
model the run calls is on OpenRouter, the grader of `reasons` and `explore-agent`
included; the line naming who receives the data is left out for `hostile` and the
agent suites, which send none. An agent suite's estimate reads "up to the cap".
`--yes` answers the question, and without a terminal and without `--yes` the
command exits `1` before any call. A run on claude-sdk models alone asks nothing.
On a terminal it draws the plan, a live line and the result; in a pipe the plan
goes to stderr and stdout gets the result as `name  value` lines, the metrics then
`cost_usd`. The exit status is `0` when the run ended or reached the cost cap, and
`1` when a provider failed, a cost could not be known, the question was declined,
something the run needs is missing, the recorded spending is used up, or a
`hostile`, `source-agent` or `explore-agent` run was incomplete or had a failure
its target is 0 for (an attack, a rule violation, no answered pair). It is `2` for
an unknown suite, a `--limit` out of range or given to `hostile`, a `--model` that
cannot be used (the jury of the label set, or the grader of `reasons` and
`explore-agent`) or one missing from the OpenRouter price list. Ctrl-C on a
terminal ends the run with `Stopped` and keeps the cases stored in a run marked
incomplete; it exits `0`, except for `hostile` and the agent suites, which exit `1`.

`eval review` needs a terminal and exits `1` in a pipe. It shows one case at a
time, first those whose votes give no label, then a spot check of agreed labels,
and asks for a key: `k` or `r` on a judge case, `0` to `100` or Enter (the lower
median of the scores) on a scorer case, `o` to read the whole posting, `s` to
leave the case for next time and `q` or the end of the input to stop. Anything
else is asked again. Each answer is stored when it is given. It exits `0` after a
review, a stop or when nothing is left, and `1` when the jury, the rules or the CV
profile that identify the label set are missing.

`eval report [RUN] [--failures]` reads the stored runs and calls no model. With
no argument it shows a table per suite of the runs that compare (same suite, label
set and task version, complete, not limited), the jury metrics, the total
OpenRouter spend and how many runs were left out; a store with no run says so.
Without a jury, rules and CV profile to identify a label set it lists only the
suites that need no labels and says labels need setup. `RUN` shows that run
whatever its status, and `--failures` its failed cases. All exit `0`, except an
unknown `RUN` or `--failures` without one, which exit `2`. In a pipe it prints
`name  value` lines: a `run` line per run with its `key=value` metrics, the
`jury_*` numbers, `spent_usd`, `cap_usd` and `not_compared`.

Of the LLM layer this module imports its error classes and the model reference
only, so loading it pulls in neither DSPy nor the Claude SDK.
"""

import asyncio
import re
import sys
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, assert_never

import typer
from ijob_ai.errors import LlmError
from ijob_ai.llm.reference import Provider, parse_model_ref
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalTask, RunStatus
from rich.console import Console
from rich.live import Live
from rich.text import Text

from ijob_app.cli_llm import CONFIG_HELP, DB_HELP, SessionOpener, report_notes
from ijob_app.cli_output import ask, say_error, say_note
from ijob_app.cli_progress import ProgressLine
from ijob_app.eval_ops import (
    EvalNeedsError,
    LabelChange,
    LabelReport,
    ReviewItem,
    ReviewPlan,
)
from ijob_app.eval_report import EvalReport, RunDetail, run_cost
from ijob_app.eval_run import RunOffer, RunProgress
from ijob_app.eval_suites import AGENT_SUITES, Suite, SuiteReport
from ijob_app.eval_view import (
    cost_text,
    data_notice,
    explore_suite_panel,
    failures_panel,
    hostile_panel,
    jury_panel,
    jury_progress,
    jury_summary,
    metric_value,
    plan_panel,
    report_footer,
    report_panel,
    review_panel,
    review_prompt,
    run_panel,
    run_progress,
    run_values,
    source_suite_panel,
    suite_panel,
)
from ijob_app.service import LabelOffer, LabelProgress, Service
from ijob_app.view import (
    MUTED,
    clean,
    failed_line,
    is_terminal,
    make_console,
    next_line,
    notice_line,
    ok_line,
    posting_detail,
)

_EXIT_REFUSED: Final = 1
_EXIT_BAD_ARGUMENT: Final = 2
_AFFIRMATIVE: Final = frozenset({"y", "yes"})
_KEY_WIDTH: Final = 17
_REFRESH_PER_SECOND: Final = 8
_SPINS_PER_SECOND: Final = 10
_SCORE_MAX: Final = 100
_SCORE_DIGITS: Final = re.compile(r"[0-9]{1,3}")

type _SuiteRunner = Callable[..., Coroutine[object, object, SuiteReport]]
"""A service call that runs an offer's suite, given the offer and `on_progress`."""


class _Move(StrEnum):
    """What a typed reply asks the review to do."""

    ANSWER = "answer"
    OPEN = "open"
    SKIP = "skip"
    QUIT = "quit"
    AGAIN = "again"


@dataclass(frozen=True, slots=True)
class _Reply:
    """A typed reply to a review case.

    Attributes:
        move: What to do next.
        keep: The judge answer, when `move` is `ANSWER` for a judge case.
        score: The scorer answer, when `move` is `ANSWER` for a scorer case.
    """

    move: _Move
    keep: bool | None = None
    score: int | None = None


@contextmanager
def _eval_failures() -> Iterator[None]:
    """Turn what an eval operation raises into a message and an exit status.

    A value the run refuses exits `2`; everything the LLM layer raises exits `1`,
    and what is missing is shown with the command that supplies it.

    Raises:
        typer.Exit: After printing the error to stderr.
    """
    try:
        yield
    except InvalidValueError as error:
        say_error(f"{error}")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
    except EvalNeedsError as error:
        if error.fix.startswith("run `"):
            say_error(error.what, error.fix.removeprefix("run `").removesuffix("`"))
        else:
            say_error(f"{error}")
        raise typer.Exit(code=_EXIT_REFUSED) from error
    except LlmError as error:
        say_error(f"{error}")
        raise typer.Exit(code=_EXIT_REFUSED) from error


def _line(name: str, value: object) -> str:
    """Write a `name  value` line with the names in one column."""
    return f"{name:<{_KEY_WIDTH}}{value}"


def _plan_lines(offer: LabelOffer) -> list[str]:
    """Write the plan of a run as plain lines."""
    lines = [_line("jury", f"{offer.plan.cases} postings, judge + scorer")]
    lines.extend(
        _line("juror", f"{ref}  {cost_text(cost)}") for ref, cost in offer.costs
    )
    lines.append(_line("estimate", cost_text(offer.estimate)))
    lines.append(_line("cap", f"${offer.cap:.2f}"))
    return lines


def _result_lines(report: LabelReport) -> list[str]:
    """Write the result of a run as plain lines."""
    tally = report.tally
    return [
        _line("votes", report.stored),
        _line("requested", report.requested),
        _line("failed", report.failed),
        _line("skipped", report.skipped),
        _line("judge_agree", tally.judge.agreed),
        _line("judge_review", tally.judge.review),
        _line("scorer_agree", tally.scorer.agreed),
        _line("scorer_review", tally.scorer.review),
        _line("cost_usd", f"{report.operation.spent:.4f}"),
    ]


def _changes_note(changes: tuple[LabelChange, ...]) -> str:
    """Say what changed since the last run, which makes a new label set."""
    named = " and ".join(str(change) for change in changes)
    return f"{named} changed since the last jury run; this starts a new label set"


def _show_plan(offer: LabelOffer, notice: str | None) -> None:
    """Show the jurors, the estimate, the cap and who receives the data."""
    if is_terminal():
        make_console().print(
            jury_panel(
                postings=offer.plan.cases,
                jurors=offer.costs,
                estimate=offer.estimate,
                cap=offer.cap,
                notice=notice,
            )
        )
        return
    for line in _plan_lines(offer):
        typer.echo(line, err=True)
    if notice is not None:
        say_note(f"note: {notice}")


def _ask(question: str) -> str:
    """Ask the question where the user sees it, and keep it out of a piped result.

    A terminal gets the drawn prompt on standard output; with standard output
    piped the question goes to standard error, so `> file` holds only the result.
    """
    if is_terminal():
        return ask(question)
    return str(
        typer.prompt(
            f"{question} [y/N]",
            default="",
            show_default=False,
            prompt_suffix=" ",
            err=True,
        )
    )


def _say_stopped(text: str) -> None:
    """Report a juror's stop on standard error, its provider text taken literally.

    `say_error` reads a trailing `; run: <command>` as a fix to draw, and a
    provider's own error text must never be able to offer one.
    """
    console = make_console(stderr=True)
    if console.is_terminal:
        console.print(failed_line(text))
    else:
        typer.echo(f"error: {text}", err=True)


def _confirm(
    offer: LabelOffer,
    notice: str | None,
    *,
    yes: bool,
    is_interactive: Callable[[], bool],
) -> None:
    """Ask before a run that sends data out or starts a new label set.

    Raises:
        typer.Exit: With `1`, when the user must be asked and cannot be, or
            declines.
    """
    changes = offer.plan.changed
    if changes:
        say_note(f"note: {_changes_note(changes)}")
    if yes or not offer.plan.items or (notice is None and not changes):
        return
    if not is_interactive():
        say_error("confirmation needed; pass --yes")
        raise typer.Exit(code=_EXIT_REFUSED)
    answer = _ask("Start a new label set?" if changes else "Start the jury?")
    if answer.strip().lower() not in _AFFIRMATIVE:
        say_note("note: the jury was not started")
        raise typer.Exit(code=_EXIT_REFUSED)


def _run_on_terminal(service: Service, offer: LabelOffer) -> LabelReport | None:
    """Run the jury with a live line of progress.

    Returns:
        What the run did, or `None` when the user stopped it.
    """
    console = make_console()
    state = LabelProgress(stored=0, total=len(offer.plan.items), spent=0.0)

    def draw() -> Text:
        return jury_progress(
            stored=state.stored,
            total=state.total,
            spent=state.spent,
            cap=offer.cap,
            frame=int(time.monotonic() * _SPINS_PER_SECOND),
        )

    def progress(update: LabelProgress) -> None:
        nonlocal state
        state = update

    report: LabelReport | None = None
    with (
        Live(
            get_renderable=draw,
            console=console,
            refresh_per_second=_REFRESH_PER_SECOND,
            transient=True,
        ),
        suppress(KeyboardInterrupt),
    ):
        report = asyncio.run(service.eval_label(offer, on_progress=progress))
    if report is None:
        console.print(
            notice_line("Stopped", f"{state.stored} votes saved; run ijob eval label")
        )
    return report


def _run_in_a_pipe(service: Service, offer: LabelOffer) -> LabelReport:
    """Run the jury, showing progress on a terminal's standard error only."""
    line = ProgressLine(sys.stderr)

    def progress(update: LabelProgress) -> None:
        line.show_live(
            f"jury: {update.stored} of {update.total} votes, "
            f"${update.spent:.2f} of ${offer.cap:.2f}"
        )

    try:
        return asyncio.run(service.eval_label(offer, on_progress=progress))
    finally:
        line.clear()


def _show_result(report: LabelReport) -> None:
    """Show how the run ended: drawn on a terminal, plain lines in a pipe."""
    if is_terminal():
        make_console().print(jury_summary(report))
    else:
        for line in _result_lines(report):
            typer.echo(line)
        for juror in report.stopped_jurors:
            _say_stopped(f"{juror.juror} stopped: {juror.reason}")
        if report.cost_unknown is not None:
            say_error(f"{report.cost_unknown}")
        if report.operation.stopped_by == "cap":
            say_note(
                "note: stopped at the cost cap (eval.max_cost_usd), votes stand; "
                "run: ijob eval label"
            )
        if report.failed:
            say_note(
                f"note: {report.failed} votes unusable and asked again next run; "
                "run: ijob eval label"
            )
        if report.skipped:
            say_note(
                f"note: {report.skipped} postings changed since the draw "
                "and got no vote"
            )
    for note in report_notes(report.operation):
        say_note(note)


def _reply(item: ReviewItem, text: str) -> _Reply:
    """Read what the user typed at a review case.

    `q`, `s` and `o` work on every case; `k` and `r` only on a judge case; a
    number from 0 to 100, or nothing (the median), only on a scorer case.
    Anything else asks again.
    """
    word = text.strip().lower()
    moves = {"q": _Move.QUIT, "s": _Move.SKIP, "o": _Move.OPEN}
    if word in moves:
        return _Reply(moves[word])
    if item.task is EvalTask.JUDGE:
        if word in {"k", "r"}:
            return _Reply(_Move.ANSWER, keep=word == "k")
        return _Reply(_Move.AGAIN)
    if not word:
        return _Reply(_Move.ANSWER, score=item.median)
    if _SCORE_DIGITS.fullmatch(word) and int(word) <= _SCORE_MAX:
        return _Reply(_Move.ANSWER, score=int(word))
    return _Reply(_Move.AGAIN)


def _typed(console: Console, item: ReviewItem) -> str:
    """Ask for the user's reply to a case; the end of the input is `q`."""
    try:
        return console.input(review_prompt(item))
    except EOFError:
        return "q"


def _show_skipped(plan: ReviewPlan) -> None:
    """Say how many cases were left out because their posting changed or went."""
    if plan.skipped:
        noun = "posting" if plan.skipped == 1 else "postings"
        make_console().print(
            notice_line(f"{plan.skipped} {noun} changed or gone since the draw")
        )


def _show_nothing_to_review(plan: ReviewPlan) -> None:
    """Say there is nothing to review, and what to run when no label set exists."""
    console = make_console()
    if plan.has_set:
        console.print(ok_line("Nothing to review"))
    else:
        console.print(ok_line("Nothing to review", "no votes yet"))
        console.print(next_line("ijob eval label"))


def _review(service: Service, plan: ReviewPlan, now: Callable[[], datetime]) -> None:
    """Show each case and store the answers, until the cases or the user run out."""
    console = make_console()
    total = len(plan.items)
    for number, item in enumerate(plan.items, start=1):
        console.print(review_panel(item, number=number, total=total, rules=plan.rules))
        while True:
            reply = _reply(item, _typed(console, item))
            match reply.move:
                case _Move.QUIT:
                    return
                case _Move.SKIP:
                    break
                case _Move.OPEN:
                    saved = service.saved_detail(item.posting.id)
                    console.print(posting_detail(saved, now(), score=None))
                    continue
                case _Move.AGAIN:
                    console.print(
                        Text("   Not an answer here; use the keys shown", MUTED)
                    )
                    continue
                case _Move.ANSWER:
                    service.eval_record_review(item, keep=reply.keep, score=reply.score)
                    break
                case _:
                    assert_never(reply.move)
    console.print(ok_line("Review finished"))


def _seconds_line(millis: float | None) -> str:
    """Write a latency in seconds for a plain line, `-` when there is none."""
    return "-" if millis is None else f"{millis / 1000:.1f}"


def _report_lines(report: EvalReport) -> list[str]:
    """Write the report as plain lines: a line per run, then the jury and spend."""
    lines = [_line("runs", sum(len(suite.lines) for suite in report.suites))]
    for suite in report.suites:
        for item in suite.lines:
            run = item.run
            pairs = [
                f"{key}={metric_value(key, value)}"
                for key, value in item.metrics.items()
            ]
            pairs.append(f"cost_usd={run_cost(run):.4f}")
            pairs.append(f"p50_s={_seconds_line(item.p50_ms)}")
            lines.append(
                _line(
                    "run",
                    f"{run.id}  {run.suite}  {run.model}  {run.task_version}  "
                    + " ".join(pairs),
                )
            )
    if report.jury is not None:
        for name, shown in (
            ("jury_agreement", report.jury.agreement),
            ("jury_spot_check", report.jury.spot_check),
        ):
            lines.append(_line(name, "-" if shown is None else f"{shown:.2f}"))
        lines.append(_line("jury_overruled", report.jury.overruled))
    lines.append(_line("spent_usd", f"{report.spent:.4f}"))
    lines.append(_line("cap_usd", f"{report.cap:.4f}"))
    lines.append(_line("not_compared", report.others))
    return lines


def _show_report(report: EvalReport) -> None:
    """Show the comparable runs of every suite: drawn on a terminal, else plain."""
    terminal = is_terminal()
    if report.empty:
        if terminal:
            console = make_console()
            console.print(notice_line("No eval run yet"))
            console.print(next_line("ijob eval run hostile"))
        else:
            typer.echo(_line("runs", 0))
        return
    if terminal:
        console = make_console()
        for suite in report.suites:
            console.print(report_panel(suite))
        console.print(report_footer(report))
        return
    for line in _report_lines(report):
        typer.echo(line)
    if not report.labels_ready:
        say_note("note: labels need setup; run: ijob eval label")


def _show_run(detail: RunDetail) -> None:
    """Show one run's metrics, cost and speed: drawn on a terminal, else plain."""
    if is_terminal():
        make_console().print(run_panel(detail))
        return
    run = detail.run
    typer.echo(_line("suite", run.suite))
    typer.echo(_line("model", run.model))
    typer.echo(_line("status", run.status.value))
    typer.echo(_line("limited", str(run.limited).lower()))
    for key, value in detail.metrics.items():
        typer.echo(_line(key, metric_value(key, value)))
    typer.echo(_line("cost_usd", f"{run_cost(run):.4f}"))
    typer.echo(_line("p50_s", _seconds_line(detail.p50_ms)))
    typer.echo(_line("p95_s", _seconds_line(detail.p95_ms)))


def _show_failures(detail: RunDetail) -> None:
    """Show the failed cases of a run: drawn on a terminal, else plain lines."""
    if is_terminal():
        console = make_console()
        if detail.failed:
            console.print(failures_panel(detail))
        else:
            console.print(ok_line("No failed case", f"run {detail.run.id}"))
        return
    for result in detail.failed:
        typer.echo(
            _line(
                clean(result.case_key),
                f"{clean(result.answer)}  {clean(result.detail)}",
            ).rstrip()
        )


def _add_report(group: typer.Typer, open_session: SessionOpener) -> None:
    """Add the `report` command to the `eval` group.

    Args:
        group: The group to add the command to.
        open_session: Opens the service for a command.
    """

    @group.command("report")
    def report(
        run: Annotated[
            int | None,
            typer.Argument(
                min=1,
                metavar="[RUN]",
                help="Show this run; all comparable runs if absent.",
            ),
        ] = None,
        *,
        failures: Annotated[
            bool, typer.Option("--failures", help="List the cases of RUN that failed.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Show the stored eval runs side by side, or one run and its failures."""
        if failures and run is None:
            say_error("--failures needs a run number")
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
        with open_session(db, config, models=True) as session:
            service = session.service
            with _eval_failures():
                if run is None:
                    _show_report(service.eval_report())
                else:
                    detail = service.eval_run_detail(run)
                    if failures:
                        _show_failures(detail)
                    else:
                        _show_run(detail)


def _suite(name: str) -> Suite:
    """Read the suite a user named.

    Raises:
        typer.Exit: With `2`, after naming the suites there are.
    """
    try:
        return Suite(name)
    except ValueError as error:
        names = ", ".join(suite.value for suite in Suite)
        say_error(f"unknown suite {name!r}; the suites are {names}")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error


def _run_plan_lines(offer: RunOffer) -> list[str]:
    """Write the plan of a suite run as plain lines."""
    plan = offer.plan
    lines = [_line("suite", plan.suite.value), _line("cases", plan.cases)]
    lines.extend(
        _line("model", f"{ref}  {cost_text(cost)}") for ref, cost in offer.costs
    )
    lines.append(_line("estimate", cost_text(offer.estimate)))
    lines.append(_line("cap", f"${offer.cap:.2f}"))
    return lines


def _suite_label(suite: Suite) -> str:
    """Write a suite's name for a title: `source-agent` is `Source agent`."""
    return suite.value.replace("-", " ").capitalize()


def _show_run_plan(offer: RunOffer, notice: str | None) -> None:
    """Show the models, the estimate, the cap and who receives the data."""
    if is_terminal():
        plan = offer.plan
        make_console().print(
            plan_panel(
                f"{_suite_label(plan.suite)} · {plan.cases} cases",
                column="MODEL",
                models=offer.costs,
                estimate=offer.estimate,
                cap=offer.cap,
                notice=notice,
            )
        )
        return
    for line in _run_plan_lines(offer):
        typer.echo(line, err=True)
    if notice is not None:
        say_note(f"note: {notice}")


def _confirm_run(
    offer: RunOffer,
    *,
    yes: bool,
    is_interactive: Callable[[], bool],
) -> None:
    """Ask before a run that calls a model on OpenRouter.

    Raises:
        typer.Exit: With `1`, when the user must be asked and cannot be, or
            declines.
    """
    paid = any(ref.provider is Provider.OPENROUTER for ref in offer.plan.callers)
    if yes or not paid:
        return
    if not is_interactive():
        say_error("confirmation needed; pass --yes")
        raise typer.Exit(code=_EXIT_REFUSED)
    if _ask("Start the run?").strip().lower() not in _AFFIRMATIVE:
        say_note("note: the run was not started")
        raise typer.Exit(code=_EXIT_REFUSED)


def _runner(service: Service, offer: RunOffer) -> _SuiteRunner:
    """Pick the service call that runs an offer's suite; agents need web clients."""
    if offer.plan.suite in AGENT_SUITES:
        return service.eval_run_agents
    return service.eval_run


def _run_suite_on_terminal(service: Service, offer: RunOffer) -> SuiteReport | None:
    """Run the suite with a live line of progress.

    Returns:
        What the run did, or `None` when the user stopped it.
    """
    console = make_console()
    name = _suite_label(offer.plan.suite)
    state = RunProgress(done=0, stored=0, total=offer.plan.cases, spent=0.0)

    def draw() -> Text:
        return run_progress(
            name,
            f"{state.done} of {state.total} cases",
            done=state.done,
            total=state.total,
            spent=state.spent,
            cap=offer.cap,
            frame=int(time.monotonic() * _SPINS_PER_SECOND),
        )

    def progress(update: RunProgress) -> None:
        nonlocal state
        state = update

    report: SuiteReport | None = None
    with (
        Live(
            get_renderable=draw,
            console=console,
            refresh_per_second=_REFRESH_PER_SECOND,
            transient=True,
        ),
        suppress(KeyboardInterrupt),
    ):
        report = asyncio.run(_runner(service, offer)(offer, on_progress=progress))
    if report is None:
        console.print(
            notice_line(
                "Stopped", f"{state.stored} cases stored; the run is kept as incomplete"
            )
        )
    return report


def _run_suite_in_a_pipe(service: Service, offer: RunOffer) -> SuiteReport:
    """Run the suite, showing progress on a terminal's standard error only."""
    line = ProgressLine(sys.stderr)

    def progress(update: RunProgress) -> None:
        line.show_live(
            f"{offer.plan.suite.value}: {update.done} of {update.total} cases, "
            f"${update.spent:.2f} of ${offer.cap:.2f}"
        )

    try:
        return asyncio.run(_runner(service, offer)(offer, on_progress=progress))
    finally:
        line.clear()


def _say_why_incomplete(report: SuiteReport) -> None:
    """Say what stopped a run, or that it is incomplete with no stop to name."""
    stopped_by = report.operation.stopped_by
    if stopped_by == "provider":
        _say_stopped(f"{report.model} stopped: {report.stop_error}")
    elif stopped_by == "cost_unknown":
        _say_stopped(f"{report.stop_error}")
    elif stopped_by == "cap":
        say_note(
            "note: stopped at the cost cap (eval.max_cost_usd), "
            "the run is kept as incomplete"
        )
    elif report.status is RunStatus.INCOMPLETE:
        say_note(
            "note: the run is kept as incomplete, with no stop to name; "
            f"see ijob eval report {report.run_id} --failures"
        )


def _show_suite_result(detail: RunDetail, report: SuiteReport) -> None:
    """Show a finished run: drawn on a terminal, `name  value` lines in a pipe."""
    if is_terminal():
        console = make_console()
        match report.suite:
            case Suite.HOSTILE:
                console.print(hostile_panel(detail))
            case Suite.SOURCE_AGENT:
                console.print(source_suite_panel(detail))
            case Suite.EXPLORE_AGENT:
                console.print(explore_suite_panel(detail))
            case _:
                console.print(suite_panel(detail))
    else:
        for key, _, text in run_values(detail):
            typer.echo(_line(key, text))
        typer.echo(_line("cost_usd", f"{run_cost(detail.run):.4f}"))
        number = report.run_id
        say_note(f"note: run {number}; run: ijob eval report {number} --failures")
    _say_why_incomplete(report)
    if report.skipped:
        say_note(
            f"note: {report.skipped} cases changed since the draw and were left out"
        )
    for note in report_notes(report.operation):
        say_note(note)


def _add_run(
    group: typer.Typer, open_session: SessionOpener, is_interactive: Callable[[], bool]
) -> None:
    """Add the `run` command to the `eval` group.

    Args:
        group: The group to add the command to.
        open_session: Opens the service for a command.
        is_interactive: Whether the user can be asked a question; `run` asks
            only then.
    """

    @group.command("run")
    def run(  # noqa: PLR0913  # the command's options, one each
        suite: Annotated[
            str,
            typer.Argument(
                metavar="SUITE",
                help="The suite: " + ", ".join(each.value for each in Suite) + ".",
            ),
        ],
        *,
        model: Annotated[
            str | None,
            typer.Option(
                metavar="PROVIDER/MODEL",
                help="The model under test; the suite's configured one if absent.",
            ),
        ] = None,
        limit: Annotated[
            int | None,
            typer.Option(
                help="Cover only this many cases, from 1 to 100; "
                "source-agent 1 to 150, explore-agent 1 to 5."
            ),
        ] = None,
        yes: Annotated[
            bool, typer.Option("--yes", help="Start without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Run a suite on a model and show what it measured."""
        chosen = _suite(suite)
        with open_session(db, config, models=True) as session:
            service = session.service
            with _eval_failures():
                offer = asyncio.run(
                    service.eval_prepare_run(
                        chosen,
                        model=None
                        if model is None
                        else parse_model_ref(model, "model"),
                        limit=limit,
                    )
                )
            notice = (
                data_notice(offer.plan.callers)
                if offer.plan.sends_personal_data
                else None
            )
            _show_run_plan(offer, notice)
            _confirm_run(offer, yes=yes, is_interactive=is_interactive)
            with _eval_failures():
                report = (
                    _run_suite_on_terminal(service, offer)
                    if is_terminal()
                    else _run_suite_in_a_pipe(service, offer)
                )
                detail = (
                    None if report is None else service.eval_run_detail(report.run_id)
                )
            if report is None or detail is None:
                raise typer.Exit(
                    code=_EXIT_REFUSED
                    if chosen is Suite.HOSTILE or chosen in AGENT_SUITES
                    else 0
                )
            _show_suite_result(detail, report)
            if report.exit_status:
                raise typer.Exit(code=report.exit_status)


def eval_app(
    open_session: SessionOpener,
    is_interactive: Callable[[], bool],
    now: Callable[[], datetime],
) -> typer.Typer:
    """Build the `eval` command group.

    Args:
        open_session: Opens the service for a command.
        is_interactive: Whether the user can be asked a question; `label` and
            `run` ask only then.
        now: The wall clock, returning timezone-aware times; `review` shows
            the age of a posting by it.

    Returns:
        The group, with `label`, `review`, `run` and `report`.
    """
    group = typer.Typer(
        add_completion=False,
        help="Label postings with a jury, run eval suites and read their reports.",
    )

    @group.command("label")
    def label(
        *,
        limit: Annotated[
            int | None,
            typer.Option(
                min=1, max=100, help="Cover only this many postings, from 1 to 100."
            ),
        ] = None,
        yes: Annotated[
            bool, typer.Option("--yes", help="Start without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Have the jury vote on a sample of postings and label what they agree on."""
        with open_session(db, config, models=True) as session:
            service = session.service
            with _eval_failures():
                offer = asyncio.run(service.eval_prepare(limit=limit))
            callers = list(dict.fromkeys(item.juror for item in offer.plan.items))
            notice = data_notice(callers)
            if offer.plan.items:
                _show_plan(offer, notice)
            else:
                say_note("note: every vote is already stored")
            _confirm(offer, notice, yes=yes, is_interactive=is_interactive)
            with _eval_failures():
                report = (
                    _run_on_terminal(service, offer)
                    if is_terminal()
                    else _run_in_a_pipe(service, offer)
                )
            if report is None:
                return
            _show_result(report)
            if report.stopped_jurors or report.cost_unknown is not None:
                raise typer.Exit(code=_EXIT_REFUSED)

    @group.command("review")
    def review(
        *,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Settle the cases the jury did not agree on, then spot check the rest."""
        if not is_terminal():
            say_error("ijob eval review needs a terminal")
            raise typer.Exit(code=_EXIT_REFUSED)
        with open_session(db, config, models=True) as session:
            service = session.service
            with _eval_failures():
                plan = service.eval_review_items()
            _show_skipped(plan)
            if plan.items:
                _review(service, plan, now)
            else:
                _show_nothing_to_review(plan)

    _add_run(group, open_session, is_interactive)
    _add_report(group, open_session)

    return group
