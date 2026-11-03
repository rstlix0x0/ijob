"""The terminal UI, driven through its keys over a real store with fake engines.

No test here waits on real time: each one lets the app settle and then checks
what it holds. Network and models are fakes, so a run takes a fraction of a second.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from ijob_ai.errors import NoCvError
from ijob_app.home import IjobHome, setup_home
from ijob_app.llm_ops import LlmOptions, OperationReport, ScoreItem, ScoreStatus
from ijob_app.service import (
    FetchProgress,
    RefreshReport,
    Service,
    SourceEvent,
    open_service,
)
from ijob_app.tui import IjobApp
from ijob_app.tui.filters import FilterScreen
from ijob_app.tui.jobs import JobsPane, chips, matches
from ijob_app.tui.note import NoteScreen
from ijob_core.model import (
    ExternalId,
    Instructions,
    Posting,
    PostingId,
    Score,
    SightingDraft,
    SourceRef,
    parse_source_ref,
)
from ijob_core.store import Store
from ijob_sources.polite_http import PoliteClient
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import ContentSwitcher, DataTable, Input, Static

_NOW = datetime(2026, 11, 3, tzinfo=UTC)
_REF = parse_source_ref("lever:acme")
_TITLES = ("Golang Engineer", "Product Manager", "ICU Nurse")


def _now() -> datetime:
    return _NOW


@pytest.fixture
def home(tmp_path: Path) -> IjobHome:
    """Give a home whose database holds a source that shows three jobs."""
    ijob_home = IjobHome(tmp_path / "home")
    setup_home(ijob_home)
    with Store.open(ijob_home.db) as store:
        store.import_seeds([(_REF, "Acme")])
        store.record(
            [
                SightingDraft(
                    source=_REF,
                    external_id=ExternalId(str(number)),
                    title=title,
                    company="Acme",
                    location="Remote",
                    url=f"https://example.com/{number}",
                    posted_at=_NOW,
                    raw="{}",
                    description=f"About the {title} role",
                )
                for number, title in enumerate(_TITLES)
            ],
            seen_at=_NOW,
        )
        store.mark_fetched(_REF, _NOW)
    return ijob_home


class _Quiet:
    """Stands in for the slow parts of a service, so nothing leaves the machine."""

    def __init__(
        self, service: Service, *, score_error: Exception | None = None
    ) -> None:
        self._service = service
        self._score_error = score_error
        self.last_report: OperationReport | None = None
        self.scored: list[list[PostingId]] = []
        self.fetches = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._service, name)

    async def refresh_off_loop(
        self,
        _refs: Sequence[SourceRef],
        *,
        on_progress: object,
        on_source: object,
    ) -> RefreshReport:
        self.fetches += 1
        cast("_Progress", on_progress)(FetchProgress(total=1, done=1, failed=0))
        cast("_Source", on_source)(SourceEvent(ref=_REF, listed=3))
        return RefreshReport(
            due=[], kept=[], fetched=[], rejected={}, failures=[], refused={}, stale=[]
        )

    async def score(self, ids: Sequence[PostingId]) -> AsyncIterator[ScoreItem]:
        self.scored.append(list(ids))
        if self._score_error is not None:
            raise self._score_error
        for position, posting_id in enumerate(ids):
            posting = self._service.saved_detail(posting_id).posting
            score = Score(score=90 - position * 10, reasons=("Fits",))
            yield ScoreItem(posting, score, ScoreStatus.JUDGED)
        self.last_report = OperationReport(
            done=len(ids), not_done=0, spent=0.01, cap=0.5, stopped_by=None
        )


class _Progress:
    def __call__(self, progress: FetchProgress) -> None: ...


class _Source:
    def __call__(self, event: SourceEvent) -> None: ...


def _client(_cache: object) -> PoliteClient:
    return PoliteClient()


def _run(
    home: IjobHome,
    steps: Callable[[IjobApp, Pilot[None], _Quiet], Awaitable[None]],
    *,
    score_error: Exception | None = None,
) -> _Quiet:
    """Open the app over the home and run a test's steps against it."""

    async def main() -> _Quiet:
        with open_service(
            home,
            None,
            _client,
            now=_now,
            llm=LlmOptions(config_path=home.config),
        ) as service:
            quiet = _Quiet(service, score_error=score_error)
            app = IjobApp(
                cast("Service", quiet), None, default_config=home.config, now=_now
            )
            async with app.run_test(size=(130, 38)) as pilot:
                await _settle(app, pilot)
                await steps(app, pilot, quiet)
            return quiet

    return asyncio.run(main())


