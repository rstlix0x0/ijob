"""The judge task: decide whether the seeker's rules keep a posting.

The rules are the seeker's own and the posting is third-party text; each goes in
its own input field and the instruction text holds neither.
"""

from collections.abc import Sequence

import dspy
from ijob_core.errors import InvalidValueError
from ijob_core.model import Posting, Verdict

from ijob_ai.errors import TaskOutputError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import VerdictOut
from ijob_ai.tasks.signatures import JUDGE_VERSION, JudgeSig
from ijob_ai.tasks.text import numbered_rules, posting_block, sanitize


def _verdict(answer: VerdictOut, rule_count: int) -> Verdict:
    """Build the core verdict from an answer, or raise `TaskOutputError`."""
    if answer.rule is not None and answer.rule > rule_count:
        msg = f"rule {answer.rule} is not one of the {rule_count} rules"
        raise TaskOutputError(Task.JUDGE.value, msg)
    reason = sanitize(answer.reason)
    if not reason:
        raise TaskOutputError(Task.JUDGE.value, "the reason is empty")
    try:
        return Verdict(keep=answer.keep, reason=reason, rule=answer.rule)
    except InvalidValueError as error:
        raise TaskOutputError(Task.JUDGE.value, str(error)) from error


async def judge_posting(  # noqa: PLR0913  # the call's whole contract, one argument each
    posting: Posting,
    rules: Sequence[str],
    *,
    lm: dspy.LM,
    provider: Provider,
    budget: Budget,
    timeout_s: float,
) -> Verdict:
    """Judge whether a posting passes the seeker's rules.

    The rules, numbered from 1, are the `rules` input and the posting block is
    the `posting` input; the instruction text holds neither. The caller checks
    `Budget.try_start` first.

    Args:
        posting: The posting to judge.
        rules: The seeker's rules, in the order they are numbered.
        lm: The judge task's LM.
        provider: The provider serving `lm`.
        budget: The operation's budget, charged with the call's cost.
        timeout_s: Seconds the call may take.

    Returns:
        The verdict, with its reason cleaned of control characters.

    Raises:
        TaskOutputError: If the answer cites a rule beyond `rules`, is
            inconsistent (a keep naming a rule, or a rejection naming none),
            or has no reason once cleaned.
        LlmError: Any error `run_call` raises.
    """
    predict = dspy.Predict(JudgeSig)
    prediction = await run_call(
        predict,
        lm,
        inputs={"rules": numbered_rules(rules), "posting": posting_block(posting)},
        provider=provider,
        budget=budget,
        timeout_s=timeout_s,
        task=Task.JUDGE,
        task_version=JUDGE_VERSION,
        posting_id=posting.id,
    )
    return _verdict(prediction.verdict, len(rules))
