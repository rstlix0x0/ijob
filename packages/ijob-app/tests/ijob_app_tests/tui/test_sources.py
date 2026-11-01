import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
from ijob_app.service import Service
from ijob_app.tui import IjobApp
from ijob_app.tui.sources import SourcesScreen
from ijob_core.filters import FilterSpec
from ijob_core.model import ExternalId, SightingDraft, parse_source_ref
from ijob_core.store import Store
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, Static

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
_BOARD = (_FIXTURES / "greenhouse" / "tailscale_jobs.json").read_bytes()
_ALLOWING_ROBOTS = "User-agent: *\nDisallow: /embed/\n"
_TAILSCALE = parse_source_ref("greenhouse:tailscale")


def _serving(requests: list[httpx.Request], board: bytes = _BOARD) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        requests.append(request)
        return httpx.Response(200, content=board)

    return handler


async def _settle(app: IjobApp, pilot: Pilot[None]) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _open_sources(app: IjobApp, pilot: Pilot[None]) -> SourcesScreen:
    await _settle(app, pilot)
    await pilot.press("s")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, SourcesScreen)
    return screen


def _refs(screen: SourcesScreen) -> list[str]:
    table = screen.query_one("#sources", DataTable)
    return [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


def _error(screen: SourcesScreen) -> str:
    return str(screen.query_one("#error", Static).content)


def _posting(seed: Seeder) -> None:
    seed(
        [
            SightingDraft(
                source=_TAILSCALE,
                external_id=ExternalId("1"),
                title="Backend Engineer",
                company="Tailscale",
                location=None,
                url="https://jobs.example.test/1",
                posted_at=None,
                raw="{}",
                description="Body text.",
            )
        ]
    )


def test_sources_screen_add_verifies_then_lists(opener: Opener) -> None:
    requests: list[httpx.Request] = []

    async def run() -> tuple[list[str], list[str], str]:
        with opener(_serving(requests)) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                before = _refs(screen)
                screen.query_one("#ref", Input).value = "greenhouse:acme"
                screen.query_one("#company", Input).value = "Tailscale"
                await pilot.click("#add")
                await _settle(app, pilot)
                return before, _refs(screen), _error(screen)

    before, after, error = asyncio.run(run())
    assert len(requests) == 1
    assert "greenhouse:acme" not in before
    assert "greenhouse:acme" in after
    assert error == ""


def test_sources_table_shows_company_text_literally(opener: Opener, db: Path) -> None:
    company = "[/x] [@click=app.quit]Acme[/] [link=file:///etc/passwd]y[/link]"
    with Store.open(db) as store:
        store.add_source(parse_source_ref("greenhouse:acme"), company)

    async def run() -> tuple[str, list[object]]:
        with opener(_serving([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                table = screen.query_one("#sources", DataTable)
                cell = table.get_row_at(_refs(screen).index("greenhouse:acme"))[1]
                return str(cell), list(getattr(cell, "spans", ["not a Text"]))

    shown, spans = asyncio.run(run())
    assert shown == company
    assert spans == []


def test_sources_screen_remove(opener: Opener, seed: Seeder) -> None:
    _posting(seed)

    async def run() -> tuple[list[str], list[str], list[str]]:
        with opener(_serving([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                before = _refs(screen)
                table = screen.query_one("#sources", DataTable)
                table.move_cursor(row=before.index("greenhouse:tailscale"))
                await pilot.pause()
                await pilot.click("#remove")
                await _settle(app, pilot)
                after = _refs(screen)
                await pilot.press("escape")
                await _settle(app, pilot)
                main = app.query_one("#results", DataTable)
                return (
                    after,
                    before,
                    [str(main.get_row_at(i)[1]) for i in range(main.row_count)],
                )

    after, before, main_titles = asyncio.run(run())
    assert "greenhouse:tailscale" in before
    assert "greenhouse:tailscale" not in after
    assert main_titles == []


def test_sources_screen_add_url(opener: Opener) -> None:
    requests: list[httpx.Request] = []

    async def run() -> list[str]:
        with opener(_serving(requests)) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                screen.query_one(
                    "#url", Input
                ).value = "https://job-boards.greenhouse.io/acme?gh_src=x"
                screen.query_one("#url-company", Input).value = "Tailscale"
                await pilot.click("#add-url")
                await _settle(app, pilot)
                return _refs(screen)

    assert "greenhouse:acme" in asyncio.run(run())
    assert len(requests) == 1


def test_sources_screen_shows_error(opener: Opener) -> None:
    async def run() -> tuple[str, list[str], str, list[str]]:
        with opener(_serving([], board=b'{"jobs": []}')) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                before = _refs(screen)
                screen.query_one("#ref", Input).value = "bogus:acme"
                screen.query_one("#company", Input).value = "Tailscale"
                await pilot.click("#add")
                await _settle(app, pilot)
                unknown_kind = _error(screen)
                screen.query_one("#ref", Input).value = "greenhouse:acme"
                screen.query_one("#company", Input).focus()
                await pilot.press("enter")
                await _settle(app, pilot)
                return unknown_kind, before, _error(screen), _refs(screen)

    unknown_kind, before, empty_board, after = asyncio.run(run())
    assert unknown_kind.startswith("invalid source_ref:")
    assert "bogus" in unknown_kind
    assert empty_board.startswith("cannot use greenhouse:acme")
    assert after == before


def test_main_actions_are_off_on_the_sources_screen(
    opener: Opener, seed: Seeder
) -> None:
    _posting(seed)

    async def run() -> int:
        with opener(_serving([])) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                screen.query_one("#sources", DataTable).focus()
                await pilot.press("u")
                await _settle(app, pilot)
                return len(
                    service.labels(
                        [p.id for p in service.open_postings(FilterSpec(), [])]
                    )
                )

    assert asyncio.run(run()) == 0
