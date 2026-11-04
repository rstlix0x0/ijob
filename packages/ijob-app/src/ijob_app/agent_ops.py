"""Run the source agent and the explore agent.

The source agent finds the one source that lists a company's openings; the
explore agent carries out a free-text instruction by running the source agent
for companies and proposing what it verified. Both are written the same way:
a bare run that opens no budget, no `run_items` and no trace, and an operation
that wraps it.

`run_source_agent` is one agent run. It takes the budget, the clients and the
model from its caller and opens no budget, no `run_items` and no trace, so
`ijob explore`, a suite of evaluation cases and `find_operation` can all call
it on their own, and the spans of its model calls and tool calls sit under
whatever operation trace the caller is in. `find_operation` is the whole
operation behind `ijob sources find`: one such run under one `run_items`,
capped by `[llm].max_cost_usd`.

`run_explore_agent` and `explore_operation` are the same pair for `ijob
explore`. Every source-agent run the explore agent starts shares the explore
run's budget, and the spans of all of them sit directly under the one
`ijob explore` trace. A run that a cap, a provider or cost failure, or an
unusable answer ends early is returned, not raised, and what it proposed
stands.

An answer counts only when a tool of the run verified the source it names. An
answer that names a source no tool verified, as `<kind>:<key>` or as a web
address, is thrown away and counted as a rule violation, noted on a span of its
own; any other answer, `none` or a sentence, names no source and is no
violation. A run that a cap, a provider or cost failure, or an unusable
answer ends early is returned, not raised, with the reason in `stop` and the
most recent source a tool verified as `found`. `add_found` stores what was
found.

At module level this module imports no `ijob_ai` module: the agent and its
tools load only when a run is about to start, so a command that finds nothing
stays fast.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Protocol

from ijob_core.store import SourceRow, Store
from ijob_sources.polite_http import PoliteClient

from ijob_app.agent_site import SiteRule
from ijob_app.llm_ops import LlmRuntime, OperationReport, StopReason, run_items
from ijob_app.recipe_ops import RecipeProposal

if TYPE_CHECKING:
    from ijob_ai.agents.react import AgentStep, StepCallback
    from ijob_ai.llm.budget import Budget
    from ijob_ai.llm.reference import ModelRef, Provider
    from ijob_core.model import SourceRef

    from ijob_app.agent_tools import Proposal, SourceAttempt, VerifiedSource

type PageClientFor = Callable[[SiteRule], PoliteClient]
"""Builds the client one run reads pages through, given the run's site rule.

