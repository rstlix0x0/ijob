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

On a terminal every command draws with the parts in `ijob_app.view`: boxes,
tables, progress and a one-line result, and a problem comes with its fix. In a
pipe it prints the plain lines described below, so scripts keep working.

`ijob search` lists the open, filtered postings. On a terminal the saved postings
show at once, while the sources that are due fetch beside them and new postings
join the list as each source is saved; `ctrl+c` stops it and keeps what finished.
`--title` narrows the titles by whole words, `--no-fetch` leaves the sources alone,
`--all` lists every posting at the end, and `--score` has a model score up to
`--top` postings against the CV, sorting them best first as each score arrives.

In a pipe it validates every flag and the config file, fetches first, then prints
one tab-separated line per posting: the id, the title (after a `? ` flag when its
fields were parsed heuristically), company, location, the sources that showed it
and its URL. With `--score` each line instead holds the id, score, title, company,
location and URL, as each score arrives. `--source` repeated limits the run to the
named sources. Problems with a source go to stderr, and so does a notice for each
source whose last fetch is too old for its postings to show. A source's rejected
jobs are counted in one `<source>: skipped N invalid jobs` line; `--verbose` also
prints a line for each rejected job and the reason. The exit status is `0` when at
least one chosen source was fetched or kept, `1` when every chosen source failed
or the HTTP cache cannot be opened, and `2` for a flag, source reference or config
file that cannot be used.

`--judge` has a model check each posting that passes the filters against the
stored rules. A kept posting prints as soon as it is judged, ending with a tab and
a check mark. With `--dropped` the rest print too: a cross and the reason, or a dot
and why there is no verdict (`cap reached`, `provider error`, `judge failed`), with
` (no description)` added when the model saw none. A summary goes to stderr,
`judged N, cached K, dropped M, not judged U, cost $X of $Y`, ending
`; stopped: <why>` when judging stopped early. The status is `1` after printing when
judging stopped for a provider error or an unknown cost. A missing judge model or
another error of the LLM layer exits `1` before any line prints.

`ijob fetch` fetches the sources that are due and saves each as it ends. On a
terminal it prints a line for each source that brought something new or failed.

`ijob tui` opens the terminal UI, which shows the saved postings at once and fetches
behind them. It reads the config file and opens the database and HTTP cache before
the screen appears, so it exits `2` for a config file that cannot be used and `1`
when the cache cannot be opened. It takes `--db` and `--config`.

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
both. `ijob sources find "<company or URL>" [--company C] [--model M] [--yes]`
has an agent find the source that lists a company's jobs, shows the steps it took
and a sample of the jobs, and adds the source once you confirm. `ijob explore
"<instruction>" [--model M] [--yes]` has an agent carry out an instruction and
propose sources, and adds the ones you pick; see `ijob_app.cli_agents`.

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

