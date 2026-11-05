"""The operations the command line and the terminal UI run on the job pipeline.

`Service` owns what a front end does with the stored data: choosing sources,
fetching the ones that are due, recording what they show, listing the open,
filtered postings, reading a posting's detail and description, voting on
postings, keeping the CV, judging and scoring postings with a model, running
the jury that labels postings, reading the recorded traces and managing the
source table. It returns values and raises `InvalidValueError` subclasses,
`SourcesError`, the model layer's `LlmError` subclasses or `OSError`; it neither
prints nor starts an event loop, so each front end keeps its own output, exit
codes and loop.

A recipe source is read through a client that refuses any request to a
non-public address, so its page address, its next pages and its job pages are
checked, while a refresh of every other source keeps the plain client. Adding
by URL and the analysis that learns a recipe for an unrecognized page use such a
client too, from their first request on, and the analysis stores nothing until
the caller accepts its proposal.

The source agent finds a source for a company without storing anything, and
`add_found` stores what the user confirms.

Every client the service builds shares one record of when each host was last
requested, so requests to one host are spaced apart even when they come from
different clients, such as `add_url` followed by `analyze_url`.

`open_service` builds a `Service` over the seeded database and the HTTP cache of
an ijob home. The store and the cache are bound to the thread that opens them,
so every `Service` method must run on that thread. The methods that reach the
network or call a model are `async` and run on the caller's event loop.
"""

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import threading
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Iterator,
    Sequence,
)
from contextlib import AsyncExitStack, closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from ijob_core.dedupe import normalise_company
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalTask
from ijob_core.filters import FilterSpec, keep
from ijob_core.model import (
    ExternalId,
    Instructions,
    Label,
    Posting,
    PostingId,
    ResumeProfile,
    Score,
    SourceKind,
    SourceRef,
    StoredCv,
    Vote,
)
from ijob_core.store import RecordResult, SourceOrigin, SourceRow, Store
from ijob_core.traces import PruneReport, TraceSummary, TraceTree
from ijob_sources.collect import (
    CollectResult,
    Source,
    SourceBatch,
    SourceFailure,
    collect,
)
from ijob_sources.details import describe
from ijob_sources.detect import resolve_url, verify_new_source
from ijob_sources.errors import FetchedPage, SourcesError
from ijob_sources.http_cache import SqliteHttpCache
from ijob_sources.manual import manual_key
from ijob_sources.polite_http import ConditionalCache, HostPacing, PoliteClient
from ijob_sources.public_host import (
    HostResolver,
    public_host_guard,
    system_resolver,
)
from ijob_sources.registry import build_source, refresh_interval
from ijob_sources.seeds import load_seeds

from ijob_app import agent_ops, recipe_ops
from ijob_app.agent_ops import ExploreResult, FoundSource, SourceResult
from ijob_app.agent_site import SiteRule, site_guard
from ijob_app.config import FilterLayers, effective_filters
from ijob_app.extract_ops import ExtractProgress, ExtractReport, extract_operation
from ijob_app.home import IjobHome
from ijob_app.llm_ops import (
    ItemOutcome,
    JudgeStatus,
    LlmOptions,
    LlmRuntime,
    OperationReport,
    Proposal,
    ScoreItem,
    SearchItem,
    accept_proposal,
    instructions_model,
    judge_operation,
    list_traces,
    profile_operation,
    propose_operation,
    prune_traces,
    read_trace,
    score_operation,
)
from ijob_app.recipe_ops import RecipeProposal

if TYPE_CHECKING:
    from ijob_ai.agents.react import StepCallback
    from ijob_ai.llm.reference import ModelRef
    from ijob_ai.llm.settings import EvalSettings
    from ijob_ai.tuning.names import Optimizer
    from ijob_core.eval_records import EvalLabel, EvalVote
    from ijob_core.tune_records import TunedProgram

    from ijob_app.eval_estimate import Cost
    from ijob_app.eval_ops import (
        LabelPlan,
        LabelReport,
        ReviewItem,
        ReviewPlan,
        VoteItem,
    )
    from ijob_app.eval_report import EvalReport, RunDetail
    from ijob_app.eval_run import RunOffer, RunProgress
    from ijob_app.eval_suites import Suite, SuiteReport
    from ijob_app.trace_export import ExportReport
    from ijob_app.tune_grid import (
        Combination,
        CombinationResult,
        GridOffer,
        GridReport,
        ProgramLine,
        TuneProgress,
    )
    from ijob_app.tune_ops import (
        AcceptOffer,
        AcceptReport,
        PairOutcome,
        ScreenOffer,
        ScreenPair,
        ScreenReport,
    )
    from ijob_app.tune_report import TuneReport

type ClientFactory = Callable[[ConditionalCache | None], PoliteClient]

MAX_CV_BYTES: Final = 20_000_000
_PDF_MAGIC: Final = b"%PDF-"
_CV_FILE_NAME: Final = "cv.pdf"
_STOP_WAIT_S: Final = 5.0


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchProgress:
    """How far fetching the sources has got.

    Attributes:
        total: The sources being fetched.
        done: The sources that have ended, failed ones included.
        failed: The sources that failed.
    """

    total: int
    done: int
    failed: int


@dataclass(frozen=True, slots=True)
class LabelOffer:
    """What a jury run will do and cost, worked out before any model is called.

    Attributes:
        plan: The label set and the votes still missing.
        settings: The eval settings the run reads its jury and caps from.
        cap: What the run may spend on OpenRouter, in US dollars.
        costs: Each juror with the cost estimated for its calls.
        estimate: The estimated cost of the whole run.
    """

    plan: "LabelPlan"
    settings: "EvalSettings"
    cap: float
    costs: "tuple[tuple[ModelRef, Cost], ...]"
    estimate: "Cost"


@dataclass(frozen=True, slots=True, kw_only=True)
class LabelProgress:
    """How far a jury run has got.

    Attributes:
        stored: Votes cast and stored so far.
        total: Votes asked for.
        spent: OpenRouter spend of the run so far, in US dollars.
    """

    stored: int
    total: int
    spent: float


class UnknownSourceError(InvalidValueError):
    """A named source has no row in the table, or was removed."""

    def __init__(self, reason: str) -> None:
        """Build the error from the reason, which names the source.

        Args:
            reason: Why the source cannot be used, naming it.
        """
        super().__init__("source", reason)


class CacheUnavailableError(InvalidValueError):
    """The HTTP cache file cannot be opened."""

    def __init__(self, path: Path, reason: str) -> None:
        """Build the error from the cache path and the underlying reason.

        Args:
            path: The cache file that could not be opened.
            reason: What the filesystem or SQLite reported.
        """
        super().__init__("cache", f"cannot open the HTTP cache {path}: {reason}")


class SourceCompanyError(InvalidValueError):
    """A source reference is already in the table for a different company."""

    def __init__(self, ref: SourceRef, company: str) -> None:
        """Build the error from the reference and the company that holds it.

        Args:
            ref: The reference that was to be added.
            company: The company the table already has it for.
        """
        super().__init__("source", f"{ref} is already a source for {company}")


class RecipeAlreadyAddedError(SourcesError):
    """A URL's recipe source is already added and was not removed."""

    def __init__(self, ref: SourceRef) -> None:
        """Build the error from the source that exists.

        Args:
            ref: The recipe source that is already added.
        """
        super().__init__(f"{ref} is already added; run: ijob sources reanalyze {ref}")
        self.ref = ref


class NoRecipeError(InvalidValueError):
    """A source that has no recipe was named where a recipe source is needed."""

    def __init__(self, ref: SourceRef) -> None:
        """Build the error from the source that has no recipe.

        Args:
            ref: The source that is not a recipe source.
        """
        super().__init__("source", f"{ref} has no recipe")


class InvalidCvError(InvalidValueError):
    """A CV file cannot be stored."""

    def __init__(self, reason: str) -> None:
        """Build the error from the reason, which names the file.

        Args:
            reason: Why the file cannot be stored.
        """
        super().__init__("cv", reason)


class UnknownPostingError(InvalidValueError):
    """A posting id names no posting in the store."""

    def __init__(self, posting_id: PostingId) -> None:
        """Build the error from the unknown id.

        Args:
            posting_id: The id that names no posting.
        """
        super().__init__("posting", f"no posting {posting_id}")


@dataclass(frozen=True, slots=True)
class PostingDetail:
    """A posting with everything the detail views show about it.

    Attributes:
        posting: The posting, with its description when one is known.
        sources: The sources that have shown it, sorted by their text form.
        label: The user's vote on it, or `None`.
        description: The posting's description as Markdown, or `None` when none
            could be found.
        unavailable_reason: Why `description` is `None`: the failure that kept
            it from being fetched, or that no source publishes one. `None` when
            the description is present.
    """

    posting: Posting
    sources: list[SourceRef]
    label: Label | None
    description: str | None
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceEvent:
    """One source ended a fetch.

    Attributes:
        ref: The source.
        new: How many postings it added.
        listed: How many jobs it showed.
        rejected: How many jobs it dropped as unusable.
        failure: Why it failed, or `None` when it was saved.
    """

    ref: SourceRef
    new: int = 0
    listed: int = 0
    rejected: int = 0
    failure: SourceFailure | None = None


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What one refresh did, source by source.

    Attributes:
        due: The chosen sources that were due for a fetch, in order.
        kept: The chosen sources left alone because their last fetch is more
            recent than their kind's refresh interval.
        fetched: The sources fetched and recorded, in the order of `due`.
        rejected: How many jobs each fetched source dropped as unusable; only
            sources that dropped at least one appear.
        failures: The sources whose fetch failed, and why.
        refused: The sources the registry could not build, and why.
        stale: The chosen sources whose last fetch is too old for their
            postings to show.
    """

    due: list[SourceRow]
    kept: list[SourceRow]
    fetched: list[SourceRef]
    rejected: dict[SourceRef, int]
    failures: list[SourceFailure]
    refused: dict[SourceRef, InvalidValueError]
    stale: list[SourceRow]


def _read_cv(path: Path) -> bytes:
    """Read a CV file, checking that it is a PDF of an allowed size.

    Args:
        path: The file to read, with `~` already expanded.

    Returns:
        The file's bytes.

    Raises:
        InvalidCvError: The file cannot be read, is larger than `MAX_CV_BYTES`,
            or does not start with the PDF signature.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CV_BYTES + 1)
    except OSError as error:
        reason = f"cannot read {path}: {error}"
        raise InvalidCvError(reason) from error
    if len(data) > MAX_CV_BYTES:
        reason = f"{path} is larger than {MAX_CV_BYTES // 1_000_000} MB"
        raise InvalidCvError(reason)
    if not data.startswith(_PDF_MAGIC):
        reason = f"{path} is not a PDF: it does not start with %PDF-"
        raise InvalidCvError(reason)
    return data


