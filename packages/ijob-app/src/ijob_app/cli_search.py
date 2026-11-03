"""The live views of `ijob search`, `ijob fetch` and `ijob score` on a terminal.

Each one shows what is ready at once, updates in place as results arrive, and
leaves a tidy result in the scrollback. `ctrl+c` stops it and shows what is done,
since each source and each score is saved as it ends.
"""

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from ijob_ai.errors import MissingModelError, NoCvError
from ijob_core.model import PostingId, SourceRef
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    IdentityMismatchError,
    ParseError,
    RobotsDisallowedError,
    SourceNotFoundError,
)
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from ijob_app.config import FilterLayers
from ijob_app.extract_ops import ExtractProgress, ExtractReport
from ijob_app.job_search import JobList, judge_jobs, run_search
from ijob_app.llm_config import read_llm_settings
from ijob_app.llm_ops import (
    STATUS_TEXT,
    OperationReport,
    ScoreItem,
    ScoreStatus,
    cost_line,
)
from ijob_app.service import FetchProgress, RefreshReport, Service, SourceEvent
from ijob_app.view import (
    boxed,
    extract_status,
    failed_line,
    fetch_status,
    final_search,
    fix_line,
    live_search,
    make_console,
    next_line,
    notice_line,
    ok_line,
    progress_text,
    score_rows,
    source_event_line,
    unread_table,
)

_REFRESH_PER_SECOND: Final = 8
_SCREEN_CHROME: Final = 7
_FINAL_SCORED_JOBS: Final = 8
_FINAL_SCREEN_CHROME: Final = 8
_MIN_ROWS: Final = 5
_DEFAULT_CONCURRENCY: Final = 4
_FAILURES_SHOWN: Final = 5
_FAILING_STOPS: Final = frozenset({"provider", "cost_unknown"})


def failure_reason(error: Exception) -> str:
    """Say in a few words why a source could not be read.

    Args:
        error: What fetching it raised.

    Returns:
        The reason, for a person.
    """
    match error:
        case RobotsDisallowedError():
            return "disallowed by robots.txt"
        case IdentityMismatchError(reason=reason):
            return f"identity check failed: {reason}"
        case SourceNotFoundError():
            return "not found"
        case HttpStatusError(status=status):
            return f"HTTP {status}"
        case FetchError(reason=reason) | ParseError(reason=reason):
            return reason
        case _:
            return str(error)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchOptions:
    """What the user asked `search` to do.

    Attributes:
        title: What to call the list: the words searched for, if any.
        fetch: Whether to fetch the sources that are due.
        score: Whether to score jobs against the CV.
        score_limit: The most jobs to score, newest first.
        show_all: Whether to list every job at the end.
        config: The config file in force, for the number of calls run at once.
    """

    title: str | None
    fetch: bool
    score: bool
    score_limit: int
    show_all: bool
    config: Path


def _concurrency(config: Path) -> int:
    """Read how many model calls run at once, for how many jobs show as scoring."""
    try:
        return read_llm_settings(config).concurrency
    except ValueError:
        return _DEFAULT_CONCURRENCY


def _final_limit(console: Console, options: SearchOptions) -> int | None:
    """Pick how many jobs a result shows: all of them, or what is worth reading."""
    if options.show_all:
        return None
    if options.score:
        return _FINAL_SCORED_JOBS
    return max(console.height - _FINAL_SCREEN_CHROME, _MIN_ROWS)


def _score_fix(error: Exception) -> str | None:
    """Name the command that lets scoring run, when one does."""
    match error:
        case NoCvError():
            return "ijob cv set cv.pdf"
        case MissingModelError(task=task):
            return f"set [models].{task} in config.toml"
        case _:
            return None


def _print_score_summary(
    console: Console, jobs: JobList, report: OperationReport
) -> None:
    """Print what scoring did and spent."""
    detail = f"{report.cached} from saved scores · {cost_line(report)}"
    console.print(ok_line(f"{report.done + report.cached} scored", detail))
    if jobs.hidden_from_scoring:
        console.print(
            notice_line(
                f"{jobs.hidden_from_scoring:,} jobs left unscored",
                "narrow the search, or raise --top",
            )
        )
    if report.stopped_by is not None:
        console.print(notice_line("Scoring stopped early", report.stopped_by))


