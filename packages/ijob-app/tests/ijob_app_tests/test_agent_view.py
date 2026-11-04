import json
from io import StringIO

import pytest
from ijob_ai.agents.react import AgentStep, ToolOutcome
from ijob_app.agent_ops import ExploreResult, FoundSource
from ijob_app.agent_tools import Proposal, VerifiedSource
from ijob_app.agent_view import (
    explore_line,
    found_lines,
    found_panel,
    not_found_view,
    proposals_lines,
    proposals_panel,
    sample_titles,
    step_line,
    step_what,
)
from ijob_app.llm_ops import OperationReport
from ijob_app.recipe_ops import RecipeProposal
from ijob_app.view import PROMPT_MARK
from ijob_core.model import ExternalId, SightingDraft, SourceRef, parse_source_ref
from ijob_sources.recipe_document import RecipeFormat
from rich.console import Console, RenderableType

_LEVER = parse_source_ref("lever:example-co")
_OK = ToolOutcome.OK
_FAILED = ToolOutcome.FAILED
_NOT_OPENED = (
    "learn_page works only on a page that open_page opened and found no source on; "
    "open it first"
)
_HOSTILE = "[bold red]gone[/] [link=https://evil.example]x[/link]\x1b[2J\x07"


def _words(line: str) -> str:
    return " ".join(line.split())


def _draw(item: RenderableType, width: int = 100) -> str:
    out = StringIO()
    Console(file=out, width=width, height=40, highlight=False).print(item)
    return out.getvalue()


def _found(
    ref: SourceRef = _LEVER,
    *,
    jobs: int = 41,
    sample: tuple[str, ...] = ("Staff Engineer", "Sales Lead", "Data Analyst"),
    target_url: str | None = None,
    proposal: RecipeProposal | None = None,
) -> FoundSource:
    return FoundSource(
        ref=ref,
        company="Example Co",
        target_url=target_url,
        jobs=jobs,
        proposal=proposal,
        sample=sample,
    )


def _step(
    tool: str, result: str = "", outcome: ToolOutcome = _OK, /, **arguments: str
) -> AgentStep:
    return AgentStep(tool, outcome, arguments, result)


def _steps(*pairs: tuple[str, ToolOutcome]) -> list[AgentStep]:
    return [AgentStep(tool, outcome) for tool, outcome in pairs]


_RUN = [
    _step("try_board", "no board", kind="greenhouse", key="example-co"),
    _step(
        "open_page",
        "no source found on this page; 2 links on the site:\n- x",
        url="https://example.com/careers",
    ),
    _step(
        "try_board", "found 41 jobs, company matches", kind="lever", key="example-co"
    ),
    _step("finish", "Completed."),
]


def _draft(title: str) -> SightingDraft:
    return SightingDraft(
        source=parse_source_ref("recipe:careers"),
        external_id=ExternalId("1"),
        title=title,
        company="Example Co",
        location=None,
        url="https://example.test/1",
        posted_at=None,
        raw="",
    )


def _proposal(*titles: str) -> RecipeProposal:
    return RecipeProposal(
        ref=parse_source_ref("recipe:careers"),
        target_url="https://example.test/careers",
        company="Example Co",
        recipe="{}",
        recipe_format=RecipeFormat.HTML,
        recipe_url="https://example.test/careers",
        jobs=tuple(_draft(title) for title in titles),
        invalid=0,
        model="claude-sdk/made-up-model",
        report=OperationReport(
            done=1, not_done=0, spent=0.001, cap=0.5, stopped_by=None
        ),
    )


def test_step_what_names_the_tool_and_its_outcome() -> None:
    assert step_what(AgentStep("try_board", _OK)) == ("try board", "ok")
    assert step_what(AgentStep("open_page", _FAILED)) == ("open page", "failed")
    assert step_what(AgentStep("finish", _OK)) == ("finish", "ok")


_TRY = {"kind": "greenhouse", "key": "example-co"}


