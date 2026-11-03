from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from ijob_app.job_search import JobList, JobRow, ScoreState
from ijob_app.service import FetchProgress, PostingDetail, SourceEvent
from ijob_app.view import (
    age_text,
    ago_text,
    bar,
    clean,
    duration_text,
    error_lines,
    extract_status,
    fetch_status,
    final_search,
    fix_line,
    instructions_view,
    is_terminal,
    live_search,
    posting_detail,
    profile_view,
    recipe_view,
    score_color,
    search_title,
    source_event_line,
    sources_table,
    traces_table,
    unread_table,
)
from ijob_core.model import (
    ExternalId,
    Posting,
    PostingId,
    ResumeProfile,
    Score,
    Seniority,
    SightingDraft,
    Verdict,
    parse_source_ref,
)
from ijob_core.store import SourceOrigin, SourceRow
from ijob_core.traces import TraceSummary
from ijob_sources.collect import SourceFailure
from ijob_sources.errors import SourceNotFoundError
from rich.console import Console, RenderableType

_NOW = datetime(2026, 11, 3, tzinfo=UTC)
_REF = parse_source_ref("lever:acme")


def _posting(posting_id: int = 1, title: str = "Go Engineer") -> Posting:
    return Posting(
        id=PostingId(posting_id),
        title=title,
        company="Acme",
        location="Berlin",
        url="https://example.com/1",
        posted_at=_NOW - timedelta(days=2),
        first_seen=_NOW,
        last_seen=_NOW,
    )


def _draw(item: RenderableType, width: int = 100) -> str:
    out = StringIO()
    Console(file=out, width=width, height=40, highlight=False).print(item)
    return out.getvalue()


def _jobs(*rows: JobRow) -> JobList:
    return JobList(rows={row.posting.id: row for row in rows}, loaded=True)


@pytest.mark.parametrize(
    ("value", "drawn"),
    [(0, "░░░░░"), (50, "██░░░"), (100, "█████"), (140, "█████"), (-5, "░░░░░")],
)
def test_bar(value: int, drawn: str) -> None:
    assert bar(value, 5) == drawn


def test_clean_removes_escapes_and_keeps_one_line() -> None:
    assert clean("Go\x1b[31m\n  Engineer\u200b") == "Go[31m Engineer"


def test_stronger_fits_get_their_own_colors() -> None:
    colors = {score_color(score) for score in (90, 70, 50, 10)}
    assert len(colors) == 4


def test_age_uses_days_then_hours() -> None:
    assert age_text(_posting(), _NOW) == "2d"
    recent = replace(_posting(), posted_at=_NOW - timedelta(hours=3))
    assert age_text(recent, _NOW) == "3h"


def test_age_falls_back_to_when_it_was_first_seen() -> None:
    posting = replace(_posting(), posted_at=None, first_seen=_NOW - timedelta(days=5))
    assert age_text(posting, _NOW) == "5d"


def test_duration_for_a_person() -> None:
    assert duration_text(4_000) == "4s"
    assert duration_text(72_000) == "1m 12s"


def test_outside_text_cannot_carry_markup() -> None:
    row = JobRow(_posting(title="[bold red]Hacked[/bold red] [/oops]"))

    drawn = _draw(
        final_search(_jobs(row), _NOW, title=None, with_score=False, limit=None),
        width=120,
    )

    assert "[bold red]Hacked[/bold red]" in drawn


def test_live_search_shows_what_is_running() -> None:
    scored = JobRow(_posting(1), Score(score=91, reasons=("Fits",)), ScoreState.SCORED)
    running = JobRow(_posting(2, "Second"), state=ScoreState.QUEUED)
    waiting = JobRow(_posting(3, "Third"), state=ScoreState.QUEUED)
    jobs = _jobs(scored, running, waiting)
    jobs.fetch = FetchProgress(total=168, done=84, failed=1)
    jobs.new_jobs = 12

    drawn = _draw(
        live_search(
            jobs, _NOW, title="golang", with_score=True, room=10, concurrency=1
        ),
        width=120,
    )

    assert "91" in drawn
    assert "scoring" in drawn
    assert "queued" in drawn
    assert "fetching 84/168" in drawn
    assert "1 failed" in drawn
    assert "+12 new" in drawn
    assert "ctrl+c stops" in drawn


