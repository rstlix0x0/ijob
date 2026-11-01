"""Per-call meter of what a model call cost, carried in a context variable."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class CallMeter:
    """What one model call used, filled in by the provider that served it.

    Attributes:
        cost_usd: Cost in US dollars, or None while unknown.
        input_tokens: Tokens sent, or None while unknown.
        output_tokens: Tokens received, or None while unknown.
        model: The model that answered, or None until known.
        completed: True once a provider answered: a claude-sdk `ResultMessage`
            was received, or an OpenRouter history entry exists.
    """

    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    completed: bool = False


_NO_METER = "no call meter is open; wrap the call in open_meter()"

_CURRENT: ContextVar[CallMeter | None] = ContextVar("ijob_call_meter", default=None)


@contextmanager
def open_meter() -> Iterator[CallMeter]:
    """Install a fresh meter for the current context and remove it on exit.

    Yields:
        The new meter, returned by `current_meter` inside the block.
    """
    meter = CallMeter()
    token = _CURRENT.set(meter)
    try:
        yield meter
    finally:
        _CURRENT.reset(token)


def current_meter() -> CallMeter:
    """Return the meter of the enclosing `open_meter` block.

    Returns:
        The current meter.

    Raises:
        RuntimeError: If called outside an `open_meter` block.
    """
    meter = _CURRENT.get()
    if meter is None:
        raise RuntimeError(_NO_METER)
    return meter
