"""Learn a recipe for a page no source kind recognizes, and store it once confirmed.

`analyze_operation` reads the page (fetching it, or taking the one `add_url`
already fetched), asks the `source_recipe` model for a recipe, tries the recipe
on the very page it was learned from, and returns a `RecipeProposal` for the
user to confirm. Nothing is stored until `accept_recipe` is called. When the
page renders its jobs with JavaScript, the model may instead point at a JSON
document; the operation then fetches that document and asks once more, so one
analysis makes at most two model calls and shares one budget and one trace.
A failure after money was spent carries the cost as a note on the error
(`BaseException.add_note`), so the caller can show it beside the reason.

At module level this module imports only `ijob_core`, `ijob_sources` and
`ijob_app`: every `ijob_ai` import is inside the function that needs it, so a
search that learns nothing loads no `ijob_ai` module. The same holds for
`ijob_app.recipe_errors`, which imports `ijob_ai.errors`.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import urljoin, urlsplit

from ijob_core.model import SightingDraft, SourceRef
from ijob_core.store import Store
from ijob_sources.errors import (
    FetchedPage,
    InvalidRecipeError,
    RecipeEvaluationError,
    SourcesError,
    UnsupportedRecipeVersionError,
    UnsupportedUrlError,
)
from ijob_sources.polite_http import PoliteClient
from ijob_sources.recipe import parse_recipe, read_page, recipe_text
from ijob_sources.recipe_document import (
    Document,
    HtmlDocument,
    RecipeFormat,
    fetch_document,
    load_document,
    sample_text,
)

from ijob_app.llm_ops import (
    ItemOutcome,
    ItemStatus,
    LlmRuntime,
    OperationReport,
    cost_line,
    run_items,
)

if TYPE_CHECKING:
    from ijob_ai.llm.budget import Budget
    from ijob_ai.llm.reference import Provider
    from ijob_ai.tasks.source_recipe import ProposedRecipe

_OPERATION = "sources add"
_WEB_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True, kw_only=True)
class RecipeProposal:
    """A recipe the model learned, with what it read on the page it was learned on.

    Attributes:
        ref: The source the recipe would belong to.
        target_url: The page the user added.
        company: The company the source belongs to.
        recipe: The recipe as canonical JSON text, as it would be stored.
        recipe_format: The kind of document the recipe reads.
        recipe_url: The address the recipe reads: the added page, or the JSON
            document the model pointed at.
        jobs: The jobs the recipe read from its first page.
        invalid: How many entries the recipe selected that were not jobs.
        model: The reference of the model that learned the recipe.
        report: What the analysis did and spent.
    """

    ref: SourceRef
    target_url: str
    company: str
    recipe: str
    recipe_format: RecipeFormat
    recipe_url: str
    jobs: tuple[SightingDraft, ...]
    invalid: int
    model: str
    report: OperationReport


@dataclass(frozen=True, slots=True)
class _Page:
    """A fetched document and the address it finally came from."""

    document: Document
    url: str


@dataclass(frozen=True, slots=True)
class _Learned:
    """A recipe the model wrote, and the page it must be tried on.

    Attributes:
        recipe_json: The recipe as the model wrote it.
        page: The document the recipe reads.
        recipe_url: The address that becomes the recipe's `url`.
    """

    recipe_json: str
    page: _Page
    recipe_url: str


async def analyze_operation(  # noqa: PLR0913 - the operation's inputs, one each
    runtime: LlmRuntime,
    client: PoliteClient,
    *,
    ref: SourceRef,
    target_url: str,
    company: str,
    page: FetchedPage | None = None,
) -> RecipeProposal:
    """Learn a recipe for a page and try it on that page, without storing it.

    Every fetch goes through `client`, which the caller guards so that it only
    reaches public addresses. The page is fetched once unless `page` is given,
    and when the model points at a JSON document that is fetched once more.
    There are at most two model calls and no retry.

    Once the model run has finished, every `LlmError` or `SourcesError` that
    turning its outcome into a proposal raises carries the line `cost $X of $Y`
    as a note when the run spent anything. An error that ends the run itself,
    such as a cancellation, carries none.

    Args:
        runtime: The runtime holding the settings and the model registry.
        client: The open client every fetch goes through.
        ref: The source the recipe would belong to.
        target_url: The page the user added.
        company: The company the source belongs to.
        page: The page `add_url` already fetched, which is read in place of
            fetching `target_url`; its final address is where the jobs' links
            resolve from.

    Returns:
        The proposal for the user to confirm.

    Raises:
        MissingModelError: No model is set for the `source_recipe` task; nothing
            is fetched.
        UnsupportedUrlError: `target_url` is malformed; nothing is fetched.
        SourcesError: A page or the JSON document cannot be fetched, is not a
            public address, is on a reserved host, is over 5 MiB or is of
            another type than HTML or JSON, which also holds for a given
            `page`; or the JSON address serves HTML.
        RecipeOutputError: The model's answer or recipe is unusable, the JSON
            address it gave is malformed or not an http or https address, or the
            recipe fails when tried on the page.
        NoTrialJobsError: The recipe reads no job from the page.
        RecipeCapReachedError: The cost cap stopped the analysis before the
            first call, or was reached after it, before the JSON recipe.
        ProviderUnavailableError: The provider cannot be used.
        ProviderFailedError: The provider failed a call.
        CostUnknownError: The cost of a call cannot be determined.
    """
    from ijob_ai.errors import LlmError  # noqa: PLC0415
    from ijob_ai.llm.settings import Task  # noqa: PLC0415

    model = runtime.settings.model_for(Task.SOURCE_RECIPE)
    try:
        urlsplit(target_url)
    except ValueError as error:
        raise UnsupportedUrlError(target_url, str(error)) from error
    if page is None:
        document, final_url = await fetch_document(client, target_url)
    else:
        final_url = page.final_url
        document = load_document(page.body, page.content_type, final_url)
    # A fetch failure of the JSON address is a value of the worker, not an
    # exception: anything but an `LlmError` raised in a worker reaches the
    # caller wrapped in an exception group.
    run = await run_items(
        runtime,
        _OPERATION,
        (_Page(document, final_url),),
        _Learner(runtime, client, provider=model.provider, target_url=target_url),
    )
    try:
        return _proposal(
            run.outcomes[0],
            run.report,
            ref=ref,
            target_url=target_url,
            company=company,
            model=str(model),
        )
    except (LlmError, SourcesError) as error:
        if run.report.spent > 0:
            error.add_note(cost_line(run.report))
        raise


def _proposal(  # noqa: PLR0913 - the run and the facts of the proposal, one each
    outcome: ItemOutcome[_Page, _Learned | SourcesError],
    report: OperationReport,
    *,
    ref: SourceRef,
    target_url: str,
    company: str,
    model: str,
) -> RecipeProposal:
    """Turn the outcome of the run into a proposal, or raise why it has none.

    Raises:
        RecipeCapReachedError: The cap stopped the run without an error.
        RecipeOutputError: The model's answer or recipe is unusable.
        NoTrialJobsError: The recipe reads no job from the page.
        SourcesError: The JSON address could not be fetched or serves HTML.
        LlmError: The provider failed, or the cost is unknown.
    """
    from ijob_ai.errors import TaskOutputError  # noqa: PLC0415

    from ijob_app.recipe_errors import (  # noqa: PLC0415
        NoTrialJobsError,
        RecipeCapReachedError,
        RecipeOutputError,
    )

    if outcome.error is not None:
        if isinstance(outcome.error, TaskOutputError):
            raise RecipeOutputError(outcome.error.reason) from outcome.error
        raise outcome.error
    learned = outcome.result
    if outcome.status is not ItemStatus.DONE or learned is None:
        raise RecipeCapReachedError(started=False)
    if isinstance(learned, SourcesError):
        raise learned
    try:
        recipe = parse_recipe(_with_url(learned.recipe_json, learned.recipe_url))
        read = read_page(
            recipe,
            learned.page.document,
            page_url=learned.page.url,
            ref=ref,
            company=company,
        )
    except (
        InvalidRecipeError,
        UnsupportedRecipeVersionError,
        RecipeEvaluationError,
    ) as error:
        reason = (
            str(error)
            if isinstance(error, UnsupportedRecipeVersionError)
            else error.reason
        )
        raise RecipeOutputError(reason) from error
    if not read.drafts:
        raise NoTrialJobsError
    return RecipeProposal(
        ref=ref,
        target_url=target_url,
        company=company,
        recipe=recipe_text(recipe),
        recipe_format=recipe.format,
        recipe_url=recipe.url,
        jobs=tuple(read.drafts),
        invalid=read.rejected,
        model=model,
        report=report,
    )


def accept_recipe(store: Store, proposal: RecipeProposal, *, at: datetime) -> None:
    """Store a confirmed proposal as a source and its recipe.

    A source that was removed is restored, and a recipe already stored for the
    source is replaced.

    Args:
        store: The open store.
        proposal: The proposal the user confirmed.
        at: When the recipe is learned, timezone-aware.

    Raises:
        InvalidValueError: The recipe or the model reference is empty, or `at`
            has no timezone.
        sqlite3.Error: The store cannot be written.
    """
    store.set_recipe_source(
        proposal.ref,
        company=proposal.company,
        target_url=proposal.target_url,
        recipe=proposal.recipe,
        model=proposal.model,
        at=at,
    )


class _Learner:
    """The worker of one analysis: up to two model calls and one extra fetch."""

    def __init__(
        self,
        runtime: LlmRuntime,
        client: PoliteClient,
        *,
        provider: "Provider",
        target_url: str,
    ) -> None:
        self._runtime = runtime
        self._client = client
        self._provider = provider
        self._target_url = target_url

    async def __call__(self, page: _Page, budget: "Budget") -> _Learned | SourcesError:
        """Ask for a recipe, and for a second one when the model points at JSON.

        Returns:
            The recipe and the page to try it on, or the `SourcesError` of
            fetching the JSON address, which a worker must return rather than
            raise.

        Raises:
            RecipeCapReachedError: The cap was reached after the first call and
                the model asked for a second.
            RecipeOutputError: The JSON address the model gave is malformed or
                not an http or https address.
            LlmError: A call fails or its answer is unusable.
        """
        from ijob_app.recipe_errors import (  # noqa: PLC0415
            RecipeCapReachedError,
            RecipeOutputError,
        )

        first = await self._propose(page, budget)
        if first.recipe_json is not None:
            return _Learned(first.recipe_json, page, self._target_url)
        try:
            json_url = urljoin(page.url, _required(first.json_url))
            parts = urlsplit(json_url)
        except ValueError as error:
            reason = f"the JSON address cannot be read: {error}"
            raise RecipeOutputError(reason) from error
        if parts.scheme not in _WEB_SCHEMES or not parts.hostname:
            reason = f"the JSON address is not an http or https address: {json_url}"
            raise RecipeOutputError(reason)
        try:
            document, final_url = await fetch_document(self._client, json_url)
        except SourcesError as error:
            return error
        if isinstance(document, HtmlDocument):
            return UnsupportedUrlError(json_url, "expected JSON")
        if not budget.try_start():
            raise RecipeCapReachedError
        json_page = _Page(document, final_url)
        second = await self._propose(json_page, budget)
        return _Learned(_required(second.recipe_json), json_page, json_url)

    async def _propose(self, page: _Page, budget: "Budget") -> "ProposedRecipe":
        """Make one call on a page."""
        from ijob_ai.llm.settings import Task  # noqa: PLC0415
        from ijob_ai.tasks.source_recipe import propose_recipe  # noqa: PLC0415

        registry = await self._runtime.registry_for(Task.SOURCE_RECIPE)
        return await propose_recipe(
            page.url,
            _format_of(page.document),
            sample_text(page.document),
            lm=registry.lm_for(Task.SOURCE_RECIPE),
            provider=self._provider,
            budget=budget,
            timeout_s=self._runtime.settings.timeout_s,
        )


def _format_of(document: Document) -> Literal["html", "json"]:
    """Return the name of the recipe format that reads a document."""
    if isinstance(document, HtmlDocument):
        return "html"
    return "json"


def _required(value: str | None) -> str:
    """Return the half of a proposal that `propose_recipe` guarantees is set."""
    if value is None:
        msg = "propose_recipe returned a proposal without the expected field"
        raise RuntimeError(msg)
    return value


def _with_url(recipe_json: str, url: str) -> str:
    """Set the `url` of a recipe's JSON object to the address ijob fetched."""
    document: dict[str, object] = json.loads(recipe_json)
    document["url"] = url
    return json.dumps(document)
