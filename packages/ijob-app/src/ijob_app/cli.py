"""The `ijob` command line.

`make_app` builds the typer application around a factory for the
`PoliteClient` every request goes through, so tests inject a client over a mock
transport and `app` uses the real network, and around a clock that tests can
control. `main` runs `app`.

`ijob search` lists the open, filtered postings of the sources in the database's
table. It validates every flag and the config file before it touches the cache,
the database or the network, then opens the HTTP cache and the database and
imports the bundled seed list. It skips a source fetched more recently than its
kind's refresh interval, fetches the rest through one client backed by the
cache, records what they show and prints the postings that pass the filters,
with the sources that showed each. `--source` repeated limits
the run to the named sources; without it every source in the table is read.
Problems with a source go to stderr, and so does a notice for each source whose
last fetch is too old for its postings to show. The exit status is `0` when at
least one chosen source was fetched or kept, `1` when every chosen source
failed or the HTTP cache cannot be opened, and `2` for a flag, source reference
or config file that cannot be used.

`ijob sources` manages the database's table of sources. Each `sources` command
imports the bundled seed list whenever it opens the database, which never
revives a source the user removed. `list` prints one tab-separated line per
source, `add` and `add-url` fetch a new source once to check it before keeping
it, and `remove` drops one. `add` and `add-url` refuse, before that fetch, a
reference already in the table for another company, since the table keeps the
company it first had. For those commands the exit status is `0` on success, `1`
when a source fails its check or its fetch, and `2` for a reference, company or
removal that cannot be acted on.
"""

import asyncio
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import typer
from ijob_core.dedupe import normalise_company
from ijob_core.errors import InvalidValueError
from ijob_core.filters import FilterSpec, keep
from ijob_core.model import ExternalId, Posting, SourceRef, parse_source_ref
from ijob_core.store import SourceOrigin, SourceRow, Store, default_db_path
from ijob_sources.collect import CollectResult, Source, SourceFailure, collect
from ijob_sources.detect import Resolution, resolve_url, verify_new_source
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    IdentityMismatchError,
    ParseError,
    RobotsDisallowedError,
    SourceNotFoundError,
    SourcesError,
)
from ijob_sources.http_cache import SqliteHttpCache, default_cache_path
from ijob_sources.polite_http import ConditionalCache, PoliteClient
from ijob_sources.registry import HOST_INTERVALS, build_source, refresh_interval
from ijob_sources.seeds import load_seeds

from ijob_app.config import (
    default_config_path,
    load_filters,
    parse_country_flag,
    parse_min_salary,
    parse_remote,
    parse_since,
)

_EXIT_REFUSED: Final = 1
_EXIT_BAD_ARGUMENT: Final = 2

type _ClientFactory = Callable[[ConditionalCache | None], PoliteClient]

_DB_HELP = (
    "Database file. Default: $XDG_DATA_HOME/ijob/ijob.db, "
    "or ~/.local/share/ijob/ijob.db."
)


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


async def _collect(
    sources: list[Source], client_factory: _ClientFactory, cache: ConditionalCache
) -> CollectResult:
    """Fetch every source with one client, opened and closed on this event loop.

    Args:
        sources: The sources to fetch.
        client_factory: Builds the client the requests go through.
        cache: The HTTP cache the client revalidates responses against.

    Returns:
        What `collect` returned for the sources.
    """
    async with client_factory(cache) as client:
        return await collect(sources, client)


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


@contextmanager
def _seeded_store(db: Path | None) -> Iterator[Store]:
    """Open the database with the bundled seed sources imported.

    Args:
        db: The database file, or `None` for the default location.

    Yields:
        The open store, closed on exit.
    """
    with Store.open(db or default_db_path(os.environ)) as store:
        store.import_seeds(load_seeds())
        yield store


@contextmanager
def _open_cache(now: Callable[[], datetime]) -> Iterator[SqliteHttpCache]:
    """Open the HTTP cache at its default location.

    Args:
        now: The wall clock the cache ages its entries by.

    Yields:
        The open cache, closed on exit.

    Raises:
        typer.Exit: With status 1 after printing the reason, if the cache file
            cannot be opened.
    """
    path = default_cache_path(os.environ)
    try:
        cache = SqliteHttpCache.open(path, now=now)
    except (OSError, sqlite3.Error) as error:
        typer.echo(f"error: cannot open the HTTP cache {path}: {error}", err=True)
        raise typer.Exit(code=_EXIT_REFUSED) from error
    with cache:
        yield cache