def _print_fetch_summary(
    console: Console, report: RefreshReport, new_jobs: int
) -> None:
    """Print what a fetch did, and the sources that need a look."""
    if report.due:
        console.print(
            ok_line(
                f"{len(report.fetched)} of {len(report.due)} sources fetched",
                f"+{new_jobs:,} new jobs",
            )
        )
    failures = [(f.ref, failure_reason(f.error)) for f in report.failures]
    failures += [(ref, str(error)) for ref, error in report.refused.items()]
    for ref, reason in failures[:_FAILURES_SHOWN]:
        console.print(failed_line(str(ref), reason))
    if len(failures) > _FAILURES_SHOWN:
        left = len(failures) - _FAILURES_SHOWN
        console.print(Text(f"   + {left} more failed", style="dim"))
    if report.stale:
        console.print(
            notice_line(
                f"{len(report.stale)} sources are too old for their jobs to show",
                "ijob fetch brings them back",
            )
        )


def live_search_command(
    service: Service,
    layers: FilterLayers,
    refs: Sequence[SourceRef],
    now: Callable[[], datetime],
    options: SearchOptions,
) -> bool:
    """Run a search on the terminal, drawing it as results arrive.

    Args:
        service: The open service.
        layers: The filters the config file and the flags set.
        refs: The sources to read; empty means every source.
        now: The wall clock.
        options: What the user asked for.

    Returns:
        Whether scoring could not run, which makes the command exit `1`.
    """
    console = make_console()
    jobs = JobList()
    concurrency = _concurrency(options.config) if options.score else 0

    def draw() -> Group:
        room = max(console.height - _SCREEN_CHROME, _MIN_ROWS)
        return live_search(
            jobs,
            now(),
            title=options.title,
            with_score=options.score,
            room=room,
            concurrency=concurrency,
        )

    stopped = False
    with Live(
        get_renderable=draw,
        console=console,
        refresh_per_second=_REFRESH_PER_SECOND,
        transient=True,
    ):
        try:
            asyncio.run(
                run_search(
                    service,
                    layers,
                    refs,
                    jobs,
                    lambda: None,
                    fetch=options.fetch,
                    score=options.score,
                    score_limit=options.score_limit,
                )
            )
        except KeyboardInterrupt:
            stopped = True
    console.print(
        final_search(
            jobs,
            now(),
            title=options.title,
            with_score=options.score,
            limit=_final_limit(console, options),
        )
    )
    if stopped:
        console.print(notice_line("Stopped", "everything finished is saved"))
    if jobs.score_report is not None:
        _print_score_summary(console, jobs, jobs.score_report)
    if jobs.fetch_report is not None:
        _print_fetch_summary(console, jobs.fetch_report, jobs.new_jobs)
    if jobs.score_error is not None:
        console.print(failed_line(f"Scoring did not run: {jobs.score_error}"))
        if (fix := _score_fix(jobs.score_error)) is not None:
            console.print(fix_line(fix))
    ranked = jobs.ranked()
    if ranked:
        first = ranked[0].posting.id
        console.print(
            next_line(f"ijob show {first}", f"ijob label {first} up", "ijob tui")
        )
    stopped_short = (
        jobs.score_report is not None and jobs.score_report.stopped_by in _FAILING_STOPS
    )
    return jobs.score_error is not None or stopped_short


@dataclass(frozen=True, slots=True, kw_only=True)
class JudgeOptions:
    """What the user asked `search --judge` to do.

    Attributes:
        title: What to call the list: the words searched for, if any.
        fetch: Whether to fetch the sources that are due.
        limit: The most jobs to check, newest first.
        show_all: Whether to list every job at the end.
        show_dropped: Whether to keep the jobs that break a rule on the list.
    """

    title: str | None
    fetch: bool
    limit: int
    show_all: bool
    show_dropped: bool


