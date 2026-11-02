"""The single boundary every model call passes through: metering, tracing and errors.

Inside an operation trace each call also records one span. This module imports
DSPy, so it is imported only when a model is about to be used.
"""

import asyncio
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final, assert_never, override

import dspy
from dspy.utils.exceptions import AdapterParseError, LMError
from ijob_core.model import PostingId
from opentelemetry.trace import Span

from ijob_ai.errors import (
    CostUnknownError,
    LlmError,
    ProviderFailedError,
    ProviderUnavailableError,
    TaskOutputError,
)
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.meter import CallMeter, open_meter
from ijob_ai.llm.reference import Provider, parse_model_ref
from ijob_ai.llm.settings import Task
from ijob_ai.tracing import attributes
from ijob_ai.tracing.attributes import Outcome
from ijob_ai.tracing.setup import current_operation_trace

_PARSE_REASON = "the answer did not match the expected fields"
_CHAT: Final = "chat"
_MAX_TEXT_BYTES: Final = 16 * 1024
_TRUNCATED: Final = "[truncated]"


class _ForgivingJSONAdapter(dspy.JSONAdapter):  # type: ignore[misc]  # dspy exports it untyped
    """The JSON adapter, also reading an answer written in `[[ ## field ## ]]` sections.

    A model asked for JSON sometimes answers correctly in DSPy's section form.
    """

    @override
    def parse(self, signature: Any, completion: str) -> dict[str, Any]:
        """Read a completion as JSON, or else as sections.

        Raises:
            AdapterParseError: The completion is neither.
        """
        try:
            parsed: dict[str, Any] = super().parse(signature, completion)
        except AdapterParseError:
            parsed = dspy.ChatAdapter().parse(signature, completion)
        return parsed


def _openrouter_cost(entry: Mapping[str, Any]) -> float | None:
    """Return the cost OpenRouter billed for a history entry, else DSPy's estimate."""
    response = entry.get("response")
    provider_data = getattr(response, "provider_data", None)
    usage = provider_data.get("usage") if isinstance(provider_data, Mapping) else None
    billed = usage.get("cost") if isinstance(usage, Mapping) else None
    if isinstance(billed, int | float):
        return float(billed)
    estimate = entry.get("cost")
    if isinstance(estimate, int | float):
        return float(estimate)
    return None


def _count(value: object) -> int | None:
    """Return `value` when it is a token count, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _meter_openrouter(meter: CallMeter, history: list[Mapping[str, Any]]) -> None:
    """Fill the meter from the LM copy's last history entry, when it has one."""
    if not history:
        return
    entry = history[-1]
    meter.completed = True
    meter.cost_usd = _openrouter_cost(entry)
    response_model = entry.get("response_model")
    if isinstance(response_model, str):
        meter.model = response_model
    usage = entry.get("usage")
    if isinstance(usage, Mapping):
        meter.input_tokens = _count(usage.get("prompt_tokens"))
        meter.output_tokens = _count(usage.get("completion_tokens"))


def _meter_from_history(
    provider: Provider, meter: CallMeter, history: list[Mapping[str, Any]]
) -> None:
    """Complete the meter from history for a provider that does not meter itself."""
    match provider:
        case Provider.OPENROUTER:
            _meter_openrouter(meter, history)
        case Provider.CLAUDE_SDK:
            pass
        case _:
            assert_never(provider)


def _translate(failure: Exception, timeout_s: float, task: Task) -> LlmError:
    """Map a DSPy or timeout failure onto the project's error family."""
    if isinstance(failure, AdapterParseError):
        return TaskOutputError(task.value, _PARSE_REASON)
    if isinstance(failure, LMError):
        cause = failure.__cause__
        if isinstance(cause, LlmError):
            return cause
        return ProviderFailedError(f"{type(failure).__name__}: {failure}")
    if isinstance(failure, TimeoutError):
        return ProviderFailedError(f"timed out after {timeout_s:g} s")
    return ProviderFailedError(f"{type(failure).__name__}: {failure}")


