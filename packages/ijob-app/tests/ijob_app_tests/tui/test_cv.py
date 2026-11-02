import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import httpx
import pytest
from ijob_app.home import IjobHome
from ijob_app.service import Service
from ijob_app.tui import IjobApp
from ijob_app.tui.cv import CvScreen
from ijob_core.model import StoredCv
from textual.app import App
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import Input, Static

from ..llm_fakes import (
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    profile_answer,
    write_cv_pdf,
    write_fake_cli,
    write_llm_config,
)
from .conftest import LlmOpener

type Handler = Callable[[httpx.Request], httpx.Response]
type Opener = Callable[[Handler], AbstractContextManager[Service]]

_PDF = b"%PDF-1.4\n% a made-up CV, not a real document\n"


def _refuse(request: httpx.Request) -> httpx.Response:
    message = f"unexpected request {request.url}"
    raise AssertionError(message)


async def _open_cv(app: IjobApp, pilot: Pilot[None]) -> CvScreen:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.press("c")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, CvScreen)
    return screen


def _text(screen: CvScreen, selector: str) -> str:
    return str(screen.query_one(selector, Static).content)


def test_cv_screen_stores_pdf(opener: Opener, tmp_path: Path) -> None:
    pdf = tmp_path / "mine.pdf"
    pdf.write_bytes(_PDF)

    async def run() -> tuple[str, str, str, StoredCv | None]:
        with opener(_refuse) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_cv(app, pilot)
                before = _text(screen, "#current")
                screen.query_one("#path", Input).value = str(pdf)
                await pilot.click("#store")
                await pilot.pause()
                return (
                    before,
                    _text(screen, "#current"),
                    _text(screen, "#error"),
                    service.cv(),
                )

    before, current, error, stored = asyncio.run(run())
    assert before == "no CV stored"
    assert "mine.pdf" in current
    assert f"{len(_PDF)} bytes" in current
    assert error == ""
    assert stored is not None
    assert (stored.name, stored.size) == ("mine.pdf", len(_PDF))


def test_cv_screen_rejects_non_pdf(opener: Opener, tmp_path: Path) -> None:
    text = tmp_path / "notes.txt"
    text.write_text("not a pdf", encoding="utf-8")

    async def run() -> tuple[str, str, StoredCv | None]:
        with opener(_refuse) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_cv(app, pilot)
                screen.query_one("#path", Input).value = str(text)
                await pilot.click("#store")
                await pilot.pause()
                return _text(screen, "#current"), _text(screen, "#error"), service.cv()

    current, error, stored = asyncio.run(run())
    assert "is not a PDF" in error
    assert current == "no CV stored"
    assert stored is None