@pytest.mark.parametrize(
    ("step", "row"),
    [
        (
            _step("try_board", "found 41 jobs, company matches", kind="lever", key="a"),
            ("verify lever:a", "✓ 41 jobs, company matches"),
        ),
        (
            _step("try_board", "found 1 jobs, company matches", kind="lever", key="a"),
            ("verify lever:a", "✓ 1 job, company matches"),
        ),
        (
            _step("try_board", "no board", **_TRY),
            ("try greenhouse:example-co", "not found"),
        ),
        (
            _step("try_board", "company not on this board", **_TRY),
            ("try greenhouse:example-co", "company not on this board"),
        ),
        (
            _step("try_board", "empty board", **_TRY),
            ("try greenhouse:example-co", "empty board"),
        ),
        (
            _step("try_board", "could not check: robots.txt could not be read", **_TRY),
            (
                "try greenhouse:example-co",
                "could not check: robots.txt could not be read",
            ),
        ),
        (
            _step("try_board", "not a valid board", kind="nope", key="a"),
            ("try nope:a", "not a valid board"),
        ),
        (
            _step("try_board", "could not run try_board; it takes: kind, key", _FAILED),
            ("try board", "failed"),
        ),
        (
            _step(
                "try_board",
                "could not run try_board; it takes: kind, key",
                _FAILED,
                **_TRY,
            ),
            ("try greenhouse:example-co", "failed"),
        ),
        (_step("try_board", "no board"), ("try board", "not found")),
    ],
)
def test_step_what_draws_outcomes(step: AgentStep, row: tuple[str, str]) -> None:
    assert step_what(step) == row


@pytest.mark.parametrize(
    ("step", "row"),
    [
        (
            _step(
                "open_page",
                "no source found on this page; 3 links on the site:\n- a\n- b",
                url="https://example.com/careers",
            ),
            ("open example.com/careers", "3 links"),
        ),
        (
            _step(
                "open_page",
                "no source found on this page; 1 links on the site:\n- a",
                url="http://example.com/",
            ),
            ("open example.com/", "1 link"),
        ),
        (
            _step(
                "open_page",
                "no source found on this page; it has no links on the site",
                url="https://example.com/x",
            ),
            ("open example.com/x", "no links"),
        ),
        (
            _step(
                "open_page",
                "verified source lever:a: found 41 jobs, company matches",
                url="https://jobs.lever.co/a",
            ),
            ("open jobs.lever.co/a", "✓ 41 jobs"),
        ),
        (
            _step(
                "open_page",
                "verified source recipe:a: read 1 jobs",
                url="https://example.com/jobs.rss",
            ),
            ("open example.com/jobs.rss", "✓ 1 job"),
        ),
        (
            _step(
                "open_page",
                "greenhouse:a: no board",
                url="https://boards.greenhouse.io/a",
            ),
            ("open boards.greenhouse.io/a", "not found"),
        ),
        (
            _step(
                "open_page",
                "could not open: robots.txt disallows it",
                url="https://example.com/x",
            ),
            ("open example.com/x", "robots.txt disallows it"),
        ),
        (
            _step(
                "open_page",
                "refused: that host is not on the site",
                url="https://evil.test/x",
            ),
            ("open evil.test/x", "that host is not on the site"),
        ),
        (
            _step(
                "open_page",
                "could not run open_page; it takes: url",
                _FAILED,
                url="https://example.com/x",
            ),
            ("open example.com/x", "failed"),
        ),
        (_step("open_page", "could not open: HTTP 500"), ("open page", "HTTP 500")),
    ],
)
def test_step_what_draws_opened_pages(step: AgentStep, row: tuple[str, str]) -> None:
    assert step_what(step) == row


@pytest.mark.parametrize(
    ("step", "row"),
    [
        (
            _step("learn_page", "read 7 jobs", url="https://example.com/careers"),
            ("learn https://example.com/careers", "read 7 jobs"),
        ),
        (
            _step(
                "learn_page",
                "could not learn: the page has no jobs",
                url="https://example.com/c",
            ),
            ("learn https://example.com/c", "the page has no jobs"),
        ),
        (
            _step("learn_page", "refused: off the site", url="https://evil.test/c"),
            ("learn https://evil.test/c", "off the site"),
        ),
        (
            _step(
                "learn_page",
                _NOT_OPENED,
                url="https://example.com/c",
            ),
            (
                "learn https://example.com/c",
                _NOT_OPENED,
            ),
        ),
    ],
)
def test_step_what_draws_learned_pages(step: AgentStep, row: tuple[str, str]) -> None:
    assert step_what(step) == row


