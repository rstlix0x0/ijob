"""Test doubles for the jury: engines that answer by task and a served OpenRouter.

Nothing here reaches a model or the network. A `JurorEngine` answers a judge or a
scorer request with scripted text, a `JurorEngines` factory hands one engine to
each juror's model, and `serve_openrouter` routes OpenRouter LMs through a fake
transport so their cost is read from a recorded-shape body.
"""

import asyncio
import importlib
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, NoReturn

import pytest
from ijob_ai.llm.meter import current_meter
from ijob_ai.llm.reference import ModelRef

from .llm_fakes import (
    FakeEngine,
    Opaque,
    answer,
    request_text,
    score_answer,
    verdict_answer,
)

_testing = importlib.import_module("dspy._vendor.lm15.testing")
_transports = importlib.import_module("dspy._vendor.lm15.transports")
_router_config = importlib.import_module("dspy.lm15").RouterConfig

_SCORER_MARKER = "[[ ## profile ## ]]"


class JurorEngine:
    """An async engine that answers a judge or a scorer request with scripted text.

    Attributes:
        calls: The kind of every request received, `judge` or `scorer`, in order.
    """

    def __init__(  # one knob of the script per argument
        self,
        *,
        judge: Sequence[str] = (),
        scorer: Sequence[str] = (),
        cost: float | None = 0.001,
        raises: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        """Script the engine.

        Args:
            judge: Answers to judge requests in order, the last repeating; a
                keeping verdict when empty.
            scorer: Answers to scorer requests in order, the last repeating; a
                score of 70 when empty.
            cost: Cost to meter per call in US dollars, or None to complete the
                call without a cost.
            raises: Exception to raise on every call instead of answering.
            delay_s: Seconds to sleep before answering.
        """
        self._texts = {
            "judge": list(judge) or [verdict_answer()],
            "scorer": list(scorer) or [score_answer(70)],
        }
        self._cost = cost
        self._raises = raises
        self._delay_s = delay_s
        self.calls: list[str] = []

    async def complete(self, request: Opaque) -> Opaque:
        """Record the kind of request, then raise or meter and answer."""
        kind = "scorer" if _SCORER_MARKER in request_text(request) else "judge"
        index = self.calls.count(kind)
        self.calls.append(kind)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._raises is not None:
            raise self._raises
        meter = current_meter()
        meter.completed = True
        meter.cost_usd = self._cost
        texts = self._texts[kind]
        return answer(texts[min(index, len(texts) - 1)])

    def stream(self, _request: Opaque) -> NoReturn:
        """Refuse: streaming is not used."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release nothing."""


class JurorEngines:
    """An engine factory that gives each claude-sdk model its own engine.

    Attributes:
        calls: The `(model, cli_path)` pair of every request, in order.
    """

    def __init__(self, engines: dict[str, JurorEngine] | None = None) -> None:
        """Name the engine of each model.

        Args:
            engines: Engines keyed by model name, the part of the reference after
                `claude-sdk/`; a model without one gets a default engine.
        """
        self._engines = dict(engines or {})
        self.calls: list[tuple[ModelRef, Path]] = []

    def engine(self, model: str) -> JurorEngine:
        """Return the engine of `model`, making a default one the first time."""
        return self._engines.setdefault(model, JurorEngine())

    def __call__(self, ref: ModelRef, cli_path: Path) -> tuple[FakeEngine, JurorEngine]:
        """Record the request and return the sync stand-in and the model's engine."""
        self.calls.append((ref, cli_path))
        return FakeEngine(), self.engine(ref.model)


def openrouter_reply(text: str, *, cost: float) -> Opaque:
    """Return a wire response with HTTP 200 and an OpenRouter body.

    Args:
        text: The assistant text.
        cost: The `usage.cost` figure.
    """
    body: dict[str, Any] = {
        "id": "gen-fake-1",
        "model": "made-up/model",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
            "cost": cost,
        },
    }
    return _testing.FakeResponse(200, json.dumps(body).encode())


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


class KindFakeTransport:
    """Fake transport that answers each wire request by what it asks.

    A scorer request carries the profile marker and gets the scorer reply, any
    other request gets the judge reply, whatever order concurrent calls arrive in.

    Attributes:
        requests: Every wire request received, in arrival order.
    """

    def __init__(self, *, judge: str, scorer: str, cost: float) -> None:
        """Script the two replies.

        Args:
            judge: The assistant text served to a judge request.
            scorer: The assistant text served to a scorer request.
            cost: The `usage.cost` figure of every reply.
        """
        self._judge = judge
        self._scorer = scorer
        self._cost = cost
        self.requests: list[Opaque] = []

    def stream(self, request: Opaque) -> _ServedResponse:
        """Record the request and serve the reply for its kind."""
        self.requests.append(request)
        scorer = _SCORER_MARKER.encode() in request.body
        text = self._scorer if scorer else self._judge
        return _ServedResponse(openrouter_reply(text, cost=self._cost))

    async def aclose(self) -> None:
        """Release nothing."""


def _route_openrouter(monkeypatch: pytest.MonkeyPatch, transport: Opaque) -> None:
    """Route OpenRouter LMs through `transport` and set a dummy key."""

    def router_config(**kwargs: Opaque) -> Opaque:
        kwargs["transport"] = transport
        return _router_config(**kwargs)

    monkeypatch.setattr("dspy.clients.execution.RouterConfig", router_config)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-for-tests")


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
    _route_openrouter(monkeypatch, transport)
    return transport


def serve_openrouter_by_kind(
    monkeypatch: pytest.MonkeyPatch, *, judge: str, scorer: str, cost: float
) -> KindFakeTransport:
    """Route OpenRouter LMs through a `KindFakeTransport` and set a dummy key.

    Args:
        monkeypatch: The fixture that undoes the patch after the test.
        judge: The assistant text served to a judge request.
        scorer: The assistant text served to a scorer request.
        cost: The `usage.cost` figure of every reply.

    Returns:
        The transport, whose `requests` records every wire request.
    """
    transport = KindFakeTransport(judge=judge, scorer=scorer, cost=cost)
    _route_openrouter(monkeypatch, transport)
    return transport
