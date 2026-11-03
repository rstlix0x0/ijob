"""How an agent run is drawn: its progress, its steps and what it found.

`step_what` writes the two columns of one step from the arguments the model gave
and the result the tool returned, `step_line` draws the line shown while a run
goes on and `explore_line` the lines under it, `found_panel` draws a source an
agent found with the steps that led to it, and `found_lines` writes the same as
plain lines for a pipe. `proposals_panel` and `proposals_lines` do the same for
the sources an explore run put forward, and `not_found_view` draws a run that
found none. A step's arguments and results, a source's address and its job
titles come from the model and from pages, so everything here is drawn
literally: control characters are dropped and no text is read as Rich markup.

At module level this module imports no `ijob_ai` module, so drawing a result
loads no LLM library.
"""

import json
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ijob_app.agent_ops import ExploreResult, FoundSource
from ijob_app.view import (
    AMBER,
    CORAL,
    MINT,
    MUTED,
    PROMPT_MARK,
    SKY,
    TEXT,
    boxed,
    clean,
    failed_line,
)

if TYPE_CHECKING:
    from ijob_ai.agents.react import AgentStep

_SPINNER_FRAMES: Final = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SAMPLE_TITLES: Final = 3
_FINISH: Final = "finish"
_TICK: Final = "✓"
_CROSS: Final = "✗"
_FAILED: Final = "failed"
_OK: Final = "ok"
_SCHEME: Final = re.compile(r"^https?://", re.IGNORECASE)
_FOUND_JOBS: Final = re.compile(r"^found (\d+) jobs, company matches")
_JOBS_READ: Final = re.compile(r"\b(?:found|read) (\d+) jobs")
_LINKS: Final = re.compile(r"\b(\d+) links on the site")
_BOARD_RESULT: Final = re.compile(r"^[a-z]+:[^\s:]+: (.+)$")
_CHECKED_SOURCE: Final = re.compile(r"^- ([a-z]+):\S+ \((\d+) jobs\)")
_REASON_PREFIXES: Final = ("could not open: ", "could not learn: ", "refused: ")
_LIVE_INDENT: Final = "   "
_NO_BOARD: Final = "no board"
_COULD_NOT_CHECK: Final = "could not check: "
_NO_SOURCE_FOUND: Final = "no source found"
_COULD_NOT_FIND: Final = "could not find a source"
_VERIFIED_SOURCES: Final = "verified sources:"
_NO_SOURCES: Final = "you have no sources"


def step_what(step: "AgentStep") -> tuple[str, str]:
    """Write the two columns of a step: what it did and how it ended.

    `try_board` reads as `verify lever:acme` with `✓ 41 jobs, company matches`
    when it found jobs, and otherwise as `try lever:acme` with `not found` or
    the reason the tool gave. `open_page` reads as `open <address without its
    scheme>` and then `N links`, `✓ N jobs` for a source it verified or the
    reason it could not open the page; `learn_page` reads as `learn <address>`
    and then `read N jobs` or the reason. A call that failed ends in `failed`.
    A step that cannot be read this way, such as another tool's or one without
    its arguments, is its tool's name with underscores as spaces and the first
    line of its result, or `ok` when it has none.

    Args:
        step: A finished tool call.

    Returns:
        The two columns as plain text, with control characters dropped.
    """
    describe = _DESCRIBERS.get(step.tool)
    what, result = describe(step) if describe is not None else (None, None)
    if what is None:
        what = clean(step.tool.replace("_", " "))
    if _failed(step):
        return what, _FAILED
    return what, result or _first_line(step.result) or _OK


def step_line(
    label: str,
    step: int,
    limit: int,
    frame: int = 0,
    *,
    cost: tuple[float, float] | None = None,
) -> Text:
    """Draw the line shown while an agent runs: a spinner and the step it is on.

    Args:
        label: What the agent is doing, such as `Looking for Example Co`.
        step: The step the agent is on, from 1; a step past `limit` is drawn as
            `limit`.
        limit: How many steps the agent may take.
        frame: Which frame of the spinner to draw.
        cost: What the run has spent and its cap, in US dollars, drawn as
            `$0.03 of $0.50` after the step; `None` draws no cost.

    Returns:
        The line, with `label` drawn literally.
    """
    line = Text(f" {_SPINNER_FRAMES[frame % len(_SPINNER_FRAMES)]} ", style=AMBER)
    line.append(clean(label), style=TEXT)
    line.append(f"   step {min(step, limit)} of at most {limit}", style=MUTED)
    if cost is not None:
        line.append(f"   ${cost[0]:.2f} of ${cost[1]:.2f}", style=MUTED)
    return line


