"""Turn the user's written instructions into filters and judge rules."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import dspy
from ijob_core.errors import InvalidValueError

from ijob_ai.errors import NothingActionableError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import InstructionsOut
from ijob_ai.tasks.signatures import INSTRUCTIONS_VERSION, ParseInstructionsSig
from ijob_ai.tasks.text import sanitize

INSTRUCTIONS_LIMIT: Final = 4_000
"""The most characters of instruction text a call accepts."""


@dataclass(frozen=True, slots=True)
class ParsedInstructions:
    """What the model made of the user's instructions.

    Attributes:
        filters: Values for the config file's `[filters]` table, holding only the
            keys the instructions set. Company names are sanitized and role
            words are escaped for matching as literal text. The config builder
            validates them.
        rules: The sanitized judge rules, none empty.
    """

    filters: Mapping[str, object]
    rules: tuple[str, ...]


def _clean_words(words: list[str]) -> list[str]:
    """Sanitize each word and drop empty ones."""
    return [word for word in map(sanitize, words) if word]


def _filters(out: InstructionsOut) -> dict[str, object]:
    """Return the filters the model set, sanitized, with role words escaped."""
    filters = out.filters.model_dump(
        exclude_defaults=True, exclude={"role", "companies"}
    )
    if companies := _clean_words(out.filters.companies):
        filters["companies"] = companies
    role: dict[str, object] = {}
    if include := _clean_words(out.filters.role.include):
        role["include"] = [re.escape(word) for word in include]
    if exclude := _clean_words(out.filters.role.exclude):
        role["exclude"] = [re.escape(word) for word in exclude]
    if role:
        filters["role"] = role
    return filters


async def parse_instructions(
    text: str,
    *,
    lm: dspy.LM,
    provider: Provider,
    budget: Budget,
    timeout_s: float,
) -> ParsedInstructions:
    """Ask the model what the user's instructions mean.

    Args:
        text: The user's instructions.
        lm: The LM of the `instructions` task.
        provider: The provider serving `lm`.
        budget: The operation's budget, charged with the call's cost.
        timeout_s: Seconds the call may take.

    Returns:
        The filters and the judge rules the instructions imply.

    Raises:
        InvalidValueError: If `text` is blank or longer than 4,000 characters;
            no call is made.
        NothingActionableError: If the answer holds neither a filter nor a rule.
        TaskOutputError: If the answer did not match the expected fields.
        LlmError: Any other error `run_call` raises.
    """
    if not text.strip():
        raise InvalidValueError("instructions", "must not be blank")
    if len(text) > INSTRUCTIONS_LIMIT:
        reason = f"must be at most {INSTRUCTIONS_LIMIT:,} characters"
        raise InvalidValueError("instructions", reason)
    prediction = await run_call(
        dspy.Predict(ParseInstructionsSig),
        lm,
        inputs={"instruction_text": text},
        provider=provider,
        budget=budget,
        timeout_s=timeout_s,
        task=Task.INSTRUCTIONS,
        task_version=INSTRUCTIONS_VERSION,
        posting_id=None,
    )
    out: InstructionsOut = prediction.parsed
    filters = _filters(out)
    rules = tuple(rule for rule in map(sanitize, out.rules) if rule)
    if not filters and not rules:
        raise NothingActionableError
    return ParsedInstructions(filters=MappingProxyType(filters), rules=rules)