def test_step_what_decodes_a_quoted_reason_and_draws_it_literally() -> None:
    quoted = '"robots.txt could not be read: ConnectTimeout"'
    hostile = json.dumps(_HOSTILE)

    assert step_what(_step("try_board", f"could not check: {quoted}", **_TRY)) == (
        "try greenhouse:example-co",
        "could not check: robots.txt could not be read: ConnectTimeout",
    )
    assert step_what(
        _step("open_page", f"could not open: {quoted}", url="https://example.com/x")
    ) == ("open example.com/x", "robots.txt could not be read: ConnectTimeout")
    assert step_what(
        _step(
            "open_page",
            f"greenhouse:a: could not check: {quoted}",
            url="https://x.test/a",
        )
    ) == (
        "open x.test/a",
        "could not check: robots.txt could not be read: ConnectTimeout",
    )
    assert step_what(
        _step("learn_page", f"could not learn: {quoted}", url="https://example.com/x")
    ) == ("learn https://example.com/x", "robots.txt could not be read: ConnectTimeout")
    _what, result = step_what(
        _step("open_page", f"could not open: {hostile}", url="https://example.com/x")
    )
    assert result.startswith("[bold red]gone[/]")
    assert "\x1b" not in result
    assert "\x07" not in result


def test_step_what_keeps_an_unquoted_or_broken_reason_as_it_is() -> None:
    assert step_what(
        _step("open_page", "could not open: not a valid address", url="x")
    ) == ("open x", "not a valid address")
    assert step_what(
        _step("open_page", "could not open: the page lists no jobs", url="x")
    ) == ("open x", "the page lists no jobs")
    assert step_what(_step("open_page", 'could not open: "broken', url="x")) == (
        "open x",
        '"broken',
    )


def test_explore_line_ignores_a_trailing_failure_reason() -> None:
    step = _step(
        "find_source",
        "verified sources:\n- lever:example-co (41 jobs)\n"
        "could not find a source: later",
        company="Example Co",
    )

    assert _live(step) == "   ✓ checked Example Co  lever, 41 jobs"


def test_step_what_falls_back_to_the_first_line_of_the_text() -> None:
    assert step_what(_step("propose", "proposed\nmore")) == ("propose", "proposed")
    assert step_what(_step("finish")) == ("finish", "ok")


def test_step_what_draws_hostile_arguments_and_results_as_text() -> None:
    row = step_what(
        _step(
            "try_board",
            f"could not check: {_HOSTILE}",
            kind=f"{_HOSTILE}",
            key="a\x1b[2Jb",
        )
    )
    row_open = step_what(_step("open_page", _HOSTILE, url=f"https://x.test/{_HOSTILE}"))

    for text in (*row, *row_open):
        assert "\x1b" not in text
        assert "\x07" not in text
        assert "\n" not in text
    assert "[bold red]gone[/]" in row[1]


def test_step_what_is_plain_text_for_an_unknown_tool() -> None:
    what, result = step_what(AgentStep(f"[red]{_HOSTILE}", _OK))

    assert "\x1b" not in what
    assert "\x07" not in what
    assert result == "ok"


def test_step_line_draws_the_step_and_spins() -> None:
    first = step_line("Looking for Example Co", 1, 8)
    later = step_line("Looking for Example Co", 4, 8, frame=2)

    assert first.plain == " ⠋ Looking for Example Co   step 1 of at most 8"
    assert later.plain == " ⠹ Looking for Example Co   step 4 of at most 8"


def test_step_line_draws_the_cost_against_the_cap() -> None:
    line = step_line("Exploring", 6, 20, frame=2, cost=(0.03, 0.5))

    assert line.plain == " ⠹ Exploring   step 6 of at most 20   $0.03 of $0.50"


def test_step_line_never_passes_the_limit() -> None:
    assert step_line("Looking for Example Co", 12, 8).plain.endswith(
        "step 8 of at most 8"
    )


def test_step_line_draws_the_company_literally() -> None:
    drawn = _draw(step_line(f"Looking for {_HOSTILE}", 2, 8), width=200)

    assert "[bold red]gone[/]" in drawn
    assert "\x1b" not in drawn


def test_found_panel_draws_steps_and_sample() -> None:
    drawn = _draw(found_panel(_found(), _RUN))
    lines = drawn.splitlines()

    assert lines[0].startswith("╭─ Found · lever:example-co")
    assert lines[1].split() == ["│", "STEP", "WHAT", "RESULT", "│"]
    assert _words(lines[2]) == "│ 1 try greenhouse:example-co not found │"
    assert _words(lines[3]) == "│ 2 open example.com/careers 2 links │"
    assert (
        _words(lines[4]) == "│ 3 verify lever:example-co ✓ 41 jobs, company matches │"
    )
    assert lines[5].strip("│ ") == ""
    assert lines[6].startswith("│ Sample: Staff Engineer · Sales Lead · Data Analyst")
    assert lines[7].startswith("╰")
    assert drawn.startswith("╭")