def _outcome(error: LlmError) -> Outcome:
    """Return how a call ended, given the error it raised."""
    if isinstance(error, TaskOutputError):
        return Outcome.TASK_OUTPUT_ERROR
    if isinstance(error, ProviderUnavailableError):
        return Outcome.PROVIDER_UNAVAILABLE
    if isinstance(error, CostUnknownError):
        return Outcome.COST_UNKNOWN
    if isinstance(error.__cause__, TimeoutError):
        return Outcome.TIMEOUT
    return Outcome.PROVIDER_FAILED


def _clip(text: str) -> str:
    """Cut `text` to 16 KB of UTF-8, ending in `[truncated]` when it was cut."""
    raw = text.encode()
    if len(raw) <= _MAX_TEXT_BYTES:
        return text
    keep = raw[: _MAX_TEXT_BYTES - len(_TRUNCATED)]
    return keep.decode(errors="ignore") + _TRUNCATED


def _record_exchange(
    span: Span, history: list[Mapping[str, Any]], inputs: Mapping[str, object]
) -> None:
    """Record the prompt and completion of the last history entry.

    A call that failed before any history entry has no prompt of its own, so
    its inputs stand in for it, as JSON with sorted keys.
    """
    if not history:
        fallback = json.dumps(
            dict(inputs), ensure_ascii=False, sort_keys=True, default=str
        )
        span.set_attribute(attributes.PROMPT, _clip(fallback))
        return
    entry = history[-1]
    prompt = entry.get("messages") or entry.get("prompt")
    if prompt is not None:
        span.set_attribute(
            attributes.PROMPT,
            _clip(json.dumps(prompt, ensure_ascii=False, default=str)),
        )
    outputs = entry.get("outputs")
    if outputs:
        completion = "\n".join(str(output) for output in outputs)
        span.set_attribute(attributes.COMPLETION, _clip(completion))


def _record_call(  # noqa: PLR0913  # one fact of the call per argument
    span: Span,
    *,
    provider: Provider,
    model: str,
    meter: CallMeter,
    history: list[Mapping[str, Any]],
    inputs: Mapping[str, object],
    task: Task,
    task_version: str,
    posting_id: PostingId | None,
    outcome: Outcome | None,
) -> None:
    """Set the span's attributes from what the call used and how it ended."""
    span.set_attribute(attributes.PROVIDER_NAME, attributes.PROVIDER_NAMES[provider])
    span.set_attribute(attributes.OPERATION_NAME, _CHAT)
    span.set_attribute(attributes.REQUEST_MODEL, model)
    span.set_attribute(attributes.TASK, task.value)
    span.set_attribute(attributes.TASK_VERSION, task_version)
    if posting_id is not None:
        span.set_attribute(attributes.POSTING_ID, posting_id)
    if outcome is not None:
        span.set_attribute(attributes.OUTCOME, outcome.value)
    if meter.model is not None:
        span.set_attribute(attributes.RESPONSE_MODEL, meter.model)
    if meter.input_tokens is not None:
        span.set_attribute(attributes.INPUT_TOKENS, meter.input_tokens)
    if meter.output_tokens is not None:
        span.set_attribute(attributes.OUTPUT_TOKENS, meter.output_tokens)
    if meter.cost_usd is not None:
        span.set_attribute(attributes.COST, meter.cost_usd)
    _record_exchange(span, history, inputs)


@contextmanager
def _call_span(  # noqa: PLR0913  # one fact of the call per argument
    *,
    provider: Provider,
    model: str,
    meter: CallMeter,
    history: list[Mapping[str, Any]],
    inputs: Mapping[str, object],
    task: Task,
    task_version: str,
    posting_id: PostingId | None,
) -> Iterator[None]:
    """Record the enclosed call as a span of the current operation trace.

    Does nothing outside an operation trace. An error that is not an `LlmError`
    leaves the outcome unset.
    """
    trace = current_operation_trace()
    if trace is None:
        yield
        return
    with trace.child(f"{_CHAT} {model}") as span:
        outcome: Outcome | None = None
        try:
            yield
            outcome = Outcome.OK
        except LlmError as error:
            outcome = _outcome(error)
            raise
        finally:
            _record_call(
                span,
                provider=provider,
                model=model,
                meter=meter,
                history=history,
                inputs=inputs,
                task=task,
                task_version=task_version,
                posting_id=posting_id,
                outcome=outcome,
            )


