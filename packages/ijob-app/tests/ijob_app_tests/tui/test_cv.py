import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import httpx
from ijob_app.service import Service
from ijob_app.tui import IjobApp
from ijob_app.tui.cv import CvScreen
from ijob_core.model import StoredCv
from textual.pilot import Pilot
from textual.widgets import Input, Static

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
