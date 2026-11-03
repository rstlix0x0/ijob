"""What an `ijob eval` run will cost, before it calls any model.

Prices come from OpenRouter's public model list, read through `PoliteClient`.
Token counts follow a fixed table per kind of call: the posting block's characters
divided by four, plus a fixed instruction overhead, in; a fixed number out. A
claude-sdk model costs `subscription`. If the list cannot be read, an OpenRouter
model costs `unknown`; a model missing from a list that was read is an error.
`planned_calls` turns the votes a jury run still needs into the calls to price,
and `total_cost` and `cost_by_model` sum the estimates.
"""

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from ijob_ai.llm.reference import ModelRef, Provider
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalTask
from ijob_sources.errors import SourcesError
from ijob_sources.polite_http import MAX_BODY_BYTES, PoliteClient

from ijob_app.eval_ops import VoteItem

PRICE_LIST_URL: Final = "https://openrouter.ai/api/v1/models"

SUBSCRIPTION: Final = "subscription"
"""The cost of a claude-sdk call, which draws on a subscription, not on dollars."""

UNKNOWN: Final = "unknown"
"""The cost of an OpenRouter call when the price list could not be read."""

type Cost = float | Literal["subscription", "unknown"]
"""A cost in US dollars, or why there is no dollar figure."""

type Prices = Mapping[str, Price]
"""Prices keyed by OpenRouter model id."""

_CHARACTERS_PER_TOKEN: Final = 4


class CallKind(StrEnum):
    """A kind of model call an eval run makes."""

    JUDGE = "judge"
    SCORER = "scorer"
    GRADER = "grader"


@dataclass(frozen=True, slots=True)
class _Shape:
    """The fixed token shape of one kind of call."""

    overhead_tokens: int
    output_tokens: int


_SHAPES: Final = {
    CallKind.JUDGE: _Shape(overhead_tokens=500, output_tokens=300),
    CallKind.SCORER: _Shape(overhead_tokens=1000, output_tokens=600),
    CallKind.GRADER: _Shape(overhead_tokens=800, output_tokens=150),
}


@dataclass(frozen=True, slots=True)
class Price:
    """What one token costs on a model.

    Attributes:
        input_usd: US dollars per input token.
        output_usd: US dollars per output token.
    """

    input_usd: float
    output_usd: float


@dataclass(frozen=True, slots=True)
class PlannedCall:
    """Calls of one kind that a run will make to one model.

    Attributes:
        ref: The model called.
        kind: The kind of call.
        count: How many calls.
        characters: Characters in the posting block of one call, on average.
    """

    ref: ModelRef
    kind: CallKind
    count: int
    characters: float


@dataclass(frozen=True, slots=True)
class CallEstimate:
    """The expected tokens and cost of a `PlannedCall`.

    Attributes:
        call: The calls estimated.
        input_tokens: Input tokens over all the calls.
        output_tokens: Output tokens over all the calls.
        cost: US dollars over all the calls, or `SUBSCRIPTION` or `UNKNOWN`.
    """

    call: PlannedCall
    input_tokens: float
    output_tokens: float
    cost: Cost


