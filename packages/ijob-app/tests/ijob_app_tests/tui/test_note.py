import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

import httpx
from ijob_app.service import Service
from ijob_app.tui import IjobApp
from ijob_app.tui.note import NoteScreen
from ijob_core.filters import FilterSpec
from ijob_core.model import (
    ExternalId,
    Label,
    SightingDraft,
    Vote,
    parse_source_ref,
)
from textual.pilot import Pilot
from textual.widgets import Input

type Handler = Callable[[httpx.Request], httpx.Response]
type Opener = Callable[[Handler], AbstractContextManager[Service]]


class Seeder(Protocol):
    def __call__(self, drafts: list[SightingDraft]) -> None: ...


_SOURCE = parse_source_ref("greenhouse:stripe")


def _refuse(request: httpx.Request) -> httpx.Response:
    message = f"unexpected request {request.url}"
    raise AssertionError(message)


def _seed_one(seed: Seeder) -> None:
    seed(
        [
            SightingDraft(
                source=_SOURCE,
                external_id=ExternalId("1"),
                title="Backend Engineer",
                company="Acme",
                location=None,
                url="https://jobs.example.test/1",
                posted_at=None,
                raw="{}",
                description="Body text.",
            )
        ]
    )


def _in_modal(app: IjobApp) -> bool:
    return isinstance(app.screen, NoteScreen)


async def _settle(app: IjobApp, pilot: Pilot[None]) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _label_down(service: Service) -> None:
    [posting] = service.open_postings(_no_filters(), [])
    await service.label(posting.id, Vote.DOWN, "too far")


def _no_filters() -> FilterSpec:
    return FilterSpec()


def _stored(service: Service) -> Label:
    [posting] = service.open_postings(_no_filters(), [])
    return service.labels([posting.id])[posting.id]


def test_note_modal_saves_note(opener: Opener, seed: Seeder) -> None:
    _seed_one(seed)

    async def run() -> Label:
        with opener(_refuse) as service:
            await _label_down(service)
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await pilot.press("n")
                await pilot.pause()
                assert _in_modal(app)
                assert app.screen.query_one(Input).value == "too far"
                app.screen.query_one(Input).value = ""
                await pilot.press(*"Go + payments")
                await pilot.press("enter")
                await _settle(app, pilot)
                assert not _in_modal(app)
            return _stored(service)

    label = asyncio.run(run())
    assert label.vote is Vote.DOWN
    assert label.note == "Go + payments"


def test_note_modal_escape_keeps_label(opener: Opener, seed: Seeder) -> None:
    _seed_one(seed)

    async def run() -> tuple[Label, Label]:
        with opener(_refuse) as service:
            await _label_down(service)
            before = _stored(service)
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await pilot.press("n")
                await pilot.pause()
                await pilot.press(*"zzz")
                await pilot.press("escape")
                await _settle(app, pilot)
                assert not _in_modal(app)
            return before, _stored(service)

    before, after = asyncio.run(run())
    assert after == before


def test_note_modal_blank_removes_note(opener: Opener, seed: Seeder) -> None:
    _seed_one(seed)

    async def run() -> Label:
        with opener(_refuse) as service:
            await _label_down(service)
            app = IjobApp(service, None)
            async with app.run_test() as pilot:
                await _settle(app, pilot)
                await pilot.press("n")
                await pilot.pause()
                app.screen.query_one(Input).value = "   "
                await pilot.press("enter")
                await _settle(app, pilot)
            return _stored(service)

    label = asyncio.run(run())
    assert label.vote is Vote.DOWN
    assert label.note is None
