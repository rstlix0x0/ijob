"""The `ijob` command line.

`make_app` builds the typer application around a factory for the
`PoliteClient` every request goes through, so tests inject a client over a mock
transport and `app` uses the real network. `main` runs `app`.

`ijob search --greenhouse <keys>` fetches each named Greenhouse board, records
what it shows in the local database, and prints the postings of the boards that
were read. Problems with a board go to stderr; the exit status is `0` when at
least one board was read, `1` when none was, and `2` for a key that is not a
valid board token.
"""

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import Annotated

import typer
from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceKey, SourceKind, SourceRef, parse_source_key
from ijob_core.store import Store, default_db_path
from ijob_sources.collect import CollectResult, SourceFailure, collect
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    ParseError,
    RobotsDisallowedError,
    SourceNotFoundError,
)
from ijob_sources.greenhouse import GreenhouseBoard
from ijob_sources.polite_http import PoliteClient

_EXIT_ALL_FAILED = 1
_EXIT_BAD_KEY = 2


def _parse_keys(text: str) -> list[SourceKey]:
    """Split a comma-separated list of board keys and validate each one.

    Args:
        text: The keys, separated by commas, each optionally padded with
            whitespace.

    Returns:
        The distinct keys, in the order they first appear.

    Raises:
        InvalidValueError: If any item is not a valid source key.
    """
    keys = [parse_source_key(item.strip()) for item in text.split(",")]
    return list(dict.fromkeys(keys))


def _failure_line(failure: SourceFailure) -> str:
    """Describe a failed source for the person running the command.

    Args:
        failure: The source that failed and why.

    Returns:
        One line naming the source and the reason.
    """
    ref = failure.ref
    match failure.error:
        case RobotsDisallowedError():
            return f"skipped {ref}: disallowed by robots.txt"
        case SourceNotFoundError():
            return f"not found: {ref}"
        case HttpStatusError(status=status):
            return f"failed {ref}: HTTP {status}"
        case FetchError(reason=reason) | ParseError(reason=reason):
            return f"failed {ref}: {reason}"
        case error:
            return f"failed {ref}: {error}"


async def _collect(
    boards: list[GreenhouseBoard], client_factory: Callable[[], PoliteClient]
) -> CollectResult:
    """Fetch every board with one client, opened and closed on this event loop.

    Args:
        boards: The boards to fetch.
        client_factory: Builds the client the requests go through.

    Returns:
        What `collect` returned for the boards.
    """
    async with client_factory() as client:
        return await collect(boards, client)


def make_app(client_factory: Callable[[], PoliteClient]) -> typer.Typer:
    """Build the `ijob` application.

    Args:
        client_factory: Builds a new `PoliteClient` each time a command needs to
            fetch.

    Returns:
        The application, with its `search` command.
    """
    app = typer.Typer(add_completion=False, help="Collect and list job postings.")

    @app.callback()
    def _root() -> None:
        """Collect and list job postings."""

    @app.command()
    def search(
        greenhouse: Annotated[
            str,
            typer.Option(help="Greenhouse board keys, separated by commas."),
        ],
        db: Annotated[
            Path | None,
            typer.Option(
                help="Database file. Default: $XDG_DATA_HOME/ijob/ijob.db, "
                "or ~/.local/share/ijob/ijob.db."
            ),
        ] = None,
    ) -> None:
        """Fetch Greenhouse boards, record their jobs and list them."""
        try:
            keys = _parse_keys(greenhouse)
        except InvalidValueError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=_EXIT_BAD_KEY) from error
        boards = [GreenhouseBoard(key) for key in keys]
        refs = [SourceRef(SourceKind.GREENHOUSE, key) for key in keys]

        with Store.open(db or default_db_path(os.environ)) as store:
            result = asyncio.run(_collect(boards, client_factory))
            succeeded = [ref for ref in refs if ref in result.batches]
            drafts = chain.from_iterable(
                result.batches[ref].drafts for ref in succeeded
            )
            store.record(drafts, seen_at=datetime.now(UTC))
            postings = store.postings(sources=succeeded)
        for posting in postings:
            typer.echo(
                f"{posting.title}\t{posting.company}\t{posting.location or '-'}"
                f"\t{posting.url}"
            )

        failures = {failure.ref: failure for failure in result.failures}
        for ref in refs:
            if ref in failures:
                typer.echo(_failure_line(failures[ref]), err=True)
            elif (rejected := result.batches[ref].rejected) > 0:
                typer.echo(f"{ref}: skipped {rejected} invalid jobs", err=True)
        if not succeeded:
            raise typer.Exit(code=_EXIT_ALL_FAILED)

    return app


app = make_app(PoliteClient)


def main() -> None:
    """Run the `ijob` command line."""
    app()