The client must carry the public-host guard and `site_guard(rule)`. The run
closes it when it ends.
"""

SOURCE_MAX_STEPS: Final = 8
"""How many tool steps the source agent may take."""

EXPLORE_MAX_STEPS: Final = 20
"""How many tool steps the explore agent may take."""

_OPERATION: Final = "sources find"
_EXPLORE_OPERATION: Final = "explore"
_ANSWER_CHARS: Final = 100


@dataclass(frozen=True, slots=True, kw_only=True)
class FoundSource:
    """A source a tool verified, offered for the user to add.

    Attributes:
        ref: The source.
        company: The company it belongs to, as the run was given it.
        target_url: The address a feed, job page or recipe source is read from,
            or `None` for a hiring-system board.
        jobs: How many jobs the check read.
        proposal: The learned recipe, for a recipe source, otherwise `None`.
        known: Whether the user already has this source. `find_operation` sets
            it; a bare run never does.
        sample: A few job titles the check read, third-party text to draw
            literally.
    """

    ref: "SourceRef"
    company: str
    target_url: str | None
    jobs: int
    proposal: RecipeProposal | None = None
    known: bool = False
    sample: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceResult:
    """How one source-agent run ended.

    Attributes:
        verified: Every source a tool verified in this run, most recent last.
        violations: This run's rule violations, including a final answer that
            no tool verified and was thrown away.
        stop: What ended the run early: a reached cap, a provider or cost
            failure, or an unusable answer. `None` for a run that finished.
        found: The source offered: the verified source the answer names, or
            the most recent verified source after a stop. `None` when the
            answer was `none` or thrown away, or nothing was verified.
        steps: Every finished tool call, in order.
        could_not_check: One line per board a tool could not check.
    """

    verified: "Sequence[VerifiedSource]"
    violations: Sequence[str]
    stop: Exception | None
    found: FoundSource | None
    steps: "Sequence[AgentStep]" = ()
    could_not_check: Sequence[str] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ExploreResult:
    """How one explore-agent run ended.

    Attributes:
        proposals: The sources the agent put forward, in order. They stand
            however the run ended.
        known: The sources the user already has that the agent tried to
            propose, once each, in order. They are not in `proposals`.
        no_source: The companies a source-agent run ended without a source
            for, once each, in order.
        violations: The rule violations of the run, including those of the
            source-agent runs inside it.
        stop: What ended the run early: a reached cap, a provider or cost
            failure, or an unusable answer. `None` for a run that finished.
        steps: Every finished tool call of the explore agent itself, in
            order. The steps of the source-agent runs inside it are not
            listed.
        refused: How many `propose` calls named something no tool verified in
            the run; each is also one of the violations.
    """

    proposals: "Sequence[Proposal]"
    known: "Sequence[SourceRef]"
    no_source: Sequence[str]
    violations: Sequence[str]
    stop: Exception | None
    steps: "Sequence[AgentStep]"
    refused: int = 0


class FoundTarget(Protocol):
    """Where `add_found` stores a source: the three ways the service adds one."""

    async def add_source(self, ref: "SourceRef", company: str) -> SourceRow:
        """Verify a hiring-system board and add it."""
        ...

    async def add_url(self, url: str, company: str) -> SourceRow:
        """Work out what a URL is, verify it and add it."""
        ...

    def accept_recipe(self, proposal: RecipeProposal) -> SourceRow:
        """Store a learned recipe as a source."""
        ...


async def run_source_agent(  # noqa: PLR0913, PLR0917  # the run's whole context
    runtime: LlmRuntime,
    company: str,
    url: str | None,
    model: "ModelRef",
    budget_for: "Callable[[Provider], Budget]",
    public_client: PoliteClient,
    page_client_for: PageClientFor,
    on_step: "StepCallback | None" = None,
) -> SourceResult:
    """Run the source agent once for a company, within eight tool steps.

    Opens no budget, no `run_items` and no trace: every model call is charged
    to the budget `budget_for` gives and every span is opened under the trace
    the caller is in, if any.

    Args:
        runtime: The runtime holding the settings and the model registry.
        company: The company to find a source for; a board or page must name it.
        url: The company's careers address, which fixes the site the agent may
            read, or `None` to start from the name alone.
        model: The model the agent runs on.
        budget_for: Picks the budget a call is charged to, given the provider
            serving it: the agent's own model and the `source_recipe` model
            when `learn_page` runs.
        public_client: The client boards are checked through. The caller owns
            and closes it.
        page_client_for: Builds the client the run reads pages through, given
            the run's site rule. The run closes it.
        on_step: Called with the finished step after each tool call. An error
            it raises ends the run at the next model call.

    Returns:
        The result. A cap, a provider or cost failure and an unusable answer
        end the run early and are returned as `stop`, not raised.

    Raises:
        Exception: An error `on_step` raised.
    """
    from ijob_ai.agents.react import AgentRun, GuardedReAct  # noqa: PLC0415
    from ijob_ai.errors import (  # noqa: PLC0415
        CapReachedError,
        CostUnknownError,
        ProviderFailedError,
        ProviderUnavailableError,
        TaskOutputError,
    )
    from ijob_ai.llm.settings import Task  # noqa: PLC0415
    from ijob_ai.tasks.signatures import (  # noqa: PLC0415
        SOURCE_AGENT_VERSION,
        SourceAgentSig,
    )

    from ijob_app.agent_tools import SourceTools  # noqa: PLC0415

    rule = SiteRule(url)
    run = AgentRun(on_step=on_step)
    stop: Exception | None = None
    answer = ""
    async with page_client_for(rule) as page_client:
        tools = SourceTools(
            company=company,
            rule=rule,
            run=run,
            public_client=public_client,
            page_client=page_client,
            runtime=runtime,
            budget_for=budget_for,
        )
        try:
            registry = await runtime.registry_for_ref(model)
            agent = GuardedReAct(
                SourceAgentSig,
                [tools.try_board, tools.open_page, tools.learn_page],
                max_iters=SOURCE_MAX_STEPS,
                lm=registry.lm_for_ref(model),
                provider=model.provider,
                budget=budget_for(model.provider),
                timeout_s=runtime.settings.timeout_s,
                task=Task.SOURCE_AGENT,
                task_version=SOURCE_AGENT_VERSION,
                run=run,
            )
            prediction = await agent.acall(company=company, careers_url=url or "")
            answer = str(prediction.source)
        except (
            CapReachedError,
            ProviderFailedError,
            ProviderUnavailableError,
            CostUnknownError,
            TaskOutputError,
        ) as error:
            stop = error
    found = _offered(company, tools.verified, stop, answer)
    if stop is None and found is None:
        _note_unverified_answer(run.violations, answer)
    return SourceResult(
        verified=list(tools.verified),
        violations=list(run.violations),
        stop=stop,
        found=found,
        steps=list(run.steps),
        could_not_check=list(tools.could_not_check),
    )


async def find_operation(  # noqa: PLR0913  # the operation's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    company: str,
    url: str | None,
    *,
    model: "ModelRef | None",
    public_client: PoliteClient,
    page_client_for: PageClientFor,
    on_step: "StepCallback | None" = None,
    on_cost: Callable[[float], None] | None = None,
) -> tuple[SourceResult, OperationReport]:
    """Find the source of a company in one operation under `[llm].max_cost_usd`.

    One `run_source_agent` under one `run_items`, traced as `ijob sources find`.
    The report names why the run stopped early when a cap, a provider failure
    or an unknown cost ended it; an unusable answer leaves `stopped_by` unset
    and shows only in the result's `stop`. A source the user already has is
    flagged `known`.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store, asked which sources the user already has.
        company: The company to find a source for.
        url: The company's careers address, or `None` to start from the name.
        model: The model that replaces `[models].source_agent` for this run, or
            `None` to use the configured one.
        public_client: The client boards are checked through. The caller owns
            and closes it.
        page_client_for: Builds the client the run reads pages through.
        on_step: Called with the finished step after each tool call.
        on_cost: Called with the money the operation's budget has spent so
            far, in US dollars, just before each `on_step` call, and also when
            `on_step` is `None`. An error it raises ends the run like one
            `on_step` raises.

    Returns:
        The result and what the operation did and spent.

    Raises:
        MissingModelError: `model` is `None` and no model is set for the
            `source_agent` task; nothing is called.
        ExceptionGroup: The run raised an error other than an `LlmError`, such
            as one `on_step` raised.
        sqlite3.Error: The store cannot be read.
    """
    from ijob_ai.errors import CapReachedError  # noqa: PLC0415
    from ijob_ai.llm.settings import Task  # noqa: PLC0415

    chosen = (
        model if model is not None else runtime.settings.model_for(Task.SOURCE_AGENT)
    )

    async def work(item: tuple[str, str | None], budget: "Budget") -> SourceResult:
        name, address = item
        return await run_source_agent(
            runtime,
            name,
            address,
            chosen,
            lambda _provider: budget,
            public_client,
            page_client_for,
            _with_cost(on_step, on_cost, budget),
        )

    run = await run_items(runtime, _OPERATION, [(company, url)], work)
    [outcome] = run.outcomes
    result = outcome.result
    if result is None:
        if outcome.error is not None:
            raise outcome.error
        # The budget had no room to start the run at all.
        result = SourceResult(
            verified=[], violations=[], stop=CapReachedError(), found=None
        )
    if result.found is not None:
        row = store.source(result.found.ref)
        known = row is not None and not row.removed
        result = replace(result, found=replace(result.found, known=known))
    return result, replace(run.report, stopped_by=_stopped_by(result.stop))


async def run_explore_agent(  # noqa: PLR0913, PLR0917  # the run's whole context
    runtime: LlmRuntime,
    instruction: str,
    model: "ModelRef",
    budget_for: "Callable[[Provider], Budget]",
    store: Store,
    public_client: PoliteClient,
    page_client_for: PageClientFor,
    on_step: "StepCallback | None" = None,
) -> ExploreResult:
    """Run the explore agent once for an instruction, within twenty tool steps.

    Opens no budget, no `run_items` and no trace: every model call, the
    agent's own and those of the source-agent runs inside it, is charged to
    the budget `budget_for` gives and every span is opened under the trace the
    caller is in, if any. Each source-agent run uses the `[models].source_agent`
    model, or `model` when none is set.

    Args:
        runtime: The runtime holding the settings and the model registry.
        instruction: The user's own request, given to the agent as it is.
        model: The model the explore agent runs on.
        budget_for: Picks the budget a call is charged to, given the provider
            serving it.
        store: The open store, read for the user's sources and for the
            companies of stored postings.
        public_client: The client boards are checked through. The caller owns
            and closes it.
        page_client_for: Builds the client a source-agent run reads pages
            through, given its site rule. Each run closes its own.
        on_step: Called with the finished step after each tool call of the
            explore agent, once per call. An error it raises ends the run at
            the next model call.

    Returns:
        The result. A cap, a provider or cost failure and an unusable answer
        end the run early and are returned as `stop`, not raised.

    Raises:
        sqlite3.Error: The store cannot be read.
    """
    from ijob_ai.agents.react import AgentRun, GuardedReAct  # noqa: PLC0415
    from ijob_ai.errors import (  # noqa: PLC0415
        CapReachedError,
        CostUnknownError,
        MissingModelError,
        ProviderFailedError,
        ProviderUnavailableError,
        TaskOutputError,
    )
    from ijob_ai.llm.settings import Task  # noqa: PLC0415
    from ijob_ai.tasks.signatures import (  # noqa: PLC0415
        EXPLORE_AGENT_VERSION,
        ExploreAgentSig,
    )

    from ijob_app.agent_tools import ExploreTools  # noqa: PLC0415

    try:
        source_model = runtime.settings.model_for(Task.SOURCE_AGENT)
    except MissingModelError:
        source_model = model

    async def find_source(company: str) -> "SourceAttempt":
        return await run_source_agent(
            runtime,
            company,
            None,
            source_model,
            budget_for,
            public_client,
            page_client_for,
        )

    run = AgentRun(on_step=on_step)
    tools = ExploreTools(store=store, run=run, find_source=find_source)
    stop: Exception | None = None
    try:
        registry = await runtime.registry_for_ref(model)
        agent = GuardedReAct(
            ExploreAgentSig,
            [
                tools.list_sources,
                tools.companies_in_store,
                tools.find_source,
                tools.propose,
            ],
            max_iters=EXPLORE_MAX_STEPS,
            lm=registry.lm_for_ref(model),
            provider=model.provider,
            budget=budget_for(model.provider),
            timeout_s=runtime.settings.timeout_s,
            task=Task.EXPLORE_AGENT,
            task_version=EXPLORE_AGENT_VERSION,
            run=run,
        )
        await agent.acall(instruction_text=instruction)
    except (
        CapReachedError,
        ProviderFailedError,
        ProviderUnavailableError,
        CostUnknownError,
        TaskOutputError,
    ) as error:
        stop = error
    return ExploreResult(
        proposals=list(tools.proposals),
        known=list(tools.known),
        no_source=list(tools.no_source),
        violations=list(run.violations),
        stop=stop,
        steps=list(run.steps),
        refused=tools.refused,
    )


async def explore_operation(  # noqa: PLR0913  # the operation's inputs, one each
    runtime: LlmRuntime,
    store: Store,
    instruction: str,
    *,
    model: "ModelRef | None",
    public_client: PoliteClient,
    page_client_for: PageClientFor,
    on_step: "StepCallback | None" = None,
    on_cost: Callable[[float], None] | None = None,
) -> tuple[ExploreResult, OperationReport]:
    """Carry out an instruction in one operation under `[llm].max_cost_usd`.

    One `run_explore_agent` under one `run_items`, traced as `ijob explore`.
    The report names why the run stopped early when a cap, a provider failure
    or an unknown cost ended it; an unusable answer leaves `stopped_by` unset
    and shows only in the result's `stop`.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        instruction: The user's own request.
        model: The model that replaces `[models].explore_agent` for this run,
            or `None` to use the configured one.
        public_client: The client boards are checked through. The caller owns
            and closes it.
        page_client_for: Builds the client a source-agent run reads pages
            through.
        on_step: Called with the finished step after each tool call of the
            explore agent.
        on_cost: Called with the money the operation's budget has spent so
            far, in US dollars, source-agent runs inside the explore run
            included, just before each `on_step` call, and also when `on_step`
            is `None`. An error it raises ends the run like one `on_step`
            raises.

    Returns:
        The result and what the operation did and spent.

    Raises:
        MissingModelError: `model` is `None` and no model is set for the
            `explore_agent` task; nothing is called.
        ExceptionGroup: The run raised an error other than an `LlmError`, such
            as one `on_step` raised.
        sqlite3.Error: The store cannot be read.
    """
    from ijob_ai.errors import CapReachedError  # noqa: PLC0415
    from ijob_ai.llm.settings import Task  # noqa: PLC0415

    chosen = (
        model if model is not None else runtime.settings.model_for(Task.EXPLORE_AGENT)
    )

    async def work(text: str, budget: "Budget") -> ExploreResult:
        return await run_explore_agent(
            runtime,
            text,
            chosen,
            lambda _provider: budget,
            store,
            public_client,
            page_client_for,
            _with_cost(on_step, on_cost, budget),
        )

    run = await run_items(runtime, _EXPLORE_OPERATION, [instruction], work)
    [outcome] = run.outcomes
    result = outcome.result
    if result is None:
        if outcome.error is not None:
            raise outcome.error
        # The budget had no room to start the run at all.
        result = ExploreResult(
            proposals=[],
            known=[],
            no_source=[],
            violations=[],
            stop=CapReachedError(),
            steps=[],
        )
    return result, replace(run.report, stopped_by=_stopped_by(result.stop))


def found_of(proposal: "Proposal") -> FoundSource:
    """Turn a proposal into the source `add_found` stores.

    Args:
        proposal: A proposal of an explore run.

    Returns:
        The source, for the company the proposal was found for. It is not
        flagged `known`, since a known source is never proposed.
    """
    source = proposal.source
    return FoundSource(
        ref=source.ref,
        company=proposal.company,
        target_url=source.target_url,
        jobs=source.jobs,
        proposal=source.proposal,
        sample=source.sample,
    )


async def add_found(target: FoundTarget, found: FoundSource) -> SourceRow:
    """Store a found source the way its kind is stored.

    A recipe goes through `accept_recipe`, a board through `add_source`, and a
    feed or job page through `add_url` with the address it was read from.

    Args:
        target: Stores the source; the `Service`.
        found: The source the user confirmed.

    Returns:
        The source's row.

    Raises:
        Exception: Whatever the chosen way of adding raises; see `Service`.
    """
    if found.proposal is not None:
        return target.accept_recipe(found.proposal)
    if found.target_url is None:
        return await target.add_source(found.ref, found.company)
    return await target.add_url(found.target_url, found.company)


def _with_cost(
    on_step: "StepCallback | None",
    on_cost: Callable[[float], None] | None,
    budget: "Budget",
) -> "StepCallback | None":
    """Make a step callback that reports the budget's spend just before each step."""
    if on_cost is None:
        return on_step

    def step(finished: "AgentStep") -> None:
        on_cost(budget.spent)
        if on_step is not None:
            on_step(finished)

    return step


