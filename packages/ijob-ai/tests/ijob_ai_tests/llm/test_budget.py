import asyncio

from ijob_ai.llm.budget import Budget


def test_budget_starts_while_under_cap() -> None:
    budget = Budget(0.50)
    assert budget.cap == 0.50
    assert budget.spent == 0.0
    assert not budget.reached
    assert budget.try_start()


def test_budget_stops_starts_once_cap_is_charged() -> None:
    budget = Budget(0.50)
    budget.charge(0.50)
    assert budget.spent == 0.50
    assert budget.reached
    assert not budget.try_start()


def test_budget_overshoot_bound() -> None:
    cap = 0.50
    cost = 0.002
    workers = 4
    budget = Budget(cap)
    starts = 0

    async def worker() -> None:
        nonlocal starts
        while budget.try_start():
            starts += 1
            await asyncio.sleep(0)
            budget.charge(cost)

    async def main() -> None:
        await asyncio.gather(*(worker() for _ in range(workers)))

    asyncio.run(main())
    assert cap <= budget.spent <= cap + workers * cost
    assert 250 <= starts <= 250 + workers
