"""The model-calling operations of the service, and the machinery they share.

`LlmOptions` carries what a front end can inject into the LLM layer. `LlmRuntime`
holds the settings and the model registry of one `Service` and checks the
`claude` CLI once. `run_items` runs one operation's items under a spending
budget, a concurrency limit and a trace. `profile_operation` reads the CV
profile, `propose_operation` and `accept_proposal` turn the user's
instructions into stored filters and rules, `judge_operation` judges postings
against those rules, and `score_operation` scores them against the CV profile.
`list_traces`, `read_trace` and `prune_traces` read and trim the recorded
traces.

This module and `ijob_app.service` import no `ijob_ai` LLM module, `dspy` or
`claude_agent_sdk` at module level: those load only inside the functions that
are about to call a model, so a command that calls none stays fast. Names the
module needs only for annotations come in under `TYPE_CHECKING`.
"""

import asyncio
import json
import shutil
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Self

from ijob_core.errors import InvalidValueError
from ijob_core.model import Instructions, Posting, Score, Verdict
from ijob_core.traces import PruneReport, TraceStore, TraceSummary, TraceTree

from ijob_app.config import effective_filters
from ijob_app.home import IjobHome

if TYPE_CHECKING:
    import requests
    from dspy.clients.engines.base import AsyncEngine, Engine
    from ijob_ai.errors import LlmError
    from ijob_ai.llm.budget import Budget
    from ijob_ai.llm.reference import ModelRef
    from ijob_ai.llm.registry import Registry
    from ijob_ai.llm.settings import LlmSettings, Task, TracingSettings
    from ijob_ai.tasks.instructions import ParsedInstructions
    from ijob_core.filters import FilterSpec
    from ijob_core.model import ResumeProfile
    from ijob_core.store import Store

    from ijob_app.config import FilterLayers, ShadowedField

_BYTES_PER_MB: Final = 2**20

type EngineFactory = Callable[[ModelRef, Path], tuple[Engine, AsyncEngine]]
type Worker[Item, Result] = Callable[[Item, Budget], Awaitable[Result]]


class JudgeStatus(StrEnum):
    """Why a posting does or does not carry a judge verdict."""

    NOT_RUN = "not_run"
    CACHED = "cached"
    JUDGED = "judged"
    NO_DESCRIPTION = "no_description"
    FAILED = "failed"
    CAP_REACHED = "cap_reached"
    STOPPED = "stopped"
    PROVIDER_ERROR = "provider_error"


class ScoreStatus(StrEnum):
    """Why a posting does or does not carry a score."""

    NOT_RUN = "not_run"
    CACHED = "cached"
    JUDGED = "judged"
    FAILED = "failed"
    CAP_REACHED = "cap_reached"
    STOPPED = "stopped"
    PROVIDER_ERROR = "provider_error"


STATUS_TEXT: Final[Mapping[type[StrEnum], Mapping[str, str]]] = {
    JudgeStatus: {
        JudgeStatus.FAILED: "judge failed",
        JudgeStatus.CAP_REACHED: "cap reached",
        JudgeStatus.STOPPED: "stopped",
        JudgeStatus.PROVIDER_ERROR: "provider error",
    },
    ScoreStatus: {
        ScoreStatus.FAILED: "score failed",
        ScoreStatus.CAP_REACHED: "cap reached",
        ScoreStatus.STOPPED: "stopped",
        ScoreStatus.PROVIDER_ERROR: "provider error",
    },
}
"""What the user reads beside an item that carries no result, per status enum.

Index by the status enum, then by a status of it: `STATUS_TEXT[JudgeStatus]
[status]`. Only the statuses that mark a missing result have a text.
"""


type StopReason = Literal["cap", "provider", "cost_unknown"]
"""Why an operation started no further items."""


class ItemStatus(StrEnum):
    """How `run_items` ended one item.

    Separate from `JudgeStatus` and `ScoreStatus` because it is the one status
    set every operation shares, and it alone has `DONE`: a finished item becomes
    `JUDGED`, `NO_DESCRIPTION` or `CACHED` only once the operation knows more.
    """

    DONE = "done"
    FAILED = "failed"
    CAP_REACHED = "cap_reached"
    STOPPED = "stopped"
    PROVIDER_ERROR = "provider_error"


