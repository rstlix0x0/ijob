"""The `ijob` command line.

`make_app` builds the typer application around a factory for the
`PoliteClient` every request goes through, so tests inject a client over a mock
transport and `app` uses the real network, around a clock that tests can
control, and around a builder of the LLM options, where tests inject fake
engines. `main` runs `app`.

Everything ijob keeps lives in one home directory, `$IJOB_HOME` or `~/.ijob`:
`config.toml`, `ijob.db`, `traces.db` and `cache/http.sqlite`. `ijob setup`
creates it, owner access only, with a commented `config.toml` written only when
absent and a migrated, seeded database, and prints each item as `created` or
`already there` beside its path; it can be run again at any time and exits `1`
when the home cannot be created. Every other command first requires the home to
be a directory and otherwise exits `1` with `error: ijob home not found at
<path>, run: ijob setup`, creating nothing. `--db` and `--config` replace the database
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
last fetch is too old for its postings to show. A source's rejected jobs are
counted in one `<source>: skipped N invalid jobs` line; `--verbose` also prints
a line for each rejected job and the reason. The exit status is `0` when at
least one chosen source was fetched or kept, `1` when every chosen source
failed or the HTTP cache cannot be opened, and `2` for a flag, source reference
or config file that cannot be used.

When the stored instructions hold rules, `search` also has a model judge each
posting that passes the filters. A kept posting prints as soon as it is judged,
ending with a tab and a check mark. With `--dropped` the rest print too: a cross
and the reason, or a dot and why there is no verdict (`cap reached`, `provider
error`, `judge failed`), with ` (no description)` added when the model saw none.
A summary goes to stderr, `judged N, cached K, dropped M, not judged U, cost $X of
$Y`, ending `; stopped: <why>` when judging stopped early. The status is `1`
after printing when judging stopped for a provider error or an unknown cost. A
missing judge model or another error of the LLM layer exits `1` before any line
prints. With no rules the output is the plain lines above.

`ijob tui` opens the terminal UI on the stored postings: the same filters as
`search`, a detail pane, refresh, votes and notes, the sources table and the CV.
It reads the config file and opens the database and HTTP cache before the screen
appears, so it exits `2` for a config file that cannot be used and `1` when the
cache cannot be opened. It takes `--db` and `--config`; the file in force, which
is `--config` or else the home's, sets the starting filters, the models, the
spending cap and the CV screen's model check.

`ijob sources` manages the database's table of sources. Each `sources` command
imports the bundled seed list whenever it opens the database, which never
revives a source the user removed. `list` prints one tab-separated line per
source, `add` and `add-url` fetch a new source once to check it before keeping
it, and `remove` drops one. `add` and `add-url` refuse, before that fetch, a
reference already in the table for another company, since the table keeps the
company it first had. For those commands the exit status is `0` on success, `1`
when a source fails its check or its fetch, and `2` for a reference, company or
removal that cannot be acted on. `add-url` sends every request, robots.txt and
redirect hops included, through a check that refuses a malformed URL, a host
that is not a public address and the hosts another source kind reads, before
anything is sent, and it stores the address a redirect ends on. A host whose
robots.txt cannot be fetched is reported as `robots.txt could not be read`, not
as disallowed.

When `add-url` finds a page that is no hiring system, job posting or feed and
`[models].source_recipe` is set, that model learns a recipe for the page. It is
tried on the page and shown as `recipe: <format> at <url>`, `read N jobs, M
invalid` and up to five tab-separated job lines on stdout, with `cost $X of $Y`
on stderr, and the source is stored after you answer `y` or `yes` to `Store this
source? [y/N]` or pass `--yes`; any other answer prints `not stored` and exits
`1`. The page that was found unrecognized is read once, not fetched again. Without
`--yes` and without a terminal it exits `1` with `error:
confirmation needed; pass --yes`, before any model call; without a model it
exits `1` with the usual error and a `note:` naming `[models].source_recipe`.
When the model was paid and the analysis then fails, the `cost $X of $Y` line
follows the `error:` line on stderr.
`ijob sources reanalyze REF [--yes]` learns a recipe source's page again the
same way and replaces the stored recipe on confirmation, exiting `2` for a
source that is unknown, removed or has no recipe. `--db` and `--config` apply to
both.

`ijob show ID` prints one stored posting: title, company, one `name: value` line
for each other field, the vote and note when it is labelled, then its
description, which is fetched and stored the first time it is wanted. A
description that cannot be had prints as `description unavailable: <reason>` and
the exit status stays `0`. `ijob label ID up|down [--note TEXT]` sets or
replaces the vote and prints the id, vote and note; `ijob label ID --clear`
removes it. `ijob cv set PATH` stores a PDF as the CV and prints its name, size
and SHA-256. When `[models].cv_profile` is set it then reads the CV's profile
with that model and prints it as `key: value` lines; otherwise it notes on
stderr that the profile was not built. A profile that cannot be read exits `1`
with the CV kept. `ijob cv profile [--refresh]` prints the profile, reading the
CV first when none is stored or `--refresh` is given. `ijob cv show` prints the
stored CV's name, size and stored time, or `no CV stored`, and loads no LLM
library. An error of the LLM layer exits `1`, and a config setting it refuses
exits `2`. All of these take the posting ids `search` prints and the same
`--db`. The exit status is `0` on success, `1` when the HTTP cache cannot be
opened, and `2` for an unknown posting, a vote or note that cannot be recorded,
a `label` call that gives both or neither of a vote and `--clear`, or a CV file
that cannot be stored.

`ijob instructions set TEXT` has a model read your words into filters
and judge rules, prints them with a warning for each filter that your config
overrides, and stores them after you answer `y` or `yes` to `Store these? [y/N]`
or pass `--yes`; without `--yes` and without a terminal it exits `1` with
`error: confirmation needed; pass --yes`, and text that is blank or too long
exits `2`. `ijob instructions show` prints the stored text, filters and numbered
rules, or `no instructions set`, and `ijob instructions clear` removes them and
prints `cleared`; neither loads an LLM library.

`ijob score ID...` has a model score the postings against the CV's profile, from
0 to 100. It prints, in the order the ids were given, `<id>`, the score and the
title, tab-separated, with each reason on its own line indented two spaces; a
posting with no score prints `-` for the score and a final cell saying why, such
as `· score failed` or `· cap reached`. A summary, `scored N, cached K, not
scored U, cost $X of $Y`, goes to stderr, ending `; stopped: <why>` when scoring
stopped early. An unknown id exits `2`; a missing CV, an error of the LLM layer,
or a stop for a provider error or an unknown cost exits `1`, the last after
printing.

`ijob traces [--limit N]` lists the newest N recorded traces (1 to 1,000, default
20), one tab-separated line each: the trace id, when it started, the operation,
the number of model calls, the cost and the duration in milliseconds. `ijob
traces show ID` prints a trace as a tree of spans, two spaces further in per
level, with the tokens, cost and duration of each model call, and exits `2` for
an unknown id. `ijob traces prune` deletes the oldest traces until the trace
database fits `[tracing].max_size_mb` and prints `pruned N traces, size X MB`.
None of the `traces` commands loads an LLM library.
"""

