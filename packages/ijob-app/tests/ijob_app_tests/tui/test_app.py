import asyncio
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import httpx
import pytest
from ijob_app.cli import make_app
from ijob_app.home import IjobHome
from ijob_app.service import ClientFactory, Service, open_service
from ijob_app.tui import IjobApp
from ijob_core.countries import CountryCode
from ijob_core.filters import FilterSpec
from ijob_core.model import (
    ExternalId,
    Label,
    Posting,
    RemoteScope,
    SightingDraft,
    SourceRef,
    Vote,
    parse_source_ref,
)
from ijob_core.money import Money, Period
from ijob_sources.polite_http import ConditionalCache, PoliteClient
from textual.pilot import Pilot
from textual.widgets import Checkbox, DataTable, Input, Markdown
from textual.worker import WorkerState
from typer.testing import CliRunner

type Handler = Callable[[httpx.Request], httpx.Response]
type Opener = Callable[[Handler], AbstractContextManager[Service]]


class Seeder(Protocol):
    def __call__(
        self, drafts: Sequence[SightingDraft], *, seen_at: datetime = ...
    ) -> None: ...


_FIXTURES = (
    Path(__file__).parents[4]
    / "ijob-sources"
    / "tests"
    / "ijob_sources_tests"
    / "fixtures"
)
_WORKABLE_JOB: dict[str, object] = json.loads(
    (_FIXTURES / "workable" / "huggingface_job.json").read_bytes()
)
_SHORTCODE = str(_WORKABLE_JOB["shortcode"])
_HUGGINGFACE = parse_source_ref("workable:huggingface")
_ALLOWING_ROBOTS = "User-agent: *\nDisallow: /embed/\n"
_BOARD = (_FIXTURES / "greenhouse" / "tailscale_jobs.json").read_bytes()
_TAILSCALE = parse_source_ref("greenhouse:tailscale")
_YESTERDAY = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
_STRIPE = parse_source_ref("greenhouse:stripe")
_ANTHROPIC = parse_source_ref("greenhouse:anthropic")


def _draft(
    source: SourceRef,
    external_id: str,
    title: str,
    *,
    country: str | None = None,
    salary: bool = False,
) -> SightingDraft:
    return SightingDraft(
        source=source,
        external_id=ExternalId(external_id),
        title=title,
        company="Acme",
        location="Somewhere",
        url=f"https://jobs.example.test/{external_id}",
        posted_at=None,
        raw="{}",
        salaries=(Money(Decimal(6000), None, "SGD", Period.MONTH),) if salary else (),
        country=CountryCode(country) if country else None,
        remote=RemoteScope.COUNTRY if country else None,
        description="Body text.",
    )


