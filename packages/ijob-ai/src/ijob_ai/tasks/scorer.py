"""The scorer task: rate how well a posting fits the candidate's profile."""

import dspy
from ijob_core.errors import InvalidValueError
from ijob_core.model import Posting, ResumeProfile, Score

from ijob_ai.errors import TaskOutputError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import ScoreOut
from ijob_ai.tasks.signatures import SCORER_VERSION, ScoreSig
from ijob_ai.tasks.text import posting_block, sanitize


def _score(answer: ScoreOut) -> Score:
    """Build the core score from an answer, or raise `TaskOutputError`."""
    reasons = tuple(text for reason in answer.reasons if (text := sanitize(reason)))
    if not reasons:
        raise TaskOutputError(Task.SCORER.value, "no reason is left once cleaned")
    try:
        return Score(score=answer.score, reasons=reasons)
    except InvalidValueError as error:
        raise TaskOutputError(Task.SCORER.value, str(error)) from error


async def score_posting(  # noqa: PLR0913  # the call's whole contract, one argument each
    posting: Posting,
    profile: ResumeProfile,
    *,
    lm: dspy.LM,
    provider: Provider,
    budget: Budget,
    timeout_s: float,
) -> Score:
    """Score how well a posting fits a profile.

    The posting block and the profile's JSON are the inputs. The caller checks
    `Budget.try_start` first.

    Args:
        posting: The posting to score.
        profile: The candidate's profile.
        lm: The scorer task's LM.
        provider: The provider serving `lm`.
        budget: The operation's budget, charged with the call's cost.
        timeout_s: Seconds the call may take.

    Returns:
        The score, with its reasons cleaned of control characters and the empty
        ones dropped.

    Raises:
        TaskOutputError: If no reason is left once cleaned, or the core type
            rejects the answer.
        LlmError: Any error `run_call` raises.
    """
    prediction = await run_call(
        dspy.Predict(ScoreSig),
        lm,
        inputs={"posting": posting_block(posting), "profile": profile.to_json()},
        provider=provider,
        budget=budget,
        timeout_s=timeout_s,
        task=Task.SCORER,
        task_version=SCORER_VERSION,
        posting_id=posting.id,
    )
    return _score(prediction.score)
