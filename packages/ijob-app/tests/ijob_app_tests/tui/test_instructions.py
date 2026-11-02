import asyncio
from collections.abc import Sequence

from ijob_app.home import IjobHome
from ijob_app.tui import IjobApp
from ijob_app.tui.instructions import InstructionsScreen
from ijob_core.model import Instructions
from textual.pilot import Pilot
from textual.widgets import Button, DataTable, Static, TextArea

from ..llm_fakes import (
    FakeAsyncEngine,
    FakeEngines,
    instructions_answer,
    verdict_answer,
)
from .conftest import InstructionStorer, LlmOpener, Seeder, seed_three, titles

_TEXT = "Rust roles, remote from Indonesia, no crypto"
_RULE = "no crypto, blockchain or Web3 companies"


def _engines(
    filters: dict[str, object] | None = None, rules: Sequence[str] = (_RULE,)
) -> FakeEngines:
    return FakeEngines(
        FakeAsyncEngine([instructions_answer(filters, rules), verdict_answer()])
    )


async def _open(app: IjobApp, pilot: Pilot[None]) -> InstructionsScreen:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.press("i")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, InstructionsScreen)
    return screen


async def _submit(app: IjobApp, pilot: Pilot[None], text: str) -> InstructionsScreen:
    screen = await _open(app, pilot)
    screen.query_one("#text", TextArea).text = text
    await pilot.click("#submit")
    await app.workers.wait_for_complete()
    await pilot.pause()
    return screen


def _text(screen: InstructionsScreen, selector: str) -> str:
    return str(screen.query_one(selector, Static).content)


def _confirm_shown(screen: InstructionsScreen) -> bool:
    return screen.query_one("#confirm").display


def test_i_opens_with_stored_text(
    instructions_opener: LlmOpener,
    store_instructions: InstructionStorer,
    home: IjobHome,
) -> None:
    store_instructions()

    async def run() -> tuple[str, bool]:
        with instructions_opener(FakeEngines()) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _open(app, pilot)
                return screen.query_one("#text", TextArea).text, _confirm_shown(screen)

    text, confirm = asyncio.run(run())
    assert text == "no crypto"
    assert not confirm


def test_i_opens_empty_without_stored_text(
    instructions_opener: LlmOpener, home: IjobHome
) -> None:
    async def run() -> str:
        with instructions_opener(FakeEngines()) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _open(app, pilot)
                return screen.query_one("#text", TextArea).text

    assert asyncio.run(run()) == ""


def test_submit_shows_proposal_and_accept_stores(
    instructions_opener: LlmOpener, home: IjobHome
) -> None:
    engines = _engines({"countries": ["ID"], "role": {"include": ["Rust"]}})

    async def run() -> tuple[str, bool, bool, Instructions | None, bool]:
        with instructions_opener(engines) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _submit(app, pilot, _TEXT)
                proposal = _text(screen, "#proposal")
                shown = _confirm_shown(screen)
                before = service.instructions() is None
                await pilot.click("#accept")
                await pilot.pause()
                return (
                    proposal,
                    shown,
                    before,
                    service.instructions(),
                    isinstance(app.screen, InstructionsScreen),
                )

    proposal, shown, before, stored, still_open = asyncio.run(run())
    lines = proposal.splitlines()
    assert "filters:" in lines
    assert "  countries: ID" in lines
    assert "  role.include: Rust" in lines
    assert "rules:" in lines
    assert f"  1. {_RULE}" in lines
    assert shown
    assert before
    assert stored is not None
    assert stored.rules == (_RULE,)
    assert stored.text == _TEXT
    assert not still_open


def test_cancel_stores_nothing(instructions_opener: LlmOpener, home: IjobHome) -> None:
    async def run() -> tuple[bool, bool]:
        with instructions_opener(_engines({"countries": ["ID"]})) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                await _submit(app, pilot, _TEXT)
                await pilot.click("#cancel")
                await pilot.pause()
                return (
                    service.instructions() is None,
                    isinstance(app.screen, InstructionsScreen),
                )

    nothing_stored, still_open = asyncio.run(run())
    assert nothing_stored
    assert not still_open


