"""Rails for model calls an optimizer makes from its own worker threads.

An optimizer calls its LM directly, so `run_call` never runs for those calls.
`RailedLM` stands in for the LM and sends each call to the command's running
event loop, where it is checked against the budgets, metered, traced, timed out
and counted. This module imports DSPy, so it is imported only when a model is
about to be used.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, override

import dspy
from dspy.clients._deprecation import adapter_message_call

from ijob_ai.errors import (
    CapReachedError,
    CostUnknownError,
    LlmError,
    ProviderUnavailableError,
)
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import call_span, meter_from_history, translate_failure
from ijob_ai.llm.meter import CallMeter, open_meter
from ijob_ai.llm.reference import Provider, parse_model_ref
from ijob_ai.llm.settings import Task
from ijob_ai.tracing.setup import OperationTrace

logger = logging.getLogger(__name__)


class RailGuard:
    """What all the rails of one tuning run share.

    Every method and property runs on the event loop's thread, or after the
    run, so none needs a lock.

    Attributes:
        calls: Calls that were let through, whatever their outcome.
        failed: Calls that failed, timeouts included; calls that ended the run
            through `fatal` are not counted here.
        first_error: The text of the first failure counted in `failed`, or None.
        cap_reached: True once a call was refused because a budget had no room.
        fatal: The `CostUnknownError` or `ProviderUnavailableError` that ended
            the run, or None.
        on_call: Called with the guard on the loop after each call that was let
            through, whether it succeeded or failed, or None. An error it raises
            is logged and dropped, so it never changes a call's result or count.
    """

    def __init__(self, *, on_call: Callable[["RailGuard"], None] | None = None) -> None:
        """Create a guard with nothing counted and no stop requested.

        Args:
            on_call: Callback to run after each call that was let through. It
                is progress only: an error it raises is logged and dropped.
        """
        self._stopped = False
        self.calls = 0
        self.failed = 0
        self.first_error: str | None = None
        self.cap_reached = False
        self.fatal: CostUnknownError | ProviderUnavailableError | None = None
        self.on_call = on_call

    @property
    def stopped(self) -> bool:
        """Whether later calls are refused.

        It says nothing about why: a fatal call sets it, a direct `stop` sets it,
        and whoever runs the rails sets it when the run ends.
        """
        return self._stopped

    def stop(self) -> None:
        """Refuse every later call."""
        self._stopped = True


def _check_provider(lm: dspy.BaseLM, provider: Provider) -> str:
    """Return the model name of `lm`, which `provider` must serve.

    Raises:
        ValueError: If `lm.model` names a provider other than `provider`.
        InvalidValueError: If `lm.model` is not a `<provider>/<model>` reference.
    """
    model_ref = parse_model_ref(str(lm.model), "lm.model")
    if model_ref.provider is not provider:
        msg = f"provider {provider.value!r} does not serve the LM {str(lm.model)!r}"
        raise ValueError(msg)
    return model_ref.model


class RailedLM(dspy.BaseLM):  # type: ignore[misc]  # dspy exports it untyped
    """A DSPy LM whose every call runs under the rails of a tuning run.

    The call is made from the optimizer's thread and runs as a coroutine on the
    given loop, on a copy of the wrapped LM, so history is never shared. Calls
    are not retried, whatever `num_retries` the wrapped LM was built with, since a
    retry would reach the provider without being counted or charged. The rail
    reports the wrapped LM's capabilities
    (`supported_params` and the `supports_*` flags), so an adapter builds the
    same request as it does for the wrapped LM.
    """

    def __init__(  # noqa: PLR0913  # the rail's whole contract, one argument each
        self,
        lm: dspy.LM,
        *,
        provider: Provider,
        budgets: Sequence[Budget],
        timeout_s: float,
        task: Task,
        task_version: str,
        trace: OperationTrace | None,
        loop: asyncio.AbstractEventLoop,
        guard: RailGuard,
    ) -> None:
        """Wrap a registry LM.

        Args:
            lm: The task's LM.
            provider: The provider serving `lm`; it must be the provider named
                by `lm.model`. OpenRouter's cost, model and tokens are read from
                the copy's history; claude-sdk's are metered by its engine.
            budgets: Budgets that must all have room before a call and are all
                charged with its cost.
            timeout_s: Seconds a call may take.
            task: The task the calls serve, recorded on each span.
            task_version: Version of the task's program, recorded on each span.
            trace: The operation trace the spans go under, or None to use the
                current operation trace, if there is one.
            loop: The running loop that runs the calls.
            guard: The guard shared by all the rails of one run.

        Raises:
            ValueError: If `lm.model` names a provider other than `provider`.
            InvalidValueError: If `lm.model` is not a `<provider>/<model>`
                reference.
        """
        super().__init__(str(lm.model), cache=False, num_retries=0)
        self._model_name = _check_provider(lm, provider)
        self._lm = lm
        self._provider = provider
        self._budgets = tuple(budgets)
        self._timeout_s = timeout_s
        self._task = task
        self._task_version = task_version
        self._trace = trace
        self._loop = loop
        self._guard = guard

    @property
    def guard(self) -> RailGuard:
        """The guard this rail counts against."""
        return self._guard

    @property
    @override
    def supported_params(self) -> set[str]:
        """The request parameters the wrapped LM accepts."""
        return set(self._lm.supported_params)

    @property
    @override
    def supports_response_schema(self) -> bool:
        """Whether the wrapped LM accepts a response schema."""
        return bool(self._lm.supports_response_schema)

    @property
    @override
    def supports_function_calling(self) -> bool:
        """Whether the wrapped LM supports function calling."""
        return bool(self._lm.supports_function_calling)

    @property
    @override
    def supports_reasoning(self) -> bool:
        """Whether the wrapped LM supports reasoning."""
        return bool(self._lm.supports_reasoning)

    @override
    def __call__(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[Mapping[str, Any]] | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """Run one call on the loop and wait for its outputs.

        Call it from a thread other than the loop's: the loop cannot run the call
        while its own thread waits for it, so a call made there raises
        `RuntimeError` at once.

        Args:
            prompt: The prompt, when the call is made in that shape.
            messages: The chat messages, when the call is made in that shape.
            **kwargs: Request parameters for the wrapped LM.

        Returns:
            The model's outputs.

        Raises:
            CapReachedError: The guard is stopped or a budget has no room;
                nothing was called or charged.
            CostUnknownError: The call's cost cannot be found. The guard is
                stopped.
            ProviderUnavailableError: The provider cannot be used. The guard is
                stopped.
            ProviderFailedError: The provider failed or the call timed out.
        """
        self._refuse_on_the_loops_thread()
        future = asyncio.run_coroutine_threadsafe(
            self._rail(prompt, messages, kwargs), self._loop
        )
        return future.result()

    @override
    def copy(self, **kwargs: Any) -> "RailedLM":
        """Return a rail with the same guard and budgets around a changed LM.

        Args:
            **kwargs: Updates applied to the wrapped LM, as `dspy.LM.copy` does.

        Returns:
            The new rail.
        """
        return RailedLM(
            self._lm.copy(**kwargs),
            provider=self._provider,
            budgets=self._budgets,
            timeout_s=self._timeout_s,
            task=self._task,
            task_version=self._task_version,
            trace=self._trace,
            loop=self._loop,
            guard=self._guard,
        )

    def __deepcopy__(self, _memo: dict[int, Any]) -> "RailedLM":
        """Return a rail around a copy of the LM, on the same rails.

        A copy must still count against the same guard and budgets and run on the
        same loop under the same trace, so all of them are shared, not copied.
        """
        return self.copy()

    def _refuse_on_the_loops_thread(self) -> None:
        """Raise `RuntimeError` when the caller is running on the rail's loop."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        if running is self._loop:
            msg = "a railed call cannot be made on its event loop's own thread"
            raise RuntimeError(msg)

    def _refuse_if_blocked(self) -> None:
        """Raise `CapReachedError`, spending nothing, when no call may start."""
        if self._guard.stopped:
            raise CapReachedError
        if not all(budget.try_start() for budget in self._budgets):
            self._guard.cap_reached = True
            raise CapReachedError

    async def _rail(
        self,
        prompt: str | None,
        messages: Sequence[Mapping[str, Any]] | None,
        kwargs: Mapping[str, Any],
    ) -> list[Any]:
        """Run one call under the rails, on the loop."""
        self._refuse_if_blocked()
        guard = self._guard
        guard.calls += 1
        try:
            return await self._attempt(prompt, messages, kwargs)
        finally:
            if guard.on_call is not None:
                try:
                    guard.on_call(guard)
                except Exception:
                    # An isolation point: progress must never change a result.
                    logger.exception("on_call raised; the call's result is kept")

    async def _attempt(
        self,
        prompt: str | None,
        messages: Sequence[Mapping[str, Any]] | None,
        kwargs: Mapping[str, Any],
    ) -> list[Any]:
        """Call a copy of the LM, then meter, charge and count the call."""
        attempt = self._lm.copy(num_retries=0)
        failure: Exception | None = None
        outputs: list[Any] = []
        with (
            open_meter() as meter,
            call_span(
                provider=self._provider,
                model=self._model_name,
                meter=meter,
                history=attempt.history,
                inputs={"prompt": prompt, "messages": messages},
                task=self._task,
                task_version=self._task_version,
                posting_id=None,
                trace=self._trace,
            ),
        ):
            try:
                async with asyncio.timeout(self._timeout_s):
                    if messages is not None:
                        with adapter_message_call(attempt, messages):
                            outputs = await attempt.acall(messages=messages, **kwargs)
                    else:
                        outputs = await attempt.acall(prompt, **kwargs)
            except asyncio.CancelledError:
                # The provider bills what the meter already shows, cancelled or not.
                self._settle(meter, attempt.history)
                raise
            except Exception as error:  # noqa: BLE001  # every failure is counted and translated
                failure = error
            unknown = self._settle(meter, attempt.history)
            if unknown is not None:
                raise unknown
            if failure is not None:
                translated = self._count_failure(failure)
                if translated is failure or translated is failure.__cause__:
                    raise translated
                raise translated from failure
        return outputs

    def _settle(
        self, meter: CallMeter, history: list[Mapping[str, Any]]
    ) -> CostUnknownError | None:
        """Charge what the meter shows, stopping the run if a cost cannot be found.

        Args:
            meter: The call's meter.
            history: The history of the LM copy the call ran on.

        Returns:
            The error to raise when the call completed with no known cost, or None.
        """
        meter_from_history(self._provider, meter, history)
        if not meter.completed:
            return None
        if meter.cost_usd is None:
            unknown = CostUnknownError(str(self._lm.model))
            self._guard.fatal = unknown
            self._guard.stop()
            return unknown
        for budget in self._budgets:
            budget.charge(meter.cost_usd)
        return None

    def _count_failure(self, failure: Exception) -> LlmError:
        """Count a failed call, stopping the run on an error that must end it."""
        translated = (
            failure
            if isinstance(failure, LlmError)
            else translate_failure(failure, self._timeout_s, self._task)
        )
        if isinstance(translated, CostUnknownError | ProviderUnavailableError):
            self._guard.fatal = translated
            self._guard.stop()
            return translated
        self._guard.failed += 1
        if self._guard.first_error is None:
            self._guard.first_error = str(translated)
        return translated
