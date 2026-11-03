"""The command-line side of the agents.

`find_command` adds `ijob sources find "<company or URL>"` to the `sources`
group: an agent looks for the one source that lists a company's openings, the
command shows the steps it took and a sample of the jobs, and adds the source
once the user confirms. A URL, which needs `--company`, fixes the site the agent
may read; a name alone starts without one. `--model` replaces
`[models].source_agent` for this run, and `--yes` confirms without asking.

The exit status is `0` when the source was added, was already one of the user's,
or the run reached its cost cap with nothing wrong; `1` when nothing was found,
the user declined, a provider or cost failure or an unusable answer ended the
run (a source verified before it is still offered), the model is missing or the
user cannot be asked and gave no `--yes`; and `2` for a blank input, a URL
without `--company`, `--company` beside a name, a `--model` that cannot be
used, or a source that another company already holds. On a terminal it draws a
spinner while the agent runs and the result in boxes; in a pipe it prints plain
lines.

`explore_command` adds `ijob explore "<instruction>"`: an agent carries out the
instruction by looking for the sources of companies, the command shows what it
proposes, and the user picks which to add (`1,2`, `all`, `none`, or Enter for
none; anything else asks again, and the end of input means none). `--yes` adds
every proposal without asking. `--model` replaces `[models].explore_agent` for
this run. A blank instruction or one over 4,000 characters, or a `--model` that
cannot be used, exits `2`. The proposals are shown however the run ended: a
reached cost cap exits `0`; a provider or cost failure or an unusable answer
exits `1`, as does a proposal that fails to add (the rest are still added), a
missing model, and a user who cannot be asked and gave no `--yes`.

`read_model`, `require_confirmation`, `steps_shown`, `say_stopped` and
`shell_quoted` are what a command that runs an agent shares. Of the LLM layer
this module imports its error classes and the model reference only, so loading
it pulls in neither DSPy nor the Claude SDK. Only an instruction that is not
blank loads the module that holds the length limit, and with it DSPy.
"""

import asyncio
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import typer
from ijob_ai.errors import LlmError
from ijob_ai.llm.reference import ModelRef, parse_model_ref
from ijob_core.dedupe import normalise_company
from ijob_core.errors import InvalidValueError
from ijob_sources.errors import SourcesError
from rich.console import Group
from rich.live import Live
from rich.text import Text

from ijob_app.agent_ops import (
    EXPLORE_MAX_STEPS,
    SOURCE_MAX_STEPS,
    ExploreResult,
    FoundSource,
    SourceResult,
    found_of,
)
from ijob_app.agent_view import (
    explore_line,
    found_lines,
    found_panel,
    not_found_view,
    proposals_lines,
    proposals_panel,
    shown_steps,
    step_line,
)
from ijob_app.cli_llm import (
    CONFIG_HELP,
    DB_HELP,
    LlmSession,
    SessionOpener,
    report_notes,
)
from ijob_app.cli_output import ask, say_cost, say_done, say_error, say_note
from ijob_app.cli_progress import ProgressLine
from ijob_app.cli_recipes import recipe_failures
from ijob_app.llm_ops import OperationReport
from ijob_app.render import error_text
from ijob_app.service import Service, SourceCompanyError
from ijob_app.view import (
    AMBER,
    GROUND,
    MUTED,
    clean,
    is_terminal,
    make_console,
)

if TYPE_CHECKING:
    from ijob_ai.agents.react import AgentStep, StepCallback

_EXIT_REFUSED: Final = 1
_EXIT_BAD_ARGUMENT: Final = 2

_CONFIRM_PROMPT: Final = "Add this source?"
_PICK_PROMPT: Final = "Add which?"
_PICK_HINT: Final = "  1,2   all   none  "
_LIVE_LINES: Final = 8
_AFFIRMATIVE: Final = frozenset({"y", "yes"})
_URL: Final = re.compile(r"https?://", re.IGNORECASE)
_SHELL_SPECIAL: Final = re.compile(r'(["\\$`])')
_MAX_PICK_DIGITS: Final = 9  # a longer run is no position in any list
_SPINS_PER_SECOND: Final = 10
_REFRESH_PER_SECOND: Final = 10
_CAP_NOTE: Final = (
    "note: Stopped at the cost cap; raise [llm].max_cost_usd to let it go on"
)


