import asyncio
import dataclasses
import re
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
import pytest
from ijob_app.recipe_ops import RecipeProposal
from ijob_app.service import Service
from ijob_app.tui import IjobApp
from ijob_app.tui.recipe import RecipeProposalScreen
from ijob_app.tui.sources import SourcesScreen
from ijob_core.filters import FilterSpec
from ijob_core.model import ExternalId, SightingDraft, parse_source_ref
from ijob_core.store import Store
from ijob_sources.errors import FetchedPage
from ijob_sources.manual import manual_key
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, Static

from ..llm_fakes import (
    FakeAsyncEngine,
    FakeEngines,
    json_url_answer,
    recipe_answer,
)
from ..recipe_pages import JS_ONLY_PAGE, LIST_PAGE
from .conftest import RecipeOpener

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


_CAREERS_URL = "https://jobs.example.test/careers"
_RECIPE_REF = f"recipe:{manual_key(_CAREERS_URL)}"
_RECIPE: dict[str, object] = {
    "version": 1,
    "format": "html",
    "url": _CAREERS_URL,
    "items": "//li[@class='job']",
    "fields": {"title": ".//a/text()", "url": ".//a/@href"},
}
_JAZZHR = _FIXTURES / "jsonld" / "jazzhr.html"
_JOB_PAGE_URL = "https://jobs.example.test/jobs/release-engineer"


def _page(body: bytes) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path in {"/careers", "/jobs/release-engineer"}:
            return httpx.Response(
                200, content=body, headers={"Content-Type": "text/html"}
            )
        return httpx.Response(404)

    return handler


def _status(screen: SourcesScreen) -> str:
    return str(screen.query_one("#status", Static).content)


async def _submit_url(
    pilot: Pilot[None], screen: SourcesScreen, url: str, company: str = "Example"
) -> None:
    screen.query_one("#url", Input).value = url
    screen.query_one("#url-company", Input).value = company
    await pilot.click("#add-url")
    await pilot.pause()


async def _wait_for_modal(app: IjobApp, pilot: Pilot[None]) -> RecipeProposalScreen:
    for _ in range(100):
        if isinstance(app.screen, RecipeProposalScreen):
            await pilot.pause()
            return app.screen
        await pilot.pause(0.02)
    msg = "the proposal modal never opened"
    raise AssertionError(msg)


def test_add_url_unrecognized_accept_stores(recipe_opener: RecipeOpener) -> None:
    async def run() -> tuple[list[str], str, str]:
        engines = FakeEngines(FakeAsyncEngine([recipe_answer(_RECIPE)]))
        with recipe_opener(_page(LIST_PAGE.encode()), engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)
                await pilot.click("#accept")
                await _settle(app, pilot)
                return _refs(screen), _status(screen), _error(screen)

    refs, status, error = asyncio.run(run())
    assert _RECIPE_REF in refs
    assert status == f"added {_RECIPE_REF}"
    assert error == ""


def test_add_url_unrecognized_cancel_stores_nothing(
    recipe_opener: RecipeOpener,
) -> None:
    async def run() -> tuple[list[str], str]:
        engines = FakeEngines(FakeAsyncEngine([recipe_answer(_RECIPE)]))
        with recipe_opener(_page(LIST_PAGE.encode()), engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)
                await pilot.click("#cancel")
                await _settle(app, pilot)
                return _refs(screen), _status(screen)

    refs, status = asyncio.run(run())
    assert _RECIPE_REF not in refs
    assert status == "not stored"


def test_add_url_without_model_shows_error(recipe_opener: RecipeOpener) -> None:
    async def run() -> tuple[str, str, bool]:
        engine = FakeAsyncEngine([recipe_answer(_RECIPE)])
        with recipe_opener(
            _page(LIST_PAGE.encode()), FakeEngines(engine), model=False
        ) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _settle(app, pilot)
                return (
                    _error(screen),
                    _status(screen),
                    isinstance(app.screen, SourcesScreen),
                )

    error, status, on_sources = asyncio.run(run())
    assert error.startswith("no model for source_recipe; set [models].source_recipe")
    assert "cost" not in error
    assert status == ""
    assert on_sources


