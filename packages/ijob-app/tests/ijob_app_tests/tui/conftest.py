"""Fixtures shared by the terminal UI tests.

The terminal UI reaches the database only through a `Service`, so these fixtures
seed a temporary database, open a service on it over the shared mock transport,
and keep the user's real ijob home out of reach. The LLM opener also answers the
judge with fake engines, so no model or network is reached.
"""

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, override

import httpx
import pytest
from ijob_app.home import IjobHome
from ijob_app.llm_ops import LlmOptions
from ijob_app.service import ClientFactory, Service, open_service
from ijob_app.tui import IjobApp
from ijob_core.countries import CountryCode
from ijob_core.model import (
    ExternalId,
    Instructions,
    RemoteScope,
    SightingDraft,
    SourceRef,
    parse_source_ref,
)
from ijob_core.money import Money, Period
from ijob_core.store import Store
from ijob_sources.seeds import load_seeds
from textual.widgets import DataTable

from ..llm_fakes import (
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    Opaque,
    write_fake_cli,
    write_llm_config,
)

type Handler = Callable[[httpx.Request], httpx.Response]
type ClientFactoryBuilder = Callable[[Handler], ClientFactory]
type Opener = Callable[[Handler], AbstractContextManager[Service]]
type LlmOpener = Callable[[FakeEngines], AbstractContextManager[Service]]


class RecipeOpener(Protocol):
    """Open a service that can learn recipes, over a handler and fake engines."""

    def __call__(
        self,
        handler: Handler,
        engines: FakeEngines,
        *,
        model: bool = True,
        max_cost_usd: float = ...,
    ) -> AbstractContextManager[Service]:
        """Open a service.

        `model` says whether `[models].source_recipe` is set, and `max_cost_usd`
        is the spending cap, which defaults to `COST_CAP`.
        """


class Seeder(Protocol):
    """Record drafts as a source's latest fetch."""

    def __call__(
        self, drafts: Sequence[SightingDraft], *, seen_at: datetime = ...
    ) -> None:
        """Record `drafts` as fetched at `seen_at`."""


class InstructionStorer(Protocol):
    """Store instructions the way an accepted proposal would."""

    def __call__(self, *, rules: tuple[str, ...] = ..., filters: str = ...) -> None:
        """Store instructions holding `rules` and the `filters_json` text."""


async def public_resolver(host: str, port: int) -> Sequence[str]:
    """Resolve every host to one public address, so no test asks the network."""
    del host, port
    return ["93.184.216.34"]


SEEN = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
COST_CAP = 0.5
STRIPE = parse_source_ref("greenhouse:stripe")
ANTHROPIC = parse_source_ref("greenhouse:anthropic")


def draft(
    source: SourceRef,
    external_id: str,
    title: str,
    *,
    country: str | None = None,
    salary: bool = False,
) -> SightingDraft:
    """Build a posting draft of the company Acme, with a one-line body."""
    return SightingDraft(
        source=source,
        external_id=ExternalId(external_id),
        title=title,
        company="Acme",
        location="Somewhere",
        url=f"https://jobs.example.test/{external_id}",
        posted_at=None,
        raw="{}",
        salaries=(Money(Decimal(6000), None, "SGD", Period.MONTH),) if salary else (),
        country=CountryCode(country) if country else None,
        remote=RemoteScope.COUNTRY if country else None,
        description="Body text.",
    )


def titles(app: IjobApp) -> list[str]:
    """Read the title column of the results table, top to bottom."""
    table = app.query_one("#results", DataTable)
    return [str(table.get_row_at(index)[1]) for index in range(table.row_count)]


def seed_three(seed: Seeder) -> None:
    """Record three postings: two at Stripe and one at Anthropic."""
    seed(
        [
            draft(STRIPE, "1", "Backend Engineer", country="SG"),
            draft(STRIPE, "2", "Platform Engineer", country="JP"),
            draft(ANTHROPIC, "3", "Research Engineer", country="US"),
        ]
    )


