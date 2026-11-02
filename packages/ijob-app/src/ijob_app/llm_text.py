"""The words the command line and the terminal UI use for the LLM operations.

One home for how a verdict, a summary, a profile, a set of instructions, a
shadowed filter, a learned recipe and a cost read, so `ijob search`, `ijob
score`, `ijob cv`, `ijob instructions`, `ijob sources` and their terminal
screens agree; the cost line they share is `ijob_app.llm_ops.cost_line`. This
module imports the service's types only, so loading it pulls in neither DSPy nor
the Claude SDK.
"""

import unicodedata
from collections.abc import Iterator, Mapping
from typing import Final

from ijob_core.model import ResumeProfile, SightingDraft
from ijob_core.money import money_text

from ijob_app.config import ShadowedField
from ijob_app.llm_ops import (
    STATUS_TEXT,
    JudgeStatus,
    OperationReport,
    SearchItem,
    StopReason,
    cost_line,
)
from ijob_app.recipe_ops import RecipeProposal

STOP_TEXT: Final[Mapping[StopReason, str]] = {
    "cap": "cap reached",
    "provider": "provider error",
    "cost_unknown": "cost unknown",
}
"""What the user reads for each reason an operation started no further items."""

_PASSED: Final = "✔"
_DROPPED: Final = "✘"
MISSING_MARK: Final = "·"
"""The mark beside an item that has no result, before the reason."""

_PROPOSAL_JOBS_SHOWN: Final = 5


def verdict_cell(item: SearchItem) -> str:
    """Write what the judge made of a posting, for the end of its line.

    Args:
        item: The posting and its verdict.

    Returns:
        A check mark for a posting kept, a cross and the reason for one dropped,
        or a dot and the reason there is no verdict; ` (no description)` follows
        when the judge saw no description.
    """
    verdict = item.verdict
    if verdict is None:
        text = STATUS_TEXT[JudgeStatus].get(item.status, item.status.value)
        cell = f"{MISSING_MARK} {text}"
    elif verdict.keep:
        cell = _PASSED
    else:
        cell = f"{_DROPPED} {verdict.reason}"
    return f"{cell} (no description)" if item.no_description else cell


def _cost_and_stop(report: OperationReport) -> str:
    """Write the spend of an operation and, when it stopped early, why."""
    text = cost_line(report)
    if report.stopped_by is not None:
        text += f"; stopped: {STOP_TEXT[report.stopped_by]}"
    return text


def search_summary(report: OperationReport) -> str:
    """Write the one-line summary of a judged search.

    Args:
        report: What the judging did and spent.

    Returns:
        `judged N, cached K, dropped M, not judged U, cost $X of $Y`, ending
        `; stopped: <why>` when no further posting was started.
    """
    return (
        f"judged {report.done}, cached {report.cached}, dropped {report.dropped}, "
        f"not judged {report.not_done}, {_cost_and_stop(report)}"
    )


def score_summary(report: OperationReport) -> str:
    """Write the one-line summary of a scoring run.

    Args:
        report: What the scoring did and spent.

    Returns:
        `scored N, cached K, not scored U, cost $X of $Y`, ending
        `; stopped: <why>` when no further posting was started.
    """
    return (
        f"scored {report.done}, cached {report.cached}, "
        f"not scored {report.not_done}, {_cost_and_stop(report)}"
    )


def _printable(text: str) -> str:
    """Drop the control and format characters of text a page or a model wrote.

    Terminal escape sequences and bidirectional overrides would otherwise
    rewrite what the user reads before confirming.
    """
    return "".join(
        char for char in text if unicodedata.category(char) not in {"Cc", "Cf"}
    )


def _job_line(job: SightingDraft) -> str:
    """Write one job of a proposal as tab-separated cells, each with no controls."""
    salary = "; ".join(money_text(money) for money in job.salaries) or "-"
    cells = [job.title, job.company, job.location or "-", salary, job.url]
    return "\t".join(_printable(cell) for cell in cells)


def recipe_proposal_lines(proposal: RecipeProposal) -> list[str]:
    """Lay a learned recipe out as the lines the user confirms it by.

    Args:
        proposal: The recipe and what it read from its page.

    Returns:
        `recipe: <format> at <url>`, then `read N jobs, M invalid`, then a
        tab-separated line for each of the first five jobs: title, company,
        location, salary and address, with `-` for a location or salary the
        job lacks. Control and format characters are dropped from the text the
        page and the model supplied.
    """
    lines = [
        f"recipe: {proposal.recipe_format} at {_printable(proposal.recipe_url)}",
        f"read {len(proposal.jobs)} jobs, {proposal.invalid} invalid",
    ]
    lines += [_job_line(job) for job in proposal.jobs[:_PROPOSAL_JOBS_SHOWN]]
    return lines


def profile_lines(profile: ResumeProfile) -> list[str]:
    """Lay a CV profile out as `key: value` lines.

    Args:
        profile: The profile to show.

    Returns:
        The summary, years of experience (`-` when unknown) and seniority, then
        each list comma-joined, one line apiece.
    """
    years = "-" if profile.years_experience is None else profile.years_experience
    return [
        f"summary: {profile.summary}",
        f"years_experience: {years}",
        f"seniority: {profile.seniority}",
        f"roles: {', '.join(profile.roles)}",
        f"skills: {', '.join(profile.skills)}",
        f"spoken_languages: {', '.join(profile.spoken_languages)}",
        f"locations: {', '.join(profile.locations)}",
        f"work_authorization: {', '.join(profile.work_authorization)}",
    ]


def _flat(prefix: str, table: Mapping[str, object]) -> Iterator[tuple[str, object]]:
    """Walk a filters table, naming a nested key by its dotted path."""
    for key, value in table.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            yield from _flat(f"{name}.", value)
        else:
            yield name, value


def _value(value: object) -> str:
    """Write one filter value as text: a list comma-joined, a flag as a word."""
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def instruction_lines(
    filters: Mapping[str, object], rules: tuple[str, ...]
) -> list[str]:
    """Lay instruction filters and rules out as `filters:` and `rules:` blocks.

    Args:
        filters: The filters in the shape of the config file's `[filters]` table.
        rules: The judge rules.

    Returns:
        A `filters:` line with one indented `name: value` line per filter and a
        `rules:` line with one indented numbered line per rule; a block with
        nothing in it reads `filters: none` or `rules: none`.
    """
    lines = ["filters:"] if filters else ["filters: none"]
    lines += [f"  {name}: {_value(value)}" for name, value in _flat("", filters)]
    lines += ["rules:"] if rules else ["rules: none"]
    lines += [f"  {number}. {rule}" for number, rule in enumerate(rules, start=1)]
    return lines


def shadow_line(shadowed: ShadowedField) -> str:
    """Write the warning for an instruction filter that something else overrides."""
    return (
        f"warning: {shadowed.field}: your {shadowed.used_from}'s "
        f"{shadowed.used_value} is used; the instruction's "
        f"{shadowed.ignored_value} is ignored"
    )