def test_add_url_invalid_recipe_shows_error(recipe_opener: RecipeOpener) -> None:
    async def run() -> tuple[str, list[str]]:
        engines = FakeEngines(
            FakeAsyncEngine([recipe_answer({**_RECIPE, "items": "//li["})])
        )
        with recipe_opener(_page(LIST_PAGE.encode()), engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _settle(app, pilot)
                return _error(screen), _refs(screen)

    error, refs = asyncio.run(run())
    reason, cost = error.split("\n")
    assert reason.startswith("the model's recipe is invalid: ")
    assert cost == "cost $0.0010 of $0.50"
    assert _RECIPE_REF not in refs


def test_add_url_recognized_unchanged(recipe_opener: RecipeOpener) -> None:
    async def run() -> tuple[list[str], str, str, bool]:
        engine = FakeAsyncEngine()
        with recipe_opener(
            _page(_JAZZHR.read_bytes()), FakeEngines(engine), model=False
        ) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _JOB_PAGE_URL, "Eclipse Foundation")
                await _settle(app, pilot)
                return (
                    _refs(screen),
                    _error(screen),
                    _status(screen),
                    isinstance(app.screen, SourcesScreen),
                )

    refs, error, status, on_sources = asyncio.run(run())
    assert error == ""
    assert f"jobposting:{manual_key(_JOB_PAGE_URL)}" in refs
    assert status == ""
    assert on_sources


def test_status_shows_cost(recipe_opener: RecipeOpener) -> None:
    async def run() -> str:
        engines = FakeEngines(FakeAsyncEngine([recipe_answer(_RECIPE)]))
        with recipe_opener(_page(LIST_PAGE.encode()), engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)
                return _status(screen)

    status = asyncio.run(run())
    assert re.fullmatch(r"cost \$\d+\.\d{4} of \$0\.50", status)


