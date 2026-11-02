"""The job extract task: read one job a source's parser could not read.

The item's text is third-party text and goes only in the `item_text` input
field. Whether the answer describes a usable posting is left to the caller.
"""

from dataclasses import dataclass

import dspy

from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.signatures import JOB_EXTRACT_VERSION, JobExtractSig
from ijob_ai.tasks.text import sanitize


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractedJob:
    """What the model read from one item; an empty value is `None`.

    Attributes:
        is_job: Whether the item is a job posting.
        title: The job title the item states.
        company: The hiring company the item states.
        location: Where the job is, when the item states it.
        url: The job's address the item states.
    """

    is_job: bool
    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None


def _clean(value: str | None) -> str | None:
    """Clean a model-written value; `None` when nothing is left."""
    return (sanitize(value) or None) if value is not None else None


async def extract_job(  # noqa: PLR0913  # the call's whole contract, one argument each
    text: str,
    *,
    url: str | None = None,
    lm: dspy.LM,
    provider: Provider,
    budget: Budget,
    timeout_s: float,
) -> ExtractedJob:
    """Ask the model whether an item is a job and what it states.

    The item's text, cleaned of control characters, and its address are the
    inputs. The caller checks `Budget.try_start` first.

    Args:
        text: The item as the source gave it.
        url: The item's address, when one is known.
        lm: The job extract task's LM.
        provider: The provider serving `lm`.
        budget: The operation's budget, charged with the call's cost.
        timeout_s: Seconds the call may take.

    Returns:
        The answer, each value cleaned of control characters and `None` when
        empty.

    Raises:
        LlmError: Any error `run_call` raises.
    """
    prediction = await run_call(
        dspy.Predict(JobExtractSig),
        lm,
        inputs={"item_text": sanitize(text), "item_url": sanitize(url or "")},
        provider=provider,
        budget=budget,
        timeout_s=timeout_s,
        task=Task.JOB_EXTRACT,
        task_version=JOB_EXTRACT_VERSION,
        posting_id=None,
    )
    answer = prediction.answer
    return ExtractedJob(
        is_job=answer.is_job,
        title=_clean(answer.title),
        company=_clean(answer.company),
        location=_clean(answer.location),
        url=_clean(answer.url),
    )