def live_judge_command(
    service: Service,
    layers: FilterLayers,
    refs: Sequence[SourceRef],
    now: Callable[[], datetime],
    options: JudgeOptions,
) -> bool:
    """Check jobs against the user's rules on the terminal, as results arrive.

    The saved jobs show at once and are checked first, while the sources that
    are due fetch beside them; jobs a fetch brings in are checked after it.

    Args:
        service: The open service.
        layers: The filters the config file and the flags set.
        refs: The sources to read; empty means every source.
        now: The wall clock.
        options: What the user asked for.

    Returns:
        Whether checking could not run or stopped short, which makes the command
        exit `1`.
    """
    console = make_console()
    jobs = JobList()
    loaded = asyncio.Event()

    def changed() -> None:
        if jobs.loaded:
            loaded.set()

    def draw() -> Group:
        room = max(console.height - _SCREEN_CHROME, _MIN_ROWS)
        return live_search(
            jobs,
            now(),
            title=options.title,
            with_score=False,
            room=room,
            concurrency=0,
            with_rules=True,
        )

    def unchecked() -> list[PostingId]:
        rows = list(jobs.rows.values())
        done = sum(row.verdict is not None for row in rows)
        room = max(options.limit - done, 0)
        return [row.posting.id for row in rows if row.verdict is None][:room]

    async def check(fetcher: asyncio.Task[None]) -> None:
        await loaded.wait()
        if ids := unchecked():
            await judge_jobs(service, jobs, ids, changed)
        await asyncio.wait((fetcher,))
        if not jobs.score_error and (ids := unchecked()):
            await judge_jobs(service, jobs, ids, changed)

    async def run() -> None:
        async with asyncio.TaskGroup() as group:
            fetcher = group.create_task(
                run_search(service, layers, refs, jobs, changed, fetch=options.fetch)
            )
            group.create_task(check(fetcher))

    stopped = False
    with Live(
        get_renderable=draw,
        console=console,
        refresh_per_second=_REFRESH_PER_SECOND,
        transient=True,
    ):
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            stopped = True
    limit = (
        None
        if options.show_all
        else max(console.height - _FINAL_SCREEN_CHROME, _MIN_ROWS)
    )
    console.print(
        final_search(
            jobs,
            now(),
            title=options.title,
            with_score=False,
            limit=limit,
            with_rules=True,
            show_dropped=options.show_dropped,
        )
    )
    if stopped:
        console.print(notice_line("Stopped", "everything finished is saved"))
    return _print_judge_summary(console, jobs)


def _print_judge_summary(console: Console, jobs: JobList) -> bool:
    """Print what checking the rules did, and say whether it fell short."""
    rows = list(jobs.rows.values())
    fits = sum(row.verdict is not None and row.verdict.keep for row in rows)
    breaks = sum(row.verdict is not None and not row.verdict.keep for row in rows)
    report = jobs.judge_report
    detail = f"{breaks} break a rule"
    if report is not None:
        detail += f" · {report.cached} from saved · {cost_line(report)}"
    console.print(ok_line(f"{fits} fit your rules", detail))
    if breaks:
        console.print(Text("   add --dropped to list the others", style="dim"))
    if unchecked := sum(row.verdict is None for row in rows):
        console.print(
            notice_line(
                f"{unchecked:,} jobs not checked", "narrow the search, or raise --top"
            )
        )
    if jobs.fetch_report is not None:
        _print_fetch_summary(console, jobs.fetch_report, jobs.new_jobs)
    if jobs.score_error is not None:
        console.print(failed_line(f"Checking did not run: {jobs.score_error}"))
        if (fix := _score_fix(jobs.score_error)) is not None:
            console.print(fix_line(fix))
    stopped_short = report is not None and report.stopped_by in _FAILING_STOPS
    if report is not None and report.stopped_by is not None:
        console.print(notice_line("Checking stopped early", report.stopped_by))
    return jobs.score_error is not None or stopped_short


def live_extract_command(
    service: Service, refs: Sequence[SourceRef]
) -> ExtractReport | None:
    """Have a model read the skipped jobs on the terminal, showing how far it got.

    Args:
        service: The open service.
        refs: The sources to read; empty means every source.

    Returns:
        What the run did, or `None` when it was stopped.

    Raises:
        MissingModelError: No model is set for the task.
        LlmError: The run could not start.
    """
    console = make_console()
    state = ExtractProgress(total=0, added=0, not_jobs=0, unread=0)

    def draw() -> Text:
        return extract_status(
            state.total, read=state.read, added=state.added, not_jobs=state.not_jobs
        )

    def progress(update: ExtractProgress) -> None:
        nonlocal state
        state = update

    report: ExtractReport | None = None
    with (
        Live(
            get_renderable=draw,
            console=console,
            refresh_per_second=_REFRESH_PER_SECOND,
            transient=True,
        ),
        suppress(KeyboardInterrupt),
    ):
        report = asyncio.run(service.extract_skipped(refs, on_progress=progress))
    if report is None:
        console.print(notice_line("Stopped", f"{state.added} jobs saved so far"))
        return None
    if not report.added and not report.not_jobs and not report.failed:
        console.print(ok_line("Nothing to read", "every skipped job has been read"))
        return report
    if report.failed:
        console.print(
            unread_table(
                (str(failure.row.source), failure.preview, failure.reason)
                for failure in report.failed
            )
        )
    console.print(
        ok_line(
            f"{report.added} jobs added",
            f"{report.not_jobs} not jobs, {len(report.failed)} still unread · "
            f"{cost_line(report.report)}",
        )
    )
    if report.added:
        console.print(next_line("ijob search", "ijob tui"))
    return report