def test_cv_key_gives_the_screen_the_config_in_force(
    llm_opener: LlmOpener, home: IjobHome, tmp_path: Path
) -> None:
    other = tmp_path / "other.toml"

    async def run(config: Path | None) -> Path | None:
        with llm_opener(FakeEngines()) as service:
            app = IjobApp(service, config, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _open_cv(app, pilot)
                return screen._config_path

    other.write_text("", encoding="utf-8")
    assert asyncio.run(run(other)) == other
    assert asyncio.run(run(None)) == home.config


def test_cv_screen_shows_the_stored_cv_and_escape_returns(
    opener: Opener, tmp_path: Path
) -> None:
    pdf = tmp_path / "mine.pdf"
    pdf.write_bytes(_PDF)

    async def run() -> tuple[str, bool]:
        with opener(_refuse) as service:
            service.store_cv(pdf)
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_cv(app, pilot)
                current = _text(screen, "#current")
                await pilot.press("escape")
                await pilot.pause()
                return current, isinstance(app.screen, CvScreen)

    current, still_open = asyncio.run(run())
    assert "mine.pdf" in current
    assert not still_open


def test_cv_screen_shows_a_failed_copy(opener: Opener, tmp_path: Path) -> None:
    pdf = tmp_path / "mine.pdf"
    pdf.write_bytes(_PDF)
    (tmp_path / "cv.pdf").mkdir()

    async def run() -> tuple[str, StoredCv | None]:
        with opener(_refuse) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_cv(app, pilot)
                screen.query_one("#path", Input).value = str(pdf)
                await pilot.click("#store")
                await pilot.pause()
                return _text(screen, "#error"), service.cv()

    error, stored = asyncio.run(run())
    assert "cv.pdf" in error
    assert stored is None


@pytest.fixture
def profile_opener(home: IjobHome, llm_opener: LlmOpener) -> LlmOpener:
    """Return the LLM opener with a `cv_profile` model named in its config."""
    cli = write_fake_cli(home.config.parent.parent / "bin")
    write_llm_config(
        home.config, judge=None, cv_profile=MADE_UP_CLAUDE, cli_path=cli.path
    )
    return llm_opener


async def _store_through_screen(
    app: IjobApp, pilot: Pilot[None], pdf: Path
) -> CvScreen:
    screen = await _open_cv(app, pilot)
    screen.query_one("#path", Input).value = str(pdf)
    await pilot.click("#store")
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
    return screen


def test_profile_shown_after_store(profile_opener: LlmOpener, tmp_path: Path) -> None:
    pdf = write_cv_pdf(tmp_path / "mine.pdf")
    engines = FakeEngines(FakeAsyncEngine([profile_answer()]))

    async def run() -> tuple[str, str, str]:
        with profile_opener(engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_cv(app, pilot)
                before = _text(screen, "#profile")
                await _store_through_screen(app, pilot, pdf)
                return before, _text(screen, "#profile"), _text(screen, "#note")

    before, profile, note = asyncio.run(run())
    assert before == "no profile yet"
    assert "seniority: senior" in profile
    assert "summary: Backend engineer" in profile
    assert "years_experience: 7" in profile
    assert note == ""
    assert len(engines.engine.requests) == 1


def test_profile_shown_when_one_is_stored(
    profile_opener: LlmOpener, tmp_path: Path
) -> None:
    pdf = write_cv_pdf(tmp_path / "mine.pdf")
    engines = FakeEngines(FakeAsyncEngine([profile_answer()]))

    async def run() -> str:
        with profile_opener(engines) as service:
            service.store_cv(pdf)
            await service.build_profile(refresh=False)
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                return _text(await _open_cv(app, pilot), "#profile")

    assert "seniority: senior" in asyncio.run(run())


def test_refresh_rebuilds(profile_opener: LlmOpener, tmp_path: Path) -> None:
    pdf = write_cv_pdf(tmp_path / "mine.pdf")
    engines = FakeEngines(
        FakeAsyncEngine([profile_answer("first"), profile_answer("second")])
    )

    async def run() -> tuple[int, int, str]:
        with profile_opener(engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _store_through_screen(app, pilot, pdf)
                before = len(engines.engine.requests)
                await pilot.click("#refresh")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                return before, len(engines.engine.requests), _text(screen, "#profile")

    before, after, profile = asyncio.run(run())
    assert after == before + 1
    assert "summary: second" in profile


def test_refresh_without_a_cv_says_so(profile_opener: LlmOpener) -> None:
    engines = FakeEngines()

    async def run() -> tuple[str, str]:
        with profile_opener(engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _open_cv(app, pilot)
                await pilot.click("#refresh")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                return _text(screen, "#profile"), _text(screen, "#note")

    profile, note = asyncio.run(run())
    assert profile == "no profile yet"
    assert note != ""
    assert engines.engine.requests == []


def test_no_model_shows_note(llm_opener: LlmOpener, tmp_path: Path) -> None:
    pdf = write_cv_pdf(tmp_path / "mine.pdf")
    engines = FakeEngines()

    async def run() -> tuple[str, str]:
        with llm_opener(engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _store_through_screen(app, pilot, pdf)
                return _text(screen, "#profile"), _text(screen, "#note")

    profile, note = asyncio.run(run())
    assert note == "profile not built; set [models].cv_profile"
    assert profile == "no profile yet"
    assert engines.engine.requests == []


def test_configured_check_does_not_start_the_runtime(
    llm_opener: LlmOpener,
    home: IjobHome,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = write_cv_pdf(tmp_path / "mine.pdf")

    async def refuse(*_args: object, **_kwargs: object) -> None:
        message = "the profile was built without a model"
        raise AssertionError(message)

    monkeypatch.setattr(Service, "build_profile", refuse)

    class Host(App[None]):
        def __init__(self, service: Service) -> None:
            super().__init__()
            self._service = service

        def on_mount(self) -> None:
            self.push_screen(CvScreen(self._service, config_path=home.config))

    async def run() -> str:
        with llm_opener(FakeEngines()) as service:
            host = Host(service)
            async with host.run_test() as pilot:
                await pilot.pause()
                screen = host.screen
                assert isinstance(screen, CvScreen)
                screen.query_one("#path", Input).value = str(pdf)
                await pilot.click("#store")
                await pilot.pause()
                await host.workers.wait_for_complete()
                await pilot.pause()
                return _text(screen, "#note")

    assert asyncio.run(run()) == "profile not built; set [models].cv_profile"


def test_profile_text_literal(profile_opener: LlmOpener, tmp_path: Path) -> None:
    pdf = write_cv_pdf(tmp_path / "mine.pdf")
    profile = json.loads(profile_answer())
    profile["profile"]["skills"] = ["[red]x[/red]"]
    engines = FakeEngines(FakeAsyncEngine([json.dumps(profile)]))

    async def run() -> tuple[str, str]:
        with profile_opener(engines) as service:
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                screen = await _store_through_screen(app, pilot, pdf)
                shown = screen.query_one("#profile", Static)
                visual = shown.visual
                assert isinstance(visual, Content)
                return str(shown.content), visual.plain

    content, plain = asyncio.run(run())
    assert "skills: [red]x[/red]" in content
    assert "[red]x[/red]" in plain
