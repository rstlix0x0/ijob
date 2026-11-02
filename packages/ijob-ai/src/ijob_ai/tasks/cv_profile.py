"""Read the structured profile out of the text of a CV."""

import dspy
from ijob_core.errors import InvalidValueError
from ijob_core.model import ResumeProfile

from ijob_ai.errors import TaskOutputError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import ProfileOut
from ijob_ai.tasks.signatures import CV_PROFILE_VERSION, CvProfileSig
from ijob_ai.tasks.text import sanitize


def _clean(items: list[str]) -> tuple[str, ...]:
    """Sanitize each item and keep those that are not empty afterwards."""
    return tuple(item for item in map(sanitize, items) if item)


async def build_profile(
    cv_text: str,
    *,
    lm: dspy.LM,
    provider: Provider,
    budget: Budget,
    timeout_s: float,
) -> ResumeProfile:
    """Ask the model for the facts a CV states and build a profile from them.

    Every string the model returns is sanitized, and a list item that is empty
    afterwards is dropped.

    Args:
        cv_text: The text of the CV.
        lm: The LM of the `cv_profile` task.
        provider: The provider serving `lm`.
        budget: The operation's budget, charged with the call's cost.
        timeout_s: Seconds the call may take.

    Returns:
        The profile the CV states.

    Raises:
        TaskOutputError: If the answer did not match the expected fields, the
            summary is empty after sanitizing, or the profile breaks a limit of
            `ResumeProfile`.
        LlmError: Any other error `run_call` raises.
    """
    prediction = await run_call(
        dspy.Predict(CvProfileSig),
        lm,
        inputs={"cv_text": cv_text},
        provider=provider,
        budget=budget,
        timeout_s=timeout_s,
        task=Task.CV_PROFILE,
        task_version=CV_PROFILE_VERSION,
        posting_id=None,
    )
    out: ProfileOut = prediction.profile
    summary = sanitize(out.summary)
    if not summary:
        raise TaskOutputError(Task.CV_PROFILE.value, "the summary is empty")
    try:
        return ResumeProfile(
            summary=summary,
            years_experience=out.years_experience,
            seniority=out.seniority,
            roles=_clean(out.roles),
            spoken_languages=_clean(out.spoken_languages),
            locations=_clean(out.locations),
            work_authorization=_clean(out.work_authorization),
            skills=_clean(out.skills),
        )
    except InvalidValueError as error:
        raise TaskOutputError(Task.CV_PROFILE.value, str(error)) from error
