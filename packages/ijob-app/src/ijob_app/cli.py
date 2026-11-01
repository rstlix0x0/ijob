"""The `ijob` command line.

`make_app` builds the typer application around a factory for the
`PoliteClient` every request goes through, so tests inject a client over a mock
transport and `app` uses the real network, and around a clock that tests can
control. `main` runs `app`.

Everything ijob keeps lives in one home directory, `$IJOB_HOME` or `~/.ijob`:
`config.toml`, `ijob.db` and `cache/http.sqlite`. `ijob setup` creates it, owner
access only, with a commented `config.toml` written only when absent and a
migrated, seeded database, and prints each item as `created` or `already there`
beside its path; it can be run again at any time and exits `1` when the home
cannot be created. Every other command first requires the home to be a
directory and otherwise exits `1` with `error: ijob home not found at <path>,
run: ijob setup`, creating nothing. `--db` and `--config` replace the database
and the config file inside the home.

`ijob search` lists the open, filtered postings of the sources in the database's
table. It validates every flag and the config file before it touches the cache,
the database or the network, then opens the HTTP cache and the database and
imports the bundled seed list. It skips a source fetched more recently than its
kind's refresh interval, fetches the rest through one client backed by the
cache, records what they show and prints the postings that pass the filters, one
tab-separated line each: the posting id, the title (after a `? ` flag when its
fields were parsed heuristically), company, location, the sources that showed
it and its URL. `--source` repeated limits
the run to the named sources; without it every source in the table is read.
Problems with a source go to stderr, and so does a notice for each source whose
last fetch is too old for its postings to show. The exit status is `0` when at
least one chosen source was fetched or kept, `1` when every chosen source
failed or the HTTP cache cannot be opened, and `2` for a flag, source reference
or config file that cannot be used.

`ijob tui` opens the terminal UI on the stored postings: the same filters as
`search`, a detail pane, refresh, votes and notes, the sources table and the CV.
It reads the config file and opens the database and HTTP cache before the screen
appears, so it exits `2` for a config file that cannot be used and `1` when the
cache cannot be opened. It takes `--db` and `--config`.

`ijob sources` manages the database's table of sources. Each `sources` command
imports the bundled seed list whenever it opens the database, which never
revives a source the user removed. `list` prints one tab-separated line per
source, `add` and `add-url` fetch a new source once to check it before keeping
it, and `remove` drops one. `add` and `add-url` refuse, before that fetch, a
reference already in the table for another company, since the table keeps the
company it first had. For those commands the exit status is `0` on success, `1`
when a source fails its check or its fetch, and `2` for a reference, company or
removal that cannot be acted on.

`ijob show ID` prints one stored posting: title, company, one `name: value` line
for each other field, the vote and note when it is labelled, then its
description, which is fetched and stored the first time it is wanted. A
description that cannot be had prints as `description unavailable: <reason>` and
the exit status stays `0`. `ijob label ID up|down [--note TEXT]` sets or
replaces the vote and prints the id, vote and note; `ijob label ID --clear`
removes it. `ijob cv set PATH` stores a PDF as the CV and prints its name, size
and SHA-256; `ijob cv show` prints the stored CV's name, size and stored time,
or `no CV stored`. All of these take the posting ids `search` prints and the same
`--db`. The exit status is `0` on success, `1` when the HTTP cache cannot be
opened, and `2` for an unknown posting, a vote or note that cannot be recorded,
a `label` call that gives both or neither of a vote and `--clear`, or a CV file
that cannot be stored.
"""

import asyncio
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Final

import typer
from ijob_core.errors import InvalidValueError
from ijob_core.model import Posting, PostingId, SourceRef, Vote, parse_source_ref
from ijob_sources.collect import SourceFailure
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    IdentityMismatchError,
    ParseError,
    RobotsDisallowedError,
    SourceNotFoundError,
    SourcesError,
)
from ijob_sources.polite_http import ConditionalCache, PoliteClient
from ijob_sources.registry import HOST_INTERVALS, refresh_interval

