"""The command-line side of reading the jobs a source's parser could not read.

`extract_command` adds `ijob sources extract`, which has the `job_extract` model
read each skipped item once and stores the jobs it finds. `unread_note` is the
hint `search` prints when items are waiting. Of the LLM layer this module
imports only its error classes, so loading it pulls in neither DSPy nor the
Claude SDK.
"""

import asyncio
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Final

import typer
from ijob_ai.errors import MissingModelError
from ijob_core.model import SourceRef

from ijob_app.cli_llm import CONFIG_HELP, DB_HELP, report_notes
from ijob_app.cli_progress import ProgressLine
from ijob_app.cli_recipes import recipe_failures
from ijob_app.extract_ops import ExtractProgress, ExtractReport
from ijob_app.llm_ops import cost_line
from ijob_app.service import Service

_EXIT_REFUSED: Final = 1
_NO_MODEL_NOTE: Final = "note: set [models].job_extract to let ijob read skipped jobs"


def unread_note(count: int) -> str | None:
    """Write the hint that skipped jobs wait for a model, or `None` when none do."""
    if count <= 0:
        return None
    return f"note: {count} skipped jobs unread; run: ijob sources extract"


def _progress_text(progress: ExtractProgress) -> str:
    """Write how far an extract run has got."""
    return (
        f"read {progress.read} of {progress.total}: "
        f"{progress.added} jobs, {progress.not_jobs} not jobs, "
        f"{progress.unread} unread"
    )


def extract_lines(result: ExtractReport) -> list[str]:
    """Write the summary line of an extract run and a line per item left unread."""
    lines = [
        (
            f"{result.added} jobs added, {result.not_jobs} not jobs, "
            f"{len(result.failed)} still unread"
        )
    ]
    lines.extend(
        f"unread {failure.row.source} {failure.preview!r}: {failure.reason}"
        for failure in result.failed
    )
    return lines


def extract_command(
    group: typer.Typer,
    *,
    open_session: Callable[[Path | None, Path | None], AbstractContextManager[Service]],
    read_ref: Callable[[str], SourceRef],
) -> None:
    """Add `extract` to the `sources` group.

    Args:
        group: The `sources` group.
        open_session: Locates the ijob home when called and gives the context
            that opens the service with LLM options for the `--db` and
            `--config` values.
        read_ref: Parses a source reference, exiting `2` when it is invalid.
    """

    @group.command()
    def extract(
        refs: Annotated[
            list[str] | None,
            typer.Argument(help="Source references, kind:key; none means all."),
        ] = None,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Have the job_extract model read each job a parser skipped, once."""
        session = open_session(db, config)
        chosen: Sequence[SourceRef] = list(dict.fromkeys(map(read_ref, refs or [])))
        progress = ProgressLine(sys.stderr)
        with session as service, recipe_failures():
            try:
                result = asyncio.run(
                    service.extract_skipped(
                        chosen,
                        on_progress=lambda now: progress.show(_progress_text(now)),
                    )
                )
            except MissingModelError as error:
                typer.echo(f"error: {error}", err=True)
                typer.echo(_NO_MODEL_NOTE, err=True)
                raise typer.Exit(code=_EXIT_REFUSED) from error
            finally:
                progress.close()
        for line in extract_lines(result):
            typer.echo(line)
        typer.echo(cost_line(result.report), err=True)
        for note in report_notes(result.report):
            typer.echo(note, err=True)