def _no_requests(requests: list[httpx.Request]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    return handler


def _titles(app: IjobApp) -> list[str]:
    table = app.query_one("#results", DataTable)
    return [str(table.get_row_at(index)[1]) for index in range(table.row_count)]


def _label_of(app: IjobApp, title: str) -> str:
    return _labels(app)[_titles(app).index(title)]


def _labels(app: IjobApp) -> list[str]:
    table = app.query_one("#results", DataTable)
    return [str(table.get_row_at(index)[0]) for index in range(table.row_count)]


async def _settle(app: IjobApp, pilot: Pilot[None]) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


def _seed_three(seed: Seeder) -> None:
    seed(
        [
            _draft(_STRIPE, "1", "Backend Engineer", country="SG"),
            _draft(_STRIPE, "2", "Platform Engineer", country="JP"),
            _draft(_ANTHROPIC, "3", "Research Engineer", country="US"),
        ]
    )


def test_app_opens_on_stored_postings_without_fetching(
    opener: Opener, seed: Seeder
) -> None:
    _seed_three(seed)
    requests: list[httpx.Request] = []

    async def run() -> list[str]:
        with opener(_no_requests(requests)) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                return _titles(app)

    assert sorted(asyncio.run(run())) == [
        "Backend Engineer",
        "Platform Engineer",
        "Research Engineer",
    ]
    assert requests == []


def test_app_shows_labels_in_rows(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> dict[str, str]:
        with opener(_no_requests([])) as service:
            for posting in service.open_postings(FilterSpec(), []):
                if posting.title == "Backend Engineer":
                    await service.label(posting.id, Vote.UP, None)
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                return dict(zip(_titles(app), _labels(app), strict=True))

    labels = asyncio.run(run())
    assert labels["Backend Engineer"] == "👍"
    assert labels["Platform Engineer"] == ""


def test_quit_binding(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> bool:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await pilot.press("q")
                await pilot.pause()
                return not app.is_running

    assert asyncio.run(run())


_PAST_DEBOUNCE = 0.35


async def _fill(app: IjobApp, pilot: Pilot[None], field: str, text: str) -> None:
    app.query_one(f"#{field}", Input).value = text
    await pilot.pause(_PAST_DEBOUNCE)
    await _settle(app, pilot)


def _invalid(app: IjobApp, field: str) -> bool:
    return "-invalid" in app.query_one(f"#{field}", Input).classes


def test_inputs_start_from_config(opener: Opener, seed: Seeder, tmp_path: Path) -> None:
    _seed_three(seed)
    config = tmp_path / "config.toml"
    config.write_text('[filters]\ncountries = ["SG"]\n', encoding="utf-8")

    async def run() -> tuple[str, list[str]]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, config)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                return app.query_one("#country", Input).value, _titles(app)

    assert asyncio.run(run()) == ("SG", ["Backend Engineer"])


def test_country_filter_narrows_results(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> tuple[list[str], list[str]]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _fill(app, pilot, "country", "FR")
                narrowed = _titles(app)
                await _fill(app, pilot, "country", "")
                return narrowed, sorted(_titles(app))

    narrowed, restored = asyncio.run(run())
    assert narrowed == []
    assert len(restored) == 3


def test_several_countries_comma_separated(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> list[str]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                app.query_one("#country", Input).focus()
                await pilot.press(*"SG, JP")
                await pilot.pause(_PAST_DEBOUNCE)
                await _settle(app, pilot)
                return sorted(_titles(app))

    assert asyncio.run(run()) == ["Backend Engineer", "Platform Engineer"]


def test_require_salary_checkbox(opener: Opener, seed: Seeder) -> None:
    seed(
        [
            _draft(_STRIPE, "1", "Backend Engineer", salary=True),
            _draft(_STRIPE, "2", "Platform Engineer"),
        ]
    )

    async def run() -> tuple[int, list[str]]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                before = len(_titles(app))
                app.query_one("#require-salary", Checkbox).value = True
                await _settle(app, pilot)
                return before, _titles(app)

    assert asyncio.run(run()) == (2, ["Backend Engineer"])


def test_source_filter_limits_to_named_source(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> list[str]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _fill(app, pilot, "source", "greenhouse:anthropic")
                return _titles(app)

    assert asyncio.run(run()) == ["Research Engineer"]


def test_unknown_source_marks_input_invalid(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> tuple[bool, list[str], list[str]]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                before = sorted(_titles(app))
                await _fill(app, pilot, "source", "greenhouse:nope")
                return _invalid(app, "source"), before, sorted(_titles(app))

    invalid, before, after = asyncio.run(run())
    assert invalid
    assert len(before) == 3
    assert after == before


def test_invalid_since_keeps_last_results(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> tuple[bool, bool, list[str], list[str]]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                before = sorted(_titles(app))
                await _fill(app, pilot, "since", "abc")
                bad = _invalid(app, "since")
                after = sorted(_titles(app))
                await _fill(app, pilot, "since", "7d")
                return bad, _invalid(app, "since"), before, after

    bad, still_bad, before, after = asyncio.run(run())
    assert bad
    assert not still_bad
    assert after == before


def _detail_text(app: IjobApp) -> str:
    return app.query_one("#detail", Markdown).source


async def _move_to(app: IjobApp, pilot: Pilot[None], title: str) -> None:
    app.query_one("#results", DataTable).move_cursor(row=_titles(app).index(title))
    await _settle(app, pilot)


def test_detail_shows_description(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> str:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _move_to(app, pilot, "Platform Engineer")
                return _detail_text(app)

    text = asyncio.run(run())
    assert "# Platform Engineer" in text
    assert "Body text." in text
    assert "country: JP" in text


def _workable_seed(seed: Seeder) -> None:
    workable = _draft(_HUGGINGFACE, _SHORTCODE, "ML Engineer")
    seed(
        [
            replace(
                workable, description=None, raw=json.dumps({"shortcode": _SHORTCODE})
            ),
            _draft(_STRIPE, "2", "Platform Engineer"),
        ]
    )


def _workable_handler(requests: list[httpx.Request], *, status: int = 200) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        requests.append(request)
        if status != 200:
            return httpx.Response(status, text="oops")
        return httpx.Response(200, json=_WORKABLE_JOB)

    return handler


def test_detail_fetches_workable_once(opener: Opener, seed: Seeder) -> None:
    _workable_seed(seed)
    requests: list[httpx.Request] = []

    async def run() -> str:
        with opener(_workable_handler(requests)) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _move_to(app, pilot, "ML Engineer")
                await _move_to(app, pilot, "Platform Engineer")
                await _move_to(app, pilot, "ML Engineer")
                return _detail_text(app)

    text = asyncio.run(run())
    assert [(r.url.host, r.url.path) for r in requests] == [
        ("apply.workable.com", f"/api/v2/accounts/huggingface/jobs/{_SHORTCODE}")
    ]
    assert "At Hugging Face" in text


def test_detail_unavailable_reason(opener: Opener, seed: Seeder) -> None:
    _workable_seed(seed)

    async def run() -> str:
        with opener(_workable_handler([], status=500)) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _move_to(app, pilot, "ML Engineer")
                return _detail_text(app)

    assert "description unavailable:" in asyncio.run(run())


def test_detail_escapes_posting_text(opener: Opener, seed: Seeder) -> None:
    seed([_draft(_STRIPE, "1", "[click](https://evil.test) <b>Engineer</b>")])

    async def run() -> str:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                return _detail_text(app)

    text = asyncio.run(run())
    assert text.splitlines()[0] == (
        r"# \[click\]\(https\:\/\/evil\.test\) \<b\>Engineer\<\/b\>"
    )
    assert "<b>" not in text


def test_link_clicked_opens_only_http(
    opener: Opener, seed: Seeder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three(seed)
    opened: list[str] = []

    async def run() -> None:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            monkeypatch.setattr(app, "open_url", opened.append)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                pane = app.query_one("#detail", Markdown)
                for href in (
                    "https://x.test",
                    "file:///etc/passwd",
                    "mailto:a@b.test",
                    "ftp://x.test/a",
                    "javascript:alert(1)",
                    "https:///nohost",
                    "HTTP://y.test/p",
                ):
                    pane.post_message(Markdown.LinkClicked(pane, href))
                await _settle(app, pilot)

    asyncio.run(run())
    assert opened == ["https://x.test", "HTTP://y.test/p"]


class _Notices:
    """Record what the app notifies, in place of showing it."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.markups: list[bool] = []

    def __call__(
        self,
        message: str,
        *,
        severity: str = "information",
        markup: bool = True,
        **_: object,
    ) -> None:
        self.lines.append((severity, message))
        self.markups.append(markup)


def _boards(failing: frozenset[str] = frozenset()) -> Handler:
    """Serve robots.txt and every Greenhouse board; the `failing` keys answer 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        key = request.url.path.split("/")[3]
        if key in failing:
            return httpx.Response(500, text="oops")
        return httpx.Response(200, content=_BOARD)

    return handler


def test_refresh_fetches_and_reloads(opener: Opener, seed: Seeder) -> None:
    seed([_draft(_STRIPE, "9", "Seeded Engineer")], seen_at=_YESTERDAY)

    async def run() -> tuple[list[str], list[str]]:
        with opener(_boards()) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _fill(app, pilot, "source", "greenhouse:tailscale")
                before = _titles(app)
                await pilot.press("r")
                await _settle(app, pilot)
                return before, _titles(app)

    before, after = asyncio.run(run())
    assert before == []
    assert after == ["Analytics Engineer, Data", "Analytics Engineer, Data"]


def test_refresh_failure_notifies(
    opener: Opener, seed: Seeder, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed([_draft(_STRIPE, "9", "Seeded Engineer")], seen_at=_YESTERDAY)
    notices = _Notices()

    async def run() -> list[str]:
        with opener(_boards(failing=frozenset({"stripe"}))) as service:
            app = IjobApp(service, None)
            monkeypatch.setattr(app, "notify", notices)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _fill(
                    app, pilot, "source", "greenhouse:stripe, greenhouse:tailscale"
                )
                await pilot.press("r")
                await _settle(app, pilot)
                return _titles(app)

    titles = asyncio.run(run())
    assert [t for t in titles if t.startswith("Analytics")] == [
        "Analytics Engineer, Data",
        "Analytics Engineer, Data",
    ]
    assert [
        (severity, "greenhouse:stripe" in text) for severity, text in notices.lines
    ] == [("warning", True)]
    assert notices.markups == [False]


def test_refresh_unknown_source_marks_input_invalid(
    opener: Opener, seed: Seeder
) -> None:
    _seed_three(seed)

    async def run() -> bool:
        with opener(_boards()) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _fill(app, pilot, "source", "greenhouse:stripe")
                service.remove_source(_STRIPE)
                await pilot.press("r")
                await _settle(app, pilot)
                return _invalid(app, "source")

    assert asyncio.run(run())


def test_highlight_during_refresh_does_not_cancel_it(
    db: Path, seed: Seeder, home: IjobHome, clock: Callable[[], datetime]
) -> None:
    seed(
        [
            _draft(_TAILSCALE, "1", "Old Engineer"),
            _draft(_TAILSCALE, "2", "Older Engineer"),
        ],
        seen_at=_YESTERDAY,
    )

    async def run() -> tuple[WorkerState, list[str]]:
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ALLOWING_ROBOTS)
            waiting.set()
            await release.wait()
            return httpx.Response(200, content=_BOARD)

        async def no_sleep(_seconds: float) -> None:
            return None

        def client_factory(cache: ConditionalCache | None) -> PoliteClient:
            return PoliteClient(
                transport=httpx.MockTransport(handler),
                clock=lambda: 0.0,
                sleep=no_sleep,
                cache=cache,
            )

        factory: ClientFactory = client_factory
        with open_service(home, db, factory, now=clock) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _fill(app, pilot, "source", "greenhouse:tailscale")
                await pilot.press("r")
                await asyncio.wait_for(waiting.wait(), timeout=5)
                refresh = next(w for w in app.workers if w.group == "refresh")
                app.query_one("#results", DataTable).move_cursor(row=1)
                await pilot.pause()
                release.set()
                await _settle(app, pilot)
                return refresh.state, _titles(app)

    state, titles = asyncio.run(run())
    assert state is WorkerState.SUCCESS
    assert titles == ["Analytics Engineer, Data", "Analytics Engineer, Data"]


def _stored(service: Service, title: str) -> Label | None:
    [posting] = [p for p in service.open_postings(FilterSpec(), []) if p.title == title]
    return service.labels([posting.id]).get(posting.id)


def test_up_and_down_set_label(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> tuple[str, Label | None, str, Label | None, str]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _move_to(app, pilot, "Backend Engineer")
                await pilot.press("u")
                await _settle(app, pilot)
                up_cell = _label_of(app, "Backend Engineer")
                up = _stored(service, "Backend Engineer")
                await pilot.press("d")
                await _settle(app, pilot)
                return (
                    up_cell,
                    up,
                    _label_of(app, "Backend Engineer"),
                    _stored(service, "Backend Engineer"),
                    _detail_text(app),
                )

    up_cell, up, down_cell, down, detail = asyncio.run(run())
    assert (up_cell, down_cell) == ("👍", "👎")
    assert up is not None
    assert up.vote is Vote.UP
    assert down is not None
    assert down.vote is Vote.DOWN
    assert "- label: 👎 down" in detail


def test_clear_label(opener: Opener, seed: Seeder) -> None:
    _seed_three(seed)

    async def run() -> tuple[str, Label | None, str]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _move_to(app, pilot, "Backend Engineer")
                await pilot.press("u")
                await _settle(app, pilot)
                await pilot.press("x")
                await _settle(app, pilot)
                return (
                    _label_of(app, "Backend Engineer"),
                    _stored(service, "Backend Engineer"),
                    _detail_text(app),
                )

    cell, stored, detail = asyncio.run(run())
    assert cell == ""
    assert stored is None
    assert "label:" not in detail


def test_note_needs_a_vote(
    opener: Opener, seed: Seeder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three(seed)
    notices = _Notices()

    async def run() -> tuple[int, Label | None]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            monkeypatch.setattr(app, "notify", notices)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await _move_to(app, pilot, "Backend Engineer")
                await pilot.press("n")
                await _settle(app, pilot)
                return len(app.screen_stack), _stored(service, "Backend Engineer")

    depth, stored = asyncio.run(run())
    assert depth == 1
    assert stored is None
    assert notices.lines == [("warning", "label it first with u or d")]
    assert notices.markups == [False]


def test_tui_command_opens_the_app_on_a_service(
    db: Path,
    home: IjobHome,
    clock: Callable[[], datetime],
    client_factory_for: Callable[[Handler], ClientFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[Service] = []
    home.root.mkdir()
    env = {"IJOB_HOME": str(home.root)}

    def fake_run(self: IjobApp) -> None:
        ran.append(self._service)

    monkeypatch.setattr(IjobApp, "run", fake_run)
    app = make_app(client_factory_for(_no_requests([])), now=clock)
    broken = db.parent / "broken.toml"
    broken.write_text("filters = 3\n", encoding="utf-8")

    opened = CliRunner().invoke(app, ["tui", "--db", str(db)], env=env)
    refused = CliRunner().invoke(
        app, ["tui", "--db", str(db), "--config", str(broken)], env=env
    )

    assert opened.exit_code == 0
    assert len(ran) == 1
    assert refused.exit_code == 2
    assert "error:" in refused.stderr
    assert len(ran) == 1


def test_tui_command_reads_the_homes_config_file(
    db: Path,
    home: IjobHome,
    clock: Callable[[], datetime],
    client_factory_for: Callable[[Handler], ClientFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[Service] = []
    home.root.mkdir()
    home.config.write_text("filters = 3\n", encoding="utf-8")
    monkeypatch.setattr(IjobApp, "run", lambda self: ran.append(self._service))
    app = make_app(client_factory_for(_no_requests([])), now=clock)

    result = CliRunner().invoke(
        app, ["tui", "--db", str(db)], env={"IJOB_HOME": str(home.root)}
    )

    assert result.exit_code == 2
    assert "invalid filters" in result.stderr
    assert ran == []


_HOSTILE = [
    "Senior Engineer [/remote]",
    "[@click=app.vote_up]Vote[/]",
    "[link=file:///etc/passwd]x[/link]",
    "[bold red]loud[/bold red] [remote]",
]


@pytest.mark.parametrize("field", ["title", "company", "location"])
@pytest.mark.parametrize("text", _HOSTILE)
def test_table_shows_posting_text_literally(
    opener: Opener, seed: Seeder, field: str, text: str
) -> None:
    fields = {"title": "Engineer", "company": "Acme", "location": "Somewhere"}
    fields[field] = text
    seed(
        [
            replace(
                _draft(_STRIPE, "1", fields["title"]),
                company=fields["company"],
                location=fields["location"],
            )
        ]
    )
    column = ("mark", "title", "company", "location").index(field)

    async def run() -> tuple[str, list[object]]:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                cell = app.query_one("#results", DataTable).get_row_at(0)[column]
                return str(cell), list(getattr(cell, "spans", ["not a Text"]))

    shown, spans = asyncio.run(run())
    assert shown == text
    assert spans == []


def test_clicking_a_title_with_a_click_action_votes_nothing(
    opener: Opener, seed: Seeder
) -> None:
    title = "[@click=app.vote_up]Vote[/]"
    seed([_draft(_STRIPE, "1", title)])

    async def run() -> Label | None:
        with opener(_no_requests([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                for x in range(0, 40, 2):
                    await pilot.click("#results", offset=(x, 1))
                    await _settle(app, pilot)
                return _stored(service, title)

    assert asyncio.run(run()) is None


def test_burst_of_filter_changes_reloads_once(
    opener: Opener, seed: Seeder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three(seed)

    async def run() -> tuple[int, int, list[str]]:
        with opener(_no_requests([])) as service:
            reads: list[int] = []
            original = service.open_postings

            def counting(spec: FilterSpec, refs: Sequence[SourceRef]) -> list[Posting]:
                reads.append(1)
                return original(spec, refs)

            monkeypatch.setattr(service, "open_postings", counting)
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await pilot.pause(_PAST_DEBOUNCE)
                await _settle(app, pilot)
                before = len(reads)
                app.query_one("#country", Input).focus()
                await pilot.press(*"SG,JP")
                typing = len(reads) - before
                await pilot.pause(_PAST_DEBOUNCE)
                await _settle(app, pilot)
                return typing, len(reads) - before, sorted(_titles(app))

    typing, total, titles = asyncio.run(run())
    assert typing == 0
    assert total == 1
    assert titles == ["Backend Engineer", "Platform Engineer"]


def test_two_quick_votes_both_mark_their_rows(
    db: Path, seed: Seeder, home: IjobHome, clock: Callable[[], datetime]
) -> None:
    _workable_seed(seed)

    async def run() -> tuple[str, str, Vote | None, Vote | None]:
        release = asyncio.Event()
        waiting = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=_ALLOWING_ROBOTS)
            waiting.set()
            await release.wait()
            return httpx.Response(200, json=_WORKABLE_JOB)

        async def no_sleep(_seconds: float) -> None:
            return None

        def client_factory(cache: ConditionalCache | None) -> PoliteClient:
            return PoliteClient(
                transport=httpx.MockTransport(handler),
                clock=lambda: 0.0,
                sleep=no_sleep,
                cache=cache,
            )

        factory: ClientFactory = client_factory
        with open_service(home, db, factory, now=clock) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.query_one("#results", DataTable)
                table.move_cursor(row=_titles(app).index("ML Engineer"))
                await pilot.pause()
                await pilot.press("u")
                await asyncio.wait_for(waiting.wait(), timeout=5)
                table.move_cursor(row=_titles(app).index("Platform Engineer"))
                await pilot.pause()
                await pilot.press("d")
                await pilot.pause()
                release.set()
                await _settle(app, pilot)
                ml = _stored(service, "ML Engineer")
                platform = _stored(service, "Platform Engineer")
                return (
                    _label_of(app, "ML Engineer"),
                    _label_of(app, "Platform Engineer"),
                    None if ml is None else ml.vote,
                    None if platform is None else platform.vote,
                )

    assert asyncio.run(run()) == ("👍", "👎", Vote.UP, Vote.DOWN)