def _two_pages() -> Handler:
    """Serve a list page at the careers address and a job posting at its own."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        body = {
            "/careers": LIST_PAGE.encode(),
            "/jobs/release-engineer": _JAZZHR.read_bytes(),
        }.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"Content-Type": "text/html"})

    return handler


def test_submit_clears_the_status_of_the_last_run(
    recipe_opener: RecipeOpener,
) -> None:
    async def run() -> tuple[str, str, str, list[str]]:
        engines = FakeEngines(FakeAsyncEngine([recipe_answer(_RECIPE)]))
        with recipe_opener(_two_pages(), engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)
                await pilot.click("#cancel")
                await _settle(app, pilot)
                after_cancel = _status(screen)
                await _submit_url(pilot, screen, _JOB_PAGE_URL, "Eclipse Foundation")
                await _settle(app, pilot)
                return after_cancel, _status(screen), _error(screen), _refs(screen)

    after_cancel, status, error, refs = asyncio.run(run())
    assert after_cancel == "not stored"
    assert status == ""
    assert error == ""
    assert f"jobposting:{manual_key(_JOB_PAGE_URL)}" in refs


def test_failed_analysis_clears_the_status_and_a_new_run_clears_the_error(
    recipe_opener: RecipeOpener,
) -> None:
    async def run() -> tuple[str, str, str, str]:
        engines = FakeEngines(
            FakeAsyncEngine(
                [recipe_answer({**_RECIPE, "items": "//li["}), recipe_answer(_RECIPE)]
            )
        )
        with recipe_opener(_page(LIST_PAGE.encode()), engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _settle(app, pilot)
                failed = _error(screen), _status(screen)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)
                return *failed, _error(screen), _status(screen)

    failed_error, failed_status, error, status = asyncio.run(run())
    assert failed_error.startswith("the model's recipe is invalid: ")
    assert failed_error.endswith("\ncost $0.0010 of $0.50")
    assert failed_status == ""
    assert error == ""
    assert status.startswith("cost $")


def test_store_failure_at_accept_shows_the_error(
    recipe_opener: RecipeOpener, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(_store: Store, *_args: object, **_kwargs: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(Store, "set_recipe_source", locked)

    async def run() -> tuple[str, str, list[str], bool]:
        engines = FakeEngines(FakeAsyncEngine([recipe_answer(_RECIPE)]))
        with recipe_opener(_page(LIST_PAGE.encode()), engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)
                await pilot.click("#accept")
                await _settle(app, pilot)
                return (
                    _error(screen),
                    _status(screen),
                    _refs(screen),
                    isinstance(app.screen, SourcesScreen),
                )

    error, status, refs, on_sources = asyncio.run(run())
    assert error == "database is locked"
    assert status == ""
    assert _RECIPE_REF not in refs
    assert on_sources


def test_analysis_warnings_become_notifications(
    recipe_opener: RecipeOpener, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyze = Service.analyze_url

    async def noisy(
        self: Service, url: str, company: str, **kwargs: FetchedPage | None
    ) -> RecipeProposal:
        proposal = await analyze(self, url, company, **kwargs)
        report = dataclasses.replace(
            proposal.report, warnings=("warning: tracing is off",), dropped_spans=3
        )
        return dataclasses.replace(proposal, report=report)

    shown: list[tuple[str, dict[str, object]]] = []

    def record(_screen: SourcesScreen, message: str, **kwargs: object) -> None:
        shown.append((message, kwargs))

    monkeypatch.setattr(Service, "analyze_url", noisy)
    monkeypatch.setattr(SourcesScreen, "notify", record)

    async def run() -> None:
        engines = FakeEngines(FakeAsyncEngine([recipe_answer(_RECIPE)]))
        with recipe_opener(_page(LIST_PAGE.encode()), engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)

    asyncio.run(run())
    assert [message for message, _ in shown] == [
        "warning: tracing is off",
        "3 spans not stored",
    ]
    assert all(
        kwargs == {"severity": "warning", "markup": False} for _, kwargs in shown
    )


def test_second_submit_during_analysis_is_refused(
    recipe_opener: RecipeOpener,
) -> None:
    async def run() -> tuple[int, str, str, str, int]:
        engine = FakeAsyncEngine([recipe_answer(_RECIPE)], delay_s=0.3)
        with recipe_opener(_page(LIST_PAGE.encode()), FakeEngines(engine)) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await pilot.pause(0.1)
                analyzing = _status(screen)
                await pilot.click("#add-url")
                await pilot.pause()
                refusal = _status(screen)
                await _wait_for_modal(app, pilot)
                await pilot.click("#cancel")
                await _settle(app, pilot)
                return (
                    len(engine.requests),
                    analyzing,
                    refusal,
                    _status(screen),
                    len(app.screen_stack),
                )

    calls, analyzing, refusal, final, depth = asyncio.run(run())
    assert calls == 1
    assert analyzing.startswith("analyzing ")
    assert refusal.startswith("an analysis is running")
    assert final == "not stored"
    assert depth == 2


def test_add_during_analysis_is_refused_and_leaves_no_stale_status(
    recipe_opener: RecipeOpener,
) -> None:
    async def run() -> tuple[int, str, str, str]:
        engine = FakeAsyncEngine([recipe_answer(_RECIPE)], delay_s=0.3)
        with recipe_opener(_page(LIST_PAGE.encode()), FakeEngines(engine)) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await pilot.pause(0.1)
                screen.query_one("#ref", Input).value = "nonsense"
                screen.query_one("#company", Input).value = "Example"
                await pilot.click("#add")
                await pilot.pause()
                during = _error(screen)
                await _wait_for_modal(app, pilot)
                await pilot.click("#cancel")
                await _settle(app, pilot)
                return len(engine.requests), during, _status(screen), _error(screen)

    calls, during, status, error = asyncio.run(run())
    assert calls == 1
    assert during == ""
    assert status == "not stored"
    assert error == ""


def test_long_error_wraps_within_the_screen(opener: Opener) -> None:
    async def run() -> tuple[int, int, int]:
        with opener(_serving([])) as service:
            app = IjobApp(service, None)
            async with app.run_test(size=(80, 24)) as pilot:
                screen = await _open_sources(app, pilot)
                screen.query_one("#ref", Input).value = "x" * 150
                screen.query_one("#company", Input).value = "Example"
                await pilot.click("#add")
                await _settle(app, pilot)
                region = screen.query_one("#error", Static).region
                return region.height, region.right, len(_error(screen))

    height, right, length = asyncio.run(run())
    assert length > 150
    assert right <= 80
    assert height * 80 >= length


class _CountingSite:
    """Serves one page per path and counts the requests, robots.txt apart."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self._pages = pages
        self.pages_requested: list[str] = []
        self.robots_requested = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            self.robots_requested += 1
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        self.pages_requested.append(request.url.path)
        body = self._pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        kind = "application/json" if body.startswith(b"{") else "text/html"
        return httpx.Response(200, content=body, headers={"Content-Type": kind})