from ijob_app.config import FilterFlags, read_filters
from ijob_app.home import (
    HomeError,
    IjobHome,
    require_home,
    resolve_home,
    setup_home,
)
from ijob_app.render import posting_fields
from ijob_app.service import (
    CacheUnavailableError,
    ClientFactory,
    InvalidCvError,
    PostingDetail,
    RefreshReport,
    Service,
    SourceCompanyError,
    UnknownSourceError,
    open_service,
)
from ijob_app.tui import IjobApp

_EXIT_REFUSED: Final = 1
_EXIT_BAD_ARGUMENT: Final = 2

_DB_HELP: Final = (
    "Database file. Default: ijob.db in the ijob home, $IJOB_HOME or ~/.ijob."
)
_CONFIG_HELP: Final = (
    "Config file. Default: config.toml in the ijob home, $IJOB_HOME or ~/.ijob."
)


def _require_home() -> IjobHome:
    """Locate the ijob home and check that it exists.

    Every command but `setup` calls this first in its body, before any
    filesystem access, so a missing home leaves nothing behind.

    Returns:
        The home named by the process environment.

    Raises:
        typer.Exit: With status 1 after printing `error: <reason>`, if the home
            cannot be located or is not a directory.
    """
    try:
        home = resolve_home(os.environ)
        require_home(home)
    except HomeError as error:
        typer.echo(f"error: {error.reason}", err=True)
        raise typer.Exit(code=_EXIT_REFUSED) from error
    return home


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
        case IdentityMismatchError(reason=reason):
            return f"skipped {ref}: identity check failed: {reason}"
        case SourceNotFoundError():
            return f"not found: {ref}"
        case HttpStatusError(status=status):
            return f"failed {ref}: HTTP {status}"
        case FetchError(reason=reason) | ParseError(reason=reason):
            return f"failed {ref}: {reason}"
        case error:
            return f"failed {ref}: {error}"


def _production_client(cache: ConditionalCache | None) -> PoliteClient:
    """Build the client that talks to the real network.

    Args:
        cache: Where conditional requests keep their responses, or `None`.

    Returns:
        A client paced per host by the registry's `HOST_INTERVALS`.
    """
    return PoliteClient(cache=cache, host_intervals=HOST_INTERVALS)


def _utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def _read_ref(text: str) -> SourceRef:
    """Parse a source reference given on the command line.

    Args:
        text: The `<kind>:<key>` text.

    Returns:
        The reference.

    Raises:
        typer.Exit: With status 2 after printing the reason, if `text` is not a
            valid reference.
    """
    try:
        return parse_source_ref(text)
    except InvalidValueError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error


def _read_company(text: str) -> str:
    """Trim a company name and refuse a blank one.

    A blank company would leave a hiring-system board with nothing to be
    checked against.

    Args:
        text: The `--company` value.

    Returns:
        The trimmed name.

    Raises:
        typer.Exit: With status 2 after printing the reason, if nothing is left
            after trimming.
    """
    company = text.strip()
    if not company:
        typer.echo("error: --company must not be blank", err=True)
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
    return company


def _error_text(error: InvalidValueError) -> str:
    """Say why the service refused, as the command line reports it.

    Args:
        error: What the service raised.

    Returns:
        The bare reason for the service's own refusals of a source or a CV, and
        the full message for any other value error.
    """
    if isinstance(error, UnknownSourceError | SourceCompanyError | InvalidCvError):
        return error.reason
    return str(error)


@contextmanager
def _refusals() -> Iterator[None]:
    """Turn what a service call refuses into a message and an exit status.

    A cache that cannot be opened exits `1`, as it does for `search`; any other
    value error exits `2`.

    Raises:
        typer.Exit: After printing `error: <reason>` to stderr.
    """
    try:
        yield
    except CacheUnavailableError as error:
        typer.echo(f"error: {error.reason}", err=True)
        raise typer.Exit(code=_EXIT_REFUSED) from error
    except InvalidValueError as error:
        typer.echo(f"error: {_error_text(error)}", err=True)
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error


@contextmanager
def _source_failures() -> Iterator[None]:
    """Turn what adding a source raises into a message and an exit status.

    A value the service refuses exits `2`; a source that fails its check or its
    fetch exits `1`.

    Raises:
        typer.Exit: After printing `error: <reason>` to stderr.
    """
    try:
        yield
    except InvalidValueError as error:
        typer.echo(f"error: {_error_text(error)}", err=True)
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
    except SourcesError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=_EXIT_REFUSED) from error