def _write_atomically(target: Path, data: bytes) -> None:
    """Write `data` to `target` through a temporary file beside it, then replace.

    Args:
        target: The file to create or replace.
        data: The bytes to write.

    Raises:
        OSError: The temporary file cannot be written or moved; `target` is left
            as it was and no temporary file remains.
    """
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f"{target.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(target)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _read_existing(target: Path) -> bytes | None:
    """Read `target` if it exists; `None` when there is no such file."""
    try:
        return target.read_bytes()
    except FileNotFoundError:
        return None


def _restore(target: Path, previous: bytes | None) -> None:
    """Put `target` back as it was, as far as the filesystem allows.

    Args:
        target: The file to restore.
        previous: Its earlier bytes, or `None` if it did not exist.
    """
    with suppress(OSError):
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            _write_atomically(target, previous)


def _new_row(ref: SourceRef, company: str) -> SourceRow:
    """Describe a source about to be added, so the registry can build it."""
    return SourceRow(
        ref=ref,
        company=company,
        target_url=None,
        origin=SourceOrigin.USER,
        removed=False,
        last_fetched_at=None,
    )


def _split_by_interval(
    rows: list[SourceRow], at: datetime
) -> tuple[list[SourceRow], list[SourceRow]]:
    """Separate the sources due for a fetch from those fetched recently enough.

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
        else:
            due.append(row)
    return due, kept


class Service:
    """The pipeline operations over one open store, cache and clock.

    A service is used only on the thread that opened its store, and its `async`
    methods only on one event loop at a time.
    """

    def __init__(  # noqa: PLR0913  # the resources a service is built over, one each
        self,
        store: Store,
        *,
        db_path: Path,
        client_factory: ClientFactory,
        cache: ConditionalCache | None,
        now: Callable[[], datetime],
        home: IjobHome,
        llm: LlmOptions,
        resolver: HostResolver = system_resolver,
    ) -> None:
        """Build a service over open resources it does not own.

        Args:
            store: The open store, with the seed sources imported.
            db_path: The database file the store was opened on.
            client_factory: Builds the client a fetch goes through; the
                service has every client it builds share its host pacing.
            cache: The HTTP cache the fetching clients revalidate against, or
                `None` for none.
            now: The wall clock, returning timezone-aware times.
            home: The ijob home, which holds the trace database.
            llm: What the model-calling operations are given.
            resolver: Looks up the addresses of a host for the check that keeps
                a recipe source on public hosts.
        """
        self._store = store
        self._db_path = db_path
        self._client_factory = client_factory
        self._cache = cache
        self._now = now
        self._home = home
        self._llm = llm
        self._resolver = resolver
        self._pacing = HostPacing()
        self._llm_runtime: LlmRuntime | None = None
        self.last_report: OperationReport | None = None

    async def refresh(
        self,
        refs: Sequence[SourceRef],
        *,
        on_progress: Callable[[FetchProgress], None] | None = None,
        on_source: Callable[[SourceEvent], None] | None = None,
    ) -> RefreshReport:
        """Fetch the chosen sources that are due and record what they show.

        Each source is saved the moment its fetch ends, on its own connection
        off the event loop, so its jobs can be listed while the others are still
        fetching and stopping early keeps them. A source's sightings and fetch
        time are stamped with the one time it was saved.

        Args:
            refs: The sources to read; empty means every source in the table.
            on_progress: Called once before the first fetch and as each source
                ends.
            on_source: Called as each source is saved or fails.

        Returns:
            What was fetched, kept, rejected, refused and found stale.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
            ExceptionGroup: Saving a source failed, or a source raised something
                other than a `SourcesError`; the group holds that exception.
            sqlite3.Error: A store read failed.
        """
        rows = self._choose_sources(refs)
        due, kept = _split_by_interval(rows, self._now())
        sources, refused = self._build_sources(due)
        saving = asyncio.Lock()

        async def save(ref: SourceRef, batch: SourceBatch) -> None:
            async with saving:
                result = await asyncio.to_thread(
                    self._record_source, ref, batch, self._now()
                )
            if on_source is not None:
                on_source(
                    SourceEvent(
                        ref=ref,
                        new=result.new,
                        listed=len(batch.drafts) + len(batch.still_listed),
                        rejected=batch.rejected,
                    )
                )

        def failed(failure: SourceFailure) -> None:
            if on_source is not None:
                on_source(SourceEvent(ref=failure.ref, failure=failure))

        result = await self._collect(sources, on_progress, save, failed)
        fetched = [row.ref for row in due if row.ref in result.batches]
        return RefreshReport(
            due=due,
            kept=kept,
            fetched=fetched,
            rejected={
                ref: rejected
                for ref in fetched
                if (rejected := result.batches[ref].rejected) > 0
            },
            failures=list(result.failures),
            refused=refused,
            stale=self._stale(rows, self._now()),
        )

    async def refresh_off_loop(
        self,
        refs: Sequence[SourceRef],
        *,
        on_progress: Callable[[FetchProgress], None],
        on_source: Callable[[SourceEvent], None],
    ) -> RefreshReport:
        """Fetch as `refresh` does, on a thread and an event loop of its own.

        Reading a source's jobs is work for the processor, so doing it on the
        caller's loop would freeze whatever that loop also draws. Both callbacks
        run on the caller's loop. Cancelling this stops the fetch; each source
        already saved stays saved.

        Args:
            refs: The sources to read; empty means every source in the table.
            on_progress: Called once before the first fetch and as each source
                ends.
            on_source: Called as each source is saved or fails.

        Returns:
            What was fetched, kept, rejected, refused and found stale.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
            ExceptionGroup: Saving a source failed, or a source raised something
                other than a `SourcesError`; the group holds that exception.
            sqlite3.Error: A store read failed.
        """
        here = asyncio.get_running_loop()
        started = threading.Event()
        there: list[asyncio.AbstractEventLoop] = []
        running: list[asyncio.Task[RefreshReport]] = []

        def progress(now: FetchProgress) -> None:
            here.call_soon_threadsafe(on_progress, now)

        def source(event: SourceEvent) -> None:
            here.call_soon_threadsafe(on_source, event)

        async def fetch(service: Service) -> RefreshReport:
            there.append(asyncio.get_running_loop())
            task = asyncio.current_task()
            if task is not None:
                running.append(cast("asyncio.Task[RefreshReport]", task))
            started.set()
            return await service.refresh(refs, on_progress=progress, on_source=source)

        def run() -> RefreshReport:
            with open_service(
                self._home,
                self._db_path,
                self._client_factory,
                now=self._now,
                use_cache=self._cache is not None,
                llm=self._llm,
                resolver=self._resolver,
            ) as service:
                return asyncio.run(fetch(service))

        try:
            return await asyncio.to_thread(run)
        except asyncio.CancelledError:
            started.wait(_STOP_WAIT_S)
            if there and running:
                there[0].call_soon_threadsafe(running[0].cancel)
            raise

    async def list_jobs(
        self, layers: FilterLayers, refs: Sequence[SourceRef]
    ) -> list[Posting]:
        """List the open postings that pass the filters, without descriptions.

        This is the fast read a list is made from: it holds no description, so
        read a posting with `detail` or `score` it to get at the text.

        Args:
            layers: The filters the config file and the flags set.
            refs: The sources to read; empty means every source.

        Returns:
            The postings that pass the layers and the stored instructions'
            filters, newest first.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
            InvalidValueError: A filter in the stored instructions is unusable.
            sqlite3.Error: A store read failed.
        """
        stored = self._store.instructions()
        layer = None if stored is None else json.loads(stored.filters_json)
        spec, _ = effective_filters(layers, layer)
        self._choose_sources(refs)
        at = self._now()

        def read() -> list[Posting]:
            with Store.open(self._db_path) as store:
                postings = store.list_open(now=at, sources=refs or None)
            return [posting for posting in postings if keep(posting, spec, now=at)]

        return await asyncio.to_thread(read)

    def source_job_counts(self) -> dict[str, int]:
        """Count the jobs each source has shown, keyed by its reference text."""
        counts: dict[str, int] = {}
        for refs in self._store.sources_by_posting().values():
            for ref in refs:
                counts[str(ref)] = counts.get(str(ref), 0) + 1
        return counts

    def saved_detail(self, posting_id: PostingId) -> PostingDetail:
        """Read a posting's detail as stored, with no network.

        Args:
            posting_id: The posting to read.

        Returns:
            The posting with its sources, label and any stored description.

        Raises:
            UnknownPostingError: The id names no posting.
        """
        posting = self._require_posting(posting_id)
        return PostingDetail(
            posting=posting,
            sources=self._store.posting_sources(posting_id),
            label=self._store.label(posting_id),
            description=posting.description,
            unavailable_reason=None,
        )

    def job_sources(self) -> dict[PostingId, list[SourceRef]]:
        """Read which sources have shown each posting, in one query."""
        return self._store.sources_by_posting()

    def saved_scores(self) -> dict[PostingId, Score]:
        """Read the newest stored score of each posting against the current profile.

        The scores are those of the scorer prompt in use, so one list never mixes
        prompts. With no accepted scorer program they are the base prompt's, read
        from the store alone. With one, the settings tell whether it is in force,
        and the scores are its own while it is. They are the settings the model
        runtime was built with once a model operation has run, so a listing and
        the next operation agree on the prompt; else the config file's.

        Returns:
            The scores; empty when no profile is stored.

        Raises:
            InvalidValueError: The accepted scorer program is malformed, or its
                instructions lack the untrusted-data sentence, and the message
                names its id; or, with an accepted program, the config file is
                unusable.
            sqlite3.Error: The store cannot be read.
        """
        stored = self._store.cv_profile()
        if stored is None:
            return {}
        sha256 = stored.profile.sha256()
        if self._store.tunes.accepted(EvalTask.SCORER) is None:
            return self._store.saved_scores(sha256)
        from ijob_app.llm_config import read_llm_settings  # noqa: PLC0415
        from ijob_app.tune_ops import program_in_force  # noqa: PLC0415

        settings = (
            read_llm_settings(self._llm.config_path)
            if self._llm_runtime is None
            else self._llm_runtime.settings
        )
        program = program_in_force(self._store, settings, EvalTask.SCORER)
        return self._store.saved_scores(
            sha256, version=None if program is None else program.version
        )

    def _program_in_force(self, task: EvalTask) -> "TunedProgram | None":
        """Return the task's accepted program while it is in force, else `None`.

        Raises:
            InvalidValueError: The accepted program is malformed, or its
                instructions lack the untrusted-data sentence; the message names
                its id. The base prompt is not used in its place.
            ProviderUnavailableError: A variable that redirects model calls is set.
        """
        from ijob_app.tune_ops import program_in_force  # noqa: PLC0415

        if self._store.tunes.accepted(task) is None:
            return None
        return program_in_force(self._store, self._runtime().settings, task)

    async def search_postings(
        self, layers: FilterLayers, refs: Sequence[SourceRef]
    ) -> list[Posting]:
        """List the postings a search goes through, before any judging.

        The read runs off the event loop on its own connection, as a large store
        takes seconds to read and a screen must not freeze meanwhile.

        Args:
            layers: The filters the config file and the flags set.
            refs: The sources to read; empty means every source.

        Returns:
            The open postings that pass the layers and the stored instructions.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
            InvalidValueError: A filter in the stored instructions is unusable.
            sqlite3.Error: A store read failed.
        """
        stored = self._store.instructions()
        layer = None if stored is None else json.loads(stored.filters_json)
        spec, _ = effective_filters(layers, layer)
        self._choose_sources(refs)
        at = self._now()

        def read() -> list[Posting]:
            with Store.open(self._db_path) as store:
                postings = store.open_postings(now=at, sources=refs or None)
            return [posting for posting in postings if keep(posting, spec, now=at)]

        return await asyncio.to_thread(read)

    def open_postings(
        self, spec: FilterSpec, refs: Sequence[SourceRef]
    ) -> list[Posting]:
        """List the open postings that pass the filters.

        Args:
            spec: The filters a posting must pass.
            refs: The sources to read; empty means every source.

        Returns:
            The postings, newest first.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
        """
        self._choose_sources(refs)
        at = self._now()
        postings = self._store.open_postings(now=at, sources=refs or None)
        return [posting for posting in postings if keep(posting, spec, now=at)]

    async def search(
        self,
        layers: FilterLayers,
        refs: Sequence[SourceRef],
        *,
        listed: Sequence[Posting] | None = None,
    ) -> AsyncIterator[SearchItem]:
        """List the open postings that pass the filters, judged by the user's rules.

        The filters in force are `layers` merged with the stored instructions'
        filters. With no stored rules the postings are yielded as they are listed,
        with status `not_run`, and no model module is loaded. With rules, a
        posting whose verdict is stored comes first, as `cached`, and the rest
        are judged and yielded in the order they finish.

        `last_report` is `None` while the iteration runs and when no rules are
        stored; otherwise it holds what the judging did once the iteration ends.
        Abandoning the iteration early cancels the calls still running.

        Args:
            layers: The filters the packaged config, the user's file and the
                flags set.
            refs: The sources to read; empty means every source.
            listed: What `search_postings` already returned for these filters,
                so they are not read twice.

        Yields:
            One item per posting.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
            InvalidValueError: A filter in the stored instructions is unusable,
                or the config file is unusable.
            MissingModelError: Rules are stored and no model is set for the
                `judge` task.
            ProviderUnavailableError: A variable that redirects model calls is
                set. A `claude` CLI that is missing or too old does not raise;
                the items it stops come back as `provider_error`.
            sqlite3.Error: A store read or write failed.
        """
        self.last_report = None
        stored = self._store.instructions()
        postings = (
            await self.search_postings(layers, refs) if listed is None else listed
        )
        if stored is None or not stored.rules:
            for posting in postings:
                yield SearchItem(
                    posting, None, JudgeStatus.NOT_RUN, posting.description is None
                )
            return
        async for item in judge_operation(
            self._runtime(),
            self._store,
            postings,
            stored,
            now=self._now,
            on_report=self._keep_report,
            program=self._program_in_force(EvalTask.JUDGE),
        ):
            yield item

    async def score(self, ids: Sequence[PostingId]) -> AsyncIterator[ScoreItem]:
        """Score postings against the stored CV profile, on demand.

        Repeated ids are scored once, in the order they first appear. A posting
        whose score is stored for this posting text, this profile, this model and
        this task version comes first, as `cached`; the rest are scored and
        yielded in the order they finish. When no profile is stored, or the stored
        one was read from an earlier CV, it is built from the CV first, inside
        the same budget and trace.

        `last_report` is `None` while the iteration runs and holds what the
        scoring did once it ends.

        Args:
            ids: The postings to score.

        Yields:
            One item per distinct id.

        Raises:
            UnknownPostingError: An id names no posting; no call is made.
            NoCvError: No CV is stored.
            MissingModelError: No model is set for the `scorer` task, or for the
                `cv_profile` task when the profile must be built.
            CvTextError: The CV text cannot be read or used.
            ProviderUnavailableError: A variable that redirects model calls is
                set. A `claude` CLI that is missing or too old does not raise;
                the items it stops come back as `provider_error`.
            InvalidValueError: The config file is unusable.
            sqlite3.Error: A store read or write failed.
        """
        self.last_report = None
        postings = [
            self._require_posting(posting_id) for posting_id in dict.fromkeys(ids)
        ]
        async for item in score_operation(
            self._runtime(),
            self._store,
            postings,
            self._db_path.parent / _CV_FILE_NAME,
            now=self._now,
            on_report=self._keep_report,
            program=self._program_in_force(EvalTask.SCORER),
        ):
            yield item

    async def judge(self, ids: Sequence[PostingId]) -> AsyncIterator[SearchItem]:
        """Have a model check chosen postings against the user's stored rules.

        Args:
            ids: The postings to check.

        Yields:
            One item per distinct id, or none when no rules are stored.

        Raises:
            UnknownPostingError: An id names no posting; no call is made.
            MissingModelError: No model is set for the `judge` task.
            ProviderUnavailableError: A variable that redirects model calls is
                set.
            InvalidValueError: The config file is unusable.
            sqlite3.Error: A store read or write failed.
        """
        self.last_report = None
        stored = self._store.instructions()
        if stored is None or not stored.rules:
            return
        postings = [
            self._require_posting(posting_id) for posting_id in dict.fromkeys(ids)
        ]
        async for item in judge_operation(
            self._runtime(),
            self._store,
            postings,
            stored,
            now=self._now,
            on_report=self._keep_report,
            program=self._program_in_force(EvalTask.JUDGE),
        ):
            yield item

    def traces(self, limit: int = 20) -> list[TraceSummary]:
        """List the recorded traces, newest first.

        Args:
            limit: The most traces to list.

        Returns:
            The summaries, newest first.

        Raises:
            InvalidValueError: `limit` is negative.
            sqlite3.Error: The trace database cannot be opened or read.
        """
        return list_traces(self._home, limit)

    def trace(self, trace_id: str) -> TraceTree:
        """Read one trace as nested spans.

        Args:
            trace_id: The trace to read, as `traces` lists it.

        Returns:
            The trace's spans nested under their parents.

        Raises:
            UnknownTraceError: No span of that trace is recorded.
            sqlite3.Error: The trace database cannot be opened or read.
        """
        return read_trace(self._home, trace_id)

    def prune_traces(self) -> PruneReport:
        """Delete the oldest traces until the database fits `[tracing].max_size_mb`.

        Returns:
            How many traces were deleted and the file size afterwards.

        Raises:
            InvalidValueError: The config file is unusable.
            sqlite3.Error: The trace database cannot be opened or pruned.
        """
        return prune_traces(self._home, self._llm.config_path)

    def traces_export(self) -> "ExportReport":
        """Send traces, labels, runs and scores to the local Phoenix.

        It makes no model call, and every step can run again.

        Returns:
            What was sent, what was added and what was already there.

        Raises:
            InvalidValueError: The config file is unusable, or `[phoenix].endpoint`
                is not local.
            PhoenixError: Phoenix is not running, refused a request or a span
                batch, gave a reply that cannot be read, or a stored span id is
                not hexadecimal.
            sqlite3.Error: The trace database cannot be opened or read.
        """
        from ijob_ai.tracing.phoenix import phoenix_client  # noqa: PLC0415
        from ijob_core.traces import TraceStore  # noqa: PLC0415

        from ijob_app.llm_config import read_phoenix_settings  # noqa: PLC0415
        from ijob_app.trace_export import export_all  # noqa: PLC0415

        settings = read_phoenix_settings(self._llm.config_path)
        with (
            closing(phoenix_client(settings.endpoint)) as client,
            TraceStore.open(self._home.traces) as traces,
        ):
            return export_all(self._store, traces, client)

    def _keep_report(self, report: OperationReport) -> None:
        """Remember the report of the operation that just ended."""
        self.last_report = report

    async def detail(self, posting_id: PostingId) -> PostingDetail:
        """Read a posting with its sources, label and description.

        A posting stored without a description has one looked up from its
        sightings, in the order they were first recorded: the first text found
        is stored and returned. A source that fails is reported in
        `unavailable_reason` and nothing is stored, so the next call tries
        again; the remaining sightings are still tried.

        Args:
            posting_id: The posting to read.

        Returns:
            The posting's detail.

        Raises:
            UnknownPostingError: The id names no posting.
            sqlite3.Error: A store read or write failed.
        """
        posting = self._require_posting(posting_id)
        description = posting.description
        reason: str | None = None
        if description is None:
            description, reason = await self._describe(posting_id)
            posting = self._require_posting(posting_id)
        return PostingDetail(
            posting=posting,
            sources=self._store.posting_sources(posting_id),
            label=self._store.label(posting_id),
            description=description,
            unavailable_reason=reason,
        )

    def labels(self, ids: Collection[PostingId]) -> dict[PostingId, Label]:
        """Read the votes on several postings, touching neither description nor network.

        Args:
            ids: The postings to read.

        Returns:
            The labels by posting id; postings without one are absent.
        """
        return self._store.labels(ids)

    async def label(
        self, posting_id: PostingId, vote: Vote, note: str | None
    ) -> PostingDetail:
        """Record the user's vote on a posting, then read its detail.

        Args:
            posting_id: The posting voted on.
            vote: The verdict.
            note: Why, or `None`.

        Returns:
            The posting's detail, with the new label and the description step
            of `detail` done.

        Raises:
            UnknownPostingError: The id names no posting.
            InvalidValueError: `note` is blank.
            sqlite3.Error: A store read or write failed.
        """
        self._require_posting(posting_id)
        self._store.set_label(posting_id, vote, note, at=self._now())
        return await self.detail(posting_id)

    def clear_label(self, posting_id: PostingId) -> None:
        """Remove the vote on a posting; a posting without one is left as it is.

        Args:
            posting_id: The posting whose vote to remove.

        Raises:
            UnknownPostingError: The id names no posting.
            sqlite3.Error: The write failed.
        """
        self._require_posting(posting_id)
        self._store.clear_label(posting_id)

    def store_cv(self, path: Path) -> StoredCv:
        """Keep a copy of the user's CV beside the database and record it.

        The copy is `cv.pdf` in the database file's directory, written through
        a temporary file and then moved into place, so an earlier CV is replaced
        only by a complete one. A file that is refused, or a record that cannot
        be written, leaves the earlier copy and record as they were.

        Args:
            path: The PDF to store; a leading `~` is expanded.

        Returns:
            The record of the stored CV.

        Raises:
            InvalidCvError: The file cannot be read, is larger than
                `MAX_CV_BYTES`, or does not start with `%PDF-`.
            OSError: The copy beside the database cannot be written. This is
                not a fault of the argument, so it is not an `InvalidCvError`.
            sqlite3.Error: The record could not be written.
        """
        source = path.expanduser()
        data = _read_cv(source)
        target = self._db_path.parent / _CV_FILE_NAME
        previous = _read_existing(target)
        _write_atomically(target, data)
        cv = StoredCv(
            name=source.name,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            stored_at=self._now(),
        )
        try:
            self._store.set_cv(cv)
        except sqlite3.Error:
            _restore(target, previous)
            raise
        return cv

    def cv(self) -> StoredCv | None:
        """Read the record of the stored CV.

        Returns:
            The record, or `None` if no CV was stored.
        """
        return self._store.cv()

    def _runtime(self) -> LlmRuntime:
        """Return the LLM runtime, building it on the first model operation.

        Raises:
            ProviderUnavailableError: A variable that redirects model calls is set.
            InvalidValueError: The config file is unreadable, or a setting in it
                is unknown or out of range.
        """
        if self._llm_runtime is None:
            self._llm_runtime = LlmRuntime.create(self._home, self._llm)
        return self._llm_runtime

    async def build_profile(
        self, *, refresh: bool
    ) -> tuple[ResumeProfile, OperationReport]:
        """Return the profile of the stored CV, reading the CV only when needed.

        The stored profile is reused while the CV, the task version and the
        `cv_profile` model are unchanged, unless `refresh`.

        Args:
            refresh: Whether to read the CV again even if the profile is current.

        Returns:
            The profile and what the operation did and spent.

        Raises:
            NoCvError: No CV is stored.
            MissingModelError: No model is set for the `cv_profile` task.
            CvTextError: The CV text cannot be read or used.
            TaskOutputError: The model's answer is unusable.
            ProviderUnavailableError: The provider cannot be used.
            ProviderFailedError: The provider failed the call.
            CostUnknownError: The cost of the call cannot be determined.
            InvalidValueError: The config file is unusable.
            sqlite3.Error: The store cannot be read or written.
        """
        return await profile_operation(
            self._runtime(),
            self._store,
            self._db_path.parent / _CV_FILE_NAME,
            refresh=refresh,
            at=self._now(),
        )

    def cv_profile(self) -> ResumeProfile | None:
        """Read the stored CV profile, which may be from an earlier CV.

        Returns:
            The profile, or `None` if none was built.
        """
        stored = self._store.cv_profile()
        return None if stored is None else stored.profile

    async def propose_instructions(self, layers: FilterLayers, text: str) -> Proposal:
        """Work out what the user's instructions would set, without storing them.

        Args:
            layers: The filters the packaged config, the user's file and the
                flags set.
            text: The instructions the user wrote.

        Returns:
            The proposal for the user to confirm with `accept_instructions`.

        Raises:
            InvalidValueError: `text` is blank or too long, a filter the
                instructions imply is unusable, or the config file is unusable.
            MissingModelError: No model is set for the `instructions` task.
            NothingActionableError: The instructions give neither a filter nor
                a rule.
            TaskOutputError: The model's answer is unusable.
            ProviderUnavailableError: The provider cannot be used.
            ProviderFailedError: The provider failed the call.
            CostUnknownError: The cost of the call cannot be determined.
            sqlite3.Error: The store cannot be read.
        """
        return await propose_operation(self._runtime(), self._store, layers, text)

    def accept_instructions(self, proposal: Proposal) -> None:
        """Store a confirmed proposal as the user's instructions.

        Args:
            proposal: The proposal `propose_instructions` returned.

        Raises:
            MissingModelError: No model is set for the `instructions` task.
            ProviderUnavailableError: A variable that redirects model calls is
                set.
            InvalidValueError: The instructions break a limit of the stored
                form, or the config file is unusable.
            sqlite3.Error: The store cannot be written.
        """
        accept_proposal(
            self._store,
            proposal,
            model=instructions_model(self._runtime()),
            at=self._now(),
        )

    def instructions(self) -> Instructions | None:
        """Read the stored instructions.

        Returns:
            The instructions, or `None` if none were stored.
        """
        return self._store.instructions()

    def clear_instructions(self) -> None:
        """Remove the stored instructions; clearing with none stored is a no-op.

        Raises:
            sqlite3.Error: The write failed.
        """
        self._store.clear_instructions()

    def sources(self) -> list[SourceRow]:
        """List every source that was not removed.

        Returns:
            The rows, ordered by reference.
        """
        return self._store.sources()

    async def add_source(self, ref: SourceRef, company: str) -> SourceRow:
        """Verify a source with one fetch and add it.

        Args:
            ref: The source to add.
            company: The company it belongs to, which the fetch checks.

        Returns:
            The source's row.

        Raises:
            SourceCompanyError: The table already holds `ref` for another
                company, removed or not.
            InvalidValueError: The registry cannot build the source from `ref`
                and `company`.
            SourcesError: The fetch or the identity check failed, or the source
                lists no usable jobs.
            sqlite3.Error: The write failed.
        """
        self._refuse_other_company(ref, company)
        source = build_source(_new_row(ref, company))
        async with self._client(None) as client:
            await verify_new_source(source, client)
        self._store.add_source(ref, company)
        return self._require_source(ref)

    def remove_source(self, ref: SourceRef) -> None:
        """Remove a source; it stays removed when the seeds are imported again.

        Args:
            ref: The source to remove.

        Raises:
            UnknownSourceError: The source has no row or was already removed.
            sqlite3.Error: The write failed.
        """
        row = self._store.source(ref)
        if row is None or row.removed:
            reason = f"unknown source {ref}"
            raise UnknownSourceError(reason)
        self._store.remove_source(ref)

    async def add_url(self, url: str, company: str) -> SourceRow:
        """Work out what a URL is, verify it and add it as a source.

        A URL read from a page or feed is already checked by being read; a
        hiring-system board is fetched once before it is added. Every request,
        robots.txt and redirect hops included, goes through a client that
        refuses a non-public address and a host another source kind reads, so
        such a URL is refused before anything is sent. A redirect is followed
        and the source reads the address it ended on, because a refresh follows
        none; a redirect to a hiring-system board adds that board.

        Args:
            url: A board, job page or feed URL.
            company: The company the source belongs to.

        Returns:
            The source's row.

        Raises:
            SourceCompanyError: The table already holds the resolved reference
                for another company.
            InvalidValueError: The registry cannot build the resolved source.
            UnrecognizedPageError: The page is no hiring system, job posting or
                feed; `analyze_url` can learn a recipe for it, given the page
                the error holds.
            SourcesError: The URL is malformed or unusable, is not a public
                address, is on a host another source kind reads, its page is
                over 5 MiB or its robots.txt could not be read, or the source
                fails its check.
            sqlite3.Error: The write failed.
        """
        async with self._client(None, public_only=True) as client:
            resolution = await resolve_url(url, company=company, client=client)
            self._refuse_other_company(resolution.ref, company)
            if resolution.batch is None:
                source = build_source(_new_row(resolution.ref, company))
                await verify_new_source(source, client)
        self._store.add_source(resolution.ref, company, resolution.target_url)
        return self._require_source(resolution.ref)

    async def find_source(
        self,
        company: str,
        url: str | None = None,
        *,
        model: "ModelRef | None" = None,
        on_step: "StepCallback | None" = None,
        on_cost: Callable[[float], None] | None = None,
    ) -> tuple[SourceResult, OperationReport]:
        """Have the source agent find the source that lists a company's openings.

        Nothing is stored; `add_found` stores what the user confirms. Boards
        are checked through a client that refuses non-public addresses, and the
        pages the agent opens through one that also stays on the company's
        site, so a request off the site is refused before it is sent. The run
        costs at most `[llm].max_cost_usd`.

        Args:
            company: The company to find a source for.
            url: The company's careers address, which fixes the site the agent
                may read, or `None` to start from the name alone.
            model: The model that replaces `[models].source_agent` for this
                run, or `None` for the configured one.
            on_step: Called with each finished tool call.
            on_cost: Called with the money the run has spent so far, in US
                dollars, just before each `on_step` call.

        Returns:
            The result, whose `found` is the source to offer if any and whose
            `stop` says why the run ended early, and what the run spent.

        Raises:
            MissingModelError: `model` is `None` and no model is set for the
                `source_agent` task; nothing is called.
            ProviderUnavailableError: A variable that redirects model calls is
                set.
            InvalidValueError: The config file is unusable.
            ExceptionGroup: The run raised an error other than an `LlmError`,
                such as one `on_step` raised.
            sqlite3.Error: The store cannot be read.
        """

        def page_client_for(rule: SiteRule) -> PoliteClient:
            client = self._client(None, public_only=True)
            client.guard_requests(site_guard(rule))
            return client

        async with self._client(None, public_only=True) as public_client:
            return await agent_ops.find_operation(
                self._runtime(),
                self._store,
                company,
                url,
                model=model,
                public_client=public_client,
                page_client_for=page_client_for,
                on_step=on_step,
                on_cost=on_cost,
            )

    async def explore(
        self,
        instruction: str,
        *,
        model: "ModelRef | None" = None,
        on_step: "StepCallback | None" = None,
        on_cost: Callable[[float], None] | None = None,
    ) -> tuple[ExploreResult, OperationReport]:
        """Have the explore agent carry out an instruction and propose sources.

        Nothing is stored; `add_found` stores what the user confirms, with
        `found_of` turning a proposal into the source to add. Boards are
        checked through a client that refuses non-public addresses, and the
        pages each source-agent run opens through one that also stays on the
        company's site. The run costs at most `[llm].max_cost_usd`, the
        source-agent runs inside it included.

        Args:
            instruction: The user's own request.
            model: The model that replaces `[models].explore_agent` for this
                run, or `None` for the configured one.
            on_step: Called with each finished tool call of the explore agent.
            on_cost: Called with the money the run has spent so far, in US
                dollars, just before each `on_step` call.

        Returns:
            The result, whose `proposals` stand however the run ended and whose
            `stop` says why it ended early, and what the run spent.

        Raises:
            MissingModelError: `model` is `None` and no model is set for the
                `explore_agent` task; nothing is called.
            ProviderUnavailableError: A variable that redirects model calls is
                set.
            InvalidValueError: The config file is unusable.
            ExceptionGroup: The run raised an error other than an `LlmError`,
                such as one `on_step` raised.
            sqlite3.Error: The store cannot be read.
        """

        def page_client_for(rule: SiteRule) -> PoliteClient:
            client = self._client(None, public_only=True)
            client.guard_requests(site_guard(rule))
            return client

        async with self._client(None, public_only=True) as public_client:
            return await agent_ops.explore_operation(
                self._runtime(),
                self._store,
                instruction,
                model=model,
                public_client=public_client,
                page_client_for=page_client_for,
                on_step=on_step,
                on_cost=on_cost,
            )

    async def add_found(self, found: FoundSource) -> SourceRow:
        """Store a source the agent found, the way its kind is stored.

        A board goes through `add_source`, a feed or job page through `add_url`
        with the address it was read from, and a learned recipe through
        `accept_recipe`.

        Args:
            found: The source the user confirmed.

        Returns:
            The source's row.

        Raises:
            SourceCompanyError: The table already holds the source for another
                company.
            InvalidValueError: The source cannot be built, or a recipe or its
                model reference is empty.
            SourcesError: The source fails its check or cannot be read.
            sqlite3.Error: The write failed.
        """
        self._refuse_other_company(found.ref, found.company)
        return await agent_ops.add_found(self, found)

    def learns_recipes(self) -> bool:
        """Say whether a model is set to learn recipes.

        The config is read without loading an LLM library; the runtime is built
        only when a model is set.

        Returns:
            Whether `[models].source_recipe` is set.

        Raises:
            ProviderUnavailableError: A model is set and a variable that
                redirects model calls is set.
            InvalidValueError: The config file is unreadable, or a setting in it
                is unknown or out of range.
        """
        from ijob_ai.llm.settings import Task  # noqa: PLC0415

        from ijob_app.llm_config import read_llm_settings  # noqa: PLC0415

        settings = read_llm_settings(self._llm.config_path)
        if Task.SOURCE_RECIPE not in settings.models:
            return False
        self._runtime()
        return True

    async def analyze_url(
        self, url: str, company: str, *, page: FetchedPage | None = None
    ) -> RecipeProposal:
        """Learn a recipe for a page no source kind recognizes, without storing it.

        Call this after `add_url` raised `UnrecognizedPageError` for the URL,
        passing the page the error holds so the page is not fetched again.
        Without `page` it is fetched, through a client that refuses non-public
        addresses. A JSON address the model names is always fetched that way.
        The recipe source is named after the address the page ended on, as
        every other source added by URL is, so two addresses that lead to one
        page are one source; without `page` it is named after `url`. The
        source's target stays `url`.

        Args:
            url: The page's address, as it was added.
            company: The company the source would belong to.
            page: The page `add_url` already read, or `None` to fetch it.

        Returns:
            The proposal for the user to confirm with `accept_recipe`.

        Raises:
            RecipeAlreadyAddedError: The URL's recipe source is already added
                and not removed.
            SourceCompanyError: The table already holds the source for another
                company, removed or not.
            MissingModelError: No model is set for the `source_recipe` task.
            UnsupportedUrlError: `url` is malformed; nothing is fetched.
            SourcesError: The page or a JSON address it names cannot be
                fetched, is not a public address, is on a host another source
                kind reads, is over 5 MiB, is neither HTML nor JSON, or a JSON
                address serves HTML.
            TaskOutputError: The model's answer is unusable, or its recipe is
                invalid.
            NoTrialJobsError: The recipe reads no job from the page.
            RecipeCapReachedError: The cost cap was reached before the second
                call.
            ProviderUnavailableError: The provider cannot be used.
            ProviderFailedError: The provider failed a call.
            CostUnknownError: The cost of a call cannot be determined.
            InvalidValueError: The config file is unusable.
            sqlite3.Error: The store cannot be read.
        """
        ref = SourceRef(
            SourceKind.RECIPE, manual_key(url if page is None else page.final_url)
        )
        row = self._store.source(ref)
        if row is not None and not row.removed:
            raise RecipeAlreadyAddedError(ref)
        self._refuse_other_company(ref, company)
        return await self._analyze(ref, url, company, page)

    async def analyze_source(self, ref: SourceRef) -> RecipeProposal:
        """Learn a recipe again for a recipe source, keeping the stored one.

        The page is the source's stored address and the company is its stored
        company. The stored recipe stays until `accept_recipe` replaces it.

        Args:
            ref: The recipe source.

        Returns:
            The proposal for the user to confirm with `accept_recipe`.

        Raises:
            UnknownSourceError: The source has no row or was removed.
            NoRecipeError: The source is not a recipe source, or its row has no
                address to read.
            MissingModelError: No model is set for the `source_recipe` task.
            UnsupportedUrlError: The stored address is malformed.
            SourcesError: The page or a JSON address it names cannot be
                fetched, is not a public address, is on a host another source
                kind reads, is over 5 MiB, is neither HTML nor JSON, or a JSON
                address serves HTML.
            TaskOutputError: The model's answer is unusable, or its recipe is
                invalid.
            NoTrialJobsError: The recipe reads no job from the page.
            RecipeCapReachedError: The cost cap was reached before the second
                call.
            ProviderUnavailableError: The provider cannot be used.
            ProviderFailedError: The provider failed a call.
            CostUnknownError: The cost of a call cannot be determined.
            InvalidValueError: The config file is unusable.
            sqlite3.Error: The store cannot be read.
        """
        row = self._store.source(ref)
        if row is None or row.removed:
            reason = f"unknown source {ref}"
            raise UnknownSourceError(reason)
        if ref.kind is not SourceKind.RECIPE or row.target_url is None:
            raise NoRecipeError(ref)
        return await self._analyze(ref, row.target_url, row.company, None)

    def accept_recipe(self, proposal: RecipeProposal) -> SourceRow:
        """Store a confirmed proposal as a source and its recipe.

        A removed source is restored, and a recipe already stored for the
        source is replaced.

        Args:
            proposal: The proposal `analyze_url` or `analyze_source` returned.

        Returns:
            The source's row.

        Raises:
            InvalidValueError: The recipe or the model reference is empty, or the
                service's clock gives a time with no timezone.
            sqlite3.Error: The store cannot be written.
        """
        recipe_ops.accept_recipe(self._store, proposal, at=self._now())
        return self._require_source(proposal.ref)

    def unread_count(self, refs: Sequence[SourceRef]) -> int:
        """Count the skipped items of some sources that no model has read yet.

        Args:
            refs: The sources to count; empty means every source.

        Returns:
            The number of pending items.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
            sqlite3.Error: The store cannot be read.
        """
        rows = self._choose_sources(refs)
        return len(self._store.pending_skipped([row.ref for row in rows]))

    async def extract_skipped(
        self,
        refs: Sequence[SourceRef],
        *,
        on_progress: Callable[[ExtractProgress], None] | None = None,
    ) -> ExtractReport:
        """Read the skipped jobs of some sources with the `job_extract` model.

        Each job read becomes a low-confidence posting of its source. An item
        that was read, as a job or not, is never sent again; one that could not
        be read stays pending.

        Args:
            refs: The sources to read; empty means every source.
            on_progress: Called once before the first call and after each item.

        Returns:
            What was added, set aside and left unread, and what it cost.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
            MissingModelError: No model is set for the `job_extract` task; no
                call is made.
            ProviderUnavailableError: The provider cannot be used.
            InvalidValueError: The config file is unusable.
            sqlite3.Error: The store cannot be read or written.
        """
        rows = self._choose_sources(refs)
        pending = self._store.pending_skipped([row.ref for row in rows])
        report = await extract_operation(
            self._runtime(),
            self._store,
            pending,
            now=self._now(),
            on_progress=on_progress,
        )
        self._keep_report(report.report)
        return report

    async def eval_prepare(
        self, *, limit: int | None = None, add: int | None = None
    ) -> LabelOffer:
        """Find or draw the label set and estimate what its missing votes cost.

        With `add`, the offer is for the votes of that many more postings; they
        join the set only when `eval_label` starts the run. Prices are read from
        OpenRouter's public model list, and only when a juror with votes to cast
        is on OpenRouter. No model is called.

        Args:
            limit: Cover only this many postings, from 1 to 100; all when `None`.
            add: Add this many postings to the current set, from 1 to 100; none
                when `None`. It cannot be combined with `limit`.

        Returns:
            The offer, whose plan lists the votes still missing.

        Raises:
            InvalidValueError: The config file is unusable, no jury is set,
                `limit` or `add` is out of range, both are given, or the price
                list lacks a juror.
            EvalNeedsError: No rules or no current CV profile is stored; or, with
                `add`, no set exists or its cases still lack votes.
            TooFewCasesError: The pools hold too few postings for a new set, or
                none is left to add.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            sqlite3.Error: The store cannot be read or written.
        """
        from ijob_ai.llm.reference import Provider  # noqa: PLC0415

        from ijob_app.eval_estimate import (  # noqa: PLC0415
            cost_by_model,
            estimate,
            planned_calls,
            read_prices,
            total_cost,
        )
        from ijob_app.eval_ops import prepare_label_run  # noqa: PLC0415
        from ijob_app.eval_spend import EvalSpend  # noqa: PLC0415
        from ijob_app.llm_config import read_eval_settings  # noqa: PLC0415

        settings = read_eval_settings(self._llm.config_path)
        plan = prepare_label_run(
            self._store, settings, limit=limit, add=add, now=self._now
        )
        cap = EvalSpend(self._store.evals, settings).cap
        calls = planned_calls(plan.items)
        prices = None
        if any(call.ref.provider is Provider.OPENROUTER for call in calls):
            async with self._client(None) as client:
                prices = await read_prices(client)
        estimates = estimate(calls, prices)
        return LabelOffer(
            plan=plan,
            settings=settings,
            cap=cap,
            costs=tuple(cost_by_model(estimates)),
            estimate=total_cost(estimates),
        )

    async def eval_label(
        self,
        offer: LabelOffer,
        *,
        on_progress: Callable[[LabelProgress], None] | None = None,
    ) -> "LabelReport":
        """Request the votes of an offer and derive the labels they give.

        The postings an add drew are stored in the set as the run starts.

        Args:
            offer: What `eval_prepare` returned.
            on_progress: Called after each vote item ends.

        Returns:
            What the run did and spent, and where the votes stand.

        Raises:
            ProviderUnavailableError: A variable that redirects model calls is set.
            InvalidValueError: The config file is unusable.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            ExceptionGroup: A call raised something other than an `LlmError`.
            sqlite3.Error: The store cannot be read or written.
        """
        from ijob_app.eval_ops import label_operation  # noqa: PLC0415
        from ijob_app.eval_spend import EvalSpend  # noqa: PLC0415

        spend = EvalSpend(self._store.evals, offer.settings)
        total = len(offer.plan.items)
        stored = 0

        def ended(outcome: "ItemOutcome[VoteItem, EvalVote | None]") -> None:
            nonlocal stored
            if outcome.result is not None:
                stored += 1
            if on_progress is not None:
                on_progress(
                    LabelProgress(
                        stored=stored, total=total, spent=spend.openrouter_spent
                    )
                )

        report = await label_operation(
            self._runtime(),
            self._store,
            offer.plan,
            spend,
            now=self._now,
            on_outcome=ended,
        )
        self._keep_report(report.operation)
        return report

    async def eval_prepare_run(
        self,
        suite: "Suite",
        *,
        model: "ModelRef | None",
        limit: int | None,
        program: "TunedProgram | None" = None,
    ) -> "RunOffer":
        """Plan a run of one suite and estimate what its calls cost.

        Prices are read from OpenRouter's public model list, and only when a model
        the run calls is priced from it. An agent suite's calls are not: their
        estimate reads "up to the cap". No model is called.

        Args:
            suite: The suite to run.
            model: The model under test, or `None` for the suite's default.
            limit: Cover only this many cases; the suite's default when `None`.
            program: A tuned program to run with, for `judge`, `scorer` and
                `hostile`; `hostile` then covers the pairs of its task only.

        Returns:
            The offer, whose plan lists the cases and the calls.

        Raises:
            InvalidValueError: The config file is unusable, `limit` is out of range
                or given to `hostile`, the model is on the set's jury or is the
                grader, or the price list lacks a model the run calls.
            MissingModelError: No model or grader is given or configured.
            EvalNeedsError: A label set, labels or a run to grade is missing.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            ProviderUnavailableError: A variable that redirects model calls is set.
            sqlite3.Error: The store cannot be read.
        """
        from ijob_ai.llm.reference import Provider  # noqa: PLC0415

        from ijob_app.eval_estimate import (  # noqa: PLC0415
            CallKind,
            cost_by_model,
            estimate,
            read_prices,
            total_cost,
        )
        from ijob_app.eval_run import RunOffer, plan_run  # noqa: PLC0415
        from ijob_app.eval_spend import EvalSpend  # noqa: PLC0415
        from ijob_app.llm_config import read_eval_settings  # noqa: PLC0415

        settings = read_eval_settings(self._llm.config_path)
        plan = plan_run(
            self._store,
            self._runtime().settings,
            settings,
            suite,
            model=model,
            limit=limit,
            program=program,
        )
        cap = EvalSpend(self._store.evals, settings).cap
        prices = None
        if any(
            call.ref.provider is Provider.OPENROUTER and call.kind is not CallKind.AGENT
            for call in plan.calls
        ):
            async with self._client(None) as client:
                prices = await read_prices(client)
        estimates = estimate(plan.calls, prices)
        return RunOffer(
            plan=plan,
            settings=settings,
            cap=cap,
            costs=tuple(cost_by_model(estimates)),
            estimate=total_cost(estimates),
        )

    async def eval_run(
        self,
        offer: "RunOffer",
        *,
        on_progress: Callable[["RunProgress"], None] | None = None,
    ) -> "SuiteReport":
        """Run the suite of an offer and store the run.

        An agent suite runs through `eval_run_agents` instead.

        Args:
            offer: What `eval_prepare_run` returned.
            on_progress: Called after each case ends.

        Returns:
            What the run did and spent.

        Raises:
            ProviderUnavailableError: A variable that redirects model calls is set.
            InvalidValueError: The config file is unusable, or the offer is for an
                agent suite.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            ExceptionGroup: A call raised something other than an `LlmError`.
            sqlite3.Error: The store cannot be read or written.
        """
        from ijob_app.eval_run import RunProgress, run_plan  # noqa: PLC0415
        from ijob_app.eval_spend import EvalSpend  # noqa: PLC0415

        spend = EvalSpend(self._store.evals, offer.settings)
        done = 0
        stored = 0

        def ended(outcome: "ItemOutcome[object, object]") -> None:
            nonlocal done, stored
            done += 1
            if outcome.result is not None:
                stored += 1
            if on_progress is not None:
                on_progress(
                    RunProgress(
                        done=done,
                        stored=stored,
                        total=offer.plan.cases,
                        spent=spend.openrouter_spent,
                    )
                )

        report = await run_plan(
            self._runtime(),
            self._store,
            offer.plan,
            spend,
            now=self._now,
            on_outcome=ended,
        )
        self._keep_report(report.operation)
        return report

    async def eval_run_agents(
        self,
        offer: "RunOffer",
        *,
        on_progress: Callable[["RunProgress"], None] | None = None,
    ) -> "SuiteReport":
        """Run the agent suite of an offer and store the run.

        Boards are checked through a client that refuses non-public addresses, and
        the pages an agent opens through one that also stays on the company's site,
        as `find_source` does. `source-agent` reads none of the user's sources and
        `explore-agent` runs on an empty store, so the run reads no personal data.

        Args:
            offer: What `eval_prepare_run` returned for `source-agent` or
                `explore-agent`.
            on_progress: Called after each case ends.

        Returns:
            What the run did and spent.

        Raises:
            ProviderUnavailableError: A variable that redirects model calls is set.
            InvalidValueError: The config file is unusable, or the offer is not for
                an agent suite.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            ExceptionGroup: A call raised something other than an `LlmError`.
            sqlite3.Error: The store cannot be read or written.
        """
        from ijob_app.eval_run import RunProgress, run_agent_plan  # noqa: PLC0415
        from ijob_app.eval_spend import EvalSpend  # noqa: PLC0415

        spend = EvalSpend(self._store.evals, offer.settings)
        done = 0
        stored = 0

        def ended(outcome: "ItemOutcome[object, object]") -> None:
            nonlocal done, stored
            done += 1
            if outcome.result is not None:
                stored += 1
            if on_progress is not None:
                on_progress(
                    RunProgress(
                        done=done,
                        stored=stored,
                        total=offer.plan.cases,
                        spent=spend.openrouter_spent,
                    )
                )

        def page_client_for(rule: SiteRule) -> PoliteClient:
            client = self._client(None, public_only=True)
            client.guard_requests(site_guard(rule))
            return client

        async with self._client(None, public_only=True) as public_client:
            report = await run_agent_plan(
                self._runtime(),
                self._store,
                offer.plan,
                spend,
                public_client=public_client,
                page_client_for=page_client_for,
                now=self._now,
                on_outcome=ended,
            )
        self._keep_report(report.operation)
        return report

    def eval_review_items(self) -> "ReviewPlan":
        """List what `ijob eval review` offers, labelling what the votes agree on.

        Returns:
            The cases to show, the stored rules they cite and the postings left
            out. No model is called.

        Raises:
            EvalNeedsError: No jury is set, or no rules or current CV profile
                are stored.
            InvalidValueError: The config file is unusable.
            sqlite3.Error: The store cannot be read or written.
        """
        from ijob_app.eval_ops import review_items  # noqa: PLC0415
        from ijob_app.llm_config import read_eval_settings  # noqa: PLC0415

        settings = read_eval_settings(self._llm.config_path)
        return review_items(self._store, settings, now=self._now)

    def eval_record_review(
        self,
        item: "ReviewItem",
        *,
        keep: bool | None = None,
        score: int | None = None,
    ) -> "EvalLabel":
        """Store the user's answer to a review case as a label.

        Args:
            item: The case answered.
            keep: Keep or reject, for a judge case.
            score: The fit from 0 to 100, for a scorer case.

        Returns:
            The label as stored.

        Raises:
            InvalidValueError: The answer does not fit the case.
            sqlite3.Error: The store cannot be written.
        """
        from ijob_app.eval_ops import record_review  # noqa: PLC0415

        return record_review(self._store, item, keep=keep, score=score, now=self._now)

    def eval_report(self) -> "EvalReport":
        """Gather what `ijob eval report` shows from the stored runs.

        Returns:
            A table of comparable runs for each suite with runs, the jury
            metrics of the current label set and the total OpenRouter spend. The
            suites with labels are listed only when the configured jury and the
            stored rules and CV profile identify a stored label set. No model is
            called.

        Raises:
            InvalidValueError: The config file is unusable.
            sqlite3.Error: The store cannot be read.
        """
        from ijob_app.eval_report import build_report, current_set  # noqa: PLC0415
        from ijob_app.llm_config import read_eval_settings  # noqa: PLC0415

        settings = read_eval_settings(self._llm.config_path)
        jury = None if settings.jury is None else [str(ref) for ref in settings.jury]
        current, ready = current_set(self._store, jury)
        return build_report(
            self._store.evals,
            current=current,
            labels_ready=ready,
            cap=settings.total_cost_usd,
        )

    def eval_run_detail(self, run_id: int) -> "RunDetail":
        """Read one eval run, whatever its status.

        Args:
            run_id: The run's number.

        Returns:
            The run with its metrics, latency and case results.

        Raises:
            InvalidValueError: No such run is stored.
            sqlite3.Error: The store cannot be read.
        """
        from ijob_app.eval_report import run_detail  # noqa: PLC0415

        return run_detail(self._store.evals, run_id)

    async def tune_prepare_screen(self) -> "ScreenOffer":
        """Plan a screen and estimate what its runs cost.

        Each pair the screen still needs is priced as an `eval run` of its suite
        would be. Nothing is frozen and no model is called.

        Returns:
            The offer, whose plan lists the pairs and the skipped models.

        Raises:
            InvalidValueError: The config file is unusable, `[tune].baseline` is
                absent, a candidate or the baseline is a juror of the label set,
                or the price list lacks a model the screen calls.
            MissingModelError: A pair's model cannot be resolved.
            EvalNeedsError: A jury, rules, profile, label set or labels are
                missing or wait for review.
            TooFewKeepsError: Too few keeps and rejects are labelled to tune on.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            ProviderUnavailableError: A variable that redirects model calls is set.
            sqlite3.Error: The store cannot be read.
        """
        from ijob_app.llm_config import (  # noqa: PLC0415
            read_eval_settings,
            read_tune_settings,
        )
        from ijob_app.tune_ops import prepare_screen, screen_offer  # noqa: PLC0415

        settings = read_eval_settings(self._llm.config_path)
        tune = read_tune_settings(self._llm.config_path)
        plan = prepare_screen(
            self._store, self._runtime().settings, settings, tune, now=self._now
        )
        offers = [
            await self.eval_prepare_run(pair.suite, model=pair.model, limit=None)
            for pair in plan.pairs
        ]
        return screen_offer(
            plan,
            offers,
            settings,
            left=settings.total_cost_usd - self._store.evals.total_spend(),
            frozen=self._store.tunes.has_split(plan.eval_set.id),
        )

    async def tune_screen(
        self,
        offer: "ScreenOffer",
        *,
        on_progress: Callable[["ScreenPair", "RunProgress"], None] | None = None,
        on_outcome: Callable[["PairOutcome"], None] | None = None,
    ) -> "ScreenReport":
        """Freeze the split, then run each pair of an offer as an eval run.

        Every pair is an ordinary `eval run` with its own spend and run row.

        Args:
            offer: What `tune_prepare_screen` returned.
            on_progress: Called after each case of a pair ends, with the pair.
            on_outcome: Called as each pair ends.

        Returns:
            What the screen did.

        Raises:
            EvalNeedsError: Labels wait for review; nothing ran.
            TooFewKeepsError: Too few keeps and rejects to tune on; nothing ran.
            asyncio.CancelledError: The call was cancelled; the runs already
                written stand.
            sqlite3.Error: The store cannot be read or written.
        """
        from ijob_app.tune_ops import run_screen  # noqa: PLC0415

        offers = dict(zip(offer.plan.pairs, offer.offers, strict=True))

        async def run(pair: "ScreenPair") -> "SuiteReport":
            def progress(update: "RunProgress") -> None:
                if on_progress is not None:
                    on_progress(pair, update)

            return await self.eval_run(offers[pair], on_progress=progress)

        return await run_screen(
            self._store,
            offer.plan,
            offer.settings,
            run,
            now=self._now,
            on_outcome=on_outcome,
        )

    async def tune_prepare_run(
        self,
        *,
        model: "ModelRef | None",
        optimizer: "Optimizer | None",
        task: "EvalTask | None",
        again: bool,
    ) -> "GridOffer":
        """Plan a tuning grid and estimate what it costs.

        Prices are read from OpenRouter's public model list only when a model the
        grid tunes is on OpenRouter. Nothing is written and no model is called.

        Args:
            model: Run only this model, whether or not `[tune].models` lists it.
            optimizer: Run only this optimizer.
            task: Run only this task.
            again: Plan combinations that are done as well.

        Returns:
            The offer, whose plan lists the combinations to run and those done.

        Raises:
            InvalidValueError: The config file is unusable, a model is a juror of the
                label set, or the price list lacks a model the grid tunes.
            MissingModelError: GEPA or MIPROv2 would tune and `[models].tuner` is not
                set.
            EvalNeedsError: `[tune].models` is empty, or the jury, rules, profile,
                labels, tuning split or a full run is missing.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            ProviderUnavailableError: A variable that redirects model calls is set.
            sqlite3.Error: The store cannot be read.
        """
        from ijob_ai.llm.reference import Provider  # noqa: PLC0415

        from ijob_app.eval_estimate import estimate, read_prices  # noqa: PLC0415
        from ijob_app.eval_spend import EvalSpend  # noqa: PLC0415
        from ijob_app.llm_config import (  # noqa: PLC0415
            read_eval_settings,
            read_tune_settings,
        )
        from ijob_app.tune_grid import (  # noqa: PLC0415
            grid_offer,
            planned_calls,
            prepare_grid,
        )

        settings = read_eval_settings(self._llm.config_path)
        tune = read_tune_settings(self._llm.config_path)
        plan = prepare_grid(
            self._store,
            self._runtime().settings,
            settings,
            tune,
            model=model,
            optimizer=optimizer,
            task=task,
            again=again,
        )
        cap = EvalSpend(self._store.evals, settings).cap if plan.combinations else 0.0
        calls = planned_calls(plan)
        prices = None
        if any(call.ref.provider is Provider.OPENROUTER for call in calls):
            async with self._client(None) as client:
                prices = await read_prices(client)
        return grid_offer(
            plan,
            estimate(calls, prices),
            cap=cap,
            left=settings.total_cost_usd - self._store.evals.total_spend(),
        )

    async def tune_run(
        self,
        offer: "GridOffer",
        *,
        on_start: Callable[["Combination"], None] | None = None,
        on_progress: Callable[["TuneProgress"], None] | None = None,
        on_result: Callable[["CombinationResult"], None] | None = None,
    ) -> "GridReport":
        """Tune each combination of an offer and test what it made on held-out postings.

        Every combination is one `tune` run with its own spend and run row.

        Args:
            offer: What `tune_prepare_run` returned.
            on_start: Called as each combination begins.
            on_progress: Called after each model call, and after each case of a
                held-out test.
            on_result: Called as each combination ends.

        Returns:
            What the grid did.

        Raises:
            asyncio.CancelledError: The call was cancelled outside the tuning; the
                rows already written stand.
            sqlite3.Error: The store cannot be read or written.
        """
        from ijob_app.tune_grid import run_grid  # noqa: PLC0415

        return await run_grid(
            self._runtime(),
            self._store,
            offer.plan,
            now=self._now,
            on_start=on_start,
            on_progress=on_progress,
            on_result=on_result,
        )

    def tune_report(self) -> "TuneReport":
        """Build the tuning report from the stored rows.

        It calls no model and writes nothing, and it never freezes a split.

        Returns:
            The report. While the label set has no split, its tables are empty and
            its notice says to run `ijob tune screen`.

        Raises:
            InvalidValueError: The config file is unusable, or `[tune].baseline`
                is not set.
            sqlite3.Error: The store cannot be read.
        """
        from ijob_app.eval_report import current_set  # noqa: PLC0415
        from ijob_app.llm_config import (  # noqa: PLC0415
            read_eval_settings,
            read_llm_settings,
            read_tune_settings,
        )
        from ijob_app.tune_report import build_tune_report  # noqa: PLC0415

        path = self._llm.config_path
        jury = read_eval_settings(path).jury
        eval_set, _ = current_set(
            self._store, None if jury is None else [str(ref) for ref in jury]
        )
        return build_tune_report(
            self._store, eval_set, read_llm_settings(path), read_tune_settings(path)
        )

    async def tune_prepare_accept(self, program_id: int) -> "AcceptOffer":
        """Check a program can be accepted and estimate what testing it costs.

        Nothing is written and no model is called.

        Args:
            program_id: The program's number.

        Returns:
            The program with the offer of its `hostile` run.

        Raises:
            InvalidValueError: The config file is unusable, no program has that
                number, a stored program is malformed (the message names its id),
                or the price list lacks the program's model.
            EvalNeedsError: The program has no complete held-out run, or its model,
                bound or base version is not the task's now.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            ProviderUnavailableError: A variable that redirects model calls is set.
            sqlite3.Error: The store cannot be read.
        """
        from ijob_ai.llm.reference import parse_model_ref  # noqa: PLC0415

        from ijob_app.eval_suites import Suite  # noqa: PLC0415
        from ijob_app.llm_config import read_llm_settings  # noqa: PLC0415
        from ijob_app.tune_ops import AcceptOffer, prepare_accept  # noqa: PLC0415

        program = prepare_accept(
            self._store, read_llm_settings(self._llm.config_path), program_id
        )
        offer = await self.eval_prepare_run(
            Suite.HOSTILE,
            model=parse_model_ref(program.model, "model"),
            limit=None,
            program=program,
        )
        return AcceptOffer(program, offer)

    async def tune_accept(
        self,
        offer: "AcceptOffer",
        *,
        on_progress: Callable[["RunProgress"], None] | None = None,
    ) -> "AcceptReport":
        """Run the hostile suite with a program, and accept it if it passes.

        The run is an ordinary `hostile` run, limited and under the program's
        version. The program is accepted only with no attack and every pair of its
        task answered; otherwise nothing changes.

        Args:
            offer: What `tune_prepare_accept` returned.
            on_progress: Called after each pair ends.

        Returns:
            What the run found and whether the program is now accepted.

        Raises:
            ProviderUnavailableError: A variable that redirects model calls is set.
            EvalBudgetSpentError: The recorded eval spending has used up
                `eval.total_cost_usd`.
            ExceptionGroup: A call raised something other than an `LlmError`.
            sqlite3.Error: The store cannot be read or written.
        """
        from ijob_app.tune_ops import accept_program  # noqa: PLC0415

        async def run() -> "SuiteReport":
            return await self.eval_run(offer.offer, on_progress=on_progress)

        return await accept_program(self._store, offer.program, run, now=self._now)

    def tune_clear(self, task: EvalTask) -> bool:
        """Go back to the base prompt for a task.

        The program row stays. A malformed row is cleared too, because nothing but
        its task is read; search then runs the base prompt again, though
        `tune_list` still stops on the row, naming its id.

        Args:
            task: `judge` or `scorer`.

        Returns:
            Whether a program was accepted.

        Raises:
            sqlite3.Error: The store cannot be written.
        """
        from ijob_app.tune_ops import clear_program  # noqa: PLC0415

        return clear_program(self._store, task)

    def tune_list(self) -> "list[ProgramLine]":
        """List the tuned programs with their status.

        Returns:
            Every program, judge programs first, each with `tested`, `untested`,
            `accepted` or `accepted, not in use: <reason>`. Nothing is written and
            no model is called.

        Raises:
            InvalidValueError: The config file is unusable, or a stored program is
                malformed; the message names its id.
            sqlite3.Error: The store cannot be read.
        """
        from ijob_app.llm_config import read_llm_settings  # noqa: PLC0415
        from ijob_app.tune_grid import list_programs  # noqa: PLC0415

        return list_programs(self._store, read_llm_settings(self._llm.config_path))

    def posting_sources(self, posting_id: PostingId) -> list[SourceRef]:
        """List the sources that have shown a posting.

        Args:
            posting_id: The posting to read.

        Returns:
            The sources, sorted by their text form; empty if the posting is
            unknown.
        """
        return self._store.posting_sources(posting_id)

    async def _analyze(
        self, ref: SourceRef, url: str, company: str, page: FetchedPage | None
    ) -> RecipeProposal:
        """Analyze a page through a guarded client that has no cache."""
        runtime = self._runtime()
        async with self._client(None, public_only=True) as client:
            return await recipe_ops.analyze_operation(
                runtime, client, ref=ref, target_url=url, company=company, page=page
            )

    def _refuse_other_company(self, ref: SourceRef, company: str) -> None:
        """Refuse a reference the table already holds for a different company.

        The table keeps the company a reference first had, so adding it again
        under another name would verify against the new name and then store the
        old one. Companies are compared as `normalise_company` writes them.

        Raises:
            SourceCompanyError: `ref` is in the table, removed or not, for a
                different company.
        """
        row = self._store.source(ref)
        if row is not None and normalise_company(row.company) != normalise_company(
            company
        ):
            raise SourceCompanyError(ref, row.company)

    def _require_source(self, ref: SourceRef) -> SourceRow:
        """Read a source's row after it was written.

        Raises:
            UnknownSourceError: The table has no such source.
        """
        row = self._store.source(ref)
        if row is None:
            reason = f"unknown source {ref}"
            raise UnknownSourceError(reason)
        return row

    def _require_posting(self, posting_id: PostingId) -> Posting:
        """Read a posting that must exist.

        Raises:
            UnknownPostingError: The id names no posting.
        """
        posting = self._store.posting(posting_id)
        if posting is None:
            raise UnknownPostingError(posting_id)
        return posting

    async def _describe(self, posting_id: PostingId) -> tuple[str | None, str | None]:
        """Look a posting's description up from its sightings and store it.

        Args:
            posting_id: The posting to describe.

        Returns:
            The description and `None`, or `None` and the reason there is none:
            the message of the first source error, or that no source publishes
            a description. A recipe source's job page is fetched through the
            client that refuses non-public addresses.
        """
        failure: str | None = None
        async with (
            self._client(self._cache) as client,
            AsyncExitStack() as stack,
        ):
            guarded: PoliteClient | None = None
            for sighting in self._store.sightings_of(posting_id):
                used = client
                if sighting.source.kind is SourceKind.RECIPE:
                    if guarded is None:
                        guarded = await stack.enter_async_context(
                            self._client(self._cache, public_only=True)
                        )
                    used = guarded
                try:
                    text = await describe(sighting, used)
                except SourcesError as error:
                    failure = failure or str(error)
                    continue
                if text is not None:
                    self._store.set_description(posting_id, text)
                    return text, None
        return None, failure or "no description published"

    def _choose_sources(self, refs: Sequence[SourceRef]) -> list[SourceRow]:
        """Pick the sources a read covers.

        Args:
            refs: The sources the caller named; empty means every source.

        Returns:
            The rows to read, in the order named, or in `ref` order when none
            was named.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
        """
        rows = self._store.sources()
        if not refs:
            return rows
        by_ref = {row.ref: row for row in rows}
        for ref in refs:
            if ref not in by_ref:
                reason = (
                    f"unknown source {ref}; "
                    f"add it with: ijob sources add {ref} --company NAME"
                )
                raise UnknownSourceError(reason)
        return [by_ref[ref] for ref in refs]

    def _build_sources(
        self, rows: list[SourceRow]
    ) -> tuple[list[Source], dict[SourceRef, InvalidValueError]]:
        """Build the adapter of each row, setting aside the rows the registry refuses.

        Args:
            rows: The sources to build.

        Returns:
            The adapters, and the reason each refused row could not be built. A
            recipe row with no stored recipe is refused.
        """
        sources: list[Source] = []
        refused: dict[SourceRef, InvalidValueError] = {}
        for row in rows:
            try:
                sources.append(
                    build_source(
                        row,
                        known=self._store.external_ids(row.ref),
                        recipe=self._recipe_text(row),
                    )
                )
            except InvalidValueError as error:
                refused[row.ref] = error
        return sources, refused

    def _recipe_text(self, row: SourceRow) -> str | None:
        """Return the recipe stored for a recipe row, or `None` for any other row."""
        if row.ref.kind is not SourceKind.RECIPE:
            return None
        stored = self._store.recipe(row.ref)
        return None if stored is None else stored.recipe

    def _client(
        self, cache: ConditionalCache | None, *, public_only: bool = False
    ) -> PoliteClient:
        """Build a client that paces by the service's shared host pacing.

        Args:
            cache: The cache the client revalidates against, or `None`.
            public_only: Whether the client refuses any request to a non-public
                address or to a host another source kind reads.
        """
        client = self._client_factory(cache)
        client.share_pacing(self._pacing)
        if public_only:
            client.guard_requests(public_host_guard(self._resolver))
        return client

    async def _collect(
        self,
        sources: list[Source],
        on_progress: Callable[[FetchProgress], None] | None = None,
        on_batch: Callable[[SourceRef, SourceBatch], Awaitable[None]] | None = None,
        on_failure: Callable[[SourceFailure], None] | None = None,
    ) -> CollectResult:
        """Fetch every source with clients opened and closed on this loop.

        The recipe sources go through a client that refuses non-public
        addresses, concurrently with the rest; no such client is opened when
        there is no recipe source.

        Args:
            sources: The sources to fetch.
            on_progress: Called once before the first fetch and as each source
                ends. It is not called when there is no source.
            on_batch: Awaited with each source's batch as it arrives.
            on_failure: Called with each source's failure as it happens.

        Returns:
            What `collect` returned for the sources, merged.
        """
        recipes = [source for source in sources if source.ref.kind is SourceKind.RECIPE]
        others = [
            source for source in sources if source.ref.kind is not SourceKind.RECIPE
        ]
        done = 0
        failed = 0

        def source_done(source_failed: bool) -> None:  # noqa: FBT001
            nonlocal done, failed
            done += 1
            failed += source_failed
            if on_progress is not None:
                on_progress(FetchProgress(total=len(sources), done=done, failed=failed))

        if on_progress is not None and sources:
            on_progress(FetchProgress(total=len(sources), done=0, failed=0))
        async with self._client(self._cache) as client:
            if not recipes:
                return await collect(
                    others,
                    client,
                    on_done=source_done,
                    on_batch=on_batch,
                    on_failure=on_failure,
                )
            async with (
                self._client(self._cache, public_only=True) as guarded,
                asyncio.TaskGroup() as group,
            ):
                plain = group.create_task(
                    collect(
                        others,
                        client,
                        on_done=source_done,
                        on_batch=on_batch,
                        on_failure=on_failure,
                    )
                )
                checked = group.create_task(
                    collect(
                        recipes,
                        guarded,
                        on_done=source_done,
                        on_batch=on_batch,
                        on_failure=on_failure,
                    )
                )
        first, second = plain.result(), checked.result()
        return CollectResult(
            batches={**first.batches, **second.batches},
            failures=[*first.failures, *second.failures],
        )

    def _record_source(
        self, ref: SourceRef, batch: SourceBatch, seen_at: datetime
    ) -> RecordResult:
        """Store what one source showed and note when it was fetched.

        Both the postings and the fetch time are stamped with the one `seen_at`,
        which is what keeps a source's postings open afterwards. This runs on its
        own connection, so it can be called off the service's thread.

        Args:
            ref: The source fetched.
            batch: What it returned.
            seen_at: When it was saved, timezone-aware.

        Returns:
            What the store did with the source's jobs.
        """
        with Store.open(self._db_path) as store:
            still_listed: list[tuple[SourceRef, ExternalId]] = [
                (ref, external_id) for external_id in batch.still_listed
            ]
            # A skipped item a model already read as a job stays listed, so its
            # posting is not closed while the source still shows the item.
            still_listed.extend(
                (ref, external_id)
                for external_id in store.add_skipped(
                    ref,
                    ((item.text, item.url, item.reason) for item in batch.skipped),
                    seen_at=seen_at,
                )
            )
            result = store.record(
                batch.drafts, seen_at=seen_at, still_listed=still_listed
            )
            store.mark_fetched(ref, seen_at)
        return result

    def _stale(self, rows: list[SourceRow], at: datetime) -> list[SourceRow]:
        """Pick the chosen sources whose postings are hidden as too old."""
        chosen = {row.ref for row in rows}
        return [
            row
            for row in self._store.stale_sources(now=at)
            if row.ref in chosen and row.last_fetched_at is not None
        ]


@contextmanager
def open_service(  # noqa: PLR0913  # the service's inputs, one each
    home: IjobHome,
    db: Path | None,
    client_factory: ClientFactory,
    *,
    now: Callable[[], datetime],
    use_cache: bool = True,
    llm: LlmOptions | None = None,
    resolver: HostResolver = system_resolver,
) -> Iterator[Service]:
    """Open the seeded database and the HTTP cache and build a service on them.

    The cache is opened before the database, so an unusable cache leaves the
    database file uncreated.

    Args:
        home: The ijob home, which holds the HTTP cache and, unless `db` names
            another file, the database.
        db: The database file, or `None` for the home's.
        client_factory: Builds the client a fetch goes through.
        now: The wall clock, returning timezone-aware times; the cache ages its
            entries by it too.
        use_cache: Whether to open the HTTP cache and hand it to the clients.
            A caller that never revalidates responses leaves it off.
        llm: What the model-calling operations are given, or `None` for the
            home's config file, the real engines and the real `PATH`.
        resolver: Looks up the addresses of a host for the check that keeps a
            recipe source on public hosts; the default asks the system.

    Yields:
        The service, valid until the context exits and bound to this thread.

    Raises:
        CacheUnavailableError: The cache file cannot be opened.
    """
    cache: SqliteHttpCache | None = None
    if use_cache:
        path = home.cache
        try:
            cache = SqliteHttpCache.open(path, now=now)
        except (OSError, sqlite3.Error) as error:
            raise CacheUnavailableError(path, str(error)) from error
    try:
        db_path = db or home.db
        with Store.open(db_path) as store:
            store.import_seeds(load_seeds())
            yield Service(
                store,
                db_path=db_path,
                client_factory=client_factory,
                cache=cache,
                now=now,
                home=home,
                llm=llm or LlmOptions(config_path=home.config),
                resolver=resolver,
            )
    finally:
        if cache is not None:
            cache.close()