def read_model(text: str | None) -> ModelRef | None:
    """Parse a `--model` value.

    Args:
        text: The `<provider>/<model>` text, or `None` when the flag is absent.

    Returns:
        The reference, or `None` to use the configured model.

    Raises:
        typer.Exit: With status 2 after printing the reason, when `text` is not
            a valid reference.
    """
    if text is None:
        return None
    try:
        return parse_model_ref(text, "--model")
    except InvalidValueError as error:
        say_error(f"{error}")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error


def require_confirmation(*, yes: bool, is_interactive: Callable[[], bool]) -> None:
    """Refuse to go on when nobody can confirm what the agent finds.

    Args:
        yes: Whether `--yes` was given.
        is_interactive: Whether the user can be asked a question.

    Raises:
        typer.Exit: With status 1 after printing the reason, when `--yes` is
            absent and the user cannot be asked.
    """
    if not yes and not is_interactive():
        say_error("confirmation needed; pass --yes")
        raise typer.Exit(code=_EXIT_REFUSED)


def shell_quoted(text: str) -> str:
    """Put a name in double quotes, so a command line given to the user keeps it whole.

    Args:
        text: The name, such as a company.

    Returns:
        The name on one line, with a double quote, a backslash, a dollar sign
        and a backtick each escaped by a backslash.
    """
    return '"' + _SHELL_SPECIAL.sub(r"\\\1", clean(text)) + '"'


@contextmanager
def steps_shown(label: str, limit: int) -> Iterator["StepCallback"]:
    """Show an agent's progress while it runs: a spinner on a terminal.

    On a terminal the line is drawn with the step the agent is on. In a pipe a
    terminal's standard error gets the same text as one line rewritten in
    place, and anything else gets nothing; standard output holds the result.

    Args:
        label: What the agent is doing, such as `Looking for Example Co`.
        limit: How many steps the agent may take.

    Yields:
        The callback to give the agent, called after each tool call.
    """
    finished = 0

    def on_step(_step: "AgentStep") -> None:
        nonlocal finished
        finished += 1

    if not is_terminal():
        line = ProgressLine(sys.stderr)

        def tell(step: "AgentStep") -> None:
            on_step(step)
            line.show_live(f"{clean(label)}: step {finished + 1} of at most {limit}")

        try:
            yield tell
        finally:
            line.clear()
        return

    def draw() -> Text:
        frame = int(time.monotonic() * _SPINS_PER_SECOND)
        return step_line(label, finished + 1, limit, frame)

    with Live(
        get_renderable=draw,
        console=make_console(),
        refresh_per_second=_REFRESH_PER_SECOND,
        transient=True,
    ):
        yield on_step


@contextmanager
def explore_shown(
    limit: int, cap: float
) -> Iterator[tuple["StepCallback", Callable[[float], None]]]:
    """Show an explore run's progress while it runs: a spinner on a terminal.

    On a terminal the line carries the step the agent is on and what the run
    has spent against `cap`, with a line under it for each thing the agent
    found out; those lines stay once the run ends. In a pipe a terminal's
    standard error gets one line rewritten in place, and anything else gets
    nothing; standard output holds the result.

    Args:
        limit: How many steps the agent may take.
        cap: The most the run may spend, in US dollars.

    Yields:
        The step callback and the cost callback to give the agent.
    """
    finished = 0
    spent = 0.0
    lines: list[Text] = []

    def on_cost(money: float) -> None:
        nonlocal spent
        spent = money

    def on_step(step: "AgentStep") -> None:
        nonlocal finished
        finished += 1
        line = explore_line(step)
        if line is not None:
            lines.append(line)

    if not is_terminal():
        progress = ProgressLine(sys.stderr)

        def tell(step: "AgentStep") -> None:
            on_step(step)
            progress.show_live(f"Exploring: step {finished + 1} of at most {limit}")

        try:
            yield tell, on_cost
        finally:
            progress.clear()
        return

    console = make_console()

    def draw() -> Group:
        frame = int(time.monotonic() * _SPINS_PER_SECOND)
        spinner = step_line("Exploring", finished + 1, limit, frame, cost=(spent, cap))
        return Group(spinner, *lines[-_LIVE_LINES:])

    try:
        with Live(
            get_renderable=draw,
            console=console,
            refresh_per_second=_REFRESH_PER_SECOND,
            transient=True,
        ):
            yield on_step, on_cost
    finally:
        for line in lines:
            console.print(line)