def test_live_search_says_how_many_more_do_not_fit() -> None:
    rows = [JobRow(_posting(number)) for number in range(1, 6)]

    drawn = _draw(
        live_search(
            _jobs(*rows), _NOW, title=None, with_score=False, room=2, concurrency=0
        )
    )

    assert "+ 3 more when this ends" in drawn


def test_final_search_lists_reasons_and_what_was_left_out() -> None:
    scored = JobRow(
        _posting(1), Score(score=91, reasons=("A reason", "Another")), ScoreState.SCORED
    )
    other = JobRow(_posting(2, "Other"))

    drawn = _draw(
        final_search(_jobs(scored, other), _NOW, title="go", with_score=True, limit=1)
    )

    assert "A reason" in drawn
    assert "Another" in drawn
    assert "+ 1 more, add --all" in drawn
    assert "best first" in drawn


def test_final_search_says_when_nothing_matches() -> None:
    drawn = _draw(final_search(_jobs(), _NOW, title="x", with_score=False, limit=None))

    assert "No job matches." in drawn


def test_source_event_lines() -> None:
    saved = SourceEvent(ref=_REF, new=3, listed=40, rejected=2)
    failed = SourceEvent(
        ref=_REF, failure=SourceFailure(_REF, SourceNotFoundError("https://x"))
    )

    assert "+3 new, 40 open, 2 skipped" in _draw(source_event_line(saved, None))
    assert "not found" in _draw(source_event_line(failed, "not found"))


def test_fetch_status_counts_new_jobs() -> None:
    drawn = _draw(fetch_status(FetchProgress(total=10, done=5, failed=0), 7))

    assert "fetching 5/10" in drawn
    assert "+7 new so far" in drawn


def _detail(**overrides: object) -> PostingDetail:
    values: dict[str, object] = {
        "posting": _posting(),
        "sources": [_REF],
        "label": None,
        "description": "First line\nSecond line",
        "unavailable_reason": None,
    }
    values.update(overrides)
    return PostingDetail(**values)  # type: ignore[arg-type]


def test_posting_detail_shows_score_text_and_link() -> None:
    drawn = _draw(
        posting_detail(_detail(), _NOW, score=Score(score=80, reasons=("Fits",)))
    )

    assert "Go Engineer" in drawn
    assert "no salary stated" in drawn
    assert "80" in drawn
    assert "Fits" in drawn
    assert "Second line" in drawn
    assert "https://example.com/1" in drawn


def test_posting_detail_says_why_it_has_no_description() -> None:
    drawn = _draw(
        posting_detail(
            _detail(description=None, unavailable_reason="gone"), _NOW, score=None
        )
    )

    assert "Description unavailable: gone" in drawn


def test_sources_table_shows_counts_and_fetch_age() -> None:
    row = SourceRow(
        ref=_REF,
        company="Acme",
        target_url=None,
        origin=SourceOrigin.SEED,
        removed=False,
        last_fetched_at=_NOW - timedelta(hours=5),
    )

    drawn = _draw(sources_table([row], {str(_REF): 12}, _NOW))

    assert "lever:acme" in drawn
    assert "12" in drawn
    assert "5h ago" in drawn


def test_profile_view_lists_what_the_cv_says() -> None:
    profile = ResumeProfile(
        summary="Backend engineer",
        years_experience=None,
        seniority=Seniority.SENIOR,
        roles=("Engineer",),
        spoken_languages=(),
        locations=(),
        work_authorization=(),
        skills=("Go",),
    )

    drawn = _draw(profile_view(profile))

    assert "Backend engineer" in drawn
    assert "years not stated" in drawn
    assert "Go" in drawn
    assert "none stated" in drawn


def test_instructions_view_separates_filters_and_rules() -> None:
    drawn = _draw(
        instructions_view([("title", "go")], ("Senior only",), text="My words")
    )

    assert "My words" in drawn
    assert "title: go" in drawn
    assert "1 Senior only" in drawn


