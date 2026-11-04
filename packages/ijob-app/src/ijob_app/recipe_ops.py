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

`learn_recipe` is the learning step on its own: it takes the caller's budget
and client and runs no operation, so a caller with a run of its own can charge
the learning to it. `analyze_operation` runs it as the worker of its one item.

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

    from ijob_app.recipe_errors import RecipeCapReachedError

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
class LoadedPage:
    """A fetched document and the address it finally came from.

    Attributes:
        document: The page, loaded for reading.
        url: The address the page was served from, where its links resolve from.
    """

    document: Document
    url: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LearnedRecipe:
    """A recipe the model wrote, tried on the page it was learned from.

    Attributes:
        recipe: The recipe as canonical JSON text, as it would be stored.
        recipe_format: The kind of document the recipe reads.
        recipe_url: The address the recipe reads: the page learned from, or the
            JSON document the model pointed at.
        jobs: The jobs the recipe read from its first page.
        invalid: How many entries the recipe selected that were not jobs.
        model: The reference of the model that learned the recipe.
    """

    recipe: str
    recipe_format: RecipeFormat
    recipe_url: str
    jobs: tuple[SightingDraft, ...]
    invalid: int
    model: str


@dataclass(frozen=True, slots=True)
class _Proposed:
    """A recipe the model wrote, and the page it must be tried on.

    Attributes:
        recipe_json: The recipe as the model wrote it.
        page: The document the recipe reads.
        recipe_url: The address that becomes the recipe's `url`.
    """

    recipe_json: str
    page: LoadedPage
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

    # Fails before any fetch when no model is set; `learn_recipe` reads it again.
    runtime.settings.model_for(Task.SOURCE_RECIPE)
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
        (LoadedPage(document, final_url),),
        _Learner(runtime, client, ref=ref, target_url=target_url, company=company),
    )
    try:
        return _proposal(
            run.outcomes[0],
            run.report,
            ref=ref,
            target_url=target_url,
            company=company,
        )
    except (LlmError, SourcesError) as error:
        if run.report.spent > 0:
            error.add_note(cost_line(run.report))
        raise


def _proposal(
    outcome: ItemOutcome[
        LoadedPage, "LearnedRecipe | SourcesError | RecipeCapReachedError"
    ],
    report: OperationReport,
    *,
    ref: SourceRef,
    target_url: str,
    company: str,
) -> RecipeProposal:
    """Turn the outcome of the run into a proposal, or raise why it has none.

    Raises:
        RecipeCapReachedError: The cap stopped the run, with or without an error.
        RecipeOutputError: The model's answer or recipe is unusable.
        NoTrialJobsError: The recipe reads no job from the page.
        SourcesError: The JSON address could not be fetched or serves HTML.
        LlmError: The provider failed, or the cost is unknown.
    """
    from ijob_ai.errors import TaskOutputError  # noqa: PLC0415

    from ijob_app.recipe_errors import (  # noqa: PLC0415
        RecipeCapReachedError,
        RecipeOutputError,
    )

    if outcome.error is not None:
        if isinstance(outcome.error, TaskOutputError) and not isinstance(
            outcome.error, RecipeOutputError
        ):
            raise RecipeOutputError(outcome.error.reason) from outcome.error
        raise outcome.error
    learned = outcome.result
    if outcome.status is not ItemStatus.DONE or learned is None:
        raise RecipeCapReachedError(started=False)
    if isinstance(learned, SourcesError | RecipeCapReachedError):
        raise learned
    return RecipeProposal(
        ref=ref,
        target_url=target_url,
        company=company,
        recipe=learned.recipe,
        recipe_format=learned.recipe_format,
        recipe_url=learned.recipe_url,
        jobs=learned.jobs,
        invalid=learned.invalid,
        model=learned.model,
        report=report,
    )