def _parse_price(pricing: object) -> Price | None:
    """Read a price from a list entry's `pricing`, or `None` if it has none.

    A negative price marks a router without a fixed price.
    """
    if not isinstance(pricing, dict):
        return None
    try:
        price = Price(float(pricing["prompt"]), float(pricing["completion"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(
        math.isfinite(p) and p >= 0 for p in (price.input_usd, price.output_usd)
    ):
        return None
    return price


async def read_prices(client: PoliteClient) -> Prices | None:
    """Read per-token prices from OpenRouter's public model list.

    Args:
        client: The client to fetch with; it sends ijob's User-Agent and obeys
            robots.txt.

    Returns:
        Prices keyed by model id, leaving out models without a fixed price, or
        `None` if the list could not be fetched or is not the expected JSON.
    """
    try:
        response = await client.get(PRICE_LIST_URL, max_bytes=MAX_BODY_BYTES)
    except SourcesError:
        return None
    try:
        entries = response.json()["data"]
        prices: dict[str, Price] = {}
        for entry in entries:
            price = _parse_price(entry.get("pricing"))
            if price is not None:
                prices[str(entry["id"])] = price
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return prices


def _cost(
    call: PlannedCall, prices: Prices | None, tokens: tuple[float, float]
) -> Cost:
    """Price a call's `(input, output)` tokens, or say why they have no price.

    Raises:
        InvalidValueError: The price list was read and lacks the model.
    """
    if call.ref.provider is Provider.CLAUDE_SDK:
        return SUBSCRIPTION
    if prices is None:
        return UNKNOWN
    price = prices.get(call.ref.model)
    if price is None:
        raise InvalidValueError(
            call.ref.model, "not in OpenRouter's price list; check the model name"
        )
    return tokens[0] * price.input_usd + tokens[1] * price.output_usd


def estimate(calls: Sequence[PlannedCall], prices: Prices | None) -> list[CallEstimate]:
    """Estimate the tokens and cost of each planned call.

    Args:
        calls: The calls a run will make.
        prices: The result of `read_prices`; `None` when the list was unreadable.

    Returns:
        One estimate per call, in order.

    Raises:
        InvalidValueError: `prices` was read and lacks an OpenRouter model of a
            call; the message names the model.
    """
    estimates: list[CallEstimate] = []
    for call in calls:
        shape = _SHAPES[call.kind]
        per_call_input = call.characters / _CHARACTERS_PER_TOKEN + shape.overhead_tokens
        tokens = (call.count * per_call_input, float(call.count * shape.output_tokens))
        estimates.append(CallEstimate(call, *tokens, _cost(call, prices, tokens)))
    return estimates


def planned_calls(items: Iterable[VoteItem]) -> list[PlannedCall]:
    """List the calls a jury run will make, one entry per model and kind.

    Args:
        items: The votes still to request.

    Returns:
        A `PlannedCall` per model and kind of call, judge before scorer and the
        models in the order they first appear, holding how many votes it covers
        and the mean length of their posting blocks.
    """
    from ijob_ai.tasks.text import posting_block  # noqa: PLC0415

    lengths: defaultdict[tuple[CallKind, ModelRef], list[int]] = defaultdict(list)
    for item in items:
        kind = CallKind.JUDGE if item.task is EvalTask.JUDGE else CallKind.SCORER
        lengths[kind, item.juror].append(len(posting_block(item.posting)))
    order = {kind: place for place, kind in enumerate(CallKind)}
    return [
        PlannedCall(ref, kind, len(sizes), sum(sizes) / len(sizes))
        for (kind, ref), sizes in sorted(
            lengths.items(), key=lambda entry: order[entry[0][0]]
        )
    ]


def total_cost(estimates: Sequence[CallEstimate]) -> Cost:
    """Add up the dollars of some estimates.

    Args:
        estimates: The estimates to add.

    Returns:
        The dollars of the priced calls; `UNKNOWN` if any call is unknown, and
        `SUBSCRIPTION` if none has a dollar figure.
    """
    return _sum_costs([estimate_.cost for estimate_ in estimates])


def cost_by_model(estimates: Sequence[CallEstimate]) -> list[tuple[ModelRef, Cost]]:
    """Add up the cost of each model's calls.

    Args:
        estimates: The estimates to add.

    Returns:
        The models in the order they first appear, each with its total cost.
    """
    costs: dict[ModelRef, list[Cost]] = {}
    for estimate_ in estimates:
        costs.setdefault(estimate_.call.ref, []).append(estimate_.cost)
    return [(ref, _sum_costs(each)) for ref, each in costs.items()]


def _sum_costs(costs: Sequence[Cost]) -> Cost:
    """Add dollar costs; unknown wins, and no dollars at all is a subscription."""
    if UNKNOWN in costs:
        return UNKNOWN
    dollars = [cost for cost in costs if isinstance(cost, float)]
    return sum(dollars) if dollars else SUBSCRIPTION