def explore_line(step: "AgentStep") -> Text | None:
    """Draw what one step of an explore run found out, for under its spinner.

    Args:
        step: A finished tool call of the explore agent.

    Returns:
        `✓ read your 168 sources`, `✓ checked Example Co  lever, 41 jobs` or
        `✗ checked Sample Labs  no public board`, indented and drawn literally;
        `None` for a step that tells the user nothing, such as `propose`.
    """
    if step.tool == "list_sources":
        return _sources_read(step)
    company = _argument(step, "company")
    if step.tool != "find_source" or not company:
        return None
    return _company_checked(step, f"checked {company}")


def _sources_read(step: "AgentStep") -> Text | None:
    """Draw the read of the user's own sources, or nothing when it failed."""
    first = _first_line(step.result)
    if _failed(step) or not first:
        return None
    told = first if first == _NO_SOURCES else f"read your {first.removesuffix(':')}"
    return _live(_TICK, MINT, told, "")


def _company_checked(step: "AgentStep", subject: str) -> Text | None:
    """Draw how looking for one company's source ended."""
    if _failed(step):
        return _live(_CROSS, CORAL, subject, _FAILED)
    lines = step.result.splitlines()
    first = clean(lines[0]) if lines else ""
    if first == _NO_SOURCE_FOUND:
        return _live(_CROSS, CORAL, subject, "no public board")
    if first.startswith(_COULD_NOT_FIND):
        return _live(_CROSS, CORAL, subject, _COULD_NOT_FIND)
    if first != _VERIFIED_SOURCES:
        return None
    listed = _CHECKED_SOURCE.match(clean(lines[1])) if len(lines) > 1 else None
    detail = f"{listed.group(1)}, {_jobs(int(listed.group(2)))}" if listed else ""
    return _live(_TICK, MINT, subject, detail)


def sample_titles(found: FoundSource) -> list[str]:
    """List up to three job titles that show what a found source lists.

    Args:
        found: The source found.

    Returns:
        The titles its check read, or for a recipe without any the titles the
        recipe read; empty when it has none. Each is cleaned to one line.
    """
    titles = list(found.sample)
    if not titles and found.proposal is not None:
        titles = [job.title for job in found.proposal.jobs]
    return [clean(title) for title in titles[:_SAMPLE_TITLES]]