async def learn_recipe(  # noqa: PLR0913 - the learning's inputs, one each
    runtime: LlmRuntime,
    client: PoliteClient,
    page: LoadedPage,
    budget: "Budget",
    *,
    ref: SourceRef,
    target_url: str,
    company: str,
) -> LearnedRecipe | SourcesError:
    """Learn a recipe for a page and try it on that page, on the caller's budget.

    Makes up to two model calls, charged to `budget`, and one fetch through
    `client` when the model points at a JSON document. It starts no operation
    and no trace of its own, and it checks `budget` before every call, the
    first included. The caller guards `client` so that it only reaches the
    addresses it may.

    Args:
        runtime: The runtime holding the settings and the model registry.
        client: The open client the JSON document is fetched through.
        page: The page to learn from.
        budget: The budget every call is charged to.
        ref: The source the recipe would belong to.
        target_url: The page the user added; it becomes the `url` of a recipe
            that reads `page`.
        company: The company the source belongs to.

    Returns:
        The recipe and the jobs it read, or the `SourcesError` of fetching the
        JSON address or of that address serving HTML, which is returned rather
        than raised so that a worker can hand it back as a value.

    Raises:
        MissingModelError: No model is set for the `source_recipe` task; nothing
            is called.
        RecipeCapReachedError: `budget` had no room before the first call, or
            before the JSON recipe.
        RecipeOutputError: The model's answer or recipe is unusable, the JSON
            address it gave is malformed or not an http or https address, or the
            recipe fails when tried on the page.
        NoTrialJobsError: The recipe reads no job from the page.
        ProviderUnavailableError: The provider cannot be used.
        ProviderFailedError: The provider failed a call.
        CostUnknownError: The cost of a call cannot be determined.
    """
    from ijob_ai.llm.settings import Task  # noqa: PLC0415

    from ijob_app.recipe_errors import (  # noqa: PLC0415
        RecipeCapReachedError,
        RecipeOutputError,
    )

    model = runtime.settings.model_for(Task.SOURCE_RECIPE)
    if not budget.try_start():
        raise RecipeCapReachedError(started=False)
    first = await _propose(runtime, model.provider, page, budget)
    if first.recipe_json is not None:
        proposed = _Proposed(first.recipe_json, page, target_url)
    else:
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
            document, final_url = await fetch_document(client, json_url)
        except SourcesError as error:
            return error
        if isinstance(document, HtmlDocument):
            return UnsupportedUrlError(json_url, "expected JSON")
        if not budget.try_start():
            raise RecipeCapReachedError
        json_page = LoadedPage(document, final_url)
        second = await _propose(runtime, model.provider, json_page, budget)
        proposed = _Proposed(_required(second.recipe_json), json_page, json_url)
    return _try_recipe(proposed, ref=ref, company=company, model=str(model))


def _try_recipe(
    proposed: _Proposed, *, ref: SourceRef, company: str, model: str
) -> LearnedRecipe:
    """Read the page the recipe was learned from with the recipe.

    Raises:
        RecipeOutputError: The recipe is invalid or fails on the page.
        NoTrialJobsError: The recipe reads no job from the page.
    """
    from ijob_app.recipe_errors import (  # noqa: PLC0415
        NoTrialJobsError,
        RecipeOutputError,
    )

    try:
        recipe = parse_recipe(_with_url(proposed.recipe_json, proposed.recipe_url))
        read = read_page(
            recipe,
            proposed.page.document,
            page_url=proposed.page.url,
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
    return LearnedRecipe(
        recipe=recipe_text(recipe),
        recipe_format=recipe.format,
        recipe_url=recipe.url,
        jobs=tuple(read.drafts),
        invalid=read.rejected,
        model=model,
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
    """The worker of one analysis: `learn_recipe` on the operation's budget."""

    def __init__(
        self,
        runtime: LlmRuntime,
        client: PoliteClient,
        *,
        ref: SourceRef,
        target_url: str,
        company: str,
    ) -> None:
        self._runtime = runtime
        self._client = client
        self._ref = ref
        self._target_url = target_url
        self._company = company

    async def __call__(
        self, page: LoadedPage, budget: "Budget"
    ) -> "LearnedRecipe | SourcesError | RecipeCapReachedError":
        """Learn a recipe for the page.

        Returns:
            What `learn_recipe` returns, or the cap error it raised: `run_items`
            records a worker's cap error as a stop and drops it, and with it the
            line saying which call the cap kept from being made.

        Raises:
            RecipeOutputError: The model's answer or recipe is unusable.
            NoTrialJobsError: The recipe reads no job from the page.
            LlmError: A call fails or its answer is unusable.
        """
        from ijob_app.recipe_errors import RecipeCapReachedError  # noqa: PLC0415

        try:
            return await learn_recipe(
                self._runtime,
                self._client,
                page,
                budget,
                ref=self._ref,
                target_url=self._target_url,
                company=self._company,
            )
        except RecipeCapReachedError as error:
            return error


async def _propose(
    runtime: LlmRuntime, provider: "Provider", page: LoadedPage, budget: "Budget"
) -> "ProposedRecipe":
    """Make one call on a page."""
    from ijob_ai.llm.settings import Task  # noqa: PLC0415
    from ijob_ai.tasks.source_recipe import propose_recipe  # noqa: PLC0415

    registry = await runtime.registry_for(Task.SOURCE_RECIPE)
    return await propose_recipe(
        page.url,
        _format_of(page.document),
        sample_text(page.document),
        lm=registry.lm_for(Task.SOURCE_RECIPE),
        provider=provider,
        budget=budget,
        timeout_s=runtime.settings.timeout_s,
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