def _offered(
    company: str,
    verified: "Sequence[VerifiedSource]",
    stop: Exception | None,
    answer: str,
) -> FoundSource | None:
    """Pick the source to offer: the answer's, or the latest after a stop."""
    from ijob_app.agent_tools import read_source_ref  # noqa: PLC0415

    chosen: VerifiedSource | None = None
    if stop is not None:
        chosen = verified[-1] if verified else None
    else:
        named = read_source_ref(answer)
        chosen = next((v for v in verified if v.ref == named), None)
    if chosen is None:
        return None
    return FoundSource(
        ref=chosen.ref,
        company=company,
        target_url=chosen.target_url,
        jobs=chosen.jobs,
        proposal=chosen.proposal,
        sample=chosen.sample,
    )


def _note_unverified_answer(violations: list[str], answer: str) -> None:
    """Count a final answer that names a source no tool verified as a violation.

    Inside an operation trace the violation is also set on a span under the
    trace's root, beside the spans of the tool calls. An answer that names no
    source, such as `none` or a sentence, is neither counted nor recorded.
    """
    from ijob_ai.tracing import attributes  # noqa: PLC0415
    from ijob_ai.tracing.setup import current_operation_trace  # noqa: PLC0415

    from ijob_app.agent_tools import names_a_source  # noqa: PLC0415

    if not names_a_source(answer):
        return
    violation = f"answer: {_quote(answer)} was not verified in this run"
    violations.append(violation)
    trace = current_operation_trace()
    if trace is not None:
        with trace.child("source answer") as span:
            span.set_attribute(attributes.TOOL_VIOLATION, violation)


def _quote(answer: str) -> str:
    """Quote a model's answer as one short escaped line."""
    return repr(" ".join(answer.split())[:_ANSWER_CHARS])


def _stopped_by(stop: Exception | None) -> StopReason | None:
    """Map what ended a run to the reason an operation report gives."""
    from ijob_ai.errors import (  # noqa: PLC0415
        CapReachedError,
        CostUnknownError,
        ProviderFailedError,
        ProviderUnavailableError,
    )

    if isinstance(stop, CapReachedError):
        return "cap"
    if isinstance(stop, ProviderFailedError | ProviderUnavailableError):
        return "provider"
    if isinstance(stop, CostUnknownError):
        return "cost_unknown"
    return None