async def _settle(app: IjobApp, pilot: Pilot[None]) -> None:
    """Let the workers finish, then draw what they left."""
    await app.workers.wait_for_complete()
    await pilot.pause()
    app._draw()  # the timer's job, done now so no test waits for it
    await pilot.pause()


def _titles(app: IjobApp) -> list[str]:
    """Read the titles the job table shows, top to bottom."""
    table = app.query_one("#jobs-table", DataTable)
    cells = [table.get_row_at(number)[1] for number in range(table.row_count)]
    return [cast("Text", cell).plain for cell in cells]


def test_a_draw_before_the_screen_is_ready_waits(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await app.query_one("#top").remove()
        await pilot.pause()
        app.changed()

        app._draw()

        assert app._dirty

    _run(home, steps)


def test_opens_on_the_saved_jobs(home: IjobHome) -> None:
    async def steps(app: IjobApp, _pilot: Pilot[None], quiet: _Quiet) -> None:
        assert sorted(_titles(app)) == sorted(_TITLES)
        assert quiet.fetches == 1
        status = app.query_one("#activity", Static)
        assert "up to date" in str(status.render())

    _run(home, steps)


def test_typing_filters_the_list_at_once(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("slash", *"nurse")
        await pilot.pause()

        assert _titles(app) == ["ICU Nurse"]
        count = app.query_one("#search-count", Static)
        assert "1 of 3" in str(count.render())

    _run(home, steps)


def test_enter_moves_from_the_search_box_to_the_list(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("slash", "enter")

        assert app.query_one("#jobs-table", DataTable).has_focus

    _run(home, steps)


def test_selecting_a_job_shows_its_details(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("slash", *"nurse", "enter")
        await pilot.pause()

        pane = app.query_one(JobsPane)
        selected = pane._selected
        assert selected is not None
        assert app.jobs.rows[selected].posting.title == "ICU Nurse"

    _run(home, steps)


def test_g_scores_the_list_and_sorts_it_best_first(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await pilot.press("g")
        await _settle(app, pilot)

        [ids] = quiet.scored
        assert len(ids) == 3
        assert app.jobs.scored == 3
        scores = [row.score.score for row in app.jobs.ranked() if row.score is not None]
        assert scores == sorted(scores, reverse=True)

    _run(home, steps)


def test_scoring_that_cannot_run_says_why_and_keeps_the_list(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("g")
        await _settle(app, pilot)

        assert isinstance(app.jobs.score_error, NoCvError)
        assert len(_titles(app)) == 3

    _run(home, steps, score_error=NoCvError())


def test_u_labels_the_job_good(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await pilot.press("u")
        await _settle(app, pilot)

        pane = app.query_one(JobsPane)
        selected = pane._selected
        assert selected is not None
        label = cast("Service", quiet).saved_detail(selected).label
        assert label is not None
        assert str(label.vote) == "up"

    _run(home, steps)


def test_n_asks_for_a_note(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("n")
        await pilot.pause()

        assert isinstance(app.screen, NoteScreen)

    _run(home, steps)


def test_f_changes_the_filters(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FilterScreen)

        app.screen.query_one("#filter-company", Input).value = "Globex"
        await pilot.press("enter")
        await _settle(app, pilot)

        assert app.filter_flags.companies == ("Globex",)
        assert _titles(app) == []

    _run(home, steps)


def test_a_filter_it_cannot_use_says_why_and_stays_open(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("f")
        await pilot.pause()

        app.screen.query_one("#filter-since", Input).value = "soon"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, FilterScreen)
        error = app.screen.query_one("#filter-error", Static)
        assert "since" in str(error.render())

    _run(home, steps)


@pytest.mark.parametrize(
    ("key", "pane"),
    [
        ("2", "sources"),
        ("3", "profile"),
        ("4", "profile"),
        ("5", "traces"),
        ("1", "jobs"),
    ],
)
def test_number_keys_show_each_tab(home: IjobHome, key: str, pane: str) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press(key)
        await _settle(app, pilot)

        assert app.query_one("#panes", ContentSwitcher).current == pane

    _run(home, steps)


def test_the_sources_tab_lists_each_source_with_its_jobs(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("2")
        await _settle(app, pilot)

        table = app.query_one("#sources-table", DataTable)
        rows = [table.get_row_at(number) for number in range(table.row_count)]
        ours = next(row for row in rows if cast("Text", row[0]).plain == str(_REF))
        assert cast("Text", ours[1]).plain == "3"
        assert "ok" in cast("Text", ours[2]).plain

    _run(home, steps)


def test_q_quits(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("q")
        await pilot.pause()

        assert not app.is_running

    _run(home, steps)


def test_title_search_needs_every_word_in_any_order() -> None:
    from ijob_app.job_search import JobRow  # noqa: PLC0415
    from ijob_app.tui.jobs import _matcher  # noqa: PLC0415

    row = JobRow(
        Posting(
            id=PostingId(1),
            title="Senior Product Manager",
            company="Acme",
            location=None,
            url="https://example.com",
            posted_at=None,
            first_seen=_NOW,
            last_seen=_NOW,
        )
    )

    assert matches(row, None)
    assert matches(row, _matcher("manager acme"))
    assert not matches(row, _matcher("manager globex"))


def test_chips_show_each_filter_in_force() -> None:
    from ijob_app.config import FilterFlags  # noqa: PLC0415

    plain = chips(FilterFlags(countries=("SG",), since="14d")).plain

    assert "SG" in plain
    assert "any remote" in plain
    assert "14d" in plain


def test_a_filter_applied_during_a_fetch_takes_effect_at_once(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        app._searching = True  # as it is while a fetch runs
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#filter-company", Input).value = "Globex"
        await pilot.press("enter")
        await _settle(app, pilot)

        assert _titles(app) == []

    _run(home, steps)


def test_a_country_filter_leaves_only_that_country(home: IjobHome) -> None:
    with Store.open(home.db) as store:
        store.record(
            [
                SightingDraft(
                    source=_REF,
                    external_id=ExternalId("tokyo"),
                    title="Tokyo Engineer",
                    company="Acme",
                    location="Tokyo",
                    url="https://example.com/tokyo",
                    posted_at=_NOW,
                    raw="{}",
                )
            ],
            seen_at=_NOW,
        )

    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#filter-country", Input).value = "Japan"
        await pilot.press("enter")
        await _settle(app, pilot)

        assert _titles(app) == ["Tokyo Engineer"]

    _run(home, steps)


def _other_source(home: IjobHome) -> None:
    other = parse_source_ref("greenhouse:globex")
    with Store.open(home.db) as store:
        store.import_seeds([(other, "Globex")])
        store.record(
            [
                SightingDraft(
                    source=other,
                    external_id=ExternalId("g1"),
                    title="Globex Designer",
                    company="Globex",
                    location="Remote",
                    url="https://example.com/g1",
                    posted_at=_NOW,
                    raw="{}",
                )
            ],
            seen_at=_NOW,
        )
        store.mark_fetched(other, _NOW)


@pytest.mark.parametrize(
    "typed", ["greenhouse", "globex", "GLOBEX", "greenhouse:globex"]
)
def test_a_source_can_be_chosen_by_any_part_of_it(home: IjobHome, typed: str) -> None:
    _other_source(home)

    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#filter-source", Input).value = typed
        app.screen.query_one("#filter-source", Input).focus()
        await pilot.press("enter")
        await _settle(app, pilot)

        assert _titles(app) == ["Globex Designer"]

    _run(home, steps)


def test_a_source_nothing_matches_says_so(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#filter-source", Input).value = "nosuch"
        app.screen.query_one("#filter-source", Input).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, FilterScreen)
        assert "No source matches" in str(
            app.screen.query_one("#filter-error", Static).render()
        )

    _run(home, steps)


def test_saved_rules_that_filter_show_as_a_chip(home: IjobHome) -> None:
    with Store.open(home.db) as store:
        store.set_instructions(
            Instructions(
                text="Engineers",
                filters_json='{"role": {"include": ["engineer"]}}',
                rules=(),
                model="made/up",
                task_version="1",
                set_at=_NOW,
            )
        )

    async def steps(app: IjobApp, _pilot: Pilot[None], _quiet: _Quiet) -> None:
        drawn = str(app.query_one("#search-chips", Static).render())
        assert "your rules" in drawn
        assert "engineer" in drawn

    _run(home, steps)


def test_the_search_box_matches_the_start_of_a_word(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("slash", *"urse")
        await pilot.pause()

        assert _titles(app) == []

    _run(home, steps)
