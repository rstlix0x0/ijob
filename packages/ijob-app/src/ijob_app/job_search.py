"""A search that shows what is ready and keeps improving it.

`JobList` holds what a screen shows at any moment: every job found, its score,
how far fetching and scoring have got. `run_search` fills one in the order the
user should see things. The saved jobs and their saved scores come first, with
no network and no model, then fetching and scoring run side by side and each
result lands as it arrives. It calls `on_change` after each step and leaves the
drawing to its caller, so the command line and the terminal UI share it.

Stopping it early loses nothing: each source and each score is saved as it ends.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import import_module
from typing import Final

from ijob_ai.errors import LlmError
from ijob_core.errors import InvalidValueError
from ijob_core.model import Posting, PostingId, Score, SourceRef, Verdict

from ijob_app.config import FilterLayers
from ijob_app.llm_ops import OperationReport, ScoreStatus
from ijob_app.service import FetchProgress, RefreshReport, Service, SourceEvent

type JobView = tuple[FilterLayers, Sequence[SourceRef]]
"""The filters and the sources a list shows; no sources means every source."""

DEFAULT_SCORE_LIMIT: Final = 50
_LIST_AGAIN_AFTER_S: Final = 1.0


class ScoreState(StrEnum):
    """Where a job stands with its score."""

    NONE = "none"
    QUEUED = "queued"
    SCORED = "scored"
    MISSED = "missed"


@dataclass(slots=True)
class JobRow:
    """One job on the list.

    Attributes:
        posting: The job, without its description.
        score: Its score, or `None` before it has one.
        state: Where it stands with its score.
        missed: Why it has no score, once `state` is `MISSED`.
        is_new: Whether it arrived while this search ran.
        verdict: What a model made of it against the user's rules, if anything.
    """

    posting: Posting
    score: Score | None = None
    state: ScoreState = ScoreState.NONE
    missed: str = ""
    is_new: bool = False
    verdict: Verdict | None = None


def _verdict_rank(row: JobRow) -> int:
    """Rank a job by its verdict: the fits first, those that break a rule last."""
    if row.verdict is None:
        return 1
    return 0 if row.verdict.keep else 2


@dataclass(slots=True)
class JobList:
    """What a search has found so far.

    Attributes:
        rows: The jobs, by id.
        loaded: Whether the saved jobs have been read.
        fetch: How far fetching has got, or `None` when no fetch runs.
        events: The sources that have ended a fetch, oldest first.
        new_jobs: How many jobs fetching has added to the store.
        fetch_report: What the fetch did, once it ended.
        score_report: What the scoring did, once it ended.
        score_error: Why scoring or checking could not run, if it could not.
        hidden_from_scoring: How many jobs the limit left unscored.
        checking: How many jobs wait to be checked against the rules.
        judge_report: What checking the rules did, once it ended.
    """

    rows: dict[PostingId, JobRow] = field(default_factory=dict)
    loaded: bool = False
    fetch: FetchProgress | None = None
    events: list[SourceEvent] = field(default_factory=list)
    new_jobs: int = 0
    fetch_report: RefreshReport | None = None
    score_report: OperationReport | None = None
    score_error: LlmError | InvalidValueError | None = None
    hidden_from_scoring: int = 0
    checking: int = 0
    judge_report: OperationReport | None = None

    def set_jobs(self, postings: Sequence[Posting], *, mark_new: bool) -> None:
        """Replace the list of jobs, keeping what is known about each.

        A job that is gone from `postings` is closed and leaves the list.

        Args:
            postings: The open jobs that pass the filters, in list order.
            mark_new: Whether a job not on the list arrived during this search.
        """
        rows: dict[PostingId, JobRow] = {}
        for posting in postings:
            known = self.rows.get(posting.id)
            if known is None:
                rows[posting.id] = JobRow(posting, is_new=mark_new)
            else:
                known.posting = posting
                rows[posting.id] = known
        self.rows = rows
        self.loaded = True

    def set_saved_scores(self, scores: dict[PostingId, Score]) -> None:
        """Show the scores already stored for the jobs on the list."""
        for posting_id, score in scores.items():
            row = self.rows.get(posting_id)
            if row is not None and row.score is None:
                row.score = score
                row.state = ScoreState.SCORED

    def ranked(self) -> list[JobRow]:
        """List the jobs best first, then those being scored, then the rest.

        Rows with no score keep the list's newest-first order.
        """
        rows = list(self.rows.values())
        scored = [row for row in rows if row.score is not None]
        scored.sort(key=lambda row: -row.score.score if row.score else 0)
        waiting = [row for row in rows if row.state is ScoreState.QUEUED]
        rest = [
            row for row in rows if row.score is None and row.state is ScoreState.NONE
        ]
        missed = [row for row in rows if row.state is ScoreState.MISSED]
        ordered = [*scored, *waiting, *rest, *missed]
        if any(row.verdict is not None for row in ordered):
            ordered.sort(key=_verdict_rank)
        return ordered

    @property
    def queued(self) -> int:
        """How many jobs wait for a score."""
        return sum(row.state is ScoreState.QUEUED for row in self.rows.values())

    @property
    def scored(self) -> int:
        """How many jobs have a score."""
        return sum(row.score is not None for row in self.rows.values())


async def preload_models() -> None:
    """Load the model libraries off the event loop, so a screen does not freeze."""

    def load() -> None:
        import_module("ijob_ai.llm.registry")
        import_module("ijob_ai.tasks.scorer")
        import_module("ijob_ai.tasks.judge")

    await asyncio.to_thread(load)


async def score_jobs(
    service: Service,
    jobs: JobList,
    ids: Sequence[PostingId],
    on_change: Callable[[], None],
) -> None:
    """Score chosen jobs, showing each score as it arrives.

    A failure to run lands in `jobs.score_error`.

    Args:
        service: The open service.
        jobs: Holds the jobs and receives the scores.
        ids: The jobs to score.
        on_change: Called after each change to `jobs`.
    """
    jobs.score_error = None
    for posting_id in ids:
        row = jobs.rows.get(posting_id)
        if row is not None:
            row.state = ScoreState.QUEUED
    on_change()
    try:
        await preload_models()
        await _score_pass(service, jobs, ids, on_change)
    except (LlmError, InvalidValueError) as error:
        _score_failed(jobs, error)
    on_change()


async def judge_jobs(
    service: Service,
    jobs: JobList,
    ids: Sequence[PostingId],
    on_change: Callable[[], None],
) -> None:
    """Have a model check chosen jobs against the user's rules, as results arrive.

    A failure to run lands in `jobs.score_error`.

    Args:
        service: The open service.
        jobs: Holds the jobs and receives the verdicts.
        ids: The jobs to check.
        on_change: Called after each change to `jobs`.
    """
    jobs.score_error = None
    jobs.checking = len(ids)
    on_change()
    try:
        await preload_models()
        async for item in service.judge(ids):
            jobs.checking = max(jobs.checking - 1, 0)
            row = jobs.rows.get(item.posting.id)
            if row is not None:
                row.verdict = item.verdict
            on_change()
        jobs.judge_report = service.last_report
    except (LlmError, InvalidValueError) as error:
        jobs.score_error = error
    jobs.checking = 0
    on_change()


async def _score_pass(
    service: Service,
    jobs: JobList,
    ids: Sequence[PostingId],
    on_change: Callable[[], None],
) -> None:
    """Score one batch, showing each score as it arrives."""
    async for item in service.score(ids):
        row = jobs.rows.get(item.posting.id)
        if row is None:
            continue
        if item.score is None:
            row.state = ScoreState.MISSED
            row.missed = _status_text(item.status)
        else:
            row.score = item.score
            row.state = ScoreState.SCORED
        on_change()
    jobs.score_report = service.last_report


def _score_failed(jobs: JobList, error: LlmError | InvalidValueError) -> None:
    """Note why scoring could not run, and put the waiting jobs back."""
    jobs.score_error = error
    for row in jobs.rows.values():
        if row.state is ScoreState.QUEUED:
            row.state = ScoreState.NONE


def _status_text(status: ScoreStatus) -> str:
    """Say in a word or two why a job got no score."""
    return status.value.replace("_", " ")


class _Search:
    """One run of `run_search`: its inputs and the steps it takes."""

    def __init__(  # noqa: PLR0913  # the search's inputs, one each
        self,
        service: Service,
        view: Callable[[], JobView],
        refs: Sequence[SourceRef],
        jobs: JobList,
        on_change: Callable[[], None],
        *,
        score_limit: int | None,
    ) -> None:
        self._service = service
        self._view = view
        self._refs = refs
        self._jobs = jobs
        self._on_change = on_change
        self._score_limit = score_limit
        self._fetching = False
        self._list_wanted = asyncio.Event()

    async def list_jobs(self, *, mark_new: bool) -> None:
        """Read the jobs again and show them, under the filters in force by then."""
        while True:
            view = self._view()
            postings = await self._service.list_jobs(*view)
            if self._view() is view:
                break
        self._jobs.set_jobs(postings, mark_new=mark_new)
        self._on_change()

    def _source_ended(self, event: SourceEvent) -> None:
        """Note what a source's fetch did, and ask for the list again if it grew."""
        self._jobs.events.append(event)
        self._jobs.new_jobs += event.new
        if event.new:
            self._list_wanted.set()
        self._on_change()

    def _progress(self, progress: FetchProgress) -> None:
        """Show how far fetching has got."""
        self._jobs.fetch = progress
        self._on_change()

    async def _keep_listing(self) -> None:
        """Bring new jobs onto the list while fetching runs, at a calm pace."""
        while True:
            await self._list_wanted.wait()
            self._list_wanted.clear()
            await self.list_jobs(mark_new=True)
            await asyncio.sleep(_LIST_AGAIN_AFTER_S)

    async def fetch(self) -> None:
        """Fetch the sources that are due, bringing jobs in as each one is saved."""
        self._fetching = True
        lister = asyncio.create_task(self._keep_listing())
        try:
            self._jobs.fetch_report = await self._service.refresh_off_loop(
                self._refs, on_progress=self._progress, on_source=self._source_ended
            )
        finally:
            self._fetching = False
            lister.cancel()
        await self.list_jobs(mark_new=True)

    def _score_batch(self) -> list[PostingId]:
        """Queue the jobs still to score, up to what the limit leaves."""
        rows = list(self._jobs.rows.values())
        done = sum(row.state is ScoreState.SCORED for row in rows)
        room = None if self._score_limit is None else self._score_limit - done
        todo = [row for row in rows if row.state is ScoreState.NONE]
        chosen = todo if room is None else todo[: max(room, 0)]
        self._jobs.hidden_from_scoring = len(todo) - len(chosen)
        for row in chosen:
            row.state = ScoreState.QUEUED
        return [row.posting.id for row in chosen]

    async def score(self, fetcher: asyncio.Task[None] | None) -> None:
        """Score the jobs on the list, then those a fetch brought in."""
        try:
            ids = self._score_batch()
            self._on_change()
            if ids:
                await _score_pass(self._service, self._jobs, ids, self._on_change)
            if fetcher is not None:
                await asyncio.wait((fetcher,))
                if ids := self._score_batch():
                    self._on_change()
                    await _score_pass(self._service, self._jobs, ids, self._on_change)
        except (LlmError, InvalidValueError) as error:
            _score_failed(self._jobs, error)
        self._on_change()


