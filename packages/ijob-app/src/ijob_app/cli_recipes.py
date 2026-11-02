"""The command-line side of learned source recipes.

`learn_page` is what `ijob sources add-url` does when a page is no hiring
system, job posting or feed: it has the `source_recipe` model learn a recipe,
shows what the recipe reads and stores the source once the user confirms.
`reanalyze_command` adds `ijob sources reanalyze`, which learns a stored recipe
again and replaces it on confirmation. `recipe_failures` turns what either
raises into a message and an exit status, and `confirm_and_store` is the
confirmation both share. Of the LLM layer this module imports only its error
classes, so loading it pulls in neither DSPy nor the Claude SDK.
"""

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Annotated, Final

import typer
from ijob_ai.errors import LlmError
from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceRef
from ijob_sources.errors import SourcesError, UnrecognizedPageError

from ijob_app.cli_llm import CONFIG_HELP, DB_HELP, report_notes
from ijob_app.llm_ops import cost_line
from ijob_app.llm_text import recipe_proposal_lines
from ijob_app.recipe_ops import RecipeProposal
from ijob_app.render import error_notes, error_text
from ijob_app.service import Service

_EXIT_REFUSED: Final = 1
_EXIT_BAD_ARGUMENT: Final = 2

_CONFIRM_PROMPT: Final = "Store this source? [y/N]"
_AFFIRMATIVE: Final = frozenset({"y", "yes"})
_NO_MODEL_NOTE: Final = "note: set [models].source_recipe to let ijob learn this page"


@contextmanager
def recipe_failures() -> Iterator[None]:
    """Turn what learning or storing a recipe raises into a message and an exit.

    A value the service refuses, such as a source that has no recipe or a config
    setting out of range, exits `2`; a source that cannot be read, every error
    of the LLM layer and a database that cannot be read or written exit `1`.
    Each note the error carries, such as what a failed analysis cost, is printed
    on its own line after the `error:` line.

    Raises:
        typer.Exit: After printing `error: <message>` and the notes to stderr.
    """
    try:
        yield
    except InvalidValueError as error:
        typer.echo(f"error: {error_text(error)}", err=True)
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
    except (SourcesError, LlmError, sqlite3.Error) as error:
        typer.echo(f"error: {error}", err=True)
        for note in error_notes(error):
            typer.echo(note, err=True)
        raise typer.Exit(code=_EXIT_REFUSED) from error


def _require_confirmation(*, yes: bool, is_interactive: Callable[[], bool]) -> None:
    """Refuse to go on when nobody can confirm the source.

    Raises:
        typer.Exit: With status 1 after printing the reason, when `--yes` is
            absent and the user cannot be asked.
    """
    if not yes and not is_interactive():
        typer.echo("error: confirmation needed; pass --yes", err=True)
        raise typer.Exit(code=_EXIT_REFUSED)


def _show(proposal: RecipeProposal) -> None:
    """Print what a recipe reads, then what learning it cost and any warnings."""
    for line in recipe_proposal_lines(proposal):
        typer.echo(line)
    typer.echo(cost_line(proposal.report), err=True)
    for note in report_notes(proposal.report):
        typer.echo(note, err=True)


def confirm_and_store(
    service: Service,
    proposal: RecipeProposal,
    *,
    yes: bool,
    is_interactive: Callable[[], bool],
) -> None:
    """Store a proposed recipe once the user confirms it.

    Args:
        service: The open service.
        proposal: The recipe to store.
        yes: Whether to store without asking.
        is_interactive: Whether the user can be asked a question.

    Raises:
        typer.Exit: With status 1 when the user cannot be asked and `yes` is
            false, or declines (`not stored` is printed); with status 1 or 2
            when the store refuses the recipe.
    """
    if not yes:
        _require_confirmation(yes=yes, is_interactive=is_interactive)
        answer = typer.prompt(
            _CONFIRM_PROMPT, default="", show_default=False, prompt_suffix=" "
        )
        if answer.strip().lower() not in _AFFIRMATIVE:
            typer.echo("not stored")
            raise typer.Exit(code=_EXIT_REFUSED)
    with recipe_failures():
        row = service.accept_recipe(proposal)
    typer.echo(f"added {row.ref}")


def learn_page(  # noqa: PLR0913 - the page, the error and both confirmation inputs
    service: Service,
    url: str,
    company: str,
    unrecognized: UnrecognizedPageError,
    *,
    yes: bool,
    is_interactive: Callable[[], bool],
) -> None:
    """Learn a recipe for a page no source kind recognized, and store it if confirmed.

    The steps run in this order, so nothing is spent before it can be used: the
    model must be set, the user must be able to confirm, the model learns the
    recipe, and the proposal is shown and confirmed.

    Args:
        service: The open service.
        url: The page the user gave.
        company: The company the source belongs to.
        unrecognized: Why `add_url` refused the page, printed when no model is
            set to learn it; the page it holds is learned from without being
            fetched again.
        yes: Whether to store without asking.
        is_interactive: Whether the user can be asked a question.

    Raises:
        typer.Exit: With status 1 or 2 after printing the reason, when the page
            cannot be learned, is not confirmed or cannot be stored.
    """
    with recipe_failures():
        learns = service.learns_recipes()
    if not learns:
        typer.echo(f"error: {unrecognized}", err=True)
        typer.echo(_NO_MODEL_NOTE, err=True)
        raise typer.Exit(code=_EXIT_REFUSED)
    _require_confirmation(yes=yes, is_interactive=is_interactive)
    with recipe_failures():
        proposal = asyncio.run(
            service.analyze_url(url, company, page=unrecognized.page)
        )
    _show(proposal)
    confirm_and_store(service, proposal, yes=yes, is_interactive=is_interactive)


def reanalyze_command(
    group: typer.Typer,
    *,
    open_session: Callable[[Path | None, Path | None], AbstractContextManager[Service]],
    read_ref: Callable[[str], SourceRef],
    is_interactive: Callable[[], bool],
) -> None:
    """Add `reanalyze` to the `sources` group.

    Args:
        group: The `sources` group.
        open_session: Locates the ijob home when called, so a missing home is
            refused first, and gives the context that opens the service with
            LLM options for the `--db` and `--config` values.
        read_ref: Parses a source reference, exiting `2` when it is invalid.
        is_interactive: Whether the user can be asked a question.
    """

    @group.command()
    def reanalyze(
        ref: Annotated[str, typer.Argument(help="Recipe source reference, kind:key.")],
        *,
        yes: Annotated[
            bool, typer.Option("--yes", help="Store without asking.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Learn a source's recipe again and replace the stored one if you confirm."""
        session = open_session(db, config)
        source_ref = read_ref(ref)
        _require_confirmation(yes=yes, is_interactive=is_interactive)
        with session as service:
            with recipe_failures():
                proposal = asyncio.run(service.analyze_source(source_ref))
            _show(proposal)
            confirm_and_store(service, proposal, yes=yes, is_interactive=is_interactive)
