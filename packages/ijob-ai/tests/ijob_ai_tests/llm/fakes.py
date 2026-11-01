"""Test doubles for the model layer: scripted engines and recorded OpenRouter bodies.

Nothing here performs I/O. The engines answer at the DSPy boundary with
scripted `Response` objects, and the OpenRouter bodies are served through
lm15's `FakeTransport`, so the real adapter parses them.
"""

import asyncio
import importlib
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from types import TracebackType
from typing import Any, NoReturn, override

import dspy
import pytest
from dspy.lm15 import Message, Request, Response, RouterConfig, TextPart, Usage
from ijob_ai.llm.meter import CallMeter, current_meter

# DSPy's vendored lm15 ships a py.typed marker, so a static import of it makes
# mypy resolve the whole `dspy` package and reject the untyped `dspy` names used
# elsewhere. Loading these two modules at run time keeps `dspy` untyped.
_testing = importlib.import_module("dspy._vendor.lm15.testing")
_transports = importlib.import_module("dspy._vendor.lm15.transports")

type Opaque = Any
"""A value of a vendored lm15 type that mypy does not model."""

JUNK = "this is not json at all"
VERDICT = '{"keep": true, "reason": "fits"}'


class Judge(dspy.Signature):  # type: ignore[misc]
    """Judge the posting."""

    posting: str = dspy.InputField()
    keep: bool = dspy.OutputField()
    reason: str = dspy.OutputField()


def answer(text: str) -> Response:
    """Return an assistant `Response` carrying `text`."""
    return Response(
        id="fake",
        model="fake",
        message=Message(role="assistant", parts=(TextPart(text),)),
        finish_reason="stop",
        usage=Usage(input_tokens=5, output_tokens=2),
    )


class FakeEngine:
    """Sync companion of `FakeAsyncEngine`; `acall` never uses it."""

    def complete(self, _request: Request) -> Response:
        """Refuse: only the async engine is scripted."""
        msg = "the sync engine is not scripted"
        raise RuntimeError(msg)

    def stream(self, request: Request) -> Iterator[Opaque]:
        """Refuse: streaming is not used."""
        raise NotImplementedError

    def close(self) -> None:
        """Release nothing."""