def found_panel(found: FoundSource, steps: "Sequence[AgentStep]") -> Panel:
    """Draw a source an agent found, with the steps that led to it.

    Args:
        found: The source found.
        steps: Every finished tool call of the run; `finish` is left out.

    Returns:
        The box: a row for each step, then up to three job titles.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("STEP", justify="right", no_wrap=True)
    table.add_column("WHAT", ratio=3, no_wrap=True, overflow="ellipsis")
    table.add_column("RESULT", ratio=3, no_wrap=True, overflow="ellipsis")
    for number, step in enumerate(shown_steps(steps), start=1):
        what, result = step_what(step)
        table.add_row(
            Text(str(number), style=MUTED),
            Text(what, style=TEXT, no_wrap=True, overflow="ellipsis"),
            _result(result, failed=_failed(step)),
        )
    parts: list[RenderableType] = [table]
    titles = sample_titles(found)
    if titles:
        parts += [Text(), _sample(titles)]
    return boxed(f"Found · {found.ref}", Group(*parts), accent=True)


def found_lines(found: FoundSource, steps: "Sequence[AgentStep]") -> list[str]:
    """Write a source an agent found, with its steps, as plain lines.

    Args:
        found: The source found.
        steps: Every finished tool call of the run; `finish` is left out.

    Returns:
        `found: <source>, N jobs` with `, read from <address>` for a feed, job
        page or recipe, then `step N: <what>, <result>` for each step, with a
        verified step's tick left off, then `sample: <titles>` when the source
        has any. Control characters are dropped from every line.
    """
    lines = [f"found: {_summary(found)}"]
    for number, step in enumerate(shown_steps(steps), start=1):
        what, result = step_what(step)
        lines.append(f"step {number}: {what}, {result.removeprefix(f'{_TICK} ')}")
    titles = sample_titles(found)
    if titles:
        lines.append(f"sample: {'; '.join(titles)}")
    return lines


def proposals_panel(result: ExploreResult) -> Panel:
    """Draw the sources an explore run put forward.

    Args:
        result: How the run ended.

    Returns:
        The box, titled with how many sources were proposed, how many the user
        already had and how many companies had none, with a row for each
        proposal: its number, source, company, jobs and the agent's reason. The
        company and the reason come from the model and are drawn literally.
    """
    table = Table(
        box=None,
        expand=True,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=f"bold {MUTED}",
    )
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("SOURCE", no_wrap=True, overflow="ellipsis")
    table.add_column("COMPANY", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("JOBS", justify="right", no_wrap=True)
    table.add_column("WHY", ratio=2, no_wrap=True, overflow="ellipsis")
    for number, proposal in enumerate(result.proposals, start=1):
        table.add_row(
            Text(str(number), style=MUTED),
            Text(clean(str(proposal.source.ref)), style=SKY, no_wrap=True),
            Text(clean(proposal.company), style=TEXT, no_wrap=True),
            Text(str(proposal.source.jobs), style=TEXT, no_wrap=True),
            Text(clean(proposal.why), style=MUTED, no_wrap=True),
        )
    return boxed(" · ".join(_counts(result)), table, accent=True)


def proposals_lines(result: ExploreResult) -> list[str]:
    """Write the sources an explore run put forward as plain lines for a pipe.

    Args:
        result: How the run ended.

    Returns:
        The counts as one line, such as `2 sources proposed, 1 already known`,
        then `N: <source>, <company>, <jobs>, <reason>` for each proposal, with
        control characters dropped.
    """
    lines = [", ".join(_counts(result))]
    for number, proposal in enumerate(result.proposals, start=1):
        source = proposal.source
        lines.append(
            clean(
                f"{number}: {source.ref}, {proposal.company}, "
                f"{_jobs(source.jobs)}, {proposal.why}"
            )
        )
    return lines


def not_found_view(company: str, steps: int, spent: float, fix: str) -> Group:
    """Draw a run that found no source, and the command that adds one by hand.

    Args:
        company: The company looked for.
        steps: How many steps the agent took.
        spent: What the run cost, in US dollars.
        fix: The command that adds the source by hand.

    Returns:
        A line such as ` ✗ No source found for Example Co  4 steps, $0.004` and
        the command under it, both drawn literally.
    """
    cost = f"{spent:.4f}".removesuffix("0")
    failed = failed_line(
        f"No source found for {company}", f"{_plural(steps, 'step')}, ${cost}"
    )
    command = Text(f" {PROMPT_MARK}", style=AMBER)
    command.append(clean(fix), style=SKY)
    return Group(failed, command)


def shown_steps(steps: "Sequence[AgentStep]") -> list["AgentStep"]:
    """Pick the steps worth showing: every one but the agent's closing call.

    Args:
        steps: Every finished tool call of a run.

    Returns:
        The steps, in order, without `finish`.
    """
    return [step for step in steps if step.tool != _FINISH]


def _failed(step: "AgentStep") -> bool:
    """Tell whether a step's tool call failed."""
    from ijob_ai.agents.react import ToolOutcome  # noqa: PLC0415

    return step.outcome is ToolOutcome.FAILED


def _first_line(text: str) -> str:
    """Take the first line of a tool's result as plain text."""
    lines = text.splitlines()
    return clean(lines[0]) if lines else ""


def _argument(step: "AgentStep", name: str) -> str:
    """Read an argument of a step as plain text; empty when it was not given."""
    return clean(step.arguments.get(name, ""))


def _jobs(count: int) -> str:
    """Write a count of jobs."""
    return _plural(count, "job")