async def run_call(  # noqa: PLR0913  # the call's whole contract, one argument each
    predict: dspy.Predict,
    lm: dspy.LM,
    *,
    inputs: Mapping[str, object],
    provider: Provider,
    budget: Budget,
    timeout_s: float,
    task: Task,
    task_version: str,
    posting_id: PostingId | None,
) -> dspy.Prediction:
    """Run a predictor on a copy of the LM, charge its cost and translate failures.

    The call runs with the JSON adapter, which also accepts an answer written
    in `[[ ## field ## ]]` sections, under a timeout, with a fresh meter.
    Whether the call parsed or not, its metered cost is charged to the budget;
    a call that failed before any answer is charged nothing. The caller checks
    `Budget.try_start` before calling.

    Inside an operation trace the call is recorded as a span named
    `chat <model>`, with the provider, model, tokens, cost, task, outcome,
    prompt and completion. A call that failed before the model recorded a
    prompt carries `inputs` as JSON instead. Prompt and completion are each cut
    to 16 KB. Outside one, no span is opened.

    Args:
        predict: The DSPy predictor to run.
        lm: The task's LM; the call runs on a copy, so concurrent calls do not
            share history.
        inputs: Keyword inputs of the predictor's signature.
        provider: The provider serving `lm`; it must be the provider named by
            `lm.model`. OpenRouter's cost, model and tokens are read from the
            copy's history; claude-sdk's are metered by its engine.
        budget: The operation's budget, charged with the call's cost.
        timeout_s: Seconds the call may take.
        task: The task the call serves; named in a `TaskOutputError` and
            recorded on the span.
        task_version: Version of the task's program, recorded on the span.
        posting_id: The posting the call is about, or None when it is about
            none; recorded on the span.

    Returns:
        The predictor's output.

    Raises:
        ValueError: If `lm.model` names a provider other than `provider`;
            nothing is called and nothing is charged.
        InvalidValueError: If `lm.model` is not a `<provider>/<model>` reference.
        CostUnknownError: If the call completed and its cost cannot be found.
            It takes precedence over any other failure of the same call.
        TaskOutputError: If the answer did not match the signature.
        ProviderFailedError: If the provider failed or the call timed out.
        LlmError: Any error an engine raised from this family, unchanged.
    """
    model_ref = parse_model_ref(str(lm.model), "lm.model")
    if model_ref.provider is not provider:
        msg = f"provider {provider.value!r} does not serve the LM {str(lm.model)!r}"
        raise ValueError(msg)
    attempt = lm.copy()
    failure: Exception | None = None
    with (
        open_meter() as meter,
        _call_span(
            provider=provider,
            model=model_ref.model,
            meter=meter,
            history=attempt.history,
            inputs=inputs,
            task=task,
            task_version=task_version,
            posting_id=posting_id,
        ),
    ):
        try:
            async with asyncio.timeout(timeout_s):
                with dspy.context(lm=attempt, adapter=_ForgivingJSONAdapter()):
                    prediction = await predict.acall(**inputs)
        except (AdapterParseError, LMError, TimeoutError) as error:
            failure = error
        finally:
            _meter_from_history(provider, meter, attempt.history)
            if meter.completed:
                if meter.cost_usd is None:
                    raise CostUnknownError(str(lm.model))
                budget.charge(meter.cost_usd)
        if failure is not None:
            translated = _translate(failure, timeout_s, task)
            if translated is failure.__cause__:
                raise translated
            raise translated from failure
    return prediction
