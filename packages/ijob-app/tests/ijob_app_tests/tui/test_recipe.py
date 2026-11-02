import asyncio

from ijob_app.llm_ops import OperationReport
from ijob_app.llm_text import recipe_proposal_lines
from ijob_app.recipe_ops import RecipeProposal
from ijob_app.tui.recipe import RecipeProposalScreen
from ijob_core.model import SightingDraft, parse_source_ref
from ijob_sources.recipe_document import RecipeFormat
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, Static

from .conftest import ANTHROPIC, draft

_REF = parse_source_ref("recipe:example-careers")


def _proposal(jobs: tuple[SightingDraft, ...]) -> RecipeProposal:
    return RecipeProposal(
        ref=_REF,
        target_url="https://jobs.example.test/careers",
        company="Example",
        recipe="{}",
        recipe_format=RecipeFormat.HTML,
        recipe_url="https://jobs.example.test/careers",
        jobs=jobs,
        invalid=1,
        model="claude-sdk/made-up-model",
        report=OperationReport(
            done=1, not_done=0, spent=0.001, cap=0.5, stopped_by=None
        ),
    )


class _Host(App[None]):
    """An app that opens the modal and keeps what it dismissed with."""

    def __init__(self, proposal: RecipeProposal) -> None:
        super().__init__()
        self._proposal = proposal
        self.result: list[bool | None] = []

    def on_mount(self) -> None:
        self.push_screen(RecipeProposalScreen(self._proposal), self.result.append)


def test_modal_shows_lines_and_buttons() -> None:
    proposal = _proposal(
        (draft(ANTHROPIC, "1", "[b]x[/b] [link=https://evil.test]Engineer[/link]"),)
    )

    async def run() -> tuple[list[str], list[str], list[str]]:
        app = _Host(proposal)
        async with app.run_test() as pilot:
            await pilot.pause()
            widgets = list(app.screen.query(".line").results(Static))
            contents = [str(widget.content) for widget in widgets]
            rendered = [str(widget.render()) for widget in widgets]
            labels = [str(button.label) for button in app.screen.query(Button)]
            return contents, rendered, labels

    contents, rendered, labels = asyncio.run(run())
    assert contents == recipe_proposal_lines(proposal)
    assert any(
        line.startswith("[b]x[/b] [link=https://evil.test]Engineer[/link]")
        for line in rendered
    )
    assert labels == ["Accept", "Cancel"]


async def _press(pilot: Pilot[None], target: str) -> None:
    if target == "escape":
        await pilot.press("escape")
    else:
        await pilot.click(target)
    await pilot.pause()


def test_modal_accept_and_cancel() -> None:
    proposal = _proposal(())

    async def run(target: str) -> list[bool | None]:
        app = _Host(proposal)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _press(pilot, target)
            return app.result

    assert asyncio.run(run("#accept")) == [True]
    assert asyncio.run(run("#cancel")) == [False]
    assert asyncio.run(run("escape")) == [False]


def test_enter_right_after_opening_stores_nothing() -> None:
    proposal = _proposal((draft(ANTHROPIC, "1", "Engineer"),))

    async def run() -> tuple[str | None, list[bool | None]]:
        app = _Host(proposal)
        async with app.run_test() as pilot:
            await pilot.pause()
            focused = app.screen.focused
            await pilot.press("enter")
            await pilot.pause()
            return (None if focused is None else focused.id), app.result

    focused, result = asyncio.run(run())
    assert focused == "cancel"
    assert result == [False]


def test_buttons_stay_on_a_small_screen_with_long_job_lines() -> None:
    jobs = tuple(
        draft(ANTHROPIC, str(number), "Senior Staff Platform Engineer " * 8)
        for number in range(6)
    )
    proposal = _proposal(jobs)

    async def run() -> tuple[int, int, float]:
        app = _Host(proposal)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            accept = app.screen.query_one("#accept", Button)
            cancel = app.screen.query_one("#cancel", Button)
            lines = app.screen.query_one("#lines")
            bottom = max(accept.region.bottom, cancel.region.bottom)
            return accept.region.y, bottom, lines.max_scroll_y

    top, bottom, scrollable = asyncio.run(run())
    assert top >= 0
    assert bottom <= 24
    assert scrollable > 0