`ijob eval label [--limit N] [--yes]` has the jury set under `[eval]` in the config
vote on a sample of 100 postings, or on the first N of it, and labels the cases the
jurors agree on; `ijob_app.cli_eval` describes its screen, its question, its
pipe output and its exit statuses. `ijob eval review` shows, one at a time and on a
terminal only, the cases the jurors did not agree on and then a spot check of the
ones they did, and stores each answer as a label.
"""

import asyncio
import logging
import os
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
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
from rich.live import Live

from ijob_app.cli_agents import explore_command, find_command
from ijob_app.cli_eval import eval_app
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
from ijob_app.cli_output import rejections_shown, say_done, say_error, say_note, working
from ijob_app.cli_progress import ProgressLine, fetch_progress_text
from ijob_app.cli_recipes import learn_page, reanalyze_command
from ijob_app.cli_search import (
    JudgeOptions,
    SearchOptions,
    live_fetch_command,
    live_judge_command,
    live_search_command,
)
from ijob_app.cli_tune import tune_app
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
from ijob_app.job_search import DEFAULT_SCORE_LIMIT
from ijob_app.llm_config import read_llm_settings
from ijob_app.llm_ops import JudgeStatus, LlmOptions, ScoreItem
from ijob_app.llm_text import (
    profile_lines,
    score_summary,
    search_summary,
    verdict_cell,
)
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
from ijob_app.view import (
    fix_line,
    is_terminal,
    make_console,
    next_line,
    notice_line,
    posting_detail,
    profile_view,
    sources_table,
    theme_help,
)

_EXIT_REFUSED: Final = 1
_EXIT_BAD_ARGUMENT: Final = 2
_SOURCES_CHROME: Final = 6
_MIN_SOURCES: Final = 5


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
        say_error(f"{error.reason}")
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
        say_error(f"{error}")
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
        say_error("--company must not be blank")
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
        say_error(f"{error.reason}")
        raise typer.Exit(code=_EXIT_REFUSED) from error
    except InvalidValueError as error:
        say_error(f"{error_text(error)}")
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
        say_error(f"{error_text(error)}")
        raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
    except SourcesError as error:
        say_error(f"{error}")
        raise typer.Exit(code=_EXIT_REFUSED) from error


type _LlmFor = Callable[[IjobHome, Path | None], LlmOptions]


def _sources_app(  # noqa: PLR0915  # one registration per command
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
        The group, with `list`, `add`, `remove`, `add-url`, `reanalyze`, `extract`
        and `find`.
    """
    sources = typer.Typer(
        add_completion=False,
        help="List, add, remove, reanalyze and find job sources.",
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
        *,
        all_sources: Annotated[
            bool, typer.Option("--all", help="List every source on a terminal.")
        ] = False,
    ) -> None:
        """Print every source: reference, company, origin and last fetch."""
        with service_on(_require_home(), db) as service:
            rows = service.sources()
            counts = service.source_job_counts()
        if is_terminal():
            console = make_console()
            room = max(console.height - _SOURCES_CHROME, _MIN_SOURCES)
            console.print(
                sources_table(rows, counts, now(), limit=None if all_sources else room)
            )
            return
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
        with (
            _source_failures(),
            service_on(home, db) as service,
            working(f"Checking {source_ref}"),
        ):
            asyncio.run(service.add_source(source_ref, name))
        say_done(f"added {source_ref}", f"Added {source_ref}", "checked with one fetch")

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
            say_error(f"{error.reason}")
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
        say_done(f"removed {source_ref}", f"Removed {source_ref}")

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
                with working(f"Checking {url}"):
                    added = asyncio.run(service.add_url(url, name))
            except UnrecognizedPageError as error:
                learn_page(
                    service, url, name, error, yes=yes, is_interactive=is_interactive
                )
                return
        say_done(f"added {added.ref}", f"Added {added.ref}", "checked with one fetch")

    reanalyze_command(
        sources,
        open_session=recipe_session,
        read_ref=_read_ref,
        is_interactive=is_interactive,
    )
    extract_command(sources, open_session=recipe_session, read_ref=_read_ref)
    find_command(
        sources,
        open_session=recipe_session,
        read_company=_read_company,
        is_interactive=is_interactive,
    )

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


def _plain_refresh(service: Service, refs: list[SourceRef]) -> RefreshReport:
    """Fetch the sources that are due, reporting on standard error, and return how."""
    progress = ProgressLine(sys.stderr)
    try:
        report = asyncio.run(
            service.refresh(
                refs,
                on_progress=lambda now: progress.show_live(fetch_progress_text(now)),
            )
        )
    finally:
        progress.close()
    _report(report)
    return report


def _listing_line(posting: Posting, sources: Iterable[SourceRef]) -> str:
    """Format a posting: id, title, company, location, sources and URL, tab-separated.

    The `? ` flag of a low-confidence posting leads the title, after the id.
    """
    refs = ",".join(str(ref) for ref in sources)
    flag = "? " if posting.low_confidence else ""
    return (
        f"{posting.id}\t{flag}{posting.title}\t{posting.company}"
        f"\t{posting.location or '-'}\t{refs}\t{posting.url}"
    )


def _posting_line(service: Service, posting: Posting) -> str:
    """Format a posting as `_listing_line` does, reading its sources."""
    return _listing_line(posting, service.posting_sources(posting.id))