def say_stopped(report: OperationReport, stop: Exception | None) -> bool:
    """Say why an agent run ended early, if it did.

    A reached cost cap is a note naming the setting that raises it; any other
    end, such as a provider failure or an answer that could not be used, is an
    error. The text is cut to one line with control characters dropped.

    Args:
        report: What the operation did and spent.
        stop: What ended the run early, or `None` for a run that finished.

    Returns:
        Whether the run ended in a failure, which makes the command exit `1`.
    """
    if stop is None:
        return False
    if report.stopped_by == "cap":
        say_note(_CAP_NOTE)
        return False
    say_error(clean(str(stop)), literal=True)
    return True


def _read_target(
    text: str, company: str | None, read_company: Callable[[str], str]
) -> tuple[str, str | None]:
    """Work out the company and the careers address from what the user gave.

    Raises:
        typer.Exit: With status 2 after printing the reason, when the input is
            blank, a URL has no company, or a name comes with `--company`.
    """
    given = text.strip()
    if not given:
        say_error("give a company name or a careers URL")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
    if _URL.match(given):
        if company is None:
            say_error("a URL needs --company, the company it belongs to")
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
        return read_company(company), given
    if company is not None:
        say_error("--company goes with a URL; without one, the name is the company")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
    return given, None


def _show(result: SourceResult, found: FoundSource) -> None:
    """Print the source found with the steps that led to it."""
    if is_terminal():
        make_console().print(found_panel(found, result.steps))
    else:
        for line in found_lines(found, result.steps):
            typer.echo(line)


def _say_none(
    company: str, url: str | None, result: SourceResult, spent: float
) -> None:
    """Say that nothing was found, and which command adds the source by hand."""
    steps = len(shown_steps(result.steps))
    fix = (
        f"ijob sources add-url {shell_quoted(url) if url else '<careers URL>'} "
        f"--company {shell_quoted(company)}"
    )
    if is_terminal():
        make_console(stderr=True).print(not_found_view(company, steps, spent, fix))
        return
    word = "step" if steps == 1 else "steps"
    say_error(
        f"no source found for {clean(company)} ({steps} {word}, ${spent:.4f})", fix
    )


def _check_known(service: Service, found: FoundSource) -> None:
    """Refuse a source that is already one for another company.

    Raises:
        SourceCompanyError: The user's source of that name belongs to a
            different company.
    """
    for row in service.sources():
        if row.ref == found.ref and normalise_company(row.company) != normalise_company(
            found.company
        ):
            raise SourceCompanyError(found.ref, row.company)


def _add(service: Service, found: FoundSource, *, yes: bool) -> bool:
    """Ask whether to add the source found and add it if so.

    Returns:
        Whether the source was added.
    """
    if not yes:
        answer = ask(_CONFIRM_PROMPT)
        if answer.strip().lower() not in _AFFIRMATIVE:
            say_done("not stored", "Not stored")
            return False
    with recipe_failures():
        row = asyncio.run(service.add_found(found))
    jobs = "1 job" if found.jobs == 1 else f"{found.jobs} jobs"
    say_done(f"added {row.ref}", f"Added {row.ref}", f"{jobs} on the next fetch")
    return True