def test_add_url_reads_an_unrecognized_page_and_its_robots_txt_once(
    recipe_opener: RecipeOpener,
) -> None:
    site = _CountingSite({"/careers": LIST_PAGE.encode()})

    async def run() -> str:
        engines = FakeEngines(FakeAsyncEngine([recipe_answer(_RECIPE)]))
        with recipe_opener(site, engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)
                return _error(screen)

    assert asyncio.run(run()) == ""
    assert site.pages_requested == ["/careers"]
    assert site.robots_requested == 1


def test_add_url_json_analysis_reuses_the_page_and_fetches_the_api_once(
    recipe_opener: RecipeOpener,
) -> None:
    api = b'{"jobs": [{"title": "Rust", "link": "/j/1"}]}'
    site = _CountingSite({"/careers": JS_ONLY_PAGE.encode(), "/api/jobs/": api})
    recipe = {
        "version": 1,
        "format": "json",
        "url": "https://jobs.example.test/api/jobs/",
        "items": "jobs",
        "fields": {"title": "title", "url": "link"},
    }

    async def run() -> None:
        engines = FakeEngines(
            FakeAsyncEngine([json_url_answer("/api/jobs/"), recipe_answer(recipe)])
        )
        with recipe_opener(site, engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _wait_for_modal(app, pilot)

    asyncio.run(run())
    assert site.pages_requested == ["/careers", "/api/jobs/"]
    assert site.robots_requested == 2


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (
            "https://www.jobstreet.com/careers",
            "www.jobstreet.com is read only by its own source kind",
        ),
        (
            "https://www.japan-dev.com/jobs",
            "www.japan-dev.com is read only by its own source kind",
        ),
        (
            "http://169.254.169.254/careers",
            "http://169.254.169.254/robots.txt is not a public address",
        ),
        (
            "http://127.0.0.1/careers",
            "http://127.0.0.1/robots.txt is not a public address",
        ),
        ("http://[bad/careers", "cannot use http://[bad/careers: Invalid IPv6 URL"),
    ],
)
def test_add_url_refuses_a_held_back_or_malformed_url_before_any_request(
    recipe_opener: RecipeOpener, url: str, message: str
) -> None:
    site = _CountingSite({"/careers": LIST_PAGE.encode(), "/jobs": LIST_PAGE.encode()})

    async def run() -> tuple[str, str, bool]:
        engine = FakeAsyncEngine([recipe_answer(_RECIPE)])
        with recipe_opener(site, FakeEngines(engine)) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, url)
                await _settle(app, pilot)
                return (
                    _error(screen),
                    _status(screen),
                    isinstance(app.screen, SourcesScreen),
                )

    error, status, on_sources = asyncio.run(run())
    assert error == message
    assert status == ""
    assert on_sources
    assert site.pages_requested == []
    assert site.robots_requested == 0


def test_add_url_cap_reached_shows_the_error_and_the_cost(
    recipe_opener: RecipeOpener,
) -> None:
    api = b'{"jobs": []}'
    site = _CountingSite({"/careers": JS_ONLY_PAGE.encode(), "/api/jobs/": api})

    async def run() -> tuple[str, str]:
        engines = FakeEngines(
            FakeAsyncEngine([json_url_answer("/api/jobs/"), recipe_answer(_RECIPE)])
        )
        with recipe_opener(site, engines, max_cost_usd=0.0005) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_sources(app, pilot)
                await _submit_url(pilot, screen, _CAREERS_URL)
                await _settle(app, pilot)
                return _error(screen), _status(screen)

    error, status = asyncio.run(run())
    assert error == (
        "cost cap reached before the JSON recipe; raise [llm].max_cost_usd\n"
        "cost $0.0010 of $0.00"
    )
    assert status == ""