def _new_row(ref: SourceRef, company: str, target_url: str | None) -> SourceRow:
    """Describe a source about to be added, so the registry can build it."""
    return SourceRow(
        ref=ref,
        company=company,
        target_url=target_url,
        origin=SourceOrigin.USER,
        removed=False,
        last_fetched_at=None,
    )


async def _verify(row: SourceRow, client_factory: _ClientFactory) -> None:
    """Fetch a new source once, refusing it if it cannot be shown to be genuine.

    Args:
        row: The source about to be added.
        client_factory: Builds the client the request goes through.

    Raises:
        InvalidValueError: The registry cannot build the source from the row.
        SourcesError: The fetch or the identity check failed, or the fetch
            yielded no drafts: the source lists no jobs, or every job it lists
            was rejected.
    """
    source = build_source(row)
    async with client_factory(None) as client:
        await verify_new_source(source, client)


async def _resolve(
    url: str,
    company: str,
    client_factory: _ClientFactory,
    admit: Callable[[SourceRef], None],
) -> Resolution:
    """Resolve a URL and make sure the source it names has been verified.

    A resolution that carries a batch was read from the page itself and is
    already checked; one that does not is a hiring-system board, which is
    fetched once here, after `admit` has accepted its reference.

    Args:
        url: The URL the user pasted.
        company: The company the source is registered for.
        client_factory: Builds the client the requests go through.
        admit: Called with the resolved reference before a board is fetched; it
            raises to refuse the reference.

    Returns:
        The verified resolution.

    Raises:
        SourcesError: The URL is unusable, or the source fails its check.
    """
    async with client_factory(None) as client:
        resolution = await resolve_url(url, company=company, client=client)
        admit(resolution.ref)
        if resolution.batch is None:
            source = build_source(_new_row(resolution.ref, company, None))
            await verify_new_source(source, client)
    return resolution


def _refuse_other_company(db: Path | None, ref: SourceRef, company: str) -> None:
    """Refuse a reference the table already holds for a different company.

    The table keeps the company a reference first had, so adding it again under
    another name would verify against the new name and then store the old one.
    Companies are compared as `normalise_company` writes them.

    Args:
        db: The database file, or `None` for the default location.
        ref: The source about to be added.
        company: The company it is to be added for.

    Raises:
        typer.Exit: With status 2 after printing the reason, if `ref` is in the
            table, removed or not, for a different company.
    """
    with _seeded_store(db) as store:
        row = store.source(ref)
    if row is not None and normalise_company(row.company) != normalise_company(company):
        typer.echo(f"error: {ref} is already a source for {row.company}", err=True)
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT)