def test_found_panel_leaves_the_finish_step_out() -> None:
    drawn = _draw(found_panel(_found(), _RUN))

    assert "finish" not in drawn
    assert "Completed" not in drawn


def test_found_panel_draws_a_failed_step() -> None:
    drawn = _draw(found_panel(_found(), [_step("open_page", "x", _FAILED)]))

    assert "open page" in drawn
    assert "failed" in drawn


def test_found_panel_without_sample_has_no_sample_line() -> None:
    drawn = _draw(found_panel(_found(sample=()), _RUN))

    assert "Sample" not in drawn


def test_found_panel_takes_a_recipes_titles_from_the_proposal() -> None:
    found = _found(sample=(), proposal=_proposal("Rust Engineer", "Go Engineer"))

    drawn = _draw(found_panel(found, _RUN))

    assert "Sample: Rust Engineer · Go Engineer" in drawn


def test_found_panel_draws_third_party_text_literally() -> None:
    found = _found(sample=(_HOSTILE,))
    steps = [_step("try_board", "no board", kind=_HOSTILE, key=_HOSTILE)]

    drawn = _draw(found_panel(found, steps), width=300)

    assert "[bold red]gone[/]" in drawn
    assert "[link=https://evil.example]x[/link]" in drawn
    assert "\x1b" not in drawn
    assert "\x07" not in drawn


def test_sample_titles_are_cut_to_three() -> None:
    found = _found(sample=("a", "b", "c", "d"))

    assert sample_titles(found) == ["a", "b", "c"]


def test_found_lines_are_plain() -> None:
    lines = found_lines(_found(), _RUN)

    assert lines == [
        "found: lever:example-co, 41 jobs",
        "step 1: try greenhouse:example-co, not found",
        "step 2: open example.com/careers, 2 links",
        "step 3: verify lever:example-co, 41 jobs, company matches",
        "sample: Staff Engineer; Sales Lead; Data Analyst",
    ]


def test_found_lines_name_the_address_and_leave_out_a_missing_sample() -> None:
    url = "https://example.test/jobs.rss"

    lines = found_lines(_found(sample=(), target_url=url), [])

    assert lines == [f"found: lever:example-co, 41 jobs, read from {url}"]


def test_found_lines_drop_control_characters() -> None:
    lines = found_lines(_found(sample=(_HOSTILE,)), [])

    assert all("\x1b" not in line and "\x07" not in line for line in lines)
    assert any("[bold red]gone[/]" in line for line in lines)


def test_found_lines_drop_the_tick_of_a_verified_row() -> None:
    lines = found_lines(_found(sample=()), _RUN[2:])

    assert lines[-1] == "step 1: verify lever:example-co, 41 jobs, company matches"


def _verified(ref: str = "lever:example-co") -> VerifiedSource:
    return VerifiedSource(
        ref=parse_source_ref(ref), target_url=None, jobs=41, sample=("a",)
    )


def _result(
    proposals: list[Proposal] | None = None,
    known: int = 0,
    no_source: int = 0,
) -> ExploreResult:
    return ExploreResult(
        proposals=proposals or [],
        known=[parse_source_ref(f"greenhouse:known{n}") for n in range(known)],
        no_source=[f"Nobody {n}" for n in range(no_source)],
        violations=[],
        stop=None,
        steps=[],
    )


_TWO = [
    Proposal(_verified(), "Example Co", "Tokyo fintech, hires remote"),
    Proposal(
        VerifiedSource(
            ref=parse_source_ref("ashby:sample-pay"), target_url=None, jobs=18
        ),
        "Sample Pay",
        "payments, Japan office",
    ),
]


def test_proposals_panel_draws_counts() -> None:
    drawn = _draw(proposals_panel(_result(_TWO, known=1, no_source=1)))
    lines = drawn.splitlines()

    assert lines[0].startswith("╭─ 2 sources proposed · 1 already known · 1 no source ")
    assert _words(lines[1]) == "│ # SOURCE COMPANY JOBS WHY │"
    assert _words(lines[2]) == (
        "│ 1 lever:example-co Example Co 41 Tokyo fintech, hires remote │"
    )
    assert (
        _words(lines[3])
        == "│ 2 ashby:sample-pay Sample Pay 18 payments, Japan office │"
    )
    assert lines[2].index("41") == lines[3].index("18")