class FakeAsyncEngine:
    """An async engine that answers scripted texts and meters like the claude engine.

    Attributes:
        requests: Every request the engine received, in order.
        meters_seen: The call meter open when each request arrived, in order.
    """

    def __init__(
        self,
        texts: Sequence[str] = (VERDICT,),
        *,
        cost: float | None = 0.001,
        meters: bool = True,
        raises: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        """Script the engine.

        Args:
            texts: Answers in order; the last one repeats once the list runs out.
            cost: Cost to meter per call, or None to complete the meter without a cost.
            meters: Whether to mark the call completed and meter its cost.
            raises: Exception to raise instead of answering (the meter is left
                untouched, as for an engine that failed before any answer).
            delay_s: Seconds to sleep before answering.
        """
        self._texts = list(texts)
        self._cost = cost
        self._meters = meters
        self._raises = raises
        self._delay_s = delay_s
        self.requests: list[Request] = []
        self.meters_seen: list[CallMeter] = []

    async def complete(self, request: Request) -> Response:
        """Record the request, then raise or answer the next scripted text."""
        index = len(self.requests)
        self.requests.append(request)
        self.meters_seen.append(current_meter())
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._raises is not None:
            raise self._raises
        if self._meters:
            meter = current_meter()
            meter.completed = True
            meter.cost_usd = self._cost
        return answer(self._texts[min(index, len(self._texts) - 1)])

    def stream(self, request: Request) -> NoReturn:
        """Refuse: streaming is not used."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release nothing."""


class KeyedCostEngine(FakeAsyncEngine):
    """An engine whose cost and delay depend on a marker in the request text."""

    def __init__(
        self, costs_by_marker: dict[str, float], delays_by_marker: dict[str, float]
    ) -> None:
        """Script one cost and one delay per marker.

        Args:
            costs_by_marker: Cost to meter when the request text contains the marker.
            delays_by_marker: Seconds to sleep for that marker.
        """
        super().__init__()
        self._costs = costs_by_marker
        self._delays = delays_by_marker

    @override
    async def complete(self, request: Request) -> Response:
        """Meter the cost of the marker found in the request."""
        text = "".join(
            part.text
            for message in request.messages
            for part in message.parts
            if isinstance(part, TextPart)
        )
        marker = next(m for m in self._costs if m in text)
        self.requests.append(request)
        meter = current_meter()
        self.meters_seen.append(meter)
        await asyncio.sleep(self._delays[marker])
        meter.completed = True
        meter.cost_usd = self._costs[marker]
        return answer(VERDICT)


def openrouter_body(text: str, *, cost: float | None) -> bytes:
    """Return a recorded-shape OpenRouter chat completion body.

    Args:
        text: The assistant text.
        cost: The `usage.cost` figure, or None to leave the key out.

    Returns:
        The JSON body bytes.
    """
    usage: dict[str, Any] = {
        "prompt_tokens": 11,
        "completion_tokens": 4,
        "total_tokens": 15,
    }
    if cost is not None:
        usage["cost"] = cost
    body = {
        "id": "gen-fake-1",
        "model": "anthropic/claude-sonnet-4.5",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": usage,
    }
    return json.dumps(body).encode()


def ok_response(text: str, *, cost: float | None) -> Opaque:
    """Return a wire response with HTTP 200 and an OpenRouter body.

    Args:
        text: The assistant text.
        cost: The `usage.cost` figure, or None to leave the key out.
    """
    return _testing.FakeResponse(200, openrouter_body(text, cost=cost))


def rate_limited() -> Opaque:
    """Return a wire response with HTTP 429."""
    body = json.dumps({"error": {"message": "rate limited"}}).encode()
    return _testing.FakeResponse(429, body)


class _ServedResponse:
    """Async context manager that serves one scripted wire response."""

    def __init__(self, response: Opaque) -> None:
        self._response = response

    async def __aenter__(self) -> Opaque:
        wire = self._response

        async def chunks() -> AsyncIterator[bytes]:
            for chunk in wire:
                yield chunk

        async def release(_body_consumed: bool) -> None:  # noqa: FBT001
            return None

        return _transports.AsyncTransportResponse(
            status=wire.status,
            reason=wire.reason,
            headers=wire.headers or [],
            http_version=wire.http_version,
            chunks=chunks(),
            release=release,
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class AsyncFakeTransport:
    """Async face of lm15's `FakeTransport`: the script and the record are its.

    Attributes:
        requests: Every wire request received, in order.
    """

    def __init__(self, responses: Sequence[Opaque]) -> None:
        """Script the wire responses.

        Args:
            responses: Wire responses to serve in order.
        """
        self._inner = _testing.FakeTransport(list(responses))
        self.requests: list[Opaque] = self._inner.requests

    def stream(self, request: Opaque) -> _ServedResponse:
        """Record the request and serve the next scripted response."""
        return _ServedResponse(self._inner.stream(request))

    async def aclose(self) -> None:
        """Release nothing."""


def serve_openrouter(
    monkeypatch: pytest.MonkeyPatch, responses: Sequence[Opaque]
) -> AsyncFakeTransport:
    """Route OpenRouter LMs through an `AsyncFakeTransport` and set a dummy key.

    Args:
        monkeypatch: The fixture that undoes the patch after the test.
        responses: Wire responses to serve in order.

    Returns:
        The transport, whose `requests` records every wire request.
    """
    transport = AsyncFakeTransport(responses)

    def router_config(**kwargs: Opaque) -> RouterConfig:
        kwargs["transport"] = transport
        return RouterConfig(**kwargs)

    monkeypatch.setattr("dspy.clients.execution.RouterConfig", router_config)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-for-tests")
    return transport
