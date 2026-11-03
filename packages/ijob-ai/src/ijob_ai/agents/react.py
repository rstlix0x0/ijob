"""A ReAct agent whose every model call passes through the call rails.

`GuardedReAct` is DSPy's `ReAct` with one method replaced: each model call it
makes, the steps and the final extraction alike, first honours the run's stored
stop and the budget, then goes through `run_call`, so it is metered, traced
and its failures are translated. This module imports DSPy and knows nothing of
any source or tool, so it is imported only when an agent is about to run.

Each tool is wrapped as well, so a tool failure never reaches the model as
anything but a fixed message and a failure that must end the run is raised at
the next model call. The override depends on a private DSPy method name, which
the `dspy` version pin keeps stable.
"""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, override

import dspy
from dspy.adapters.utils import format_field_value
from opentelemetry.trace import Span

from ijob_ai.errors import (
    CapReachedError,
    CostUnknownError,
    LlmError,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.settings import Task
from ijob_ai.tracing import attributes
from ijob_ai.tracing.setup import current_operation_trace

_MAX_RESULT_CHARS: Final = 4000
_CUT: Final = "[truncated]"
_SYNC_REFUSED: Final = "GuardedReAct runs only through acall"
_NO_ARGUMENTS: Final = "nothing"
_OBSERVATION: Final = dspy.Signature("observation -> x").input_fields["observation"]
_STORED_STOPS: Final = (
    CapReachedError,
    ProviderFailedError,
    ProviderUnavailableError,
    CostUnknownError,
)


class ToolOutcome(StrEnum):
    """How one tool call ended."""

    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentStep:
    """One finished tool call.

    Attributes:
        tool: Name of the tool, `finish` included.
        outcome: How the call ended.
    """

    tool: str
    outcome: ToolOutcome


type StepCallback = Callable[[str, ToolOutcome], None]


@dataclass(slots=True)
class AgentRun:
    """The state of one agent run, shared by the loop and the code around it.

    Attributes:
        stop: An error that must end the run at the next model call, or None.
            It is an `LlmError`, or the error `on_step` raised.
        steps: Every finished tool call, in order.
        violations: Rule violations noted during the run, in order. Tools
            append to it; the loop records any a tool call added on that
            call's span.
        on_step: Called after each tool call with the tool's name and outcome,
            or None.
    """

    stop: Exception | None = None
    steps: list[AgentStep] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    on_step: StepCallback | None = None

    def record_step(self, tool: str, outcome: ToolOutcome) -> None:
        """Note a finished tool call and tell `on_step`.

        An error `on_step` raises is stored as the run's stop, if it has none,
        so it ends the run at the next model call and is not raised here.

        Args:
            tool: Name of the tool.
            outcome: How the call ended.
        """
        self.steps.append(AgentStep(tool, outcome))
        if self.on_step is None:
            return
        try:
            self.on_step(tool, outcome)
        except Exception as error:  # noqa: BLE001  # the callback is the caller's code
            if self.stop is None:
                self.stop = error


def _render(result: object) -> str:
    """Render a tool result as DSPy renders an observation of a trajectory.

    Text stays as it is, and a dict or a list becomes JSON.
    """
    if isinstance(result, str):
        return result
    text: str = format_field_value(_OBSERVATION, result)
    return text


def _first_stop(error: BaseException) -> LlmError | None:
    """Find the first error that must end the run in `error` or its groups."""
    if isinstance(error, _STORED_STOPS):
        return error
    if isinstance(error, BaseExceptionGroup):
        for member in error.exceptions:
            found = _first_stop(member)
            if found is not None:
                return found
    return None


def _cut(text: str) -> str:
    """Cut `text` to 4,000 characters, ending in `[truncated]` when it was cut."""
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[: _MAX_RESULT_CHARS - len(_CUT)] + _CUT


class _GuardedTool:
    """A DSPy tool seen through the run: nothing it raises reaches the model.

    The tool's result is cut, its step noted and its call traced. A failure of
    any kind becomes a fixed message naming the tool and its arguments, and
    an error that must end the run is stored on it, also when it comes inside
    an exception group.
    """

    def __init__(self, name: str, tool: dspy.Tool, run: AgentRun) -> None:
        self._name = name
        self._tool = tool
        self._run = run

    async def acall(self, /, **kwargs: object) -> str:
        """Run the tool and return its result, or a fixed message on failure.

        `self` is positional-only so that a model-chosen argument of that name
        reaches the tool's validation instead of failing the call itself.
        A call that raises is noted as a failed step. Capped, provider and
        cost failures are also stored as the run's stop, if it has none.
        Inside an operation trace the call is a `tool <name>` span carrying the
        outcome and any violation the call added to the run.
        """
        trace = current_operation_trace()
        scope: AbstractContextManager[Span | None] = (
            trace.child(f"tool {self._name}") if trace is not None else nullcontext()
        )
        noted = len(self._run.violations)
        with scope as span:
            outcome, text = await self._invoke(kwargs)
            if span is not None:
                span.set_attribute(attributes.TOOL_NAME, self._name)
                span.set_attribute(attributes.TOOL_OUTCOME, outcome.value)
                added = self._run.violations[noted:]
                if added:
                    span.set_attribute(attributes.TOOL_VIOLATION, "; ".join(added))
        self._run.record_step(self._name, outcome)
        return text

    async def _invoke(self, kwargs: dict[str, object]) -> tuple[ToolOutcome, str]:
        """Call the tool and turn its outcome into text for the model."""
        try:
            result = await self._tool.acall(**kwargs)
            text = _cut(_render(result))
        except Exception as error:  # noqa: BLE001  # no tool error may reach the model
            stop = _first_stop(error)
            if stop is not None and self._run.stop is None:
                self._run.stop = stop
            return ToolOutcome.FAILED, self._refusal()
        return ToolOutcome.OK, text

    def _refusal(self) -> str:
        """Name the tool and the arguments it takes, and nothing else."""
        takes = ", ".join(self._tool.args) or _NO_ARGUMENTS
        return f"could not run {self._name}; it takes: {takes}"


class GuardedReAct(dspy.ReAct):  # type: ignore[misc]  # dspy exports it untyped
    """`dspy.ReAct` run under a budget, a stored stop and the call boundary.

    The model prompt is the one ReAct builds from the signature and the tools;
    only the calls and what the tools return or raise are guarded. The loop
    runs through `acall` only.
    """

    tools: dict[str, Any]

    def __init__(  # noqa: PLR0913  # the loop's whole contract, one argument each
        self,
        signature: type[dspy.Signature],
        tools: Sequence[Callable[..., Any]],
        *,
        max_iters: int,
        lm: dspy.LM,
        provider: Provider,
        budget: Budget,
        timeout_s: float,
        task: Task,
        task_version: str,
        run: AgentRun,
    ) -> None:
        """Build the agent and guard its tools.

        Args:
            signature: The agent's signature.
            tools: The agent's tools; `finish` is added by ReAct.
            max_iters: Tool steps allowed before ReAct makes its final call.
            lm: The agent task's LM.
            provider: The provider serving `lm`.
            budget: The budget every call is charged to; the caller picks it.
            timeout_s: Seconds one model call may take.
            task: The task the calls serve.
            task_version: Version of the task's program, recorded on spans.
            run: The run's state.
        """
        super().__init__(signature, list(tools), max_iters=max_iters)
        self._lm = lm
        self._provider = provider
        self._budget = budget
        self._timeout_s = timeout_s
        self._task = task
        self._task_version = task_version
        self._run = run
        self._guard_tools()

    def _guard_tools(self) -> None:
        """Replace each of `self.tools`, `finish` included, with its guarded form.

        ReAct has built its prompt from the tools by now and reads `self.tools`
        again only to run them, so the prompt is unchanged.
        """
        self.tools = {
            name: _GuardedTool(name, tool, self._run)
            for name, tool in self.tools.items()
        }

    @override
    def forward(self, **input_args: Any) -> dspy.Prediction:
        """Refuse: the sync loop would bypass the call rails.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(_SYNC_REFUSED)

    @override
    async def _async_call_with_potential_trajectory_truncation(
        self, module: Any, trajectory: dict[str, Any], **input_args: Any
    ) -> dspy.Prediction:
        """Make one model call of the loop through `run_call`.

        ReAct's retry that truncates the trajectory is not used, so a context
        overflow reaches the caller as a provider failure.

        Args:
            module: The predictor to call, `react` or `extract`.
            trajectory: The trajectory so far.
            **input_args: The agent's inputs.

        Returns:
            The predictor's output.

        Raises:
            Exception: The run's stored stop, if there is one.
            CapReachedError: The budget has no room for another call.
            LlmError: Any error `run_call` raises.
        """
        if self._run.stop is not None:
            raise self._run.stop
        if not self._budget.try_start():
            raise CapReachedError
        return await run_call(
            module,
            self._lm,
            inputs={**input_args, "trajectory": self._format_trajectory(trajectory)},
            provider=self._provider,
            budget=self._budget,
            timeout_s=self._timeout_s,
            task=self._task,
            task_version=self._task_version,
            posting_id=None,
        )
