import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from ijob_ai.errors import NoCvError
from ijob_app.config import FilterLayers
from ijob_app.job_search import JobList, ScoreState, run_search
from ijob_app.llm_ops import OperationReport, ScoreItem, ScoreStatus
from ijob_app.service import FetchProgress, RefreshReport, Service, SourceEvent
from ijob_core.model import Posting, PostingId, Score, SourceRef, parse_source_ref

_NOW = datetime(2026, 11, 3, tzinfo=UTC)
_REF = parse_source_ref("lever:acme")


def _posting(posting_id: int) -> Posting:
    return Posting(
        id=PostingId(posting_id),
        title=f"Job {posting_id}",
        company="Acme",
        location="Remote",
        url=f"https://example.com/{posting_id}",
        posted_at=None,
        first_seen=_NOW,
        last_seen=_NOW,
    )


def _report() -> RefreshReport:
    return RefreshReport(
        due=[], kept=[], fetched=[], rejected={}, failures=[], refused={}, stale=[]
    )


@dataclass
class _FakeService:
    """A service whose fetch adds `arriving` jobs, and whose scores come from `fits`."""

    saved: list[int]
    arriving: list[int] = field(default_factory=list)
    fits: dict[int, int] = field(default_factory=dict)
    stored_scores: dict[PostingId, Score] = field(default_factory=dict)
    score_error: Exception | None = None
    last_report: OperationReport | None = None
    scored_ids: list[list[PostingId]] = field(default_factory=list)
    shown_during_fetch: list[int] = field(default_factory=list)

    async def list_jobs(
        self, _layers: FilterLayers, _refs: Sequence[SourceRef]
    ) -> list[Posting]:
        return [_posting(posting_id) for posting_id in self.saved]

    def saved_scores(self) -> dict[PostingId, Score]:
        return dict(self.stored_scores)

    async def refresh_off_loop(
        self,
        _refs: Sequence[SourceRef],
        *,
        on_progress: object,
        on_source: object,
    ) -> RefreshReport:
        progress = cast("_Progress", on_progress)
        source = cast("_Source", on_source)
        progress(FetchProgress(total=1, done=0, failed=0))
        self.saved = [*self.saved, *self.arriving]
        progress(FetchProgress(total=1, done=1, failed=0))
        source(SourceEvent(ref=_REF, new=len(self.arriving), listed=len(self.saved)))
        return _report()

    async def score(self, ids: Sequence[PostingId]) -> AsyncIterator[ScoreItem]:
        self.scored_ids.append(list(ids))
        if self.score_error is not None:
            raise self.score_error
        for posting_id in ids:
            fit = self.fits.get(posting_id)
            score = None if fit is None else Score(score=fit, reasons=("because",))
            status = ScoreStatus.FAILED if score is None else ScoreStatus.JUDGED
            yield ScoreItem(_posting(posting_id), score, status)
        self.last_report = OperationReport(
            done=len(ids), not_done=0, spent=0.01, cap=0.5, stopped_by=None
        )


class _Progress:
    def __call__(self, progress: FetchProgress) -> None: ...


class _Source:
    def __call__(self, event: SourceEvent) -> None: ...


def _layers() -> FilterLayers:
    return cast("FilterLayers", object())


def _run(fake: _FakeService, jobs: JobList, **options: object) -> list[int]:
    """Run a search, returning how many jobs each change showed."""
    seen: list[int] = []
    asyncio.run(
        run_search(
            cast("Service", fake),
            _layers(),
            [],
            jobs,
            lambda: seen.append(len(jobs.rows)),
            **options,  # type: ignore[arg-type]
        )
    )
    return seen


def test_saved_jobs_show_before_anything_else() -> None:
    fake = _FakeService(saved=[1, 2], arriving=[3])
    jobs = JobList()

    seen = _run(fake, jobs)

    assert seen[0] == 2
    assert len(jobs.rows) == 3


def test_a_job_a_fetch_brings_in_is_marked_new() -> None:
    jobs = JobList()

    _run(_FakeService(saved=[1], arriving=[2]), jobs)

    assert not jobs.rows[PostingId(1)].is_new
    assert jobs.rows[PostingId(2)].is_new
    assert jobs.new_jobs == 1


def test_no_fetch_leaves_the_network_alone() -> None:
    fake = _FakeService(saved=[1], arriving=[2])
    jobs = JobList()

    _run(fake, jobs, fetch=False)

    assert jobs.fetch is None
    assert len(jobs.rows) == 1


def test_scores_rank_the_list_best_first() -> None:
    fake = _FakeService(saved=[1, 2, 3], fits={1: 40, 2: 90, 3: 70})
    jobs = JobList()

    _run(fake, jobs, fetch=False, score=True)

    assert [row.posting.id for row in jobs.ranked()] == [2, 3, 1]
    assert jobs.scored == 3


def test_a_job_without_a_score_says_why() -> None:
    jobs = JobList()

    _run(_FakeService(saved=[1], fits={}), jobs, fetch=False, score=True)

    assert jobs.rows[PostingId(1)].state is ScoreState.MISSED
    assert jobs.rows[PostingId(1)].missed == "failed"


def test_the_limit_scores_the_newest_and_counts_the_rest() -> None:
    fake = _FakeService(saved=[1, 2, 3], fits={1: 50, 2: 60, 3: 70})
    jobs = JobList()

    _run(fake, jobs, fetch=False, score=True, score_limit=2)

    assert fake.scored_ids == [[PostingId(1), PostingId(2)]]
    assert jobs.hidden_from_scoring == 1


def test_saved_scores_show_without_a_model() -> None:
    stored = {PostingId(1): Score(score=88, reasons=("saved",))}
    fake = _FakeService(saved=[1, 2], stored_scores=stored)
    jobs = JobList()

    _run(fake, jobs, fetch=False)

    assert jobs.rows[PostingId(1)].score == stored[PostingId(1)]
    assert fake.scored_ids == []


def test_saved_scores_are_not_scored_again_and_count_towards_the_limit() -> None:
    stored = {PostingId(1): Score(score=88, reasons=("saved",))}
    fake = _FakeService(saved=[1, 2, 3], stored_scores=stored, fits={2: 50, 3: 60})
    jobs = JobList()

    _run(fake, jobs, fetch=False, score=True, score_limit=2)

    assert fake.scored_ids == [[PostingId(2)]]


def test_jobs_a_fetch_brings_in_get_scored_too() -> None:
    fake = _FakeService(saved=[1], arriving=[2], fits={1: 50, 2: 60})
    jobs = JobList()

    _run(fake, jobs, score=True)

    assert jobs.rows[PostingId(2)].score is not None


def test_scoring_that_cannot_run_leaves_the_jobs_and_the_fetch() -> None:
    error = NoCvError()
    fake = _FakeService(saved=[1], arriving=[2], score_error=error)
    jobs = JobList()

    _run(fake, jobs, score=True)

    assert jobs.score_error is error
    assert len(jobs.rows) == 2
    assert all(row.state is ScoreState.NONE for row in jobs.rows.values())


def test_a_closed_job_leaves_the_list_but_a_known_score_stays() -> None:
    jobs = JobList()
    jobs.set_jobs([_posting(1), _posting(2)], mark_new=False)
    jobs.set_saved_scores({PostingId(1): Score(score=70, reasons=("r",))})

    jobs.set_jobs([_posting(1)], mark_new=True)

    assert list(jobs.rows) == [PostingId(1)]
    assert jobs.rows[PostingId(1)].score is not None
    assert not jobs.rows[PostingId(1)].is_new