import asyncio
import logging
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import typer
from ijob_ai.llm.settings import Task
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    Posting,
    PostingId,
    SourceRef,
    Vote,
    parse_source_ref,
)
from ijob_sources.collect import SourceFailure
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    IdentityMismatchError,
    ParseError,
    RobotsDisallowedError,
    SourceNotFoundError,
    SourcesError,
    UnrecognizedPageError,
)
from ijob_sources.polite_http import ConditionalCache, PoliteClient
from ijob_sources.public_host import HostResolver, system_resolver
from ijob_sources.registry import HOST_INTERVALS, refresh_interval

from ijob_app.cli_extract import extract_command, unread_note
from ijob_app.cli_llm import (
    CONFIG_HELP,
    DB_HELP,
    LlmSession,
    SessionOpener,
    instructions_app,
    llm_failures,
    report_notes,
    score_command,
    stop_exits,
    traces_app,
)
from ijob_app.cli_progress import ProgressLine, fetch_progress_text
from ijob_app.cli_recipes import learn_page, reanalyze_command
from ijob_app.config import (
    FilterFlags,
    FilterLayers,
    read_filter_layers,
)
from ijob_app.home import (
    HomeError,
    IjobHome,
    require_home,
    resolve_home,
    setup_home,
)
from ijob_app.llm_config import read_llm_settings
from ijob_app.llm_ops import JudgeStatus, LlmOptions
from ijob_app.llm_text import profile_lines, search_summary, verdict_cell
from ijob_app.render import error_text, posting_fields
from ijob_app.service import (
    CacheUnavailableError,
    ClientFactory,
    PostingDetail,
    RefreshReport,
    Service,
    UnknownSourceError,
    open_service,
)
from ijob_app.tui import IjobApp