def test_traces_table_shows_cost_and_time() -> None:
    summary = TraceSummary(
        trace_id="abc",
        started=_NOW,
        operation="ijob score",
        calls=3,
        cost_usd=0.0123,
        duration_ms=72_000,
    )

    drawn = _draw(traces_table([summary]))

    assert "$0.0123" in drawn
    assert "1m 12s" in drawn


def test_error_lines_show_the_fix() -> None:
    drawn = _draw(error_lines("No CV", "ijob cv set cv.pdf"))

    assert "No CV" in drawn
    assert "fix" in drawn
    assert "ijob cv set cv.pdf" in drawn


def test_fix_line_draws_its_command_literally() -> None:
    drawn = _draw(fix_line("ijob\x1b]0;pwned\x07 cv\nset"))

    assert "\x1b" not in drawn
    assert "\x07" not in drawn
    assert "ijob]0;pwned cv set" in drawn


def test_sources_table_shows_the_biggest_and_what_was_left_out() -> None:
    def source(key: str) -> SourceRow:
        return SourceRow(
            ref=parse_source_ref(f"lever:{key}"),
            company=key,
            target_url=None,
            origin=SourceOrigin.SEED,
            removed=False,
            last_fetched_at=None,
        )

    drawn = _draw(
        sources_table(
            [source("small"), source("big")],
            {"lever:small": 1, "lever:big": 900},
            _NOW,
            limit=1,
        )
    )

    assert "lever:big" in drawn
    assert "lever:small" not in drawn
    assert "+ 1 more, add --all" in drawn
    assert "never" in drawn


@pytest.mark.parametrize(
    ("ago", "text"),
    [
        (timedelta(seconds=10), "now"),
        (timedelta(minutes=5), "5m ago"),
        (timedelta(hours=3), "3h ago"),
        (timedelta(days=2), "2d ago"),
    ],
)
def test_ago_text(ago: timedelta, text: str) -> None:
    assert ago_text(_NOW - ago, _NOW) == text


def test_one_job_is_not_jobs() -> None:
    assert search_title("nurse", 1) == "nurse  ·  1 job"
    assert search_title(None, 2, 10) == "all jobs  ·  2 of 10 jobs"


def test_recipe_view_shows_what_was_read() -> None:
    job = SightingDraft(
        source=_REF,
        external_id=ExternalId("1"),
        title="[bold]Rust Engineer",
        company="Acme",
        location=None,
        url="https://example.com/1",
        posted_at=None,
        raw="{}",
    )

    drawn = _draw(recipe_view("html", "https://example.com", 1, [job]), width=120)

    assert "1 jobs read, 1 invalid" in drawn
    assert "[bold]Rust Engineer" in drawn
    assert "https://example.com" in drawn


def test_unread_table_gives_each_reason() -> None:
    drawn = _draw(unread_table([("hn:whoishiring", "Some post", "no title")]))

    assert "Still unread" in drawn
    assert "no title" in drawn


def test_final_search_hides_what_breaks_a_rule_unless_asked() -> None:
    fits = JobRow(
        _posting(1, "Fits"), verdict=Verdict(keep=True, reason="ok", rule=None)
    )
    breaks = JobRow(
        _posting(2, "Breaks"), verdict=Verdict(keep=False, reason="Too junior", rule=1)
    )

    hidden = _draw(
        final_search(
            _jobs(fits, breaks),
            _NOW,
            title=None,
            with_score=False,
            limit=None,
            with_rules=True,
        )
    )
    shown = _draw(
        final_search(
            _jobs(fits, breaks),
            _NOW,
            title=None,
            with_score=False,
            limit=None,
            with_rules=True,
            show_dropped=True,
        )
    )

    assert "Fits" in hidden
    assert "Breaks" not in hidden
    assert "Breaks" in shown
    assert "Too junior" in shown


def test_extract_status_before_and_after_the_count_is_known() -> None:
    assert "Looking" in _draw(extract_status(0, read=0, added=0, not_jobs=0))
    assert "reading 2/4" in _draw(extract_status(4, read=2, added=1, not_jobs=1))


def test_a_pipe_is_not_a_terminal() -> None:
    assert is_terminal() is False