def _conclude(  # noqa: PLR0913  # what the run was given and what it gave back
    service: Service,
    company: str,
    url: str | None,
    result: SourceResult,
    report: OperationReport,
    *,
    yes: bool,
) -> int:
    """Show how a run ended and, if it found a source, offer it.

    Returns:
        The exit status.
    """
    found = result.found
    if found is not None:
        _show(result, found)
    say_cost(report)
    for note in report_notes(report):
        say_note(note)
    for line in result.could_not_check:
        say_note(f"note: could not check {clean(line)}", literal=True)
    failed = say_stopped(report, result.stop)
    if found is None:
        _say_none(company, url, result, report.spent)
        return _EXIT_REFUSED
    if found.known:
        with recipe_failures():
            _check_known(service, found)
        say_done(f"{found.ref} is already a source", f"{found.ref} is already a source")
        return _EXIT_REFUSED if failed else 0
    added = _add(service, found, yes=yes)
    return 0 if added and not failed else _EXIT_REFUSED


def find_command(
    group: typer.Typer,
    *,
    open_session: Callable[[Path | None, Path | None], AbstractContextManager[Service]],
    read_company: Callable[[str], str],
    is_interactive: Callable[[], bool],
) -> None:
    """Add `find` to the `sources` group.

    Args:
        group: The `sources` group.
        open_session: Locates the ijob home when called and gives the context
            that opens the service with LLM options for the `--db` and
            `--config` values.
        read_company: Trims a company name, exiting `2` when it is blank.
        is_interactive: Whether the user can be asked a question.
    """

    @group.command()
    def find(  # noqa: PLR0913  # the input and one parameter per option
        target: Annotated[
            str,
            typer.Argument(
                metavar="COMPANY_OR_URL",
                help="A company name, or its careers URL with --company.",
            ),
        ],
        *,
        company: Annotated[
            str | None,
            typer.Option(help="The company a URL belongs to; needed with a URL."),
        ] = None,
        model: Annotated[
            str | None,
            typer.Option(
                help="Model for this run instead of the configured source_agent "
                "model, as <provider>/<model>."
            ),
        ] = None,
        yes: Annotated[
            bool, typer.Option("--yes", help="Add the source without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Have an agent find the source that lists a company's jobs, and add it."""
        name, url = _read_target(target, company, read_company)
        chosen = read_model(model)
        require_confirmation(yes=yes, is_interactive=is_interactive)
        with open_session(db, config) as service:
            with (
                recipe_failures(),
                steps_shown(f"Looking for {name}", SOURCE_MAX_STEPS) as on_step,
            ):
                result, report = asyncio.run(
                    service.find_source(name, url, model=chosen, on_step=on_step)
                )
            status = _conclude(service, name, url, result, report, yes=yes)
        if status:
            raise typer.Exit(code=status)


def _read_instruction(text: str) -> str:
    """Check the instruction the user gave.

    Returns:
        The instruction as it was given.

    Raises:
        typer.Exit: With status 2 after printing the reason, when the text is
            blank or longer than an instruction may be.
    """
    if not text.strip():
        say_error("give an instruction, such as what kind of company to look for")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
    from ijob_ai.tasks.instructions import INSTRUCTIONS_LIMIT  # noqa: PLC0415

    if len(text) > INSTRUCTIONS_LIMIT:
        say_error(f"the instruction must be at most {INSTRUCTIONS_LIMIT:,} characters")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
    return text


def _cap_of(session: LlmSession) -> float:
    """Read the most one run may spend from the config file in use."""
    from ijob_app.llm_config import read_llm_settings  # noqa: PLC0415

    return read_llm_settings(session.config or session.default_config).max_cost_usd


def _show_proposals(result: ExploreResult) -> None:
    """Print what the explore run proposed, with its counts."""
    if is_terminal():
        console = make_console()
        if result.proposals:
            console.print(proposals_panel(result))
        else:
            console.print(Text(f" {proposals_lines(result)[0]}", style=MUTED))
        return
    for line in proposals_lines(result):
        typer.echo(line)


def _ask_pick() -> str | None:
    """Ask which proposals to add; `None` when there is no more input."""
    if is_terminal():
        prompt = Text(f" {_PICK_PROMPT} ", style=f"bold {GROUND} on {AMBER}")
        prompt.append(_PICK_HINT, style=f"not bold {MUTED} on {GROUND}")
        try:
            return make_console().input(prompt)
        except EOFError:
            return None
    try:
        return str(
            typer.prompt(
                f"{_PICK_PROMPT} [1,2/all/none]",
                default="",
                show_default=False,
                prompt_suffix=" ",
            )
        )
    except typer.Abort:
        return None


def _read_pick(answer: str, count: int) -> list[int] | None:
    """Read an answer to the question of which proposals to add.

    Returns:
        The zero-based positions chosen, in order; `None` when the answer is
        none of `1,2`, `all`, `none` or nothing.
    """
    text = answer.strip().lower()
    if text in ("", "none"):
        return []
    if text == "all":
        return list(range(count))
    parts = [part.strip() for part in text.split(",")]
    if not all(
        part.isdecimal() and len(part) <= _MAX_PICK_DIGITS and 1 <= int(part) <= count
        for part in parts
    ):
        return None
    return sorted({int(part) - 1 for part in parts})


def _pick(count: int) -> list[int]:
    """Ask until the user gives a valid answer; the end of input means none."""
    while True:
        answer = _ask_pick()
        if answer is None:
            return []
        picked = _read_pick(answer, count)
        if picked is not None:
            return picked
        say_error(f"answer with numbers from 1 to {count} such as 1,2, or all, or none")


def _add_picked(service: Service, result: ExploreResult, picked: list[int]) -> bool:
    """Add the proposals at the given positions, going on past one that fails.

    Returns:
        Whether every one was added.
    """
    added: list[str] = []
    complete = True
    for position in picked:
        found = found_of(result.proposals[position])
        try:
            row = asyncio.run(service.add_found(found))
        except (InvalidValueError, SourcesError, LlmError, sqlite3.Error) as error:
            reason = (
                error_text(error)
                if isinstance(error, InvalidValueError)
                else str(error)
            )
            say_error(f"could not add {found.ref}: {clean(reason)}", literal=True)
            complete = False
            continue
        added.append(str(row.ref))
    if added:
        word = "source" if len(added) == 1 else "sources"
        say_done(
            "added " + ", ".join(added),
            f"Added {len(added)} {word}",
            "run: ijob search",
        )
    elif complete:
        say_done("not stored", "Not stored")
    return complete


def _conclude_explore(
    service: Service,
    result: ExploreResult,
    report: OperationReport,
    *,
    yes: bool,
) -> int:
    """Show how an explore run ended and add the proposals the user picks.

    Returns:
        The exit status.
    """
    _show_proposals(result)
    say_cost(report)
    for note in report_notes(report):
        say_note(note)
    failed = say_stopped(report, result.stop)
    if not result.proposals:
        return _EXIT_REFUSED if failed else 0
    count = len(result.proposals)
    picked = list(range(count)) if yes else _pick(count)
    complete = _add_picked(service, result, picked)
    return _EXIT_REFUSED if failed or not complete else 0


def explore_command(
    app: typer.Typer,
    open_session: SessionOpener,
    is_interactive: Callable[[], bool],
) -> None:
    """Add `explore` to the application.

    Args:
        app: The application.
        open_session: Opens the service for a command that calls a model.
        is_interactive: Whether the user can be asked a question.
    """

    @app.command()
    def explore(
        instruction: Annotated[
            str,
            typer.Argument(
                metavar="INSTRUCTION",
                help="What to look for, in your own words, such as "
                "'fintech companies hiring in Tokyo'.",
            ),
        ],
        *,
        model: Annotated[
            str | None,
            typer.Option(
                help="Model for this run instead of the configured explore_agent "
                "model, as <provider>/<model>."
            ),
        ] = None,
        yes: Annotated[
            bool, typer.Option("--yes", help="Add every proposal without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Have an agent carry out an instruction and propose sources to add."""
        text = _read_instruction(instruction)
        chosen = read_model(model)
        require_confirmation(yes=yes, is_interactive=is_interactive)
        with open_session(db, config, models=True) as session:
            with recipe_failures():
                cap = _cap_of(session)
                with explore_shown(EXPLORE_MAX_STEPS, cap) as (on_step, on_cost):
                    result, report = asyncio.run(
                        session.service.explore(
                            text, model=chosen, on_step=on_step, on_cost=on_cost
                        )
                    )
            status = _conclude_explore(session.service, result, report, yes=yes)
        if status:
            raise typer.Exit(code=status)