def test_proposals_panel_counts_one_source_and_leaves_out_zeros() -> None:
    drawn = _draw(proposals_panel(_result(_TWO[:1])))

    assert drawn.splitlines()[0].startswith("╭─ 1 source proposed ")
    assert "already known" not in drawn
    assert "no source" not in drawn


def test_proposals_panel_draws_third_party_text_literally() -> None:
    hostile = Proposal(_verified(), _HOSTILE, _HOSTILE)

    drawn = _draw(proposals_panel(_result([hostile])), width=300)

    assert "[bold red]gone[/]" in drawn
    assert "[link=https://evil.example]x[/link]" in drawn
    assert "\x1b" not in drawn
    assert "\x07" not in drawn


def test_proposals_lines_are_plain() -> None:
    lines = proposals_lines(_result(_TWO, known=1, no_source=2))

    assert lines == [
        "2 sources proposed, 1 already known, 2 no source",
        "1: lever:example-co, Example Co, 41 jobs, Tokyo fintech, hires remote",
        "2: ashby:sample-pay, Sample Pay, 18 jobs, payments, Japan office",
    ]


def test_proposals_lines_with_none_say_so() -> None:
    assert proposals_lines(_result(known=1)) == ["no sources proposed, 1 already known"]


def _live(step: AgentStep) -> str:
    line = explore_line(step)
    assert line is not None
    return line.plain


def test_explore_line_for_a_read_of_the_sources() -> None:
    listed = _step("list_sources", '2 sources:\n- lever:a "A"\n- ashby:b "B"')
    none = _step("list_sources", "you have no sources")

    assert _live(listed) == "   ✓ read your 2 sources"
    assert _live(none) == "   ✓ you have no sources"


def test_explore_line_for_a_company_checked() -> None:
    found = _step(
        "find_source",
        "verified sources:\n- lever:example-co (41 jobs)\n- ashby:x (3 jobs)",
        company="Example Co",
    )
    nothing = _step("find_source", "no source found", company="Sample Labs")
    junk = _step("find_source", "could not find a source: bad", company="Sample Labs")
    broke = _step("find_source", "could not run find_source", _FAILED, company="A B")

    assert _live(found) == "   ✓ checked Example Co  lever, 41 jobs"
    assert _live(nothing) == "   ✗ checked Sample Labs  no public board"
    assert _live(junk) == "   ✗ checked Sample Labs  could not find a source"
    assert _live(broke) == "   ✗ checked A B  failed"


def test_explore_line_is_none_for_the_other_tools() -> None:
    for step in (
        _step("propose", "proposed", ref="lever:a", why="x"),
        _step("companies_in_store", '- "A"', query="a"),
        _step("finish", "Completed."),
        _step("find_source", "give a company name", company=""),
    ):
        assert explore_line(step) is None


def test_explore_line_draws_the_company_literally() -> None:
    line = explore_line(_step("find_source", "no source found", company=_HOSTILE))

    assert line is not None
    assert "[bold red]gone[/]" in line.plain
    assert "\x1b" not in line.plain
    assert "\x07" not in line.plain


def test_not_found_view_draws_the_result_and_the_fix() -> None:
    drawn = _draw(
        not_found_view(
            "Example Co",
            4,
            0.004,
            'ijob sources add-url <careers URL> --company "Example Co"',
        )
    )

    assert drawn.splitlines() == [
        " ✗ No source found for Example Co  4 steps, $0.004",
        f' {PROMPT_MARK}ijob sources add-url <careers URL> --company "Example Co"',
    ]


def test_not_found_view_counts_one_step_and_keeps_small_costs() -> None:
    drawn = _draw(not_found_view("A", 1, 0.0034, "ijob sources add-url x"))

    assert drawn.splitlines()[0] == " ✗ No source found for A  1 step, $0.0034"


def test_not_found_view_draws_the_company_literally() -> None:
    drawn = _draw(not_found_view(_HOSTILE, 2, 0.01, f"fix {_HOSTILE}"), width=300)

    assert "[bold red]gone[/]" in drawn
    assert "\x1b" not in drawn
    assert "\x07" not in drawn
