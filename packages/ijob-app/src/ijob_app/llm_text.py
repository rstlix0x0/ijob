"""The words the command line and the terminal UI use for the LLM operations.

One home for how a verdict, a summary, a profile, a set of instructions and a
shadowed filter read, so `ijob search`, `ijob score`, `ijob cv`, `ijob
instructions` and their terminal screens agree. This module imports the
service's types only, so loading it pulls in neither DSPy nor the Claude SDK.
"""

from collections.abc import Iterator, Mapping
from typing import Final

from ijob_core.model import ResumeProfile

from ijob_app.config import ShadowedField
from ijob_app.llm_ops import (
    STATUS_TEXT,
    JudgeStatus,
    OperationReport,
    SearchItem,
    StopReason,
)

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
    text = f"cost ${report.spent:.4f} of ${report.cap:.2f}"
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
