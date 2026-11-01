import asyncio

import pytest
from ijob_ai.llm.meter import CallMeter, current_meter, open_meter


def test_meter_starts_empty() -> None:
    meter = CallMeter()
    assert meter.cost_usd is None
    assert meter.input_tokens is None
    assert meter.output_tokens is None
    assert meter.model is None
    assert meter.completed is False


def test_open_meter_sets_and_resets() -> None:
    with open_meter() as meter:
        assert current_meter() is meter
        meter.cost_usd = 0.3
    with pytest.raises(RuntimeError):
        current_meter()


def test_current_meter_outside_raises() -> None:
    with pytest.raises(RuntimeError, match="open_meter"):
        current_meter()


def test_meter_is_per_context() -> None:
    async def worker(cost: float) -> float | None:
        with open_meter() as meter:
            await asyncio.sleep(0)
            current_meter().cost_usd = cost
            await asyncio.sleep(0)
            assert current_meter() is meter
            return meter.cost_usd

    async def main() -> list[float | None]:
        return list(await asyncio.gather(worker(0.1), worker(0.2)))

    assert asyncio.run(main()) == [0.1, 0.2]