def live_fetch_command(
    service: Service, refs: Sequence[SourceRef]
) -> RefreshReport | None:
    """Fetch the sources, printing a line as each one ends.

    A source that brought nothing new and has no problem prints no line, so the
    list stays worth reading.

    Args:
        service: The open service.
        refs: The sources to read; empty means every source.

    Returns:
        What the fetch did, or `None` when it was stopped.
    """
    console = make_console()
    state = FetchProgress(total=0, done=0, failed=0)
    new_jobs = 0

    def draw() -> Text:
        return fetch_status(state, new_jobs)

    def progress(update: FetchProgress) -> None:
        nonlocal state
        state = update

    report: RefreshReport | None = None
    with Live(
        get_renderable=draw,
        console=console,
        refresh_per_second=_REFRESH_PER_SECOND,
        transient=True,
    ) as live:

        def ended(event: SourceEvent) -> None:
            nonlocal new_jobs
            new_jobs += event.new
            if event.failure is not None:
                reason = failure_reason(event.failure.error)
                live.console.print(source_event_line(event, reason))
            elif event.new or event.rejected:
                live.console.print(source_event_line(event, None))

        with suppress(KeyboardInterrupt):
            report = asyncio.run(
                service.refresh_off_loop(refs, on_progress=progress, on_source=ended)
            )
    if report is None:
        console.print(notice_line("Stopped", f"+{new_jobs:,} new jobs saved so far"))
        return None
    if report.due:
        console.print(
            ok_line(
                f"{len(report.fetched)} of {len(report.due)} sources fetched",
                f"+{new_jobs:,} new jobs",
            )
        )
    else:
        console.print(ok_line("Every source is fresh", "nothing to fetch"))
    for ref, error in report.refused.items():
        console.print(failed_line(str(ref), str(error)))
    if report.stale:
        console.print(
            notice_line(
                f"{len(report.stale)} sources are too old for their jobs to show"
            )
        )
    return report


def live_score_command(
    service: Service, ids: Sequence[PostingId]
) -> OperationReport | None:
    """Score jobs on the terminal, showing each score as it arrives.

    Args:
        service: The open service.
        ids: The jobs to score.

    Returns:
        What the scoring did and spent, or `None` when it was stopped first.

    Raises:
        LlmError: Scoring could not run.
        UnknownPostingError: An id names no job.
    """
    console = make_console()
    items: dict[PostingId, ScoreItem] = {}
    place = {posting_id: number for number, posting_id in enumerate(ids)}

    def table() -> Group:
        ordered = sorted(
            items.values(),
            key=lambda item: (
                -item.score.score if item.score is not None else 1,
                place[item.posting.id],
            ),
        )
        rows = [
            (
                item.posting.id,
                item.posting.title,
                item.posting.company,
                item.score,
                STATUS_TEXT[ScoreStatus].get(item.status, item.status.value),
            )
            for item in ordered
        ]
        return Group(boxed("Scored against your CV  ·  best first", score_rows(rows)))

    def draw() -> Group:
        running = progress_text("scoring", len(items), len(place))
        return Group(table(), running)

    async def collect() -> None:
        async for item in service.score(ids):
            items[item.posting.id] = item

    with (
        Live(
            get_renderable=draw,
            console=console,
            refresh_per_second=_REFRESH_PER_SECOND,
            transient=True,
        ),
        suppress(KeyboardInterrupt),
    ):
        asyncio.run(collect())
    console.print(table())
    report = service.last_report
    if report is not None:
        detail = f"{report.cached} from saved scores · {cost_line(report)}"
        console.print(ok_line(f"{report.done + report.cached} scored", detail))
    return report