def _sources_app(
    client_factory: ClientFactory, now: Callable[[], datetime]
) -> typer.Typer:
    """Build the `sources` command group.

    Args:
        client_factory: Builds the client a verification fetch goes through.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The group, with `list`, `add`, `remove` and `add-url`.
    """
    sources = typer.Typer(
        add_completion=False, help="List, add and remove job sources."
    )

    # Opens the service for a command that never reads through the cache.
    service_on = partial(
        open_service, client_factory=client_factory, now=now, use_cache=False
    )

    @sources.command("list")
    def list_sources(
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Print every source: reference, company, origin and last fetch."""
        with service_on(_require_home(), db) as service:
            rows = service.sources()
        for row in rows:
            fetched = row.last_fetched_at.isoformat() if row.last_fetched_at else "-"
            typer.echo(f"{row.ref}\t{row.company}\t{row.origin}\t{fetched}")

    @sources.command()
    def add(
        ref: Annotated[str, typer.Argument(help="Source reference, kind:key.")],
        company: Annotated[
            str, typer.Option(help="The company the source belongs to.")
        ],
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Verify a source with one fetch and add it."""
        home = _require_home()
        source_ref = _read_ref(ref)
        name = _read_company(company)
        with _source_failures(), service_on(home, db) as service:
            asyncio.run(service.add_source(source_ref, name))
        typer.echo(f"added {source_ref}")

    @sources.command()
    def remove(
        ref: Annotated[str, typer.Argument(help="Source reference, kind:key.")],
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Remove a source; it stays removed when the seeds are imported again."""
        home = _require_home()
        source_ref = _read_ref(ref)
        try:
            with service_on(home, db) as service:
                service.remove_source(source_ref)
        except UnknownSourceError as error:
            typer.echo(f"error: {error.reason}", err=True)
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
        typer.echo(f"removed {source_ref}")

    @sources.command("add-url")
    def add_url(
        url: Annotated[str, typer.Argument(help="A board, job page or feed URL.")],
        company: Annotated[
            str, typer.Option(help="The company the source belongs to.")
        ],
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Work out what a URL is, verify it and add it as a source."""
        home = _require_home()
        name = _read_company(company)
        with _source_failures(), service_on(home, db) as service:
            added = asyncio.run(service.add_url(url, name))
        typer.echo(f"added {added.ref}")

    return sources


def _report(report: RefreshReport) -> None:
    """Print the sources a refresh kept, then its problems, then its stale sources."""
    for row in report.kept:
        minutes = int(refresh_interval(row.ref.kind).total_seconds() // 60)
        typer.echo(f"kept {row.ref}: fetched less than {minutes} min ago", err=True)
    failures = {failure.ref: failure for failure in report.failures}
    for row in report.due:
        ref = row.ref
        if ref in report.refused:
            typer.echo(f"failed {ref}: {report.refused[ref]}", err=True)
        elif ref in failures:
            typer.echo(_failure_line(failures[ref]), err=True)
        elif (rejected := report.rejected.get(ref, 0)) > 0:
            typer.echo(f"{ref}: skipped {rejected} invalid jobs", err=True)
    for row in report.stale:
        if row.last_fetched_at is not None:
            day = row.last_fetched_at.date().isoformat()
            typer.echo(
                f"stale {row.ref}: last fetched {day}, postings hidden", err=True
            )


def _posting_line(service: Service, posting: Posting) -> str:
    """Format a posting: id, title, company, location, sources and URL, tab-separated.

    The `? ` flag of a low-confidence posting leads the title, after the id.
    """
    refs = ",".join(str(ref) for ref in service.posting_sources(posting.id))
    flag = "? " if posting.low_confidence else ""
    return (
        f"{posting.id}\t{flag}{posting.title}\t{posting.company}"
        f"\t{posting.location or '-'}\t{refs}\t{posting.url}"
    )


def _cv_app(client_factory: ClientFactory, now: Callable[[], datetime]) -> typer.Typer:
    """Build the `cv` command group.

    Args:
        client_factory: Builds the client the service holds; neither command
            makes a request.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The group, with `set` and `show`.
    """
    cv = typer.Typer(add_completion=False, help="Store and show your CV.")

    # Opens the service for a command that never reads through the cache.
    service_on = partial(
        open_service, client_factory=client_factory, now=now, use_cache=False
    )

    @cv.command("set")
    def set_cv(
        path: Annotated[Path, typer.Argument(help="The CV, a PDF of at most 20 MB.")],
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Keep a copy of your CV beside the database and record it."""
        home = _require_home()
        try:
            with _refusals(), service_on(home, db) as service:
                stored = service.store_cv(path)
        except OSError as error:
            typer.echo(f"error: cannot store the CV: {error}", err=True)
            raise typer.Exit(code=_EXIT_REFUSED) from error
        typer.echo(f"{stored.name}\t{stored.size}\t{stored.sha256}")

    @cv.command("show")
    def show_cv(
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Print the stored CV's name, size and the time it was stored."""
        with service_on(_require_home(), db) as service:
            stored = service.cv()
        if stored is None:
            typer.echo("no CV stored")
        else:
            typer.echo(f"{stored.name}\t{stored.size}\t{stored.stored_at.isoformat()}")

    return cv


def _detail_lines(detail: PostingDetail) -> list[str]:
    """Lay a posting's detail out as `show` prints it.

    Args:
        detail: The posting with its sources, label and description.

    Returns:
        The title, company and one `name: value` line per other field, with
        `label` and `note` only when the posting is labelled, then a blank line
        and the description or `description unavailable: <reason>`.
    """
    posting = detail.posting
    lines = [
        posting.title,
        posting.company,
        *(f"{name}: {value}" for name, value in posting_fields(detail)),
        f"url: {posting.url}",
    ]
    if detail.label is not None:
        lines.append(f"label: {detail.label.vote}")
        if detail.label.note is not None:
            lines.append(f"note: {detail.label.note}")
    lines.append("")
    if detail.description is not None:
        lines.append(detail.description)
    else:
        reason = detail.unavailable_reason or "no description published"
        lines.append(f"description unavailable: {reason}")
    return lines


def _posting_commands(
    app: typer.Typer, client_factory: ClientFactory, now: Callable[[], datetime]
) -> None:
    """Add the `show` and `label` commands, which act on one stored posting.

    Args:
        app: The application to add the commands to.
        client_factory: Builds the client a description fetch goes through.
        now: The wall clock, returning timezone-aware times.
    """

    def service_on(home: IjobHome, db: Path | None) -> AbstractContextManager[Service]:
        """Open the service with the HTTP cache, which description fetches use."""
        return open_service(home, db, client_factory, now=now)

    @app.command()
    def show(
        posting_id: Annotated[
            int, typer.Argument(metavar="ID", help="Posting id, as `search` prints it.")
        ],
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Print a posting's details and its description."""
        home = _require_home()
        with _refusals(), service_on(home, db) as service:
            detail = asyncio.run(service.detail(PostingId(posting_id)))
        for line in _detail_lines(detail):
            typer.echo(line)

    @app.command()
    def label(
        posting_id: Annotated[
            int, typer.Argument(metavar="ID", help="Posting id, as `search` prints it.")
        ],
        vote: Annotated[
            Vote | None, typer.Argument(help="Your verdict: up or down.")
        ] = None,
        *,
        note: Annotated[str | None, typer.Option(help="Why you voted so.")] = None,
        clear: Annotated[
            bool, typer.Option("--clear", help="Remove the posting's vote.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Set, replace or clear your vote on a posting."""
        home = _require_home()
        if (vote is None) == (not clear) or (clear and note is not None):
            typer.echo(
                "error: give a vote (up or down) with an optional --note, "
                "or --clear alone",
                err=True,
            )
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
        identifier = PostingId(posting_id)
        with _refusals(), service_on(home, db) as service:
            if vote is None:
                service.clear_label(identifier)
                typer.echo(f"{identifier}\tcleared")
                return
            detail = asyncio.run(service.label(identifier, vote, note))
        if detail.label is not None:
            typer.echo(f"{identifier}\t{detail.label.vote}\t{detail.label.note or '-'}")


def make_app(
    client_factory: ClientFactory, *, now: Callable[[], datetime] = _utc_now
) -> typer.Typer:
    """Build the `ijob` application.

    Args:
        client_factory: Builds a new `PoliteClient` each time a command needs to
            fetch; it is given the HTTP cache the client should use, or `None`
            for none.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The application, with its `setup`, `search`, `show`, `label` and `tui`
        commands and its `sources` and `cv` groups.
    """
    app = typer.Typer(add_completion=False, help="Collect and list job postings.")
    app.add_typer(_sources_app(client_factory, now), name="sources")
    app.add_typer(_cv_app(client_factory, now), name="cv")
    _posting_commands(app, client_factory, now)

    @app.callback()
    def _root() -> None:
        """Collect and list job postings."""

    @app.command()
    def setup() -> None:
        """Create the ijob home with its config, database and cache directory."""
        try:
            items = setup_home(resolve_home(os.environ))
        except HomeError as error:
            typer.echo(f"error: {error.reason}", err=True)
            raise typer.Exit(code=_EXIT_REFUSED) from error
        except (OSError, sqlite3.Error) as error:
            typer.echo(f"error: cannot set up the ijob home: {error}", err=True)
            raise typer.Exit(code=_EXIT_REFUSED) from error
        for item in items:
            typer.echo(f"{item.outcome}\t{item.path}")
            if item.exposed_mode is not None:
                typer.echo(
                    f"warning: {item.path} has mode {item.exposed_mode:04o}, so "
                    f"other users can read it; run: chmod 700 {item.path}",
                    err=True,
                )

    @app.command()
    def search(  # noqa: PLR0913, PLR0917 - one parameter per command-line option
        source: Annotated[
            list[str] | None,
            typer.Option(
                help="Read only this source, as kind:key. Repeat for several. "
                "Default: every source in the database."
            ),
        ] = None,
        country: Annotated[
            list[str] | None,
            typer.Option(help="Keep postings in this country, by code or name."),
        ] = None,
        remote: Annotated[
            list[str] | None,
            typer.Option(help="Keep postings with this remote scope."),
        ] = None,
        since: Annotated[
            str | None,
            typer.Option(help="Keep postings published within <n>d or <n>h."),
        ] = None,
        company: Annotated[
            list[str] | None,
            typer.Option(help="Keep postings whose company contains this text."),
        ] = None,
        min_salary: Annotated[
            str | None,
            typer.Option(help="Keep postings paying at least AMOUNT:CURRENCY:PERIOD."),
        ] = None,
        require_salary: Annotated[
            bool | None,
            typer.Option(
                "--require-salary/--no-require-salary",
                help="Drop postings that state no salary.",
            ),
        ] = None,
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
        config: Annotated[
            Path | None,
            typer.Option(help=_CONFIG_HELP),
        ] = None,
    ) -> None:
        """Fetch the sources, record their jobs and list the open, filtered ones.

        Each line starts with the posting's id, which `show` and `label` take.
        """
        home = _require_home()
        flags = FilterFlags(
            countries=tuple(country or ()),
            remotes=tuple(remote or ()),
            since=since,
            companies=tuple(company or ()),
            min_salary=min_salary,
            require_salary=require_salary,
        )
        try:
            spec = read_filters(config, flags, default=home.config)
            refs = list(dict.fromkeys(map(parse_source_ref, source or [])))
        except InvalidValueError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error

        try:
            with open_service(home, db, client_factory, now=now) as service:
                report = asyncio.run(service.refresh(refs))
                _report(report)
                for posting in service.open_postings(spec, refs):
                    typer.echo(_posting_line(service, posting))
        except CacheUnavailableError as error:
            typer.echo(f"error: {error.reason}", err=True)
            raise typer.Exit(code=_EXIT_REFUSED) from error
        except UnknownSourceError as error:
            typer.echo(f"error: {error.reason}", err=True)
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
        if report.due and not report.fetched and not report.kept:
            raise typer.Exit(code=_EXIT_REFUSED)

    @app.command()
    def tui(
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
        config: Annotated[
            Path | None,
            typer.Option(help=_CONFIG_HELP),
        ] = None,
    ) -> None:
        """Open the terminal UI on the stored postings."""
        home = _require_home()
        with (
            _refusals(),
            open_service(home, db, client_factory, now=now) as service,
        ):
            IjobApp(service, config, default_config=home.config).run()

    return app


app = make_app(_production_client)


def main() -> None:
    """Run the `ijob` command line."""
    app()
