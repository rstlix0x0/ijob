"""The terminal UI, driven through its keys over a real store with fake engines.

No test here waits on real time: each one lets the app settle and then checks
what it holds. Network and models are fakes, so a run takes a fraction of a second.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, cast

import pytest
from ijob_ai.agents.react import AgentStep, StepCallback, ToolOutcome
from ijob_ai.errors import (
    CapReachedError,
    MissingModelError,
    NoCvError,
    ProviderFailedError,
    TaskOutputError,
)
from ijob_ai.tasks.versions import SCORER_UNTRUSTED
from ijob_app.agent_ops import FoundSource, SourceResult
from ijob_app.home import IjobHome, setup_home
from ijob_app.llm_ops import (
    LlmOptions,
    OperationReport,
    ScoreItem,
    ScoreStatus,
    StopReason,
)
from ijob_app.recipe_ops import RecipeProposal
from ijob_app.service import (
    FetchProgress,
    RefreshReport,
    Service,
    SourceEvent,
    open_service,
)
from ijob_app.tui import IjobApp
from ijob_app.tui.filters import FilterScreen
from ijob_app.tui.found import SourceFoundScreen
from ijob_app.tui.jobs import JobsPane, chips, matches
from ijob_app.tui.note import NoteScreen
from ijob_app.tui.recipe import RecipeProposalScreen
from ijob_app.tui.sources import SourcesPane
from ijob_core.eval_records import EvalTask
from ijob_core.model import (
    ExternalId,
    Instructions,
    Posting,
    PostingId,
    ResumeProfile,
    Score,
    Seniority,
    SightingDraft,
    SourceRef,
    parse_source_ref,
)
from ijob_core.store import SourceRow, Store
from ijob_sources.polite_http import PoliteClient
from ijob_sources.recipe_document import RecipeFormat
from rich.text import Text
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Static

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


type _Find = Callable[
    [StepCallback | None], Awaitable[tuple[SourceResult, OperationReport]]
]


class _Quiet:
    """Stands in for the slow parts of a service, so nothing leaves the machine."""

    def __init__(
        self,
        service: Service,
        *,
        score_error: Exception | None = None,
        find: _Find | None = None,
    ) -> None:
        self._service = service
        self._score_error = score_error
        self._find = find
        self.finds: list[tuple[str, str | None]] = []
        self.on_cost: Callable[[float], None] | None = None
        self.added_found: list[FoundSource] = []
        self.added_urls: list[tuple[str, str]] = []
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

    async def find_source(
        self,
        company: str,
        url: str | None = None,
        *,
        model: object = None,
        on_step: StepCallback | None = None,
        on_cost: Callable[[float], None] | None = None,
    ) -> tuple[SourceResult, OperationReport]:
        assert model is None
        assert self._find is not None
        self.finds.append((company, url))
        self.on_cost = on_cost
        return await self._find(on_step)

    async def add_found(self, found: FoundSource) -> SourceRow:
        self.added_found.append(found)
        return self._store_source(found.ref, found.company)

    async def add_url(self, url: str, company: str) -> SourceRow:
        self.added_urls.append((url, company))
        return self._store_source(parse_source_ref("feed:made-up"), company)

    def _store_source(self, ref: SourceRef, company: str) -> SourceRow:
        store = self._service._store  # the real table, so the list can redraw
        store.add_source(ref, company)
        row = store.source(ref)
        assert row is not None
        return row

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


class _TestApp(IjobApp):
    """The app with toasts that outlive any pause a busy machine puts in a test."""

    NOTIFICATION_TIMEOUT: ClassVar[float] = 3600


def _run(
    home: IjobHome,
    steps: Callable[[IjobApp, Pilot[None], _Quiet], Awaitable[None]],
    *,
    score_error: Exception | None = None,
    find: _Find | None = None,
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
            quiet = _Quiet(service, score_error=score_error, find=find)
            app = _TestApp(
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


def _accept_a_broken_scorer_program(home: IjobHome) -> int:
    """Store a profile and an accepted scorer program whose demos cannot be read."""
    with Store.open(home.db) as store:
        profile = ResumeProfile(
            summary="Backend engineer",
            years_experience=7,
            seniority=Seniority.SENIOR,
            roles=("Backend Engineer",),
            spoken_languages=("English",),
            locations=("Jakarta",),
            work_authorization=("Indonesia",),
            skills=("Python",),
        )
        store.set_cv_profile(
            profile,
            cv_sha256="a" * 64,
            model="claude-sdk/m",
            task_version="cv_profile-1",
            made_at=_NOW,
        )
        eval_set = store.evals.find_or_create_set(
            "b" * 64, profile.sha256(), ["made-up/a", "made-up/b"], created_at=_NOW
        )
        saved = store.tunes.save_program(
            task=EvalTask.SCORER,
            model="claude-sdk/m",
            optimizer="gepa",
            base_version="scorer-1",
            set_id=eval_set.id,
            bound_sha256=profile.sha256(),
            instructions=f"Tuned. {SCORER_UNTRUSTED}",
            demos_json="[]",
            version="scorer-1+0a1b2c3d",
            tune_run_id=None,
            created_at=_NOW,
        )
        store.tunes.accept_program(saved.id, accepted_at=_NOW)
        with store._conn:
            store._conn.execute("UPDATE programs SET demos_json = 'not json'")
        return saved.id


def test_a_filter_change_with_a_broken_program_says_why_and_stays_open(
    home: IjobHome,
) -> None:
    program_id = _accept_a_broken_scorer_program(home)

    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#filter-company", Input).value = "Globex"
        await pilot.press("enter")
        await _settle(app, pilot)

        assert app.is_running
        assert any(f"program {program_id}" in note for note in _notes(app))
        assert _titles(app) == []
        assert _note_markup(app) == [False] * len(_notes(app))

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


_FIND_COMPANY = "Stripe"
_FOUND_REF = parse_source_ref("greenhouse:stripe")
_NEEDS_COMPANY = "Give a company, with a URL or kind:key if you have one."
_PAUSES = 200


def _report(
    *, spent: float = 0.002, stopped_by: StopReason | None = None
) -> OperationReport:
    return OperationReport(
        done=1, not_done=0, spent=spent, cap=0.5, stopped_by=stopped_by
    )


def _proposal() -> RecipeProposal:
    return RecipeProposal(
        ref=parse_source_ref("recipe:careers"),
        target_url="https://stripe.test/careers",
        company=_FIND_COMPANY,
        recipe="{}",
        recipe_format=RecipeFormat.HTML,
        recipe_url="https://stripe.test/careers",
        jobs=(),
        invalid=0,
        model="made-up",
        report=_report(),
    )


def _found(
    *,
    proposal: RecipeProposal | None = None,
    sample: tuple[str, ...] = ("Staff",),
    known: bool = False,
) -> FoundSource:
    return FoundSource(
        ref=_FOUND_REF,
        company=_FIND_COMPANY,
        target_url=None,
        jobs=744,
        proposal=proposal,
        known=known,
        sample=sample,
    )


def _result(
    found: FoundSource | None = None,
    stop: Exception | None = None,
    steps: Sequence[AgentStep] = (AgentStep("try_board", ToolOutcome.OK),),
) -> SourceResult:
    return SourceResult(
        verified=[], violations=[], stop=stop, found=found, steps=list(steps)
    )


def _answer(result: SourceResult, report: OperationReport | None = None) -> _Find:
    async def find(
        _on_step: StepCallback | None,
    ) -> tuple[SourceResult, OperationReport]:
        return result, report or _report()

    return find


async def _enter(app: IjobApp, pilot: Pilot[None], url: str, company: str) -> None:
    """Open the Sources tab, fill the add box and press Enter in it."""
    await pilot.press("2")
    await _settle(app, pilot)
    app.query_one("#add-url", Input).value = url
    box = app.query_one("#add-company", Input)
    box.value = company
    box.focus()
    await pilot.press("enter")


async def _until_screen(
    app: IjobApp, pilot: Pilot[None], kind: type[Screen[bool]]
) -> None:
    """Let the app run until the modal is up; the worker holds it open."""
    for _ in range(_PAUSES):
        if isinstance(app.screen, kind):
            return
        await pilot.pause()
    pytest.fail(f"{kind.__name__} never opened")


def _status(app: IjobApp) -> str:
    return str(app.query_one("#sources-status", Static).render())


def _notes(app: IjobApp) -> list[str]:
    return [note.message for note in app._notifications]


def _note_markup(app: IjobApp) -> list[bool]:
    return [note.markup for note in app._notifications]


def _modal_text(app: IjobApp) -> str:
    return "\n".join(str(line.render()) for line in app.screen.query(Static))


def test_sources_blank_url_starts_the_agent(home: IjobHome) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def find(
        on_step: StepCallback | None,
    ) -> tuple[SourceResult, OperationReport]:
        assert on_step is not None
        on_step(AgentStep("try_board", ToolOutcome.OK))
        on_step(AgentStep("open_page", ToolOutcome.FAILED))
        started.set()
        await release.wait()
        return _result(), _report()

    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await started.wait()
        await pilot.pause()

        assert quiet.finds == [(_FIND_COMPANY, None)]
        assert "Finding Stripe" in _status(app)
        assert "step 2" in _status(app)
        release.set()
        await _settle(app, pilot)

    _run(home, steps, find=find)


def test_find_status_line_shows_steps_and_cost(home: IjobHome) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    quiet_of: list[_Quiet] = []

    async def find(
        on_step: StepCallback | None,
    ) -> tuple[SourceResult, OperationReport]:
        assert on_step is not None
        [quiet] = quiet_of
        assert quiet.on_cost is not None
        quiet.on_cost(0.001)
        on_step(AgentStep("try_board", ToolOutcome.OK))
        quiet.on_cost(0.002)
        on_step(AgentStep("open_page", ToolOutcome.OK))
        started.set()
        await release.wait()
        return _result(), _report()

    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        quiet_of.append(quiet)
        await _enter(app, pilot, "", _FIND_COMPANY)
        await started.wait()
        await pilot.pause()

        assert _status(app) == "Finding Stripe in the background · step 2 · $0.002"
        release.set()
        await _settle(app, pilot)

    _run(home, steps, find=find)


def test_find_status_line_leaves_money_off_before_any_cost(home: IjobHome) -> None:
    async def steps(app: IjobApp, _pilot: Pilot[None], _quiet: _Quiet) -> None:
        pane = app.query_one("#sources", SourcesPane)

        pane.show_progress(_FIND_COMPANY, 1)
        assert _status(app) == "Finding Stripe in the background · step 1"
        pane.show_progress(_FIND_COMPANY, 0, 0.5)
        assert _status(app) == "Finding Stripe in the background."

    _run(home, steps)


def test_find_progress_after_the_status_line_is_gone_is_ignored(
    home: IjobHome,
) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        pane = app.query_one("#sources", SourcesPane)
        await app.query_one("#sources-status").remove()
        await pilot.pause()

        pane.show_progress(_FIND_COMPANY, 3)

    _run(home, steps)


def test_sources_url_and_company_uses_the_add_path(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "https://stripe.test/jobs", _FIND_COMPANY)
        await _settle(app, pilot)

        assert quiet.added_urls == [("https://stripe.test/jobs", _FIND_COMPANY)]
        assert quiet.finds == []
        assert _status(app) == "Added https://stripe.test/jobs."

    _run(home, steps)


def test_sources_url_without_company_shows_the_message(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "https://stripe.test/jobs", "")
        await _settle(app, pilot)

        assert _status(app) == _NEEDS_COMPANY
        assert quiet.added_urls == []
        assert quiet.finds == []

    _run(home, steps)


def test_sources_both_blank_shows_the_message(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "", "")
        await _settle(app, pilot)

        assert _status(app) == _NEEDS_COMPANY
        assert quiet.finds == []

    _run(home, steps)


def test_sources_placeholder_names_finding(home: IjobHome) -> None:
    async def steps(app: IjobApp, _pilot: Pilot[None], _quiet: _Quiet) -> None:
        box = app.query_one("#add-url", Input)

        assert box.placeholder == "URL, kind:key, or blank to find it"

    _run(home, steps)


def test_found_modal_cancel_has_focus(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _until_screen(app, pilot, SourceFoundScreen)

        assert app.screen.focused is not None
        assert app.screen.focused.id == "cancel"
        await pilot.press("enter")
        await _settle(app, pilot)

        assert quiet.added_found == []
        assert _status(app) == "Not stored."

    _run(home, steps, find=_answer(_result(_found())))


def test_found_modal_accept_adds(home: IjobHome) -> None:
    found = _found()

    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _until_screen(app, pilot, SourceFoundScreen)
        app.screen.query_one("#accept", Button).press()
        await _settle(app, pilot)

        assert quiet.added_found == [found]
        assert _status(app) == "Added greenhouse:stripe."
        table = app.query_one("#sources-table", DataTable)
        assert str(_FOUND_REF) in [key.value for key in table.rows]
        assert app.query_one("#add-company", Input).value == ""

    _run(home, steps, find=_answer(_result(found)))


def test_found_modal_draws_text_literally(home: IjobHome) -> None:
    found = _found(sample=("[bold red]Boss[/]", "[link=x]Click[/link]"))

    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _until_screen(app, pilot, SourceFoundScreen)

        text = _modal_text(app)
        assert "[bold red]Boss[/]" in text
        assert "[link=x]Click[/link]" in text
        assert "greenhouse:stripe" in text
        assert "744" in text
        assert "cost $0.0020 of $0.50" in text
        await pilot.press("escape")
        await _settle(app, pilot)

    _run(home, steps, find=_answer(_result(found)))


def test_find_is_busy_while_running(home: IjobHome) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def find(
        _on_step: StepCallback | None,
    ) -> tuple[SourceResult, OperationReport]:
        started.set()
        await release.wait()
        return _result(), _report()

    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await started.wait()
        await pilot.press("enter")
        await pilot.pause()

        assert "still running" in _status(app)
        assert len(quiet.finds) == 1
        release.set()
        await _settle(app, pilot)

    _run(home, steps, find=find)


def test_find_not_found_shows_status(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        assert "No source found for Stripe" in _status(app)
        assert "cost $0.0123 of $0.50" in _status(app)
        assert _notes(app) == []

    _run(home, steps, find=_answer(_result(), _report(spent=0.0123)))


def test_find_error_notifies(home: IjobHome) -> None:
    async def find(
        _on_step: StepCallback | None,
    ) -> tuple[SourceResult, OperationReport]:
        raise MissingModelError("source_agent", home.config)

    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        assert any("[models].source_agent" in note for note in _notes(app))
        assert "Finding" not in _status(app)
        await pilot.press("enter")  # the guard was let go, so this starts a find
        await _settle(app, pilot)
        assert len(quiet.finds) == 2

    _run(home, steps, find=find)


def test_find_cap_shows_the_message_on_status(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        status = _status(app)
        assert "Stopped at the cost cap" in status
        assert "cost $0.5000 of $0.50" in status
        assert "[llm].max_cost_usd" in status
        assert _notes(app) == []

    result = _result(stop=CapReachedError())
    _run(home, steps, find=_answer(result, _report(spent=0.5, stopped_by="cap")))


def test_find_provider_failure_notifies(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        assert any("the provider said no" in note for note in _notes(app))

    result = _result(stop=ProviderFailedError("the provider said no"))
    _run(home, steps, find=_answer(result, _report(stopped_by="provider")))


def test_find_unusable_answer_notifies(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        assert any("not an answer" in note for note in _notes(app))

    result = _result(stop=TaskOutputError("source_agent", "not an answer"))
    _run(home, steps, find=_answer(result))


def test_find_stop_with_a_verified_source_opens_the_modal(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _until_screen(app, pilot, SourceFoundScreen)

        assert "Stopped at the cost cap" in _status(app)
        app.screen.query_one("#accept", Button).press()
        await _settle(app, pilot)

        assert quiet.added_found == [_found()]

    result = _result(_found(), CapReachedError())
    _run(home, steps, find=_answer(result, _report(spent=0.5, stopped_by="cap")))


def test_find_learned_recipe_uses_the_recipe_modal(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _until_screen(app, pilot, RecipeProposalScreen)

        assert not isinstance(app.screen, SourceFoundScreen)
        await pilot.press("escape")
        await _settle(app, pilot)

    _run(home, steps, find=_answer(_result(_found(proposal=_proposal()))))


def test_find_error_text_from_outside_is_cleaned_and_not_markup(
    home: IjobHome,
) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        [note] = _notes(app)
        assert "\x1b" not in note
        assert "\x07" not in note
        assert "[red]x[/]" in note
        assert _note_markup(app) == [False]

    stop = ProviderFailedError("boom\x1b[2Jwiped [red]x[/]\x07")
    _run(home, steps, find=_answer(_result(stop=stop), _report(stopped_by="provider")))


def test_find_raised_error_text_from_outside_is_cleaned(home: IjobHome) -> None:
    async def find(
        _on_step: StepCallback | None,
    ) -> tuple[SourceResult, OperationReport]:
        raise ProviderFailedError("down\x1b[2J")

    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        assert _notes(app) == ["down[2J"]

    _run(home, steps, find=find)


def test_find_cap_with_nothing_verified_says_not_found_then_the_cap(
    home: IjobHome,
) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        status = _status(app)
        assert status.startswith("No source found for Stripe.")
        assert "Stopped at the cost cap" in status
        assert "[llm].max_cost_usd" in status
        assert _notes(app) == []

    result = _result(stop=CapReachedError())
    _run(home, steps, find=_answer(result, _report(spent=0.5, stopped_by="cap")))


def test_find_cap_note_outlives_accepting_the_source(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _until_screen(app, pilot, SourceFoundScreen)
        app.screen.query_one("#accept", Button).press()
        await _settle(app, pilot)

        status = _status(app)
        assert status.startswith("Added greenhouse:stripe.")
        assert "Stopped at the cost cap" in status

    result = _result(_found(), CapReachedError())
    _run(home, steps, find=_answer(result, _report(spent=0.5, stopped_by="cap")))


def test_find_known_recipe_is_not_offered_again(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _settle(app, pilot)

        assert not isinstance(app.screen, RecipeProposalScreen)
        assert "greenhouse:stripe is already a source" in _status(app)
        assert quiet.added_found == []
        await pilot.press("enter")  # the guard was let go, so this starts a find
        await _settle(app, pilot)
        assert len(quiet.finds) == 2

    found = _found(proposal=_proposal(), known=True)
    _run(home, steps, find=_answer(_result(found)))


def test_found_modal_leaves_out_the_finish_step(home: IjobHome) -> None:
    async def steps(app: IjobApp, pilot: Pilot[None], _quiet: _Quiet) -> None:
        await _enter(app, pilot, "", _FIND_COMPANY)
        await _until_screen(app, pilot, SourceFoundScreen)

        text = _modal_text(app)
        assert "try board" in text
        assert "finish" not in text
        await pilot.press("escape")
        await _settle(app, pilot)

    taken = (
        AgentStep("try_board", ToolOutcome.OK),
        AgentStep("finish", ToolOutcome.OK),
    )
    _run(home, steps, find=_answer(_result(_found(), steps=taken)))