def _sources_app(client_factory: _ClientFactory) -> typer.Typer:
    """Build the `sources` command group.

    Args:
        client_factory: Builds the client a verification fetch goes through.

    Returns:
        The group, with `list`, `add`, `remove` and `add-url`.
    """
    sources = typer.Typer(
        add_completion=False, help="List, add and remove job sources."
    )

    @sources.command("list")
    def list_sources(
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Print every source: reference, company, origin and last fetch."""
        with _seeded_store(db) as store:
            rows = store.sources()
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
        source_ref = _read_ref(ref)
        name = _read_company(company)
        _refuse_other_company(db, source_ref, name)
        try:
            asyncio.run(_verify(_new_row(source_ref, name, None), client_factory))
        except InvalidValueError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
        except SourcesError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=_EXIT_REFUSED) from error
        with _seeded_store(db) as store:
            store.add_source(source_ref, name)
        typer.echo(f"added {source_ref}")

    @sources.command()
    def remove(
        ref: Annotated[str, typer.Argument(help="Source reference, kind:key.")],
        db: Annotated[Path | None, typer.Option(help=_DB_HELP)] = None,
    ) -> None:
        """Remove a source; it stays removed when the seeds are imported again."""
        source_ref = _read_ref(ref)
        with _seeded_store(db) as store:
            row = store.source(source_ref)
            if row is None or row.removed:
                typer.echo(f"error: unknown source {source_ref}", err=True)
                raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
            store.remove_source(source_ref)
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
        name = _read_company(company)

        def admit(ref: SourceRef) -> None:
            _refuse_other_company(db, ref, name)

        try:
            resolution = asyncio.run(_resolve(url, name, client_factory, admit))
        except SourcesError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=_EXIT_REFUSED) from error
        with _seeded_store(db) as store:
            store.add_source(resolution.ref, name, resolution.target_url)
        typer.echo(f"added {resolution.ref}")

    return sources


@dataclass(frozen=True, slots=True)
class _FilterFlags:
    """The filter values given on the command line.

    Attributes:
        countries: The `--country` values; empty when none was given.
        remotes: The `--remote` values; empty when none was given.
        since: The `--since` value, or `None`.
        companies: The `--company` values; empty when none was given.
        min_salary: The `--min-salary` value, or `None`.
        require_salary: The `--require-salary` or `--no-require-salary` choice,
            or `None` when neither was given.
    """

    countries: tuple[str, ...]
    remotes: tuple[str, ...]
    since: str | None
    companies: tuple[str, ...]
    min_salary: str | None
    require_salary: bool | None


def _read_filters(config: Path | None, flags: _FilterFlags) -> FilterSpec:
    """Build the search filters from the config file with the flags laid over it.

    Args:
        config: The config file the user named, or `None` for the default
            location.
        flags: The filter values from the command line; each one given replaces
            the config's value.

    Returns:
        The filters.

    Raises:
        InvalidValueError: The config file or a flag value is unusable; a
            blank `--company` is one.
    """
    path = config or default_config_path(os.environ)
    spec = load_filters(path, explicit=config is not None)
    if flags.countries:
        spec = replace(
            spec, countries=frozenset(map(parse_country_flag, flags.countries))
        )
    if flags.remotes:
        spec = replace(spec, remote=frozenset(map(parse_remote, flags.remotes)))
    if flags.since is not None:
        spec = replace(spec, since=parse_since(flags.since))
    if flags.companies:
        if not all(name.strip() for name in flags.companies):
            raise InvalidValueError("company", "must not be blank")
        spec = replace(spec, companies=flags.companies)
    if flags.min_salary is not None:
        spec = replace(spec, min_salary=parse_min_salary(flags.min_salary))
    if flags.require_salary is not None:
        spec = replace(spec, require_salary=flags.require_salary)
    return spec


def _choose_sources(store: Store, refs: list[SourceRef]) -> list[SourceRow]:
    """Pick the sources a search reads.

    Args:
        store: The open store.
        refs: The sources the user named; empty means every source.

    Returns:
        The rows to read, in the order the user named them, or in `ref` order
        when none was named.

    Raises:
        typer.Exit: With status 2 after printing a hint, if a named source has no
            row or was removed.
    """
    rows = store.sources()
    if not refs:
        return rows
    by_ref = {row.ref: row for row in rows}
    for ref in refs:
        if ref not in by_ref:
            typer.echo(
                f"error: unknown source {ref}; "
                f"add it with: ijob sources add {ref} --company NAME",
                err=True,
            )
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
    return [by_ref[ref] for ref in refs]


def _split_by_interval(
    rows: list[SourceRow], at: datetime
) -> tuple[list[SourceRow], list[SourceRow]]:
    """Separate the sources due for a fetch from those fetched recently enough.

    A source that is kept is announced on stderr.

    Args:
        rows: The chosen sources.
        at: The time to measure from.

    Returns:
        The sources to fetch and the sources to keep as they are, each in the
        order of `rows`. A source is kept when its last fetch is less than its
        kind's refresh interval before `at`.
    """
    due: list[SourceRow] = []
    kept: list[SourceRow] = []
    for row in rows:
        interval = refresh_interval(row.ref.kind)
        if row.last_fetched_at is not None and at - row.last_fetched_at < interval:
            kept.append(row)
            minutes = int(interval.total_seconds() // 60)
            typer.echo(f"kept {row.ref}: fetched less than {minutes} min ago", err=True)
        else:
            due.append(row)
    return due, kept


def _build_sources(
    store: Store, rows: list[SourceRow]
) -> tuple[list[Source], dict[SourceRef, InvalidValueError]]:
    """Build the adapter of each row, setting aside the rows the registry refuses.

    Args:
        store: The open store, which knows the jobs each source already showed.
        rows: The sources to build.

    Returns:
        The adapters, and the reason each refused row could not be built.
    """
    sources: list[Source] = []
    refused: dict[SourceRef, InvalidValueError] = {}
    for row in rows:
        try:
            sources.append(build_source(row, known=store.external_ids(row.ref)))
        except InvalidValueError as error:
            refused[row.ref] = error
    return sources, refused


def _record(
    store: Store, rows: list[SourceRow], result: CollectResult, seen_at: datetime
) -> list[SourceRef]:
    """Store what the fetched sources showed and note when each was fetched.

    Both the postings and the fetch times are stamped with the one `seen_at`,
    which is what keeps a source's postings open afterwards.

    Args:
        store: The open store.
        rows: The sources that were due, in order.
        result: What fetching them returned.
        seen_at: When the fetch happened, timezone-aware.

    Returns:
        The sources that were fetched successfully, in the order of `rows`.
    """
    fetched = [row.ref for row in rows if row.ref in result.batches]
    batches = [result.batches[ref] for ref in fetched]
    still_listed: list[tuple[SourceRef, ExternalId]] = [
        (ref, external_id)
        for ref, batch in zip(fetched, batches, strict=True)
        for external_id in batch.still_listed
    ]
    store.record(
        (draft for batch in batches for draft in batch.drafts),
        seen_at=seen_at,
        still_listed=still_listed,
    )
    for ref in fetched:
        store.mark_fetched(ref, seen_at)
    return fetched


def _report_problems(
    rows: list[SourceRow],
    result: CollectResult,
    refused: dict[SourceRef, InvalidValueError],
) -> None:
    """Print, per source in order, why it failed or how many jobs it dropped."""
    failures = {failure.ref: failure for failure in result.failures}
    for row in rows:
        ref = row.ref
        if ref in refused:
            typer.echo(f"failed {ref}: {refused[ref]}", err=True)
        elif ref in failures:
            typer.echo(_failure_line(failures[ref]), err=True)
        elif (rejected := result.batches[ref].rejected) > 0:
            typer.echo(f"{ref}: skipped {rejected} invalid jobs", err=True)


def _report_stale(store: Store, rows: list[SourceRow], at: datetime) -> None:
    """Print a notice for each chosen source whose postings are hidden as too old."""
    chosen = {row.ref for row in rows}
    for row in store.stale_sources(now=at):
        if row.ref in chosen and row.last_fetched_at is not None:
            day = row.last_fetched_at.date().isoformat()
            typer.echo(
                f"stale {row.ref}: last fetched {day}, postings hidden", err=True
            )


def _posting_line(store: Store, posting: Posting) -> str:
    """Format a posting: title, company, location, sources and URL, tab-separated."""
    refs = ",".join(str(ref) for ref in store.posting_sources(posting.id))
    flag = "? " if posting.low_confidence else ""
    return (
        f"{flag}{posting.title}\t{posting.company}\t{posting.location or '-'}"
        f"\t{refs}\t{posting.url}"
    )


def make_app(
    client_factory: _ClientFactory, *, now: Callable[[], datetime] = _utc_now
) -> typer.Typer:
    """Build the `ijob` application.

    Args:
        client_factory: Builds a new `PoliteClient` each time a command needs to
            fetch; it is given the HTTP cache the client should use, or `None`
            for none.
        now: The wall clock, returning timezone-aware times.

    Returns:
        The application, with its `search` command and `sources` group.
    """
    app = typer.Typer(add_completion=False, help="Collect and list job postings.")
    app.add_typer(_sources_app(client_factory), name="sources")

    @app.callback()
    def _root() -> None:
        """Collect and list job postings."""

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
            typer.Option(
                help="Config file. Default: $XDG_CONFIG_HOME/ijob/config.toml, "
                "or ~/.config/ijob/config.toml."
            ),
        ] = None,
    ) -> None:
        """Fetch the sources, record their jobs and list the open, filtered ones."""
        flags = _FilterFlags(
            countries=tuple(country or ()),
            remotes=tuple(remote or ()),
            since=since,
            companies=tuple(company or ()),
            min_salary=min_salary,
            require_salary=require_salary,
        )
        try:
            spec = _read_filters(config, flags)
            refs = list(dict.fromkeys(map(parse_source_ref, source or [])))
        except InvalidValueError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error

        with _open_cache(now) as cache, _seeded_store(db) as store:
            rows = _choose_sources(store, refs)
            due, kept = _split_by_interval(rows, now())
            sources, refused = _build_sources(store, due)
            result = asyncio.run(_collect(sources, client_factory, cache))
            seen_at = now()
            fetched = _record(store, due, result, seen_at)
            _report_problems(due, result, refused)
            _report_stale(store, rows, seen_at)
            postings = store.open_postings(now=seen_at, sources=refs or None)
            for posting in postings:
                if keep(posting, spec, now=seen_at):
                    typer.echo(_posting_line(store, posting))
        if due and not fetched and not kept:
            raise typer.Exit(code=_EXIT_REFUSED)

    return app


app = make_app(_production_client)


def main() -> None:
    """Run the `ijob` command line."""
    app()