async def run_search(  # noqa: PLR0913  # the search's inputs, one each
    service: Service,
    layers: FilterLayers,
    refs: Sequence[SourceRef],
    jobs: JobList,
    on_change: Callable[[], None],
    *,
    fetch: bool = True,
    score: bool = False,
    score_limit: int | None = DEFAULT_SCORE_LIMIT,
    latest_view: Callable[[], JobView] | None = None,
) -> None:
    """Fill `jobs`, showing what is ready at once and the rest as it arrives.

    Args:
        service: The open service.
        layers: The filters the config file and the flags set.
        refs: The sources to read; empty means every source.
        jobs: Receives the results.
        on_change: Called after each change to `jobs`.
        fetch: Whether to fetch the sources that are due.
        score: Whether to score the jobs against the CV.
        score_limit: The most jobs to score, newest first, or `None` for all;
            saved scores count towards it.
        latest_view: Reads the filters and sources on show, for a front end where
            they can change while a fetch runs; `None` keeps `layers` and `refs`
            throughout. `refs` always chooses what is fetched.

    Raises:
        UnknownSourceError: A named source has no row or was removed.
        InvalidValueError: A filter or the config file is unusable.
        ExceptionGroup: Fetching failed in an unexpected way.
        sqlite3.Error: A store read or write failed.
    """
    fixed: JobView = (layers, refs)
    view = latest_view or (lambda: fixed)
    search = _Search(service, view, refs, jobs, on_change, score_limit=score_limit)
    await search.list_jobs(mark_new=False)
    jobs.set_saved_scores(service.saved_scores())
    on_change()
    async with asyncio.TaskGroup() as group:
        fetcher = group.create_task(search.fetch()) if fetch else None
        if score:
            group.create_task(search.score(fetcher))
    jobs.set_saved_scores(service.saved_scores())
    on_change()