_UNSTARTED: Final[Mapping[StopReason, ItemStatus]] = {
    "cap": ItemStatus.CAP_REACHED,
    "provider": ItemStatus.PROVIDER_ERROR,
    "cost_unknown": ItemStatus.STOPPED,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class LlmOptions:
    """What a front end can inject into the LLM layer.

    Attributes:
        config_path: The config file the settings are read from; errors name it.
        engines: Builds the sync and async engine of a claude-sdk model from its
            reference and the checked `claude` path, or None for the real
            engines, imported on first use.
        session: HTTP session for the Langfuse export, or None for the
            exporter's own.
        cli_which: Looks `claude` up on `PATH`, or None for `shutil.which`.
    """

    config_path: Path
    engines: EngineFactory | None = None
    session: "requests.Session | None" = None
    cli_which: Callable[[str], str | None] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationReport:
    """What one operation did and spent.

    `run_items` fills every field but `cached` and `dropped`, which only the
    operation that owns the items can count.

    Attributes:
        done: Items that produced a result.
        cached: Items answered from the store without a model call.
        dropped: Postings the judge dropped.
        not_done: Items that produced no result.
        spent: What the model calls cost, in US dollars.
        cap: The spending cap, in US dollars.
        stopped_by: Why no further item was started, or None when every item ran.
        warnings: Tracing set-up messages for the user.
        dropped_spans: Spans the trace store could not keep.
    """

    done: int
    cached: int = 0
    dropped: int = 0
    not_done: int
    spent: float
    cap: float
    stopped_by: StopReason | None
    warnings: tuple[str, ...] = ()
    dropped_spans: int = 0


@dataclass(frozen=True, slots=True)
class ItemOutcome[Item, Result]:
    """How one item ended.

    Attributes:
        item: The item.
        status: How it ended.
        result: The worker's result, set when the item is done.
        error: The error that ended the item, set when it failed or was stopped
            by its own error; None for an item that never started.
    """

    item: Item
    status: ItemStatus
    result: Result | None = None
    error: "LlmError | None" = None


@dataclass(frozen=True, slots=True)
class RunResult[Item, Result]:
    """The outcomes of one operation and its report.

    Attributes:
        outcomes: One outcome per item, in item order.
        report: What the operation did and spent.
    """

    outcomes: tuple[ItemOutcome[Item, Result], ...]
    report: OperationReport


class LlmRuntime:
    """The settings and model registry of one service, built on first use.

    Attributes:
        home: The ijob home; its trace database receives every trace.
        options: What the front end injected.
        settings: The model, limit and provider settings.
        tracing_settings: The trace-store and Langfuse settings.
    """

    def __init__(
        self,
        *,
        home: IjobHome,
        options: LlmOptions,
        settings: "LlmSettings",
        tracing_settings: "TracingSettings",
    ) -> None:
        """Build a runtime whose registry has built nothing yet.

        Imports `ijob_ai.llm.registry`, and with it DSPy.

        Args:
            home: The ijob home.
            options: What the front end injected.
            settings: The model, limit and provider settings.
            tracing_settings: The trace-store and Langfuse settings.
        """
        from ijob_ai.llm.registry import Registry  # noqa: PLC0415

        self.home = home
        self.options = options
        self.settings = settings
        self.tracing_settings = tracing_settings
        self._registry = Registry(settings, self._claude_engines)
        self._cli_path: Path | None = None
        self._cli_lock = asyncio.Lock()

    @classmethod
    def create(cls, home: IjobHome, options: LlmOptions) -> Self:
        """Prepare the process for DSPy and read the settings.

        Args:
            home: The ijob home.
            options: What the front end injected.

        Returns:
            The runtime.

        Raises:
            ProviderUnavailableError: A variable that redirects model calls is set.
            InvalidValueError: The config file is unreadable, or a setting in it
                is unknown or out of range.
        """
        from ijob_ai.llm.registry import prepare_environment  # noqa: PLC0415

        from ijob_app.llm_config import (  # noqa: PLC0415
            read_llm_settings,
            read_tracing_settings,
        )

        prepare_environment()
        return cls(
            home=home,
            options=options,
            settings=read_llm_settings(options.config_path),
            tracing_settings=read_tracing_settings(options.config_path),
        )

    async def registry_for(self, task: "Task") -> "Registry":
        """Return the shared registry once the task's provider is ready.

        For a claude-sdk model, the first call finds the `claude` CLI and runs
        its version check; later calls reuse the checked path, and a failed
        check is repeated by the next call.

        Args:
            task: The task about to call a model.

        Returns:
            The registry shared by every task of this runtime.

        Raises:
            MissingModelError: No model is configured for `task`.
            ProviderUnavailableError: The CLI is missing, not executable, fails
                its version check or is too old.
        """
        from ijob_ai.llm.claude_cli import check_version, resolve_cli  # noqa: PLC0415
        from ijob_ai.llm.reference import Provider  # noqa: PLC0415

        ref = self.settings.model_for(task)
        if ref.provider is Provider.CLAUDE_SDK:
            async with self._cli_lock:
                if self._cli_path is None:
                    path = resolve_cli(
                        self.settings.cli_path, self.options.cli_which or shutil.which
                    )
                    await check_version(path)
                    self._cli_path = path
        return self._registry

    def _claude_engines(self, ref: "ModelRef") -> "tuple[Engine, AsyncEngine]":
        """Build the engine pair for a claude-sdk model on the checked CLI path."""
        if self._cli_path is None:
            msg = "the claude CLI is not checked; call registry_for first"
            raise RuntimeError(msg)
        engines = self.options.engines
        if engines is None:
            from ijob_ai.llm.claude_engine import claude_engines  # noqa: PLC0415

            engines = claude_engines
        return engines(ref, self._cli_path)


class _ItemRun[Item, Result]:
    """The shared state of one `run_items` call: what stops it and what finished."""

    def __init__(
        self,
        *,
        budget: "Budget",
        concurrency: int,
        worker: Worker[Item, Result],
        items: Sequence[Item],
        on_outcome: Callable[[ItemOutcome[Item, Result]], None] | None,
    ) -> None:
        self.stopped_by: StopReason | None = None
        self.outcomes: list[ItemOutcome[Item, Result] | None] = [None] * len(items)
        self._budget = budget
        self._semaphore = asyncio.Semaphore(concurrency)
        self._worker = worker
        self._on_outcome = on_outcome

    async def run(self, index: int, item: Item) -> None:
        """Run one item when its turn comes, or mark it as never started."""
        async with self._semaphore:
            outcome = await self._outcome(item)
        self.outcomes[index] = outcome
        if self._on_outcome is not None:
            self._on_outcome(outcome)

    async def _outcome(self, item: Item) -> ItemOutcome[Item, Result]:
        from ijob_ai.errors import (  # noqa: PLC0415
            CostUnknownError,
            LlmError,
            ProviderFailedError,
            ProviderUnavailableError,
        )

        if self.stopped_by is None and not self._budget.try_start():
            self.stopped_by = "cap"
        if self.stopped_by is not None:
            return ItemOutcome(item, _UNSTARTED[self.stopped_by])
        try:
            result = await self._worker(item, self._budget)
        except (ProviderFailedError, ProviderUnavailableError) as error:
            self._stop("provider")
            return ItemOutcome(item, ItemStatus.PROVIDER_ERROR, error=error)
        except CostUnknownError as error:
            self._stop("cost_unknown")
            return ItemOutcome(item, ItemStatus.STOPPED, error=error)
        except LlmError as error:
            return ItemOutcome(item, ItemStatus.FAILED, error=error)
        return ItemOutcome(item, ItemStatus.DONE, result=result)

    def _stop(self, reason: StopReason) -> None:
        """Record why no further item starts; the first reason stays."""
        if self.stopped_by is None:
            self.stopped_by = reason


async def run_items[Item, Result](
    runtime: LlmRuntime,
    operation: str,
    items: Sequence[Item],
    worker: Worker[Item, Result],
    *,
    on_outcome: Callable[[ItemOutcome[Item, Result]], None] | None = None,
) -> RunResult[Item, Result]:
    """Run an operation's items under one budget, one concurrency limit and one trace.

    Items start in order, at most `[llm].concurrency` at a time, each only while
    the budget allows. After a provider failure or an unknown cost no further
    item starts and the rest are marked, as they are once the budget is spent;
    items already running finish and keep their results. The first reason to
    stop is the one reported.

    Each finished item reaches `on_outcome` as soon as it ends, so a caller sees
    results in completion order and keeps them if the operation is cancelled.
    Tracing starts with the call and always finishes, whether the call returns,
    fails or is cancelled.

    Args:
        runtime: The runtime holding the settings and the trace location.
        operation: The operation's name, such as `search`; the trace's root span
            is called `ijob <operation>`.
        items: The items to work on.
        worker: Does the work of one item with the operation's budget, checking
            nothing itself; it raises an `LlmError` to end that item.
        on_outcome: Called, on the event loop, with each outcome as it is known.

    Returns:
        One outcome per item in item order, and the report.

    Raises:
        asyncio.CancelledError: The call was cancelled; outcomes already handed
            to `on_outcome` stand.
        ExceptionGroup: A worker raised something other than an `LlmError`.
        sqlite3.Error: The trace store cannot be opened or pruned.
    """
    from ijob_ai.llm.budget import Budget  # noqa: PLC0415
    from ijob_ai.tracing.setup import operation_trace, start_tracing  # noqa: PLC0415

    tracing = start_tracing(
        runtime.home.traces, runtime.tracing_settings, session=runtime.options.session
    )
    budget = Budget(runtime.settings.max_cost_usd)
    run = _ItemRun(
        budget=budget,
        concurrency=runtime.settings.concurrency,
        worker=worker,
        items=items,
        on_outcome=on_outcome,
    )
    try:
        with operation_trace(tracing, operation):
            async with asyncio.TaskGroup() as group:
                for index, item in enumerate(items):
                    group.create_task(run.run(index, item))
    finally:
        # Finishing flushes the exporters for up to five seconds and prunes
        # the trace store, so it runs off the event loop.
        trace_report = await asyncio.to_thread(tracing.finish)
    outcomes = tuple(outcome for outcome in run.outcomes if outcome is not None)
    done = sum(1 for outcome in outcomes if outcome.status is ItemStatus.DONE)
    report = OperationReport(
        done=done,
        not_done=len(outcomes) - done,
        spent=budget.spent,
        cap=budget.cap,
        stopped_by=run.stopped_by,
        warnings=tuple(tracing.warnings),
        dropped_spans=trace_report.dropped,
    )
    return RunResult(outcomes, report)


@dataclass(frozen=True, slots=True)
class Proposal:
    """What the user's instructions would set, for the user to confirm.

    Attributes:
        text: The instructions as the user wrote them.
        instruction_filters: The filters the instructions imply, in the shape of
            the config file's `[filters]` table. This is the instruction layer
            alone; it is what gets stored, not `effective`.
        rules: The judge rules, none empty.
        effective: The filters in force once the instructions are stored.
        shadowed: The instruction filters that a flag or the user's file
            overrides.
        from_cache: Whether the stored parse answered, without a model call.
    """

    text: str
    instruction_filters: Mapping[str, object]
    rules: tuple[str, ...]
    effective: "FilterSpec"
    shadowed: "tuple[ShadowedField, ...]"
    from_cache: bool


async def _run_single[Item, Result](
    runtime: LlmRuntime, operation: str, item: Item, worker: Worker[Item, Result]
) -> tuple[Result, OperationReport]:
    """Run one item as a whole operation and raise the error that ended it."""
    run = await run_items(runtime, operation, (item,), worker)
    outcome = run.outcomes[0]
    if outcome.error is not None:
        raise outcome.error
    if outcome.status is ItemStatus.DONE and outcome.result is not None:
        return outcome.result, run.report
    msg = f"{operation} ended as {outcome.status} without an error"
    raise RuntimeError(msg)


def _cached_report(runtime: LlmRuntime, *, count: int = 0) -> OperationReport:
    """Return the report of an operation that the store answered, `count` items."""
    return OperationReport(
        done=0,
        cached=count,
        not_done=0,
        spent=0.0,
        cap=runtime.settings.max_cost_usd,
        stopped_by=None,
    )


async def _read_profile(
    runtime: LlmRuntime, cv_text: str, budget: "Budget"
) -> "ResumeProfile":
    """Have the `cv_profile` model read a profile out of the CV text.

    Raises:
        MissingModelError: No model is set for the `cv_profile` task.
        LlmError: The call fails or its answer is unusable.
    """
    from ijob_ai.llm.settings import Task  # noqa: PLC0415
    from ijob_ai.tasks.cv_profile import build_profile  # noqa: PLC0415

    ref = runtime.settings.model_for(Task.CV_PROFILE)
    registry = await runtime.registry_for(Task.CV_PROFILE)
    return await build_profile(
        cv_text,
        lm=registry.lm_for(Task.CV_PROFILE),
        provider=ref.provider,
        budget=budget,
        timeout_s=runtime.settings.timeout_s,
    )


async def profile_operation(
    runtime: LlmRuntime,
    store: "Store",
    cv_path: Path,
    *,
    refresh: bool,
    at: datetime,
) -> "tuple[ResumeProfile, OperationReport]":
    """Return the profile of the stored CV, reading the CV only when needed.

    The stored profile answers when it was read from this CV, by this task
    version and this model, unless `refresh`. Otherwise the CV text is read from
    `cv_path`, the model reads the profile out of it, and the profile is stored.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        cv_path: The stored copy of the CV.
        refresh: Whether to read the CV again even if the stored profile is
            current.
        at: When the profile is made, timezone-aware.

    Returns:
        The profile and what the operation did and spent.

    Raises:
        NoCvError: No CV is stored.
        MissingModelError: No model is set for the `cv_profile` task.
        CvTextError: The CV text cannot be read or used.
        TaskOutputError: The model's answer is unusable.
        ProviderUnavailableError: The provider cannot be used.
        ProviderFailedError: The provider failed the call.
        CostUnknownError: The cost of the call cannot be determined.
        sqlite3.Error: The store cannot be read or written.
    """
    from ijob_ai.errors import NoCvError  # noqa: PLC0415
    from ijob_ai.llm.settings import Task  # noqa: PLC0415
    from ijob_ai.tasks.cv_text import read_cv_text  # noqa: PLC0415
    from ijob_ai.tasks.signatures import CV_PROFILE_VERSION  # noqa: PLC0415

    cv = store.cv()
    if cv is None:
        raise NoCvError
    ref = runtime.settings.model_for(Task.CV_PROFILE)
    stored = store.cv_profile()
    if (
        not refresh
        and stored is not None
        and stored.cv_sha256 == cv.sha256
        and stored.task_version == CV_PROFILE_VERSION
        and stored.model == str(ref)
    ):
        return stored.profile, _cached_report(runtime, count=1)
    cv_text = await asyncio.to_thread(read_cv_text, cv_path)

    async def read(text: str, budget: "Budget") -> "ResumeProfile":
        return await _read_profile(runtime, text, budget)

    profile, report = await _run_single(runtime, "cv-profile", cv_text, read)
    store.set_cv_profile(
        profile,
        cv_sha256=cv.sha256,
        model=str(ref),
        task_version=CV_PROFILE_VERSION,
        made_at=at,
    )
    return profile, report


def instructions_model(runtime: LlmRuntime) -> str:
    """Return the reference of the model that reads instructions.

    Args:
        runtime: The runtime holding the settings.

    Returns:
        The `[models].instructions` reference as `<provider>/<model>`.

    Raises:
        MissingModelError: No model is set for the `instructions` task.
    """
    from ijob_ai.llm.settings import Task  # noqa: PLC0415

    return str(runtime.settings.model_for(Task.INSTRUCTIONS))


async def propose_operation(
    runtime: LlmRuntime, store: "Store", layers: "FilterLayers", text: str
) -> Proposal:
    """Work out what the user's instructions would set, without storing them.

    The stored parse answers when it was made from this very text, by this task
    version and this model. Otherwise the model parses the text. Either way the
    filters are merged with `layers` to show what would be in force and what
    would be shadowed.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        layers: The filters the packaged config, the user's file and the flags
            set.
        text: The instructions the user wrote.

    Returns:
        The proposal for the user to confirm.

    Raises:
        InvalidValueError: `text` is blank or too long, or a filter the
            instructions imply is unusable; no call is made in the first case.
        MissingModelError: No model is set for the `instructions` task.
        NothingActionableError: The instructions give neither a filter nor a rule.
        TaskOutputError: The model's answer is unusable.
        ProviderUnavailableError: The provider cannot be used.
        ProviderFailedError: The provider failed the call.
        CostUnknownError: The cost of the call cannot be determined.
        sqlite3.Error: The store cannot be read.
    """
    from ijob_ai.llm.settings import Task  # noqa: PLC0415
    from ijob_ai.tasks.instructions import (  # noqa: PLC0415
        INSTRUCTIONS_LIMIT,
        parse_instructions,
    )
    from ijob_ai.tasks.signatures import INSTRUCTIONS_VERSION  # noqa: PLC0415

    # `parse_instructions` checks the same limits, but its error would reach the
    # caller wrapped in the exception group of the operation's task group.
    if not text.strip():
        raise InvalidValueError("instructions", "must not be blank")
    if len(text) > INSTRUCTIONS_LIMIT:
        reason = f"must be at most {INSTRUCTIONS_LIMIT:,} characters"
        raise InvalidValueError("instructions", reason)
    ref = runtime.settings.model_for(Task.INSTRUCTIONS)
    stored = store.instructions()
    filters: Mapping[str, object]
    if (
        stored is not None
        and stored.text == text
        and stored.task_version == INSTRUCTIONS_VERSION
        and stored.model == str(ref)
    ):
        filters = MappingProxyType(json.loads(stored.filters_json))
        rules = stored.rules
        from_cache = True
    else:

        async def parse(
            instruction_text: str, budget: "Budget"
        ) -> "ParsedInstructions":
            registry = await runtime.registry_for(Task.INSTRUCTIONS)
            return await parse_instructions(
                instruction_text,
                lm=registry.lm_for(Task.INSTRUCTIONS),
                provider=ref.provider,
                budget=budget,
                timeout_s=runtime.settings.timeout_s,
            )

        parsed, _ = await _run_single(runtime, "instructions", text, parse)
        filters = parsed.filters
        rules = parsed.rules
        from_cache = False
    effective, shadowed = effective_filters(layers, filters)
    return Proposal(
        text=text,
        instruction_filters=filters,
        rules=rules,
        effective=effective,
        shadowed=shadowed,
        from_cache=from_cache,
    )


def accept_proposal(
    store: "Store", proposal: Proposal, *, model: str, at: datetime
) -> None:
    """Store a confirmed proposal as the user's instructions.

    Only the instruction layer is stored, as sorted-key JSON; the merged filters
    are worked out again at every search.

    Args:
        store: The open store.
        proposal: The proposal the user confirmed.
        model: The reference of the model that read the text.
        at: When the instructions are set, timezone-aware.

    Raises:
        InvalidValueError: The instructions break a limit of the stored form.
        sqlite3.Error: The store cannot be written.
    """
    from ijob_ai.tasks.signatures import INSTRUCTIONS_VERSION  # noqa: PLC0415

    store.set_instructions(
        Instructions(
            text=proposal.text,
            filters_json=json.dumps(dict(proposal.instruction_filters), sort_keys=True),
            rules=proposal.rules,
            model=model,
            task_version=INSTRUCTIONS_VERSION,
            set_at=at,
        )
    )


@dataclass(frozen=True, slots=True)
class SearchItem:
    """A posting the search found, with what the judge made of it.

    Attributes:
        posting: The posting.
        verdict: The judge's verdict, or None when the posting has none.
        status: Why it has a verdict or not.
        no_description: Whether the posting has no description, so the judge saw
            only its title and the other listed facts.
    """

    posting: Posting
    verdict: Verdict | None
    status: JudgeStatus
    no_description: bool


async def _stream_run[Item, Result](
    runtime: LlmRuntime,
    operation: str,
    items: Sequence[Item],
    worker: Worker[Item, Result],
) -> AsyncGenerator[ItemOutcome[Item, Result] | OperationReport]:
    """Run `run_items` and yield each outcome as it ends, then the report.

    Closing the iterator early cancels the run; outcomes already yielded stand.
    """
    queue: asyncio.Queue[ItemOutcome[Item, Result] | None] = asyncio.Queue()
    task = asyncio.create_task(
        run_items(runtime, operation, items, worker, on_outcome=queue.put_nowait)
    )
    task.add_done_callback(lambda _: queue.put_nowait(None))
    try:
        while (outcome := await queue.get()) is not None:
            yield outcome
        result = task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait((task,))
        if not task.cancelled():
            task.exception()  # read, so the loop does not report it as lost
    yield result.report


def _fresh_judge_status(status: ItemStatus, posting: Posting) -> JudgeStatus:
    """Name the judge status of an item that ran, or never started."""
    if status is not ItemStatus.DONE:
        return JudgeStatus(status.value)
    if posting.description is None:
        return JudgeStatus.NO_DESCRIPTION
    return JudgeStatus.JUDGED


async def judge_operation(  # noqa: PLR0913  # the operation's inputs, one each
    runtime: LlmRuntime,
    store: "Store",
    postings: Sequence[Posting],
    instructions: Instructions,
    *,
    now: Callable[[], datetime],
    on_report: Callable[[OperationReport], None],
) -> AsyncGenerator[SearchItem]:
    """Judge postings against the user's rules, yielding each as it is known.

    A posting whose verdict is stored for this exact posting text, these rules,
    this model and this task version is yielded first, in the given order,
    without a model call. The rest are judged one posting per call, at most
    `[llm].concurrency` at a time and while the budget allows, and yielded in the
    order they finish; each fresh verdict is stored as soon as it is made.
    Closing the iterator early cancels the calls still running.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        postings: The postings to judge.
        instructions: The stored instructions, whose rules the judge applies.
        now: The wall clock, returning timezone-aware times.
        on_report: Called once, after the last posting is yielded, with what the
            operation did and spent.

    Yields:
        One item per posting.

    Raises:
        MissingModelError: No model is set for the `judge` task; no call is made.
        asyncio.CancelledError: The iteration was cancelled.
        ExceptionGroup: A call raised something other than an `LlmError`.
        sqlite3.Error: The store cannot be read or written.
    """
    from ijob_ai.llm.settings import Task  # noqa: PLC0415
    from ijob_ai.tasks.judge import judge_posting  # noqa: PLC0415
    from ijob_ai.tasks.signatures import JUDGE_VERSION  # noqa: PLC0415
    from ijob_ai.tasks.text import input_sha256, posting_block  # noqa: PLC0415

    ref = runtime.settings.model_for(Task.JUDGE)
    rules_sha256 = instructions.rules_sha256
    pending: list[tuple[Posting, str]] = []
    cached: list[SearchItem] = []
    for posting in postings:
        digest = input_sha256(posting_block(posting))
        verdict = store.verdict(
            posting.id, digest, rules_sha256, str(ref), JUDGE_VERSION
        )
        if verdict is None:
            pending.append((posting, digest))
        else:
            cached.append(
                SearchItem(
                    posting, verdict, JudgeStatus.CACHED, posting.description is None
                )
            )
    dropped = 0
    for item in cached:
        dropped += item.verdict is not None and not item.verdict.keep
        yield item

    async def judge(entry: tuple[Posting, str], budget: "Budget") -> Verdict:
        posting, digest = entry
        registry = await runtime.registry_for(Task.JUDGE)
        verdict = await judge_posting(
            posting,
            instructions.rules,
            lm=registry.lm_for(Task.JUDGE),
            provider=ref.provider,
            budget=budget,
            timeout_s=runtime.settings.timeout_s,
        )
        store.put_verdict(
            posting.id,
            digest,
            rules_sha256,
            str(ref),
            JUDGE_VERSION,
            verdict,
            made_at=now(),
        )
        return verdict

    report = _cached_report(runtime)
    if pending:
        async with aclosing(_stream_run(runtime, "search", pending, judge)) as events:
            async for event in events:
                if isinstance(event, OperationReport):
                    report = event
                    continue
                posting = event.item[0]
                verdict = event.result
                if verdict is not None and not verdict.keep:
                    dropped += 1
                yield SearchItem(
                    posting,
                    verdict,
                    _fresh_judge_status(event.status, posting),
                    posting.description is None,
                )
    on_report(replace(report, cached=len(cached), dropped=dropped))


@dataclass(frozen=True, slots=True)
class ScoreItem:
    """A posting that was scored, with the score.

    Attributes:
        posting: The posting.
        score: How well it fits the CV, or None when it has no score.
        status: Why it has a score or not.
    """

    posting: Posting
    score: Score | None
    status: ScoreStatus


class _ProfileSource:
    """The CV profile one scoring run needs, built by the first item that wants it."""

    def __init__(  # noqa: PLR0913  # what building and storing the profile takes
        self,
        runtime: LlmRuntime,
        store: "Store",
        *,
        profile: "ResumeProfile | None",
        cv_sha256: str,
        cv_text: str | None,
        now: Callable[[], datetime],
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._profile = profile
        self._cv_sha256 = cv_sha256
        self._cv_text = cv_text
        self._now = now
        self._lock = asyncio.Lock()
        self._error: LlmError | None = None

    async def get(self, budget: "Budget") -> "ResumeProfile":
        """Return the profile, building and storing it on the first call if absent.

        A build that failed is not tried again: every later call raises the same
        error.

        Raises:
            LlmError: The profile cannot be built.
        """
        from ijob_ai.errors import LlmError  # noqa: PLC0415
        from ijob_ai.llm.settings import Task  # noqa: PLC0415
        from ijob_ai.tasks.signatures import CV_PROFILE_VERSION  # noqa: PLC0415

        if (current := self._profile) is not None:
            return current
        async with self._lock:
            if (current := self._profile) is not None:
                return current
            if self._error is not None:
                raise self._error
            if self._cv_text is None:
                msg = "the CV text was not read before the profile was needed"
                raise RuntimeError(msg)
            try:
                profile = await _read_profile(self._runtime, self._cv_text, budget)
            except LlmError as error:
                self._error = error
                raise
            self._store.set_cv_profile(
                profile,
                cv_sha256=self._cv_sha256,
                model=str(self._runtime.settings.model_for(Task.CV_PROFILE)),
                task_version=CV_PROFILE_VERSION,
                made_at=self._now(),
            )
            self._profile = profile
            return profile


def _score_status(status: ItemStatus) -> ScoreStatus:
    """Name the score status of an item that ran, or never started."""
    if status is ItemStatus.DONE:
        return ScoreStatus.JUDGED
    return ScoreStatus(status.value)


async def score_operation(  # noqa: PLR0913  # the operation's inputs, one each
    runtime: LlmRuntime,
    store: "Store",
    postings: Sequence[Posting],
    cv_path: Path,
    *,
    now: Callable[[], datetime],
    on_report: Callable[[OperationReport], None],
) -> AsyncGenerator[ScoreItem]:
    """Score postings against the stored CV profile, yielding each as it is known.

    A posting whose score is stored for this exact posting text, this profile,
    this model and this task version is yielded first, in the given order,
    without a model call. The rest are scored one posting per call, at most
    `[llm].concurrency` at a time and while the budget allows, and yielded in the
    order they finish; each fresh score is stored as soon as it is made.

    When no profile is stored, or the stored one was read from an earlier CV
    than the stored one, the first posting to run builds it from the CV inside
    the same operation, so its call shares the budget and the trace of the
    scores, and the profile is stored. Closing the iterator early cancels the
    calls still running.

    Args:
        runtime: The runtime holding the settings and the model registry.
        store: The open store.
        postings: The postings to score.
        cv_path: The stored copy of the CV, read only when the profile is built.
        now: The wall clock, returning timezone-aware times.
        on_report: Called once, after the last posting is yielded, with what the
            operation did and spent.

    Yields:
        One item per posting.

    Raises:
        NoCvError: No CV is stored.
        MissingModelError: No model is set for the `scorer` task, or for the
            `cv_profile` task when the profile must be built; no call is made.
        CvTextError: The CV text is needed and cannot be read or used; no call
            is made.
        asyncio.CancelledError: The iteration was cancelled.
        ExceptionGroup: A call raised something other than an `LlmError`.
        sqlite3.Error: The store cannot be read or written.
    """
    from ijob_ai.errors import NoCvError  # noqa: PLC0415
    from ijob_ai.llm.settings import Task  # noqa: PLC0415
    from ijob_ai.tasks.cv_text import read_cv_text  # noqa: PLC0415
    from ijob_ai.tasks.scorer import score_posting  # noqa: PLC0415
    from ijob_ai.tasks.signatures import SCORER_VERSION  # noqa: PLC0415
    from ijob_ai.tasks.text import input_sha256, posting_block  # noqa: PLC0415

    cv = store.cv()
    if cv is None:
        raise NoCvError
    ref = runtime.settings.model_for(Task.SCORER)
    stored = store.cv_profile()
    if stored is not None and stored.cv_sha256 != cv.sha256:
        stored = None
    cv_text: str | None = None
    if stored is None:
        # Called only to raise MissingModelError before the CV is read or any
        # call is made; `_read_profile` looks the model up again.
        runtime.settings.model_for(Task.CV_PROFILE)
        cv_text = await asyncio.to_thread(read_cv_text, cv_path)
    source = _ProfileSource(
        runtime,
        store,
        profile=None if stored is None else stored.profile,
        cv_sha256=cv.sha256,
        cv_text=cv_text,
        now=now,
    )
    profile_sha256 = None if stored is None else stored.profile.sha256()
    pending: list[tuple[Posting, str]] = []
    cached: list[ScoreItem] = []
    for posting in postings:
        digest = input_sha256(posting_block(posting))
        score = (
            None
            if profile_sha256 is None
            else store.score(
                posting.id, digest, profile_sha256, str(ref), SCORER_VERSION
            )
        )
        if score is None:
            pending.append((posting, digest))
        else:
            cached.append(ScoreItem(posting, score, ScoreStatus.CACHED))
    for item in cached:
        yield item

    async def score_one(entry: tuple[Posting, str], budget: "Budget") -> Score:
        posting, digest = entry
        profile = await source.get(budget)
        registry = await runtime.registry_for(Task.SCORER)
        result = await score_posting(
            posting,
            profile,
            lm=registry.lm_for(Task.SCORER),
            provider=ref.provider,
            budget=budget,
            timeout_s=runtime.settings.timeout_s,
        )
        store.put_score(
            posting.id,
            digest,
            profile.sha256(),
            str(ref),
            SCORER_VERSION,
            result,
            made_at=now(),
        )
        return result

    report = _cached_report(runtime)
    if pending:
        async with aclosing(
            _stream_run(runtime, "score", pending, score_one)
        ) as events:
            async for event in events:
                if isinstance(event, OperationReport):
                    report = event
                    continue
                yield ScoreItem(
                    event.item[0], event.result, _score_status(event.status)
                )
    on_report(replace(report, cached=len(cached)))


def list_traces(home: IjobHome, limit: int) -> list[TraceSummary]:
    """List the recorded traces, newest first.

    Args:
        home: The ijob home, which holds the trace database.
        limit: The most traces to list.

    Returns:
        The summaries, newest first.

    Raises:
        InvalidValueError: `limit` is negative.
        sqlite3.Error: The trace database cannot be opened or read.
    """
    with TraceStore.open(home.traces) as traces:
        return traces.summaries(limit)


def read_trace(home: IjobHome, trace_id: str) -> TraceTree:
    """Read one trace as nested spans.

    Args:
        home: The ijob home, which holds the trace database.
        trace_id: The trace to read.

    Returns:
        The trace's spans nested under their parents.

    Raises:
        UnknownTraceError: No span of that trace is recorded.
        sqlite3.Error: The trace database cannot be opened or read.
    """
    with TraceStore.open(home.traces) as traces:
        return traces.tree(trace_id)


def prune_traces(home: IjobHome, config_path: Path) -> PruneReport:
    """Delete the oldest traces until the database fits `[tracing].max_size_mb`.

    Args:
        home: The ijob home, which holds the trace database.
        config_path: The config file the size limit is read from.

    Returns:
        How many traces were deleted and the file size afterwards.

    Raises:
        InvalidValueError: The config file is unreadable, or a setting in it is
            unknown or out of range.
        sqlite3.Error: The trace database cannot be opened or pruned.
    """
    from ijob_app.llm_config import read_tracing_settings  # noqa: PLC0415

    settings = read_tracing_settings(config_path)
    with TraceStore.open(home.traces) as traces:
        return traces.prune(settings.max_size_mb * _BYTES_PER_MB)
