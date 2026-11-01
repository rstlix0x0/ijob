"""Fixtures shared by the terminal UI tests.

The terminal UI reaches the database only through a `Service`, so these fixtures
seed a temporary database, open a service on it over the shared mock transport,
and keep the user's real ijob home out of reach.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
import pytest
from ijob_app.home import IjobHome
from ijob_app.service import ClientFactory, Service, open_service
from ijob_core.model import SightingDraft
from ijob_core.store import Store
from ijob_sources.seeds import load_seeds

type Handler = Callable[[httpx.Request], httpx.Response]
type ClientFactoryBuilder = Callable[[Handler], ClientFactory]
type Opener = Callable[[Handler], AbstractContextManager[Service]]


class Seeder(Protocol):
    """Record drafts as a source's latest fetch."""

    def __call__(
        self, drafts: Sequence[SightingDraft], *, seen_at: datetime = ...
    ) -> None:
        """Record `drafts` as fetched at `seen_at`."""


SEEN = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """The database file every fixture and test here shares."""
    return tmp_path / "ijob.db"


@pytest.fixture
def seed(db: Path) -> Seeder:
    """Record drafts as the latest fetch of their sources, at `SEEN` by default.

    Every source in the drafts is marked fetched at `seen_at`, so its postings
    are open. Call it before a service is opened on the database.
    """

    def seed_drafts(
        drafts: Sequence[SightingDraft], *, seen_at: datetime = SEEN
    ) -> None:
        with Store.open(db) as store:
            store.import_seeds(load_seeds())
            store.record(drafts, seen_at=seen_at)
            for ref in {draft.source for draft in drafts}:
                store.mark_fetched(ref, seen_at)

    return seed_drafts


@pytest.fixture
def opener(
    db: Path,
    home: IjobHome,
    clock: Callable[[], datetime],
    client_factory_for: ClientFactoryBuilder,
) -> Opener:
    """Open a service on `db` whose requests a handler answers."""

    @contextmanager
    def open_with(handler: Handler) -> Iterator[Service]:
        with open_service(home, db, client_factory_for(handler), now=clock) as service:
            yield service

    return open_with
