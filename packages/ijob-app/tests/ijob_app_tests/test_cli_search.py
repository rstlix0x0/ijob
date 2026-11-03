from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from ijob_ai.errors import MissingModelError, NoCvError
from ijob_app.cli_output import say_done, say_error, say_note
from ijob_app.cli_search import (
    SearchOptions,
    failure_reason,
    live_fetch_command,
    live_score_command,
    live_search_command,
)
from ijob_app.config import FilterLayers
from ijob_app.llm_ops import OperationReport, ScoreItem, ScoreStatus
from ijob_app.service import FetchProgress, RefreshReport, Service, SourceEvent
from ijob_core.model import Posting, PostingId, Score, SourceRef, parse_source_ref
from ijob_sources.collect import SourceFailure
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    RobotsDisallowedError,
    SourceNotFoundError,
)

_NOW = datetime(2026, 11, 3, tzinfo=UTC)
_REF = parse_source_ref("lever:acme")


def _posting(posting_id: int = 1) -> Posting:
    return Posting(
        id=PostingId(posting_id),
        title=f"Job {posting_id}",
        company="Acme",
        location="Remote",
        url="https://example.com",
        posted_at=_NOW,
        first_seen=_NOW,
        last_seen=_NOW,
    )


def _report(*failures: SourceFailure) -> RefreshReport:
    return RefreshReport(
        due=[],
        kept=[],
        fetched=[],
        rejected={},
        failures=list(failures),
        refused={},
        stale=[],
    )


@dataclass
class _Fake:
    """The parts of the service the live views call."""

    interrupt: bool = False
    score_error: Exception | None = None
    last_report: OperationReport | None = None

    async def list_jobs(
        self, _layers: FilterLayers, _refs: Sequence[SourceRef]
    ) -> list[Posting]:
        return [_posting(1), _posting(2)]

    def saved_scores(self) -> dict[PostingId, Score]:
        return {}

    async def refresh_off_loop(
        self, _refs: Sequence[SourceRef], *, on_progress: object, on_source: object
    ) -> RefreshReport:
        if self.interrupt:
            raise KeyboardInterrupt
        cast("_ProgressCall", on_progress)(FetchProgress(total=1, done=1, failed=0))
        failure = SourceFailure(_REF, SourceNotFoundError("https://x"))
        cast("_SourceCall", on_source)(SourceEvent(ref=_REF, failure=failure))
        cast("_SourceCall", on_source)(SourceEvent(ref=_REF, new=2, listed=9))
        return _report(failure)

    async def score(self, ids: Sequence[PostingId]) -> AsyncIterator[ScoreItem]:
        if self.score_error is not None:
            raise self.score_error
        for posting_id in ids:
            yield ScoreItem(
                _posting(posting_id),
                Score(score=int(posting_id) * 10, reasons=("Fits",)),
                ScoreStatus.JUDGED,
            )
        self.last_report = OperationReport(
            done=len(ids), not_done=0, spent=0.01, cap=0.5, stopped_by=None
        )


class _ProgressCall:
    def __call__(self, progress: FetchProgress) -> None: ...


class _SourceCall:
    def __call__(self, event: SourceEvent) -> None: ...


def _options(tmp_path: Path, **overrides: object) -> SearchOptions:
    values: dict[str, object] = {
        "title": "golang",
        "fetch": True,
        "score": False,
        "score_limit": 50,
        "show_all": False,
        "config": tmp_path / "absent.toml",
    }
    values.update(overrides)
    return SearchOptions(**values)  # type: ignore[arg-type]


def _search(fake: _Fake, options: SearchOptions) -> bool:
    return live_search_command(
        cast("Service", fake),
        cast("FilterLayers", object()),
        [],
        lambda: _NOW,
        options,
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RobotsDisallowedError("https://x"), "disallowed by robots.txt"),
        (SourceNotFoundError("https://x"), "not found"),
        (HttpStatusError("https://x", 503), "HTTP 503"),
        (FetchError("https://x", "timed out"), "timed out"),
    ],
)
def test_failure_reason(error: Exception, reason: str) -> None:
    assert failure_reason(error) == reason


def test_search_ends_with_the_list_the_failures_and_what_next(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = _search(_Fake(), _options(tmp_path))

    out = capsys.readouterr().out
    assert failed is False
    assert "Job 1" in out
    assert "lever:acme" in out
    assert "not found" in out
    assert "ijob show 1" in out


def test_search_scores_best_first_with_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = _search(_Fake(), _options(tmp_path, score=True, fetch=False))

    out = capsys.readouterr().out
    assert failed is False
    assert out.index("Job 2") < out.index("Job 1")
    assert "Fits" in out
    assert "2 scored" in out
    assert "$0.0100" in out


@pytest.mark.parametrize(
    ("error", "fix"),
    [
        (NoCvError(), "ijob cv set cv.pdf"),
        (MissingModelError("scorer", Path("config.toml")), "[models].scorer"),
    ],
)
def test_scoring_that_cannot_run_keeps_the_list_and_gives_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception, fix: str
) -> None:
    failed = _search(
        _Fake(score_error=error), _options(tmp_path, score=True, fetch=False)
    )

    out = capsys.readouterr().out
    assert failed is True
    assert "Job 1" in out
    assert "Scoring did not run" in out
    assert fix in out


def test_stopping_a_search_shows_what_is_there(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _search(_Fake(interrupt=True), _options(tmp_path))

    assert "Stopped" in capsys.readouterr().out


def test_fetch_prints_a_line_as_each_source_ends(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = live_fetch_command(cast("Service", _Fake()), [])

    out = capsys.readouterr().out
    assert report is not None
    assert "not found" in out
    assert "+2 new" in out


def test_stopping_a_fetch_says_what_was_saved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = live_fetch_command(cast("Service", _Fake(interrupt=True)), [])

    assert report is None
    assert "Stopped" in capsys.readouterr().out


def test_score_ends_best_first(capsys: pytest.CaptureFixture[str]) -> None:
    report = live_score_command(cast("Service", _Fake()), [PostingId(1), PostingId(2)])

    out = capsys.readouterr().out
    assert report is not None
    assert out.rindex("Job 2") < out.rindex("Job 1")
    assert "2 scored" in out


def test_say_done_is_plain_in_a_pipe(capsys: pytest.CaptureFixture[str]) -> None:
    say_done("plain line", "Drawn", "detail")

    assert capsys.readouterr().out == "plain line\n"


def test_say_error_is_plain_in_a_pipe(capsys: pytest.CaptureFixture[str]) -> None:
    say_error("It broke", "ijob setup")

    assert capsys.readouterr().err == "error: It broke; run: ijob setup\n"


def test_say_note_keeps_its_prefix_in_a_pipe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    say_note("note: read this")

    assert capsys.readouterr().err == "note: read this\n"
