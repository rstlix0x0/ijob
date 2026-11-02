"""Read, once each, the jobs a source's parser could not read.

`extract_operation` sends every pending skipped item to the `job_extract` model
under the operation's cost cap. An item the model calls no job is marked
`not_job`; one it reads becomes a low-confidence sighting of the item's source
and is marked `job`. A job the model gave no title takes the first line of its
own words. An item with no words or no http(s) address, and an item whose call
failed or never started, stays pending so a later run tries again. A marked item
is never sent again.

At module level this module imports only `ijob_core`, `ijob_sources` and
`ijob_app`: every `ijob_ai` import is inside the function that needs it, so a
search that reads nothing loads no `ijob_ai` module.
"""

import html
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from ijob_core.errors import InvalidValueError
from ijob_core.model import ExternalId, SightingDraft
from ijob_core.store import SkippedRow, SkippedStatus, Store

from ijob_app.llm_ops import (
    ItemOutcome,
    ItemStatus,
    LlmRuntime,
    OperationReport,
    run_items,
)

if TYPE_CHECKING:
    from ijob_ai.llm.budget import Budget
    from ijob_ai.llm.reference import Provider
    from ijob_ai.tasks.job_extract import ExtractedJob

_OPERATION = "sources extract"
_WEB_SCHEMES = frozenset({"http", "https"})
_UNKNOWN_COMPANY: Final = "unknown"
_PREVIEW_CHARS: Final = 60
_TITLE_CHARS: Final = 120
_TAG: Final = re.compile(r"<[^>]*>")


@dataclass(frozen=True, slots=True)
class ExtractFailure:
    """An item left unread, and why.

    Attributes:
        row: The item.
        reason: Why it was not turned into a job.
    """

    row: SkippedRow
    reason: str

    @property
    def preview(self) -> str:
        """The start of the item's text on one line."""
        flat = " ".join(html.unescape(self.row.text).split())
        return flat[:_PREVIEW_CHARS]


@dataclass(frozen=True, slots=True)
class ExtractReport:
    """What one extract operation did.

    Attributes:
        added: Items read as jobs and stored as postings.
        not_jobs: Items the model called no job.
        failed: Items left unread, each with its reason.
        report: What the model calls did and spent.
    """

    added: int
    not_jobs: int
    failed: tuple[ExtractFailure, ...]
    report: OperationReport


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractProgress:
    """How far an extract operation has got.

    Attributes:
        total: The items the operation will go through.
        added: Items read as jobs so far.
        not_jobs: Items the model called no job so far.
        unread: Items left unread so far.
    """

    total: int
    added: int
    not_jobs: int
    unread: int

    @property
    def read(self) -> int:
        """How many items the operation has finished with."""
        return self.added + self.not_jobs + self.unread


def _usable(url: str | None) -> str | None:
    """Return `url` when it is an http or https address with a host."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme in _WEB_SCHEMES and parts.hostname:
        return url
    return None


def _decoded(text: str) -> str:
    """Return an item's text with its HTML entities turned back into characters."""
    return html.unescape(text)


def _first_line(text: str) -> str | None:
    """Return the first line of an item's words, without markup, or `None`."""
    words = _TAG.sub("", _decoded(text))
    for line in words.splitlines():
        flat = " ".join(line.split())
        if flat:
            return flat[:_TITLE_CHARS]
    return None


def _draft(row: SkippedRow, job: "ExtractedJob") -> SightingDraft | str:
    """Build the draft an item reads as, or the reason it cannot become one.

    A job the model gave no title, such as a post naming several teams, is
    titled by the first line of its own words.
    """
    title = job.title or _first_line(row.text)
    if title is None:
        return "the model found no title"
    url = _usable(job.url) or _usable(row.url)
    if url is None:
        return "the item has no usable http(s) address"
    try:
        return SightingDraft(
            source=row.source,
            external_id=ExternalId(f"llm-{row.item_id[:16]}"),
            title=title,
            company=job.company or _UNKNOWN_COMPANY,
            location=job.location,
            url=url,
            posted_at=None,
            raw=row.text,
            low_confidence=True,
        )
    except InvalidValueError as error:
        return str(error)