def _plural(count: int, noun: str) -> str:
    """Write a count of things, such as `1 source` or `2 sources`."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _jobs_in(line: str) -> str | None:
    """Write `N jobs` from the count a line of a verified source holds."""
    found = _JOBS_READ.search(line)
    return _jobs(int(found.group(1))) if found else None


def _board_result(line: str) -> str:
    """Write how checking a board ended: the jobs, or the reason as it was given."""
    found = _FOUND_JOBS.match(line)
    if found is not None:
        return f"{_TICK} {_jobs(int(found.group(1)))}, company matches"
    if line.startswith(_COULD_NOT_CHECK):
        return _COULD_NOT_CHECK + _unquoted(line.removeprefix(_COULD_NOT_CHECK))
    return "not found" if line == _NO_BOARD else line


def _unquoted(reason: str) -> str:
    """Decode a reason a tool quoted as a JSON string; any other reason as it is."""
    if len(reason) > 1 and reason.startswith('"') and reason.endswith('"'):
        try:
            decoded = json.loads(reason)
        except ValueError:
            return reason
        if isinstance(decoded, str):
            return clean(decoded)
    return reason


def _reason(line: str) -> str | None:
    """Take the reason out of `could not open: ...`, `refused: ...` and the like."""
    for prefix in _REASON_PREFIXES:
        if line.startswith(prefix):
            return _unquoted(line.removeprefix(prefix))
    return None


def _open_result(line: str) -> str | None:
    """Write how opening a page ended, from the first line of the tool's result."""
    if line.startswith("verified source "):
        jobs = _jobs_in(line)
        return None if jobs is None else f"{_TICK} {jobs}"
    if line.startswith("no source found on this page"):
        links = _LINKS.search(line)
        return "no links" if links is None else _plural(int(links.group(1)), "link")
    reason = _reason(line)
    if reason is not None:
        return reason
    board = _BOARD_RESULT.match(line)
    return None if board is None else _board_result(board.group(1))


def _try_board(step: "AgentStep") -> tuple[str | None, str | None]:
    """Describe a `try_board` step: what was tried and how the check ended."""
    kind = _argument(step, "kind").lower()
    key = _argument(step, "key")
    result = _board_result(_first_line(step.result)) if step.result else None
    if not kind or not key:
        return None, result
    verb = "verify" if result and result.startswith(_TICK) else "try"
    return f"{verb} {kind}:{key}", result


def _open_page(step: "AgentStep") -> tuple[str | None, str | None]:
    """Describe an `open_page` step: the page and how opening it ended."""
    url = _SCHEME.sub("", _argument(step, "url"))
    result = _open_result(_first_line(step.result)) if step.result else None
    return (f"open {url}" if url else None), result


def _learn_page(step: "AgentStep") -> tuple[str | None, str | None]:
    """Describe a `learn_page` step: the page and what was learned from it."""
    url = _argument(step, "url")
    line = _first_line(step.result)
    return (f"learn {url}" if url else None), (_reason(line) or line or None)


_DESCRIBERS: Final[
    dict[str, Callable[["AgentStep"], tuple[str | None, str | None]]]
] = {
    "try_board": _try_board,
    "open_page": _open_page,
    "learn_page": _learn_page,
}


def _live(mark: str, color: str, subject: str, detail: str) -> Text:
    """Draw one line of what an explore run found out."""
    line = Text(f"{_LIVE_INDENT}{mark} ", style=color)
    line.append(subject, style=TEXT)
    if detail:
        line.append(f"  {detail}", style=MUTED)
    return line


def _counts(result: ExploreResult) -> list[str]:
    """Count what an explore run proposed, knew already and found no source for."""
    proposed = len(result.proposals)
    counts = [
        "no sources proposed"
        if proposed == 0
        else f"{_plural(proposed, 'source')} proposed"
    ]
    if result.known:
        counts.append(f"{len(result.known)} already known")
    if result.no_source:
        counts.append(f"{len(result.no_source)} no source")
    return counts


def _summary(found: FoundSource) -> str:
    """Write the source, its jobs and where they were read from, as one line."""
    text = f"{found.ref}, {_jobs(found.jobs)}"
    if found.target_url is not None:
        text += f", read from {clean(found.target_url)}"
    return clean(text)


def _result(result: str, *, failed: bool) -> Text:
    """Draw how a step ended: green for a verified source, red for a failure."""
    color = CORAL if failed else MINT if result.startswith(_TICK) else MUTED
    return Text(result, style=color, no_wrap=True, overflow="ellipsis")


def _sample(titles: Sequence[str]) -> Text:
    """Draw the sample of job titles."""
    line = Text("Sample: ", style=MUTED)
    line.append(" · ".join(titles), style=TEXT)
    return line