def test_escape_goes_back_without_storing(
    instructions_opener: LlmOpener, home: IjobHome
) -> None:
    async def run() -> tuple[bool, bool]:
        with instructions_opener(_engines({"countries": ["ID"]})) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                await _submit(app, pilot, _TEXT)
                await pilot.press("escape")
                await pilot.pause()
                return (
                    service.instructions() is None,
                    isinstance(app.screen, InstructionsScreen),
                )

    nothing_stored, still_open = asyncio.run(run())
    assert nothing_stored
    assert not still_open


def test_blank_text_shows_error(instructions_opener: LlmOpener, home: IjobHome) -> None:
    engines = _engines()

    async def run() -> tuple[str, bool]:
        with instructions_opener(engines) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _submit(app, pilot, "   ")
                return _text(screen, "#error"), _confirm_shown(screen)

    error, confirm = asyncio.run(run())
    assert "instructions" in error
    assert not confirm
    assert engines.engine.requests == []


def test_nothing_actionable_shows_error(
    instructions_opener: LlmOpener, home: IjobHome
) -> None:
    async def run() -> tuple[str, bool, bool]:
        with instructions_opener(_engines({}, ())) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _submit(app, pilot, "hello there")
                return (
                    _text(screen, "#error"),
                    _confirm_shown(screen),
                    service.instructions() is None,
                )

    error, confirm, nothing_stored = asyncio.run(run())
    assert error.startswith("error: ")
    assert not confirm
    assert nothing_stored


def test_missing_model_shows_error(
    instructions_opener: LlmOpener, home: IjobHome
) -> None:
    home.config.write_text("", encoding="utf-8")

    async def run() -> str:
        with instructions_opener(_engines()) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _submit(app, pilot, _TEXT)
                return _text(screen, "#error")

    assert "instructions" in asyncio.run(run())


def test_shadow_warning_shown(instructions_opener: LlmOpener, home: IjobHome) -> None:
    with home.config.open("a", encoding="utf-8") as handle:
        handle.write('[filters]\ncountries = ["JP"]\n')
    engines = _engines({"countries": ["ID"], "role": {"include": ["Rust"]}})

    async def run() -> str:
        with instructions_opener(engines) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _submit(app, pilot, _TEXT)
                return _text(screen, "#proposal")

    lines = asyncio.run(run()).splitlines()
    assert (
        "warning: countries: your config's JP is used; the instruction's ID is ignored"
        in lines
    )


def test_proposal_text_is_literal(
    instructions_opener: LlmOpener, home: IjobHome
) -> None:
    rule = "avoid [bold]crypto[/bold] firms"
    engines = _engines({}, (rule,))

    async def run() -> str:
        with instructions_opener(engines) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _submit(app, pilot, _TEXT)
                return _text(screen, "#proposal")

    assert f"  1. {rule}" in asyncio.run(run()).splitlines()


def test_accept_reruns_the_search(
    instructions_opener: LlmOpener, seed: Seeder, home: IjobHome
) -> None:
    seed_three(seed)
    engines = _engines({"countries": ["SG"]}, ())

    async def run() -> tuple[list[str], list[str]]:
        with instructions_opener(engines) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                before = titles(app)
                await _submit(app, pilot, _TEXT)
                await pilot.click("#accept")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.query_one("#results", DataTable).row_count == 1
                return before, titles(app)

    before, after = asyncio.run(run())
    assert sorted(before) == [
        "Backend Engineer",
        "Platform Engineer",
        "Research Engineer",
    ]
    assert after == ["Backend Engineer"]


def test_buttons(instructions_opener: LlmOpener, home: IjobHome) -> None:
    async def run() -> list[str]:
        with instructions_opener(_engines()) as service:
            app = IjobApp(service, None, default_config=home.config)
            async with app.run_test() as pilot:
                screen = await _open(app, pilot)
                return [str(button.label) for button in screen.query(Button)]

    assert asyncio.run(run()) == ["Submit", "Accept", "Cancel"]
