"""Fixtures shared by the `ijob_app` tests.

Test modules cannot import one another, so what several of them need lives here:
an ijob home under the test's temporary directory, a clock that never reads the
same time twice, and a builder of client factories over a mock transport.
"""

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from ijob_app.home import IjobHome
from ijob_app.service import ClientFactory
from ijob_sources.polite_http import ConditionalCache, PoliteClient

type Handler = Callable[[httpx.Request], httpx.Response]
type ClientFactoryBuilder = Callable[[Handler], ClientFactory]

CLOCK_START = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def home(tmp_path: Path) -> IjobHome:
    """An ijob home under `tmp_path`, so no real user data leaks in."""
    return IjobHome(tmp_path / "home")


@pytest.fixture
def clock() -> Callable[[], datetime]:
    """A wall clock that returns a later time on every call, from `CLOCK_START`."""
    ticks = itertools.count()
    return lambda: CLOCK_START + timedelta(microseconds=next(ticks))


@pytest.fixture
def client_factory_for() -> ClientFactoryBuilder:
    """Build a client factory whose clients are served by a mock transport.

    Each factory has its own fake time, so pacing never sleeps for real.
    """

    def build(handler: Handler) -> ClientFactory:
        fake_time = _FakeTime()

        def client_factory(cache: ConditionalCache | None) -> PoliteClient:
            return PoliteClient(
                transport=httpx.MockTransport(handler),
                clock=fake_time.clock,
                sleep=fake_time.sleep,
                cache=cache,
            )

        return client_factory

    return build