class SlowEngine(FakeAsyncEngine):
    """A fake engine that records a request only when its answer is complete.

    A call cancelled while it waits leaves no request behind, so a test counts
    the calls that finished.
    """

    def __init__(self, texts: Sequence[str], *, delay_s: float) -> None:
        """Script the engine.

        Args:
            texts: Answers in order; the last one repeats.
            delay_s: Seconds each call waits before it is answered.
        """
        super().__init__(texts)
        self._wait_s = delay_s

    @override
    async def complete(self, request: Opaque) -> Opaque:
        """Wait, then record the request and answer it."""
        await asyncio.sleep(self._wait_s)
        return await super().complete(request)


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
        with open_service(
            home, db, client_factory_for(handler), now=clock, resolver=public_resolver
        ) as service:
            yield service

    return open_with


@pytest.fixture
def llm_opener(
    db: Path,
    home: IjobHome,
    clock: Callable[[], datetime],
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> LlmOpener:
    """Open a service whose judge is answered by the given fake engines.

    The config names a made-up judge model, a cap of `COST_CAP` dollars and one
    call at a time, and points at a fake `claude` executable.
    """
    tmp_path = db.parent
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    cli = write_fake_cli(tmp_path / "bin")
    write_llm_config(
        home.config,
        judge=MADE_UP_CLAUDE,
        max_cost_usd=COST_CAP,
        concurrency=1,
        cli_path=cli.path,
    )

    @contextmanager
    def open_with(engines: FakeEngines) -> Iterator[Service]:
        options = LlmOptions(config_path=home.config, engines=engines)
        with open_service(
            home,
            db,
            client_factory_for(lambda _request: httpx.Response(404)),
            now=clock,
            llm=options,
            resolver=public_resolver,
        ) as service:
            yield service

    return open_with


@pytest.fixture
def recipe_opener(
    db: Path,
    home: IjobHome,
    clock: Callable[[], datetime],
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> RecipeOpener:
    """Open a service whose recipe model is answered by the given fake engines.

    The config names a made-up recipe model, a cap of `COST_CAP` dollars and one
    call at a time, and points at a fake `claude` executable.
    """
    tmp_path = db.parent
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    cli = write_fake_cli(tmp_path / "bin")

    @contextmanager
    def open_with(
        handler: Handler,
        engines: FakeEngines,
        *,
        model: bool = True,
        max_cost_usd: float = COST_CAP,
    ) -> Iterator[Service]:
        write_llm_config(
            home.config,
            judge=None,
            source_recipe=MADE_UP_CLAUDE if model else None,
            max_cost_usd=max_cost_usd,
            concurrency=1,
            cli_path=cli.path,
        )
        options = LlmOptions(config_path=home.config, engines=engines)
        with open_service(
            home,
            db,
            client_factory_for(handler),
            now=clock,
            llm=options,
            resolver=public_resolver,
        ) as service:
            yield service

    return open_with


@pytest.fixture
def scoring_opener(llm_opener: LlmOpener, home: IjobHome, db: Path) -> LlmOpener:
    """Like `llm_opener`, with the config also naming the scorer and profile models."""
    cli = write_fake_cli(db.parent / "bin")
    write_llm_config(
        home.config,
        judge=MADE_UP_CLAUDE,
        cv_profile=MADE_UP_CLAUDE,
        scorer=MADE_UP_CLAUDE,
        max_cost_usd=COST_CAP,
        concurrency=1,
        cli_path=cli.path,
    )
    return llm_opener


@pytest.fixture
def instructions_opener(llm_opener: LlmOpener, home: IjobHome, db: Path) -> LlmOpener:
    """Like `llm_opener`, with the config also naming the instructions model."""
    cli = write_fake_cli(db.parent / "bin")
    write_llm_config(
        home.config,
        judge=MADE_UP_CLAUDE,
        instructions=MADE_UP_CLAUDE,
        max_cost_usd=COST_CAP,
        concurrency=1,
        cli_path=cli.path,
    )
    return llm_opener


@pytest.fixture
def store_instructions(db: Path) -> InstructionStorer:
    """Store instructions in the database before a service is opened on it."""

    def store(
        *, rules: tuple[str, ...] = ("no crypto companies",), filters: str = "{}"
    ) -> None:
        instructions = Instructions(
            text="no crypto",
            filters_json=filters,
            rules=rules,
            model=MADE_UP_CLAUDE,
            task_version="1",
            set_at=SEEN,
        )
        with Store.open(db) as opened:
            opened.set_instructions(instructions)

    return store
