import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.tasks.text import posting_block
from ijob_app.eval_estimate import (
    SUBSCRIPTION,
    UNKNOWN,
    CallKind,
    PlannedCall,
    Price,
    Prices,
    cost_by_model,
    estimate,
    planned_calls,
    read_prices,
    total_cost,
)
from ijob_app.eval_ops import VoteItem
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalTask
from ijob_core.model import Posting, PostingId
from ijob_sources.polite_http import PoliteClient

_OPENROUTER = ModelRef(Provider.OPENROUTER, "made-up/juror")
_CLAUDE = ModelRef(Provider.CLAUDE_SDK, "made-up-model")
_PRICES = {"made-up/juror": Price(input_usd=2e-6, output_usd=10e-6)}


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _client(models: httpx.Response | str) -> PoliteClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if isinstance(models, httpx.Response):
            return models
        return httpx.Response(200, text=models)

    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )


async def _read(models: httpx.Response | str) -> Prices | None:
    async with _client(models) as client:
        return await read_prices(client)


def _list(*entries: tuple[str, object]) -> str:
    return json.dumps(
        {"data": [{"id": id_, "pricing": pricing} for id_, pricing in entries]}
    )


def test_read_prices_reads_per_token_prices() -> None:
    body = _list(
        ("made-up/juror", {"prompt": "0.000002", "completion": "0.00001"}),
        ("made-up/router", {"prompt": "-1", "completion": "-1"}),
        ("made-up/no-prices", {}),
    )
    assert asyncio.run(_read(body)) == _PRICES


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, text='{"data": "oops"}'),
        httpx.Response(200, text="[]"),
    ],
)
def test_unreadable_list_reads_unknown(response: httpx.Response) -> None:
    prices = asyncio.run(_read(response))

    assert prices is None
    rows = estimate([PlannedCall(_OPENROUTER, CallKind.JUDGE, 100, 6132)], prices)
    assert [row.cost for row in rows] == [UNKNOWN]


def test_estimate_checked_example() -> None:
    calls = [
        PlannedCall(_OPENROUTER, CallKind.JUDGE, 100, 6132),
        PlannedCall(_OPENROUTER, CallKind.SCORER, 100, 6132),
    ]

    judge, scorer = estimate(calls, _PRICES)

    assert judge.input_tokens == pytest.approx(100 * (6132 / 4 + 500))
    assert judge.output_tokens == 100 * 300
    assert scorer.input_tokens == pytest.approx(100 * (6132 / 4 + 1000))
    assert scorer.output_tokens == 100 * 600
    assert isinstance(judge.cost, float)
    assert isinstance(scorer.cost, float)
    assert judge.cost + scorer.cost == pytest.approx(1.81, abs=0.005)


def test_estimate_prices_the_grader() -> None:
    (grader,) = estimate([PlannedCall(_OPENROUTER, CallKind.GRADER, 20, 4000)], _PRICES)

    assert grader.input_tokens == pytest.approx(20 * (4000 / 4 + 800))
    assert grader.output_tokens == 20 * 150
    assert grader.cost == pytest.approx(20 * (1800 * 2e-6 + 150 * 10e-6))


@pytest.mark.parametrize("prices", [None, {}, _PRICES])
def test_claude_sdk_reads_subscription(prices: Prices | None) -> None:
    (row,) = estimate([PlannedCall(_CLAUDE, CallKind.JUDGE, 100, 6132)], prices)

    assert row.cost == SUBSCRIPTION


def test_model_missing_from_list_is_refused() -> None:
    absent = ModelRef(Provider.OPENROUTER, "made-up/absent")

    with pytest.raises(InvalidValueError, match="made-up/absent"):
        estimate([PlannedCall(absent, CallKind.JUDGE, 1, 100)], _PRICES)


def _posting(number: int, description: str) -> Posting:
    moment = datetime(2026, 11, 3, tzinfo=UTC)
    return Posting(
        id=PostingId(number),
        title=f"Engineer {number}",
        company="Acme",
        location="Remote",
        url=f"https://example.com/{number}",
        posted_at=None,
        first_seen=moment,
        last_seen=moment,
        description=description,
    )


def test_planned_calls_group_by_model_and_kind() -> None:
    short, long = _posting(1, "a"), _posting(2, "b" * 400)
    items = [
        VoteItem(posting, task, juror)
        for posting in (short, long)
        for task in (EvalTask.JUDGE, EvalTask.SCORER)
        for juror in (_CLAUDE, _OPENROUTER)
    ]

    calls = planned_calls(items)

    assert [(call.ref, call.kind, call.count) for call in calls] == [
        (_CLAUDE, CallKind.JUDGE, 2),
        (_OPENROUTER, CallKind.JUDGE, 2),
        (_CLAUDE, CallKind.SCORER, 2),
        (_OPENROUTER, CallKind.SCORER, 2),
    ]
    mean = (len(posting_block(short)) + len(posting_block(long))) / 2
    assert [call.characters for call in calls] == [mean] * 4


def test_planned_calls_of_nothing_is_empty() -> None:
    assert planned_calls([]) == []


def test_total_cost_adds_dollars_and_ignores_subscription() -> None:
    rows = estimate(
        [
            PlannedCall(_OPENROUTER, CallKind.JUDGE, 100, 6132),
            PlannedCall(_CLAUDE, CallKind.JUDGE, 100, 6132),
        ],
        _PRICES,
    )

    total = total_cost(rows)

    assert isinstance(rows[0].cost, float)
    assert total == pytest.approx(rows[0].cost)


def test_total_cost_is_unknown_when_any_price_is_unknown() -> None:
    rows = estimate(
        [
            PlannedCall(_OPENROUTER, CallKind.JUDGE, 1, 100),
            PlannedCall(_CLAUDE, CallKind.JUDGE, 1, 100),
        ],
        None,
    )

    assert total_cost(rows) == UNKNOWN


def test_total_cost_of_subscription_only_is_subscription() -> None:
    rows = estimate([PlannedCall(_CLAUDE, CallKind.JUDGE, 1, 100)], None)

    assert total_cost(rows) == SUBSCRIPTION
    assert total_cost([]) == SUBSCRIPTION


def test_cost_by_model_sums_each_models_calls_in_order() -> None:
    rows = estimate(
        [
            PlannedCall(_CLAUDE, CallKind.JUDGE, 5, 100),
            PlannedCall(_OPENROUTER, CallKind.JUDGE, 5, 100),
            PlannedCall(_OPENROUTER, CallKind.SCORER, 5, 100),
        ],
        _PRICES,
    )

    by_model = cost_by_model(rows)

    assert [ref for ref, _ in by_model] == [_CLAUDE, _OPENROUTER]
    assert by_model[0][1] == SUBSCRIPTION
    dollars = [row.cost for row in rows[1:] if isinstance(row.cost, float)]
    assert len(dollars) == 2
    assert by_model[1][1] == pytest.approx(sum(dollars))