class _Extractor:
    """The worker of one operation: one model call per item."""

    def __init__(self, runtime: LlmRuntime, *, provider: "Provider") -> None:
        self._runtime = runtime
        self._provider = provider

    async def __call__(self, row: SkippedRow, budget: "Budget") -> "ExtractedJob":
        """Ask the model about one item, once more if the answer has the wrong form.

        A model sometimes wraps a right answer in the wrong form, and asking
        again usually fixes it. The second call needs the cost cap's room too.

        Raises:
            LlmError: A call fails, or the last answer is unusable.
        """
        from ijob_ai.errors import TaskOutputError  # noqa: PLC0415

        try:
            return await self._ask(row, budget)
        except TaskOutputError:
            if not budget.try_start():
                raise
        return await self._ask(row, budget)

    async def _ask(self, row: SkippedRow, budget: "Budget") -> "ExtractedJob":
        """Make one call about one item."""
        from ijob_ai.llm.settings import Task  # noqa: PLC0415
        from ijob_ai.tasks.job_extract import extract_job  # noqa: PLC0415

        registry = await self._runtime.registry_for(Task.JOB_EXTRACT)
        return await extract_job(
            _decoded(row.text),
            url=row.url,
            lm=registry.lm_for(Task.JOB_EXTRACT),
            provider=self._provider,
            budget=budget,
            timeout_s=self._runtime.settings.timeout_s,
        )


class _Applier:
    """Stores each outcome as soon as it is known, tallies it and reports progress."""

    def __init__(
        self,
        store: Store,
        *,
        now: datetime,
        total: int,
        on_progress: Callable[[ExtractProgress], None] | None,
    ) -> None:
        self._store = store
        self._now = now
        self._total = total
        self._on_progress = on_progress
        self.added = 0
        self.not_jobs = 0
        self.failed: list[ExtractFailure] = []

    def __call__(self, outcome: "ItemOutcome[SkippedRow, ExtractedJob]") -> None:
        row, job = outcome.item, outcome.result
        if outcome.status is not ItemStatus.DONE or job is None:
            reason = (
                "the cost cap was reached before this item"
                if outcome.error is None
                else str(outcome.error)
            )
            self.failed.append(ExtractFailure(row, reason))
        elif not job.is_job:
            self._store.mark_skipped(row.source, row.item_id, SkippedStatus.NOT_JOB)
            self.not_jobs += 1
        else:
            self._add(row, job)
        self.report_progress()

    def report_progress(self) -> None:
        """Tell the caller how far the run has got, when it asked."""
        if self._on_progress is not None:
            self._on_progress(
                ExtractProgress(
                    total=self._total,
                    added=self.added,
                    not_jobs=self.not_jobs,
                    unread=len(self.failed),
                )
            )

    def _add(self, row: SkippedRow, job: "ExtractedJob") -> None:
        """Store an item read as a job, or leave it pending with the reason."""
        draft = _draft(row, job)
        if isinstance(draft, str):
            self.failed.append(ExtractFailure(row, draft))
            return
        source = self._store.source(row.source)
        # A posting is open while its sighting is as recent as the source's
        # latest fetch, so the item takes that time rather than now.
        seen_at = (
            source.last_fetched_at
            if source is not None and source.last_fetched_at is not None
            else self._now
        )
        self._store.record([draft], seen_at=seen_at)
        self._store.mark_skipped(
            row.source, row.item_id, SkippedStatus.JOB, external_id=draft.external_id
        )
        self.added += 1


async def extract_operation(
    runtime: LlmRuntime,
    store: Store,
    rows: Sequence[SkippedRow],
    *,
    now: datetime,
    on_progress: Callable[[ExtractProgress], None] | None = None,
) -> ExtractReport:
    """Read pending skipped items with the `job_extract` model.

    Each result is stored as soon as it is known, so a run that is stopped keeps
    what it read. Items start while the cost cap allows.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        rows: The pending items to read.
        now: The wall clock, timezone-aware; stamps a posting only when its
            source was never fetched.
        on_progress: Called once before the first call and after each item.
            It is not called when there is nothing to read.

    Returns:
        What was added, set aside and left unread, and the run's report.

    Raises:
        MissingModelError: No model is set for the `job_extract` task; no call
            is made.
        asyncio.CancelledError: The call was cancelled; results already stored
            stand.
        ExceptionGroup: A call raised something other than an `LlmError`.
        sqlite3.Error: The store cannot be read or written.
    """
    from ijob_ai.llm.settings import Task  # noqa: PLC0415

    model = runtime.settings.model_for(Task.JOB_EXTRACT)
    applier = _Applier(store, now=now, total=len(rows), on_progress=on_progress)
    if rows:
        applier.report_progress()
    run = await run_items(
        runtime,
        _OPERATION,
        tuple(rows),
        _Extractor(runtime, provider=model.provider),
        on_outcome=applier,
    )
    return ExtractReport(
        added=applier.added,
        not_jobs=applier.not_jobs,
        failed=tuple(applier.failed),
        report=run.report,
    )
