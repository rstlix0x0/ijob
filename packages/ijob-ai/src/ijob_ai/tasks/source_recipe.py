"""The source recipe task: propose a recipe, or a JSON address, from one page sample.

The page sample is third-party text and goes only in the `sample` input field.
The answer is checked for shape and for agreeing with the sample's format;
whether the recipe is valid is left to the recipe parser.
"""

from dataclasses import dataclass
from typing import Final, Literal

import dspy

from ijob_ai.errors import TaskOutputError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import SourceRecipeOut
from ijob_ai.tasks.signatures import SOURCE_RECIPE_VERSION, SourceRecipeSig
from ijob_ai.tasks.text import sanitize

RECIPE_FIELDS_TEXT: Final = """\
version: always 1.
format: "html" (XPath 1.0) or "json" (JMESPath), the sample's format.
url: the address of the page the recipe reads.
items: expression on the whole document that selects each job.
fields: expressions that read one job out of an item.
  title: the job title (required).
  url: the job's address, absolute or relative to the page (required).
  company: the hiring company.
  location: where the job is.
  posted_at: when the job was published, as ISO 8601 text.
  description: the job's description, as HTML.
  salary: the pay, as free text.
  external_id: the job's identifier within the source.
salary_defaults: what a salary text falls back to when it does not say.
  currency: a currency code.
  period: hour, month or year.
next_page: expression on the whole document that gives the next page's address.
detail_description: for an html recipe, the XPath on a job's own page that gives \
its description.
"""
"""The recipe's keys and what each holds, passed as the `fields_text` input."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposedRecipe:
    """What the model proposed for a page; exactly one field is set.

    Attributes:
        recipe_json: The recipe as JSON text, for the recipe parser.
        json_url: The address of a JSON document that holds the jobs.
    """

    recipe_json: str | None = None
    json_url: str | None = None


def _proposal(
    answer: SourceRecipeOut, page_format: Literal["html", "json"]
) -> ProposedRecipe:
    """Build the proposal from an answer, or raise `TaskOutputError`."""
    task = Task.SOURCE_RECIPE.value
    if answer.recipe is not None:
        if answer.recipe.format != page_format:
            msg = f"the recipe is {answer.recipe.format} for a {page_format} sample"
            raise TaskOutputError(task, msg)
        return ProposedRecipe(
            recipe_json=answer.recipe.model_dump_json(exclude_none=True)
        )
    if page_format != "html":
        msg = f"a JSON address was given for a {page_format} sample"
        raise TaskOutputError(task, msg)
    json_url = sanitize(answer.json_url or "")
    if not json_url:
        raise TaskOutputError(task, "the JSON address is empty")
    return ProposedRecipe(json_url=json_url)


async def propose_recipe(  # noqa: PLR0913  # the call's whole contract, one argument each
    page_url: str,
    page_format: Literal["html", "json"],
    sample: str,
    *,
    lm: dspy.LM,
    provider: Provider,
    budget: Budget,
    timeout_s: float,
) -> ProposedRecipe:
    """Ask the model for a recipe, or a JSON address, from a page sample.

    The sample is cleaned of control characters and is the only text of the
    page in the call. The caller checks `Budget.try_start` first.

    Args:
        page_url: The address the sample was fetched from.
        page_format: The sample's format, `html` or `json`.
        sample: A sample of the page.
        lm: The source recipe task's LM.
        provider: The provider serving `lm`.
        budget: The operation's budget, charged with the call's cost.
        timeout_s: Seconds the call may take.

    Returns:
        A recipe as JSON text without null keys, or a cleaned JSON address.

    Raises:
        TaskOutputError: If a recipe's format differs from `page_format`, a JSON
            address is given for a `json` sample, the JSON address is empty once
            cleaned, or the answer sets both a recipe and a JSON address or
            neither.
        LlmError: Any error `run_call` raises.
    """
    prediction = await run_call(
        dspy.Predict(SourceRecipeSig),
        lm,
        inputs={
            "page_url": page_url,
            "page_format": page_format,
            "sample": sanitize(sample),
            "fields_text": RECIPE_FIELDS_TEXT,
        },
        provider=provider,
        budget=budget,
        timeout_s=timeout_s,
        task=Task.SOURCE_RECIPE,
        task_version=SOURCE_RECIPE_VERSION,
        posting_id=None,
    )
    return _proposal(prediction.answer, page_format)