_EXIT_REFUSED: Final = 1
_EXIT_BAD_ARGUMENT: Final = 2


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
        typer.echo(f"error: {error_text(error)}", err=True)
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
        typer.echo(f"error: {error_text(error)}", err=True)
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
    except SourcesError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=_EXIT_REFUSED) from error


type _LlmFor = Callable[[IjobHome, Path | None], LlmOptions]


def _sources_app(
    client_factory: ClientFactory,
    now: Callable[[], datetime],
    llm_for: _LlmFor,
    is_interactive: Callable[[], bool],
    resolver: HostResolver,
) -> typer.Typer:
    """Build the `sources` command group.

    Args:
        client_factory: Builds the client a verification fetch goes through.
        now: The wall clock, returning timezone-aware times.
        llm_for: Gives the LLM options for a home and the `--config` value.
        is_interactive: Whether the user can be asked a question.
        resolver: Looks up the addresses of a host for the check that keeps a
            recipe source on public hosts.

    Returns:
        The group, with `list`, `add`, `remove`, `add-url` and `reanalyze`.
    """
    sources = typer.Typer(
        add_completion=False,
        help="List, add, remove and reanalyze job sources.",
    )

    def service_on(
        home: IjobHome, db: Path | None, llm: LlmOptions | None = None
    ) -> AbstractContextManager[Service]:
        """Open the service for a command that never reads through the cache."""
        return open_service(
            home,
            db,
            client_factory,
            now=now,
            use_cache=False,
            llm=llm,
            resolver=resolver,
        )

    def recipe_session(
        db: Path | None, config: Path | None
    ) -> AbstractContextManager[Service]:
        """Locate the home, then give the context that opens the service for a model."""
        home = _require_home()
        return service_on(home, db, llm_for(home, config))

    @sources.command("list")
    def list_sources(
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
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
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
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
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
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
        *,
        yes: Annotated[
            bool,
            typer.Option("--yes", help="Store a learned recipe without asking."),
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Work out what a URL is, verify it and add it as a source.

        A page that is no hiring system, job posting or feed is read by the
        `source_recipe` model, which proposes a recipe to confirm.
        """
        home = _require_home()
        name = _read_company(company)
        with (
            _source_failures(),
            service_on(home, db, llm_for(home, config)) as service,
        ):
            try:
                added = asyncio.run(service.add_url(url, name))
            except UnrecognizedPageError as error:
                learn_page(
                    service, url, name, error, yes=yes, is_interactive=is_interactive
                )
                return
        typer.echo(f"added {added.ref}")

    reanalyze_command(
        sources,
        open_session=recipe_session,
        read_ref=_read_ref,
        is_interactive=is_interactive,
    )
    extract_command(sources, open_session=recipe_session, read_ref=_read_ref)

    return sources


_SOURCES_LOGGER: Final = "ijob_sources"


def _silence_source_logs() -> None:
    """Keep the sources' per-job warnings off stderr, idempotently.

    The `ijob_sources` logger gets one `NullHandler`, so Python's last-resort
    stderr handler never prints its records. Records still propagate, so
    logging the user configures keeps receiving them.
    """
    logger = logging.getLogger(_SOURCES_LOGGER)
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())


@contextmanager
def _print_rejections(*, enabled: bool) -> Iterator[None]:
    """Print each warning of the sources to stderr, message only, while it runs.

    Args:
        enabled: Whether to attach the handler at all.
    """
    if not enabled:
        yield
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(_SOURCES_LOGGER)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)


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


async def _stream_search(
    service: Service,
    layers: FilterLayers,
    refs: list[SourceRef],
    progress: ProgressLine,
    *,
    show_dropped: bool,
) -> None:
    """Print each posting of a search the moment it is ready."""
    read = 0
    kept = 0
    async for item in service.search(layers, refs):
        if item.status is JudgeStatus.NOT_RUN:
            typer.echo(_posting_line(service, item.posting))
            continue
        read += 1
        keep = item.verdict is not None and item.verdict.keep
        kept += keep
        if keep or show_dropped:
            progress.clear()
            typer.echo(f"{_posting_line(service, item.posting)}\t{verdict_cell(item)}")
        progress.show_live(f"judging jobs: {read} read, {kept} kept")


def _print_search(
    service: Service,
    layers: FilterLayers,
    refs: list[SourceRef],
    *,
    show_dropped: bool,
) -> bool:
    """Print the postings that pass the filters as each one is ready.

    With no stored rules each posting prints as `_posting_line` writes it. With
    rules a kept posting prints as soon as it is judged, with the verdict in a
    tab-separated cell, and a posting that was dropped or not judged is left out
    unless `show_dropped`. A summary and any warnings go to stderr.

    Args:
        service: The open service.
        layers: The filters the config file and the flags set.
        refs: The sources to read; empty means every source.
        show_dropped: Whether to print the postings not kept, with the reason.

    Returns:
        Whether the judging ended on a stop that makes the command exit `1`.
    """
    progress = ProgressLine(sys.stderr)
    try:
        with llm_failures():
            asyncio.run(
                _stream_search(
                    service, layers, refs, progress, show_dropped=show_dropped
                )
            )
    finally:
        progress.close()
    report = service.last_report
    if report is None:
        return False
    for note in report_notes(report):
        typer.echo(note, err=True)
    typer.echo(search_summary(report), err=True)
    return stop_exits(report)


def _llm_resolver(
    llm_options: Callable[[IjobHome, Path], LlmOptions] | None,
) -> _LlmFor:
    """Build the function that gathers the LLM options of a command.

    Args:
        llm_options: Builds the options for a home and a config file, or `None`
            for the real engines.

    Returns:
        A function from the home and the `--config` value to the options, whose
        config file is `--config` or else the home's.
    """

    def resolve(home: IjobHome, config: Path | None) -> LlmOptions:
        config_path = config or home.config
        if llm_options is None:
            return LlmOptions(config_path=config_path)
        return llm_options(home, config_path)

    return resolve


def _cv_app(
    client_factory: ClientFactory,
    now: Callable[[], datetime],
    llm_for: _LlmFor,
    resolver: HostResolver,
) -> typer.Typer:
    """Build the `cv` command group.

    Args:
        client_factory: Builds the client the service holds; no command makes a
            request.
        now: The wall clock, returning timezone-aware times.
        llm_for: Gives the LLM options for a home and the `--config` value.
        resolver: Looks up the addresses of a host for the service's public-host
            check.

    Returns:
        The group, with `set`, `show` and `profile`.
    """
    cv = typer.Typer(add_completion=False, help="Store and show your CV.")

    def service_on(
        home: IjobHome, db: Path | None, llm: LlmOptions | None = None
    ) -> AbstractContextManager[Service]:
        """Open the service for a command that never reads through the cache."""
        return open_service(
            home,
            db,
            client_factory,
            now=now,
            use_cache=False,
            llm=llm,
            resolver=resolver,
        )

    def show_profile(service: Service, *, refresh: bool) -> None:
        """Build or read the profile, print it and report what the call cost."""
        with llm_failures():
            profile, report = asyncio.run(service.build_profile(refresh=refresh))
        for line in profile_lines(profile):
            typer.echo(line)
        for note in report_notes(report):
            typer.echo(note, err=True)

    @cv.command("set")
    def set_cv(
        path: Annotated[Path, typer.Argument(help="The CV, a PDF of at most 20 MB.")],
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Keep a copy of your CV beside the database and read its profile."""
        home = _require_home()
        llm = llm_for(home, config)
        with _refusals(), service_on(home, db, llm) as service:
            try:
                stored = service.store_cv(path)
            except OSError as error:
                typer.echo(f"error: cannot store the CV: {error}", err=True)
                raise typer.Exit(code=_EXIT_REFUSED) from error
            typer.echo(f"{stored.name}\t{stored.size}\t{stored.sha256}")
            with llm_failures():
                configured = (
                    Task.CV_PROFILE in read_llm_settings(llm.config_path).models
                )
            if configured:
                show_profile(service, refresh=False)
            else:
                typer.echo("note: profile not built; set [models].cv_profile", err=True)

    @cv.command("show")
    def show_cv(
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """Print the stored CV's name, size and the time it was stored."""
        home = _require_home()
        with service_on(home, db) as service:
            stored = service.cv()
        if stored is None:
            typer.echo("no CV stored")
        else:
            typer.echo(f"{stored.name}\t{stored.size}\t{stored.stored_at.isoformat()}")

    @cv.command("profile")
    def profile_cv(
        *,
        refresh: Annotated[
            bool, typer.Option("--refresh", help="Read the CV again.")
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[Path | None, typer.Option(help=CONFIG_HELP)] = None,
    ) -> None:
        """Print the profile read from your CV, building it if there is none."""
        home = _require_home()
        with _refusals(), service_on(home, db, llm_for(home, config)) as service:
            show_profile(service, refresh=refresh)

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
    app: typer.Typer,
    client_factory: ClientFactory,
    now: Callable[[], datetime],
    resolver: HostResolver,
) -> None:
    """Add the `show` and `label` commands, which act on one stored posting.

    Args:
        app: The application to add the commands to.
        client_factory: Builds the client a description fetch goes through.
        now: The wall clock, returning timezone-aware times.
        resolver: Looks up the addresses of a host for the check that keeps a
            description fetch of a recipe source on public hosts.
    """

    def service_on(home: IjobHome, db: Path | None) -> AbstractContextManager[Service]:
        """Open the service with the HTTP cache, which description fetches use."""
        return open_service(home, db, client_factory, now=now, resolver=resolver)

    @app.command()
    def show(
        posting_id: Annotated[
            int, typer.Argument(metavar="ID", help="Posting id, as `search` prints it.")
        ],
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
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
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
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


def _session_opener(
    client_factory: ClientFactory,
    now: Callable[[], datetime],
    llm_for: _LlmFor,
    resolver: HostResolver,
) -> SessionOpener:
    """Build the function that opens the service for an LLM command.

    Args:
        client_factory: Builds the client the service holds; no command makes a
            request.
        now: The wall clock, returning timezone-aware times.
        llm_for: Gives the LLM options for a home and the `--config` value.
        resolver: Looks up the addresses of a host for the service's public-host
            check.

    Returns:
        A function that locates the ijob home, then opens the service on it with
        the LLM options only for a command that calls a model.
    """

    @contextmanager
    def open_session(
        db: Path | None, config: Path | None, *, models: bool
    ) -> Iterator[LlmSession]:
        home = _require_home()
        llm = llm_for(home, config) if models else None
        with (
            _refusals(),
            open_service(
                home,
                db,
                client_factory,
                now=now,
                use_cache=False,
                llm=llm,
                resolver=resolver,
            ) as service,
        ):
            yield LlmSession(service, config, home.config)

    return open_session


def _stdin_is_terminal() -> bool:
    """Say whether standard input is a terminal, which it is not when closed."""
    return sys.stdin is not None and sys.stdin.isatty()


def _llm_groups(  # noqa: PLR0913, PLR0917 - the application and what its groups share
    app: typer.Typer,
    client_factory: ClientFactory,
    now: Callable[[], datetime],
    llm_for: _LlmFor,
    is_interactive: Callable[[], bool],
    resolver: HostResolver,
) -> None:
    """Add the `cv`, `instructions` and `traces` groups and the `score` command.

    Args:
        app: The application to add the groups to.
        client_factory: Builds the client the service holds; no command makes a
            request.
        now: The wall clock, returning timezone-aware times.
        llm_for: Gives the LLM options for a home and the `--config` value.
        is_interactive: Whether the user can be asked a question.
        resolver: Looks up the addresses of a host for the service's public-host
            check.
    """
    app.add_typer(_cv_app(client_factory, now, llm_for, resolver), name="cv")
    opener = _session_opener(client_factory, now, llm_for, resolver)
    app.add_typer(instructions_app(opener, is_interactive), name="instructions")
    app.add_typer(traces_app(opener), name="traces")
    score_command(app, opener)


def _root() -> None:
    """Collect and list job postings."""
    _silence_source_logs()


def make_app(  # noqa: PLR0915  # one registration per command
    client_factory: ClientFactory,
    *,
    now: Callable[[], datetime] = _utc_now,
    llm_options: Callable[[IjobHome, Path], LlmOptions] | None = None,
    is_interactive: Callable[[], bool] | None = None,
    resolver: HostResolver = system_resolver,
) -> typer.Typer:
    """Build the `ijob` application.

    Args:
        client_factory: Builds a new `PoliteClient` each time a command needs to
            fetch; it is given the HTTP cache the client should use, or `None`
            for none.
        now: The wall clock, returning timezone-aware times.
        llm_options: Builds the LLM options for an ijob home and the config file
            in use, which tests use to inject fake engines; `None` for the real
            engines.
        is_interactive: Whether the user can be asked a question, which tests
            replace because their input is never a terminal; `instructions set`,
            `sources add-url` and `sources reanalyze` ask for confirmation only
            when it is true. `None` asks the process at the time of the question:
            true when standard input is a terminal.
        resolver: Looks up the addresses of a host for the check that keeps a
            recipe source on public hosts, which tests replace so that none
            resolves a name; the default asks the system.

    Returns:
        The application, with its `setup`, `search`, `show`, `label`, `score` and
        `tui` commands and its `sources`, `cv`, `instructions` and `traces`
        groups.
    """
    app = typer.Typer(
        add_completion=False, help="Collect and list job postings.", callback=_root
    )
    ask = is_interactive or _stdin_is_terminal
    llm_for = _llm_resolver(llm_options)
    app.add_typer(
        _sources_app(client_factory, now, llm_for, ask, resolver), name="sources"
    )
    _llm_groups(app, client_factory, now, llm_for, ask, resolver)
    _posting_commands(app, client_factory, now, resolver)

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
        *,
        verbose: Annotated[
            bool,
            typer.Option("--verbose", help="Also print each rejected job and why."),
        ] = False,
        dropped: Annotated[
            bool,
            typer.Option(
                "--dropped",
                help="Also list the postings not kept, with the reason.",
            ),
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[
            Path | None,
            typer.Option(help=CONFIG_HELP),
        ] = None,
    ) -> None:
        """Fetch the sources, record their jobs and list the open, filtered ones.

        Each line starts with the posting's id, which `show` and `label` take.
        When you have set instructions with rules, each line ends with the
        verdict of a model that judged the posting against them.
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
            layers = read_filter_layers(config, flags, default=home.config)
            refs = list(dict.fromkeys(map(parse_source_ref, source or [])))
        except InvalidValueError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error

        try:
            with (
                open_service(
                    home,
                    db,
                    client_factory,
                    now=now,
                    llm=llm_for(home, config),
                    resolver=resolver,
                ) as service,
                _print_rejections(enabled=verbose),
            ):
                progress = ProgressLine(sys.stderr)
                try:
                    report = asyncio.run(
                        service.refresh(
                            refs,
                            on_progress=lambda now: progress.show_live(
                                fetch_progress_text(now)
                            ),
                        )
                    )
                finally:
                    progress.close()
                _report(report)
                halted = _print_search(service, layers, refs, show_dropped=dropped)
                if note := unread_note(service.unread_count(refs)):
                    typer.echo(note, err=True)
        except CacheUnavailableError as error:
            typer.echo(f"error: {error.reason}", err=True)
            raise typer.Exit(code=_EXIT_REFUSED) from error
        except UnknownSourceError as error:
            typer.echo(f"error: {error.reason}", err=True)
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
        if halted or (report.due and not report.fetched and not report.kept):
            raise typer.Exit(code=_EXIT_REFUSED)

    @app.command()
    def tui(
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[
            Path | None,
            typer.Option(help=CONFIG_HELP),
        ] = None,
    ) -> None:
        """Open the terminal UI on the stored postings."""
        home = _require_home()
        with (
            _refusals(),
            open_service(
                home,
                db,
                client_factory,
                now=now,
                llm=llm_for(home, config),
                resolver=resolver,
            ) as service,
        ):
            IjobApp(service, config, default_config=home.config).run()

    return app


app = make_app(_production_client)


def main() -> None:
    """Run the `ijob` command line."""
    app()
