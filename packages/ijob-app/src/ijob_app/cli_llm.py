"""The command-line side of the LLM operations.

`llm_failures` turns what a model-calling service operation raises into a
message and an exit status, with `stop_exits` and `report_notes` for the other
parts every model-calling operation shares; the words the commands print come
from `ijob_app.llm_text`. `score_command` adds `score`, which prints each
posting's score and reasons, and `traces_app` builds the `traces` group that
lists, shows, prunes and exports the recorded traces. `instructions_app` builds the
`instructions` group: `set` reads the user's words into filters and rules, shows
them and stores them once confirmed, `show` prints what is stored and `clear`
removes it. This module imports the LLM layer's error classes and the service's
types only, so loading it pulls in neither DSPy nor the Claude SDK; the
`instructions` commands `show` and `clear` and the `traces` commands open the
service without LLM options and never load either.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Protocol

import typer
from ijob_ai.errors import LlmError
from ijob_core.errors import InvalidValueError
from ijob_core.model import PostingId
from ijob_core.traces import SpanNode, TraceSummary

from ijob_app.cli_output import ask, say_done, say_error, say_note, working
from ijob_app.cli_search import live_score_command
from ijob_app.config import FilterFlags, read_filter_layers
from ijob_app.llm_ops import STATUS_TEXT, OperationReport, ScoreItem, ScoreStatus
from ijob_app.llm_text import (
    MISSING_MARK,
    filter_items,
    instruction_lines,
    score_summary,
    shadow_line,
)
from ijob_app.service import Service
from ijob_app.view import (
    fix_line,
    instructions_view,
    is_terminal,
    make_console,
    notice_line,
    trace_tree,
    traces_table,
)

if TYPE_CHECKING:
    from ijob_app.trace_export import ExportReport

_EXIT_REFUSED: Final = 1
_EXIT_BAD_ARGUMENT: Final = 2

DB_HELP: Final = (
    "Database file. Default: ijob.db in the ijob home, $IJOB_HOME or ~/.ijob."
)
CONFIG_HELP: Final = (
    "Config file. Default: config.toml in the ijob home, $IJOB_HOME or ~/.ijob."
)

_DEFAULT_TRACES: Final = 20
_MAX_TRACES: Final = 1000
_NS_PER_MS: Final = 1_000_000
_BYTES_PER_MB: Final = 2**20
_FAILING_STOPS: Final = frozenset({"provider", "cost_unknown"})

_CONFIRM_PROMPT: Final = "Store these?"
_AFFIRMATIVE: Final = frozenset({"y", "yes"})


@dataclass(frozen=True, slots=True)
class LlmSession:
    """What a command that may call a model works with.

    Attributes:
        service: The open service.
        config: The `--config` value, or `None` for the home's file.
        default_config: The config file inside the ijob home.
    """

    service: Service
    config: Path | None
    default_config: Path


class SessionOpener(Protocol):
    """Opens the service for one command, locating the ijob home first."""

    def __call__(
        self, db: Path | None, config: Path | None, *, models: bool
    ) -> AbstractContextManager[LlmSession]:
        """Open a session.

        Args:
            db: The `--db` value, or `None` for the home's database.
            config: The `--config` value, or `None` for the home's file.
            models: Whether the command calls a model; a command that does not
                opens the service without LLM options.
        """
        ...


@contextmanager
def llm_failures() -> Iterator[None]:
    """Turn what a model-calling operation raises into a message and an exit status.

    A value the service refuses, such as a config setting out of range, exits
    `2`; every error of the LLM layer exits `1`.

    Raises:
        typer.Exit: After printing `error: <message>` to stderr.
    """
    try:
        yield
    except InvalidValueError as error:
        say_error(f"{error}")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
    except LlmError as error:
        say_error(f"{error}")
        raise typer.Exit(code=_EXIT_REFUSED) from error


def report_notes(report: OperationReport) -> list[str]:
    """Write the warnings an operation left: tracing set-up and spans not stored.

    Args:
        report: What the operation did.

    Returns:
        Each tracing message as the tracing layer wrote it, which already begins
        `warning: `, then `warning: N spans not stored` if the trace store could
        not keep some spans.
    """
    notes = list(report.warnings)
    if report.dropped_spans:
        notes.append(f"warning: {report.dropped_spans} spans not stored")
    return notes


def stop_exits(report: OperationReport) -> bool:
    """Say whether an operation ended on a stop that makes the command exit `1`.

    A provider that failed or a cost that could not be known is a failure; a
    spending cap that was reached is the operation working as set.
    """
    return report.stopped_by in _FAILING_STOPS


def instructions_app(
    open_session: SessionOpener, is_interactive: Callable[[], bool]
) -> typer.Typer:
    """Build the `instructions` command group.

    Args:
        open_session: Opens the service for a command.
        is_interactive: Whether the user can be asked a question; `set` asks
            only then.

    Returns:
        The group, with `set`, `show` and `clear`.
    """
    group = typer.Typer(add_completion=False, help="Tell ijob what you look for.")

    @group.command("set")
    def set_instructions(
        text: Annotated[str, typer.Argument(help="What you look for, in your words.")],
        *,
        yes: Annotated[
            bool, typer.Option("--yes", help="Store without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Read your instructions into filters and rules, show them and store them."""
        with open_session(db, config, models=True) as session:
            with llm_failures():
                layers = read_filter_layers(
                    session.config, FilterFlags(), default=session.default_config
                )
                with working("Having a model read what you wrote"):
                    proposal = asyncio.run(
                        session.service.propose_instructions(layers, text)
                    )
            if is_terminal():
                make_console().print(
                    instructions_view(
                        filter_items(proposal.instruction_filters), proposal.rules
                    )
                )
            else:
                for line in instruction_lines(
                    proposal.instruction_filters, proposal.rules
                ):
                    typer.echo(line)
            for shadowed in proposal.shadowed:
                if is_terminal():
                    say_note(shadow_line(shadowed))
                else:
                    typer.echo(shadow_line(shadowed))
            if not yes:
                if not is_interactive():
                    say_error("confirmation needed; pass --yes")
                    raise typer.Exit(code=_EXIT_REFUSED)
                answer = ask(_CONFIRM_PROMPT)
                if answer.strip().lower() not in _AFFIRMATIVE:
                    raise typer.Exit(code=_EXIT_REFUSED)
            with llm_failures():
                session.service.accept_instructions(proposal)
            if is_terminal():
                say_done("", "Saved", "filters apply now, rules run with --judge")

    @group.command("show")
    def show_instructions(
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """Print your stored instructions, the filters and rules read from them."""
        with open_session(db, None, models=False) as session:
            stored = session.service.instructions()
        if stored is None:
            if is_terminal():
                make_console().print(notice_line("No instructions set"))
                make_console().print(fix_line('ijob instructions set "what you want"'))
            else:
                typer.echo("no instructions set")
            return
        filters: Mapping[str, object] = json.loads(stored.filters_json)
        if is_terminal():
            make_console().print(
                instructions_view(filter_items(filters), stored.rules, text=stored.text)
            )
            return
        typer.echo(f"text: {stored.text}")
        for line in instruction_lines(filters, stored.rules):
            typer.echo(line)

    @group.command("clear")
    def clear_instructions(
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """Remove your stored instructions."""
        with open_session(db, None, models=False) as session:
            session.service.clear_instructions()
        say_done("cleared", "Instructions cleared")

    return group


def score_lines(item: ScoreItem) -> list[str]:
    """Write a scored posting: its id, score and title, then one line per reason.

    Args:
        item: The posting and its score.

    Returns:
        The id, score and title, tab-separated, and each reason indented by two
        spaces; for a posting with no score the id, a dash, the title and a dot
        with the reason there is no score.
    """
    posting = item.posting
    score = item.score
    if score is None:
        text = STATUS_TEXT[ScoreStatus].get(item.status, item.status.value)
        return [f"{posting.id}\t-\t{posting.title}\t{MISSING_MARK} {text}"]
    lines = [f"{posting.id}\t{score.score}\t{posting.title}"]
    lines += [f"  {reason}" for reason in score.reasons]
    return lines


async def _collect_scores(service: Service, ids: list[PostingId]) -> list[ScoreItem]:
    """Gather every item of a scoring run."""
    return [item async for item in service.score(ids)]


def score_command(app: typer.Typer, open_session: SessionOpener) -> None:
    """Add the `score` command to `app`.

    Args:
        app: The application to add the command to.
        open_session: Opens the service for a command.
    """

    @app.command("score")
    def score(
        posting_ids: Annotated[
            list[int],
            typer.Argument(metavar="ID...", help="Posting ids, as `search` prints."),
        ],
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Score postings against your CV, from 0 to 100, with the reasons."""
        ids = list(dict.fromkeys(PostingId(each) for each in posting_ids))
        with open_session(db, config, models=True) as session:
            if is_terminal():
                with llm_failures():
                    report = live_score_command(session.service, ids)
                if report is not None:
                    for note in report_notes(report):
                        say_note(note)
                    if stop_exits(report):
                        raise typer.Exit(code=_EXIT_REFUSED)
                return
            with llm_failures():
                items = asyncio.run(_collect_scores(session.service, ids))
            report = session.service.last_report
        place = {posting_id: number for number, posting_id in enumerate(ids)}
        for item in sorted(items, key=lambda each: place[each.posting.id]):
            for line in score_lines(item):
                typer.echo(line)
        if report is None:
            return
        for note in report_notes(report):
            say_note(note)
        typer.echo(score_summary(report), err=True)
        if stop_exits(report):
            raise typer.Exit(code=_EXIT_REFUSED)


def _trace_row(summary: TraceSummary) -> str:
    """Write a trace as one tab-separated line."""
    return (
        f"{summary.trace_id}\t{summary.started.isoformat()}\t{summary.operation}"
        f"\t{summary.calls}\t${summary.cost_usd:.4f}\t{summary.duration_ms:.0f}"
    )


def _cost_text(cost: object) -> str:
    """Write a span's cost in dollars, or a dash when it recorded none."""
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        return f"${cost:.4f}"
    return "-"


def _span_lines(node: SpanNode, depth: int) -> Iterator[str]:
    """Write a span and the spans under it, two spaces further in per level.

    A span of a model call also holds its input and output tokens and its cost.
    """
    span = node.span
    attributes = span.attributes
    milliseconds = f"{(span.end_ns - span.start_ns) / _NS_PER_MS:.0f} ms"
    name = f"{'  ' * depth}{span.name}"
    if "gen_ai.request.model" in attributes:
        fields = [
            f"in {attributes.get('gen_ai.usage.input_tokens', '-')}",
            f"out {attributes.get('gen_ai.usage.output_tokens', '-')}",
            _cost_text(attributes.get("gen_ai.usage.cost")),
        ]
        yield "\t".join([name, *fields, milliseconds])
    else:
        yield f"{name}\t{milliseconds}"
    for child in node.children:
        yield from _span_lines(child, depth + 1)


def _export_lines(report: "ExportReport") -> list[str]:
    """Write what an export did, and the runs and results it left out.

    The skipped lines appear only when a count is not 0, so a dropped run shows.
    """
    lines = [
        f"sent {report.spans_sent} spans",
        f"datasets: {report.datasets_added} added",
        f"examples: {report.examples_added} added, {report.examples_present} present",
        f"experiments: {report.experiments_added} added",
        f"runs: {report.runs_added} added, {report.runs_present} present",
        f"scores: {report.scores_sent} sent",
    ]
    if report.runs_skipped:
        lines.append(f"skipped {report.runs_skipped} runs: their set has no dataset")
    if report.results_skipped:
        lines.append(
            f"skipped {report.results_skipped} results: no dataset example "
            "carries their posting"
        )
    return lines


def traces_app(open_session: SessionOpener) -> typer.Typer:
    """Build the `traces` command group.

    Args:
        open_session: Opens the service for a command.

    Returns:
        The group: with no subcommand it lists the recorded traces, `show`
        prints one as a tree of spans, `prune` trims the trace database and
        `export` sends the traces and the eval records to the local Phoenix.
    """
    group = typer.Typer(
        add_completion=False,
        help="List and read the traces of model calls.",
        invoke_without_command=True,
    )

    @group.callback()
    def list_traces(
        context: typer.Context,
        limit: Annotated[
            int, typer.Option(min=1, max=_MAX_TRACES, help="Most traces to list.")
        ] = _DEFAULT_TRACES,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """List the recorded traces, newest first."""
        if context.invoked_subcommand is not None:
            return
        with open_session(db, None, models=False) as session:
            summaries = session.service.traces(limit)
        if is_terminal():
            make_console().print(traces_table(summaries))
            return
        for summary in summaries:
            typer.echo(_trace_row(summary))

    @group.command("show")
    def show_trace(
        trace_id: Annotated[
            str, typer.Argument(metavar="ID", help="Trace id, as `traces` prints it.")
        ],
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """Print one trace as a tree of spans."""
        with open_session(db, None, models=False) as session, llm_failures():
            tree = session.service.trace(trace_id)
        if is_terminal():
            make_console().print(trace_tree(tree.roots))
            return
        for root in tree.roots:
            for line in _span_lines(root, 0):
                typer.echo(line)

    @group.command("prune")
    def prune_traces(
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """Delete the oldest traces until the trace database fits its size limit."""
        with open_session(db, None, models=False) as session, llm_failures():
            report = session.service.prune_traces()
        megabytes = report.size_bytes / _BYTES_PER_MB
        say_done(
            f"pruned {report.removed} traces, size {megabytes:.1f} MB",
            f"Pruned {report.removed} traces",
            f"{megabytes:.1f} MB now",
        )

    @group.command("export")
    def export_traces(
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Send traces, labels, eval runs and scores to the local Phoenix."""
        # `models=True` so that `--config` reaches the Phoenix settings.
        with open_session(db, config, models=True) as session, llm_failures():
            try:
                report = session.service.traces_export()
            except sqlite3.Error as error:
                say_error(f"cannot read the trace database: {error}", literal=True)
                raise typer.Exit(code=_EXIT_REFUSED) from error
        for line in _export_lines(report):
            typer.echo(line)

    return group