def _print_listing(
    service: Service, layers: FilterLayers, refs: list[SourceRef]
) -> None:
    """Print the postings that pass the filters, one line each, with no model."""
    postings = asyncio.run(service.list_jobs(layers, refs))
    sources = service.job_sources()
    for posting in postings:
        typer.echo(_listing_line(posting, sources.get(posting.id, ())))


def _scored_line(item: ScoreItem) -> str:
    """Format a scored posting: id, score, title, company, location and URL."""
    posting = item.posting
    score = "-" if item.score is None else item.score.score
    return (
        f"{posting.id}\t{score}\t{posting.title}\t{posting.company}"
        f"\t{posting.location or '-'}\t{posting.url}"
    )


def _print_scored(
    service: Service, layers: FilterLayers, refs: list[SourceRef], limit: int
) -> bool:
    """Print the newest postings that pass the filters as each one is scored.

    Args:
        service: The open service.
        layers: The filters the config file and the flags set.
        refs: The sources to read; empty means every source.
        limit: The most postings to score.

    Returns:
        Whether the scoring ended on a stop that makes the command exit `1`.
    """

    async def stream() -> None:
        postings = await service.list_jobs(layers, refs)
        ids = [posting.id for posting in postings[:limit]]
        async for item in service.score(ids):
            typer.echo(_scored_line(item))

    with llm_failures():
        asyncio.run(stream())
    report = service.last_report
    if report is None:
        return False
    for note in report_notes(report):
        say_note(note)
    typer.echo(score_summary(report), err=True)
    return stop_exits(report)


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
        say_note(note)
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
        with llm_failures(), working("Reading your CV"):
            profile, report = asyncio.run(service.build_profile(refresh=refresh))
        if is_terminal():
            make_console().print(profile_view(profile))
        else:
            for line in profile_lines(profile):
                typer.echo(line)
        for note in report_notes(report):
            say_note(note)

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
                say_error(f"cannot store the CV: {error}")
                raise typer.Exit(code=_EXIT_REFUSED) from error
            say_done(
                f"{stored.name}\t{stored.size}\t{stored.sha256}",
                f"Stored {stored.name}",
                f"{stored.size:,} bytes",
            )
            with llm_failures():
                configured = (
                    Task.CV_PROFILE in read_llm_settings(llm.config_path).models
                )
            if configured:
                show_profile(service, refresh=False)
            else:
                say_note("note: profile not built; set [models].cv_profile")

    @cv.command("show")
    def show_cv(
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """Print the stored CV's name, size and the time it was stored."""
        home = _require_home()
        with service_on(home, db) as service:
            stored = service.cv()
        if stored is None:
            if is_terminal():
                console = make_console()
                console.print(notice_line("No CV stored"))
                console.print(fix_line("ijob cv set cv.pdf"))
            else:
                typer.echo("no CV stored")
        else:
            day = stored.stored_at.date().isoformat()
            say_done(
                f"{stored.name}\t{stored.size}\t{stored.stored_at.isoformat()}",
                stored.name,
                f"{stored.size:,} bytes, stored {day}",
            )

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


def _draw_detail(
    service: Service, posting_id: PostingId, now: Callable[[], datetime]
) -> None:
    """Draw a posting at once, and fetch its description beneath it if it is missing.

    Args:
        service: The open service.
        posting_id: The posting to show.
        now: The wall clock.

    Raises:
        UnknownPostingError: The id names no posting.
    """
    console = make_console()
    score = service.saved_scores().get(posting_id)
    saved = service.saved_detail(posting_id)
    if saved.description is not None:
        console.print(posting_detail(saved, now(), score=score))
        return
    pending = replace(saved, unavailable_reason="fetching…")
    with Live(
        posting_detail(pending, now(), score=score), console=console, transient=True
    ):
        detail = asyncio.run(service.detail(posting_id))
    console.print(posting_detail(detail, now(), score=score))


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
        identifier = PostingId(posting_id)
        with _refusals(), service_on(home, db) as service:
            if is_terminal():
                _draw_detail(service, identifier, now)
                return
            detail = asyncio.run(service.detail(identifier))
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
            say_error(
                "give a vote (up or down) with an optional --note, or --clear alone"
            )
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT)
        identifier = PostingId(posting_id)
        with _refusals(), service_on(home, db) as service:
            if vote is None:
                service.clear_label(identifier)
                say_done(f"{identifier}\tcleared", f"Label cleared on {identifier}")
                return
            with working("Saving your label"):
                detail = asyncio.run(service.label(identifier, vote, note))
        if detail.label is not None:
            word = "good" if vote is Vote.UP else "bad"
            say_done(
                f"{identifier}\t{detail.label.vote}\t{detail.label.note or '-'}",
                f"Labeled {word}",
                f"{detail.posting.title}, {detail.posting.company}",
            )


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
    """Add the `cv`, `instructions`, `traces`, `eval` and `tune` groups and `score`.

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
    app.add_typer(eval_app(opener, is_interactive, now), name="eval")
    app.add_typer(tune_app(opener, is_interactive), name="tune")
    score_command(app, opener)
    explore_command(app, opener, is_interactive)


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
            `sources add-url`, `sources reanalyze` and `sources find` ask for
            confirmation only when it is true. `None` asks the process at the
            time of the question: true when standard input is a terminal.
        resolver: Looks up the addresses of a host for the check that keeps a
            recipe source on public hosts, which tests replace so that none
            resolves a name; the default asks the system.

    Returns:
        The application, with its `setup`, `search`, `show`, `label`, `score` and
        `tui` commands and its `sources`, `cv`, `instructions`, `traces`, `eval`
        and `tune` groups.
    """
    theme_help()
    app = typer.Typer(
        add_completion=False,
        help="Find jobs in any field.",
        callback=_root,
        no_args_is_help=True,
    )
    ask = is_interactive or _stdin_is_terminal
    llm_for = _llm_resolver(llm_options)
    app.add_typer(
        _sources_app(client_factory, now, llm_for, ask, resolver), name="sources"
    )

    @app.command()
    def search(  # noqa: PLR0913, PLR0917 - one parameter per option
        source: Annotated[
            list[str] | None,
            typer.Option(
                help="Read only this source, as kind:key. Repeat for several. "
                "Default: every source in the database."
            ),
        ] = None,
        title: Annotated[
            list[str] | None,
            typer.Option(
                help='Keep titles holding these words, such as "product manager". '
                "Repeat for several."
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
        score: Annotated[
            bool,
            typer.Option("--score", help="Score the jobs against your CV, best first."),
        ] = False,
        top: Annotated[
            int,
            typer.Option(min=1, help="The most jobs to score, newest first."),
        ] = DEFAULT_SCORE_LIMIT,
        judge: Annotated[
            bool,
            typer.Option(
                "--judge", help="Have a model check the jobs against your rules."
            ),
        ] = False,
        all_jobs: Annotated[
            bool, typer.Option("--all", help="List every job at the end.")
        ] = False,
        fetch: Annotated[
            bool,
            typer.Option(
                "--fetch/--no-fetch",
                help="Fetch the sources that are due. Saved jobs show either way.",
            ),
        ] = True,
        verbose: Annotated[
            bool,
            typer.Option("--verbose", help="Also print each rejected job and why."),
        ] = False,
        dropped: Annotated[
            bool,
            typer.Option(
                "--dropped",
                help="With --judge, also list the postings not kept, with the reason.",
            ),
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
        config: Annotated[
            Path | None,
            typer.Option(help=CONFIG_HELP),
        ] = None,
    ) -> None:
        """List the saved jobs at once, while fetching what is due beside them.

        With --score a model scores the jobs against your CV and they sort best
        first as each score arrives. In a pipe the output is plain lines, each
        starting with the job's id, after any fetch has ended.
        """
        home = _require_home()
        flags = FilterFlags(
            titles=tuple(title or ()),
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
            say_error(f"{error}")
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
                rejections_shown(enabled=verbose),
            ):
                if is_terminal() and judge:
                    halted = live_judge_command(
                        service,
                        layers,
                        refs,
                        now,
                        JudgeOptions(
                            title=", ".join(title) if title else None,
                            fetch=fetch,
                            limit=top,
                            show_all=all_jobs,
                            show_dropped=dropped,
                        ),
                    )
                    report = None
                elif is_terminal():
                    halted = live_search_command(
                        service,
                        layers,
                        refs,
                        now,
                        SearchOptions(
                            title=", ".join(title) if title else None,
                            fetch=fetch,
                            score=score,
                            score_limit=top,
                            show_all=all_jobs,
                            config=config or home.config,
                        ),
                    )
                    report = None
                else:
                    report = _plain_refresh(service, refs) if fetch else None
                    if judge:
                        halted = _print_search(
                            service, layers, refs, show_dropped=dropped
                        )
                    elif score:
                        halted = _print_scored(service, layers, refs, top)
                    else:
                        _print_listing(service, layers, refs)
                        halted = False
                if note := unread_note(service.unread_count(refs)):
                    say_note(note)
        except CacheUnavailableError as error:
            say_error(f"{error.reason}")
            raise typer.Exit(code=_EXIT_REFUSED) from error
        except UnknownSourceError as error:
            say_error(f"{error.reason}")
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
        failed_all = (
            report is not None and report.due and not report.fetched and not report.kept
        )
        if halted or failed_all:
            raise typer.Exit(code=_EXIT_REFUSED)

    @app.command("fetch")
    def fetch_sources(
        source: Annotated[
            list[str] | None,
            typer.Option(
                help="Fetch only this source, as kind:key. Repeat for several. "
                "Default: every source in the database."
            ),
        ] = None,
        *,
        verbose: Annotated[
            bool,
            typer.Option("--verbose", help="Also print each rejected job and why."),
        ] = False,
        db: Annotated[Path | None, typer.Option(help=DB_HELP)] = None,
    ) -> None:
        """Fetch the sources that are due and save their jobs as each one ends."""
        home = _require_home()
        try:
            refs = list(dict.fromkeys(map(parse_source_ref, source or [])))
        except InvalidValueError as error:
            say_error(f"{error}")
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
        try:
            with (
                open_service(
                    home, db, client_factory, now=now, resolver=resolver
                ) as service,
                rejections_shown(enabled=verbose),
            ):
                if is_terminal():
                    report = live_fetch_command(service, refs)
                else:
                    report = _plain_refresh(service, refs)
                    typer.echo(
                        f"fetched {len(report.fetched)} of {len(report.due)} sources"
                    )
        except CacheUnavailableError as error:
            say_error(f"{error.reason}")
            raise typer.Exit(code=_EXIT_REFUSED) from error
        except UnknownSourceError as error:
            say_error(f"{error.reason}")
            raise typer.Exit(code=_EXIT_BAD_ARGUMENT) from error
        if report is not None and report.due and not report.fetched and not report.kept:
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
            IjobApp(service, config, default_config=home.config, now=now).run()

    _llm_groups(app, client_factory, now, llm_for, ask, resolver)
    _posting_commands(app, client_factory, now, resolver)

    @app.command()
    def setup() -> None:
        """Create the ijob home with its config, database and cache directory."""
        try:
            items = setup_home(resolve_home(os.environ))
        except HomeError as error:
            say_error(f"{error.reason}")
            raise typer.Exit(code=_EXIT_REFUSED) from error
        except (OSError, sqlite3.Error) as error:
            say_error(f"cannot set up the ijob home: {error}")
            raise typer.Exit(code=_EXIT_REFUSED) from error
        for item in items:
            say_done(f"{item.outcome}\t{item.path}", str(item.path), str(item.outcome))
            if item.exposed_mode is not None:
                say_note(
                    f"warning: {item.path} has mode {item.exposed_mode:04o}, so "
                    f"other users can read it; run: chmod 700 {item.path}"
                )
        if is_terminal():
            make_console().print(
                next_line("ijob fetch", "ijob cv set cv.pdf", "ijob tui")
            )

    return app


app = make_app(_production_client)


def main() -> None:
    """Run the `ijob` command line."""
    app()
