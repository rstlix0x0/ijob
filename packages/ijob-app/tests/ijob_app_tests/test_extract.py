import asyncio
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import typer
from ijob_ai.errors import MissingModelError
from ijob_app.cli import make_app
from ijob_app.extract_ops import ExtractReport
from ijob_app.home import IjobHome
from ijob_app.llm_ops import LlmOptions
from ijob_app.service import Service, open_service
from ijob_core.model import Posting, SourceKind, SourceRef, parse_source_key
from ijob_core.store import Store
from ijob_sources.collect import CollectResult, SkippedItem, SourceBatch
from typer.testing import CliRunner, Result

from .conftest import CLOCK_START, ClientFactoryBuilder
from .llm_fakes import (
    JUNK,
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    extract_answer,
    request_text,
    write_fake_cli,
    write_llm_config,
)

_REF = SourceRef(SourceKind.GREENHOUSE, parse_source_key("acme"))
_FETCHED = CLOCK_START - timedelta(days=1)
_PROSE = "Acme | Backend Engineer | Remote | https://jobs.example.test/acme/1"
_OTHER = "Great weather today, thanks for the thread"
_THIRD = "Beta is hiring a designer, mail me"
_EXTRACT = ("sources", "extract")


def _never(request: httpx.Request) -> httpx.Response:
    msg = f"unexpected request {request.url}"
    raise AssertionError(msg)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _seed(db: Path, *texts: str, url: str | None = None) -> None:
    with Store.open(db) as store:
        store.add_source(_REF, "Acme", None)
        store.mark_fetched(_REF, _FETCHED)
        store.add_skipped(
            _REF, [(text, url, "no title") for text in texts], seen_at=_FETCHED
        )


def _write_config(
    home: IjobHome, tmp_path: Path, *, model: bool, max_cost_usd: float | None
) -> None:
    write_llm_config(
        home.config,
        judge=None,
        job_extract=MADE_UP_CLAUDE if model else None,
        max_cost_usd=max_cost_usd,
        concurrency=1,
        cli_path=write_fake_cli(tmp_path / "bin").path,
    )


class _Env:
    def __init__(
        self,
        tmp_path: Path,
        home: IjobHome,
        clock: Callable[[], datetime],
        client_factory_for: ClientFactoryBuilder,
    ) -> None:
        self.db = tmp_path / "ijob.db"
        self._tmp_path = tmp_path
        self._home = home
        self._clock = clock
        self._factory = client_factory_for(_never)

    @contextmanager
    def service(
        self,
        engine: FakeAsyncEngine,
        *,
        model: bool = True,
        max_cost_usd: float | None = None,
    ) -> Iterator[Service]:
        _write_config(
            self._home, self._tmp_path, model=model, max_cost_usd=max_cost_usd
        )
        with open_service(
            self._home,
            self.db,
            self._factory,
            now=self._clock,
            llm=LlmOptions(config_path=self._home.config, engines=FakeEngines(engine)),
        ) as service:
            yield service

    def extract(
        self, engine: FakeAsyncEngine, **options: float | bool | None
    ) -> ExtractReport:
        with self.service(engine, **options) as service:  # type: ignore[arg-type]
            return asyncio.run(service.extract_skipped([]))

    def postings(self) -> list[Posting]:
        with Store.open(self.db) as store:
            return store.open_postings(now=CLOCK_START, sources=[_REF])

    def pending(self) -> int:
        with Store.open(self.db) as store:
            return len(store.pending_skipped([_REF]))


@pytest.fixture
def env(
    tmp_path: Path,
    home: IjobHome,
    clock: Callable[[], datetime],
    client_factory_for: ClientFactoryBuilder,
) -> _Env:
    return _Env(tmp_path, home, clock, client_factory_for)


def test_a_prose_comment_becomes_a_visible_low_confidence_posting(env: _Env) -> None:
    _seed(env.db, _PROSE)

    result = env.extract(FakeAsyncEngine([extract_answer()]))

    assert (result.added, result.not_jobs, result.failed) == (1, 0, ())
    (posting,) = env.postings()
    assert posting.title == "Backend Engineer"
    assert posting.company == "Acme"
    assert posting.low_confidence
    assert posting.url == "https://jobs.example.test/acme/1"
    assert env.pending() == 0


def test_a_missing_company_shows_unknown(env: _Env) -> None:
    _seed(env.db, _PROSE)

    env.extract(FakeAsyncEngine([extract_answer(company=None)]))

    assert [posting.company for posting in env.postings()] == ["unknown"]


def test_a_non_job_is_marked_and_not_sent_again(env: _Env) -> None:
    _seed(env.db, _OTHER)
    engine = FakeAsyncEngine([extract_answer(is_job=False)])

    first = env.extract(engine)
    second = env.extract(engine)

    assert (first.added, first.not_jobs) == (0, 1)
    assert (second.added, second.not_jobs, second.failed) == (0, 0, ())
    assert len(engine.requests) == 1
    assert env.postings() == []
    with Store.open(env.db) as store:
        assert store.pending_skipped([_REF]) == []


def test_a_second_run_spends_nothing(env: _Env) -> None:
    _seed(env.db, _PROSE)
    engine = FakeAsyncEngine([extract_answer()])

    env.extract(engine)
    second = env.extract(engine)

    assert second.report.spent == 0
    assert len(engine.requests) == 1
    assert len(env.postings()) == 1


def test_an_item_without_an_address_stays_pending(env: _Env) -> None:
    _seed(env.db, _THIRD)

    result = env.extract(FakeAsyncEngine([extract_answer(url=None)]))

    assert result.added == 0
    assert [failure.reason for failure in result.failed] == [
        "the item has no usable http(s) address"
    ]
    assert env.pending() == 1
    assert env.postings() == []


def test_a_job_with_no_title_takes_the_first_line_in_the_items_words(
    env: _Env,
) -> None:
    text = (
        'We&#x27;re hiring <a href="https://a.test">engineers</a> &amp; more\nMail us'
    )
    _seed(env.db, text)

    result = env.extract(FakeAsyncEngine([extract_answer(title=None)]))

    assert result.added == 1
    assert [posting.title for posting in env.postings()] == [
        "We're hiring engineers & more"
    ]


def test_an_item_with_no_words_at_all_stays_pending(env: _Env) -> None:
    _seed(env.db, "<br>")

    result = env.extract(FakeAsyncEngine([extract_answer(title=None)]))

    assert [failure.reason for failure in result.failed] == ["the model found no title"]
    assert env.pending() == 1


def test_the_model_reads_decoded_text(env: _Env) -> None:
    _seed(env.db, "We&#x27;re hiring&#x2F;growing")
    engine = FakeAsyncEngine([extract_answer()])

    env.extract(engine)

    assert "We're hiring/growing" in request_text(engine.requests[0])


def test_an_unread_items_preview_is_decoded(env: _Env) -> None:
    _seed(env.db, "We&#x27;re hiring")

    result = env.extract(FakeAsyncEngine([extract_answer(url=None)]))

    assert result.failed[0].preview == "We're hiring"


def test_the_items_own_address_is_used_when_the_model_gives_none(env: _Env) -> None:
    _seed(env.db, _PROSE, url="https://jobs.example.test/item/9")

    env.extract(FakeAsyncEngine([extract_answer(url="mailto:a@b.test")]))

    assert [posting.url for posting in env.postings()] == [
        "https://jobs.example.test/item/9"
    ]


def test_a_failed_call_leaves_the_item_pending_for_a_later_run(env: _Env) -> None:
    _seed(env.db, _PROSE)

    failed = env.extract(FakeAsyncEngine([JUNK, JUNK]))
    retried = env.extract(FakeAsyncEngine([extract_answer()]))

    assert failed.added == 0
    assert len(failed.failed) == 1
    assert failed.failed[0].reason
    assert retried.added == 1


def test_the_cost_cap_stops_the_run_and_keeps_what_was_read(env: _Env) -> None:
    _seed(env.db, _PROSE, _OTHER, _THIRD)
    engine = FakeAsyncEngine([extract_answer()], cost=0.001)

    result = env.extract(engine, max_cost_usd=0.001)

    assert result.report.stopped_by == "cap"
    assert result.added == 1
    assert len(result.failed) == 2
    assert len(engine.requests) == 1
    assert len(env.postings()) == 1
    assert env.pending() == 2


def test_no_model_is_an_error_and_makes_no_call(env: _Env) -> None:
    _seed(env.db, _PROSE)
    engine = FakeAsyncEngine([extract_answer()])

    with pytest.raises(MissingModelError):
        env.extract(engine, model=False)

    assert engine.requests == []
    assert env.pending() == 1


def _refresh_with(service: Service, items: Sequence[SkippedItem]) -> None:
    async def collect(_self: Service, _sources: object, _on: object) -> CollectResult:
        batch = SourceBatch(drafts=[], rejected=0, skipped=tuple(items))
        return CollectResult(batches={_REF: batch}, failures=[])

    with patch.object(Service, "_collect", collect):
        asyncio.run(service.refresh([_REF]))


def test_a_refresh_keeps_a_posting_read_by_the_model_open(env: _Env) -> None:
    _seed(env.db, _PROSE)
    env.extract(FakeAsyncEngine([extract_answer()]))
    item = SkippedItem(text=_PROSE, url=None, reason="no title")

    with env.service(FakeAsyncEngine()) as service:
        _refresh_with(service, [item])
        now = service._now()
    with Store.open(env.db) as store:
        opened = store.open_postings(now=now, sources=[_REF])

    assert [posting.title for posting in opened] == ["Backend Engineer"]


def test_a_refresh_that_no_longer_shows_the_item_closes_the_posting(env: _Env) -> None:
    _seed(env.db, _PROSE)
    env.extract(FakeAsyncEngine([extract_answer()]))

    with env.service(FakeAsyncEngine()) as service:
        _refresh_with(service, [])
        now = service._now()
    with Store.open(env.db) as store:
        assert store.open_postings(now=now, sources=[_REF]) == []


def test_a_refresh_saves_the_skipped_items_once(env: _Env) -> None:
    _seed(env.db)
    item = SkippedItem(text=_PROSE, url=None, reason="no title")

    with env.service(FakeAsyncEngine()) as service:
        _refresh_with(service, [item])
        _refresh_with(service, [item])
        assert service.unread_count([]) == 1


def _app(
    env: _Env, tmp_path: Path, engine: FakeAsyncEngine, *, model: bool
) -> typer.Typer:
    _write_config(IjobHome(tmp_path / "home"), tmp_path, model=model, max_cost_usd=None)
    engines = FakeEngines(engine)

    def options(_home: object, config_path: Path) -> LlmOptions:
        return LlmOptions(config_path=config_path, engines=engines)

    return make_app(env._factory, llm_options=options)


def _invoke(app_: typer.Typer, tmp_path: Path, *args: str) -> Result:
    argv = [*args, "--db", str(tmp_path / "ijob.db")]
    return CliRunner().invoke(app_, argv, env={"IJOB_HOME": str(tmp_path / "home")})


def test_cli_extract_reads_pending_jobs_end_to_end(env: _Env, tmp_path: Path) -> None:
    _seed(env.db, _PROSE, _OTHER)
    engine = FakeAsyncEngine([extract_answer(), extract_answer(is_job=False)])
    app_ = _app(env, tmp_path, engine, model=True)

    result = _invoke(app_, tmp_path, "sources", "extract")

    assert result.exit_code == 0, result.output
    assert "1 jobs added, 1 not jobs, 0 still unread" in result.stdout
    assert "cost $" in result.stderr
    assert [posting.title for posting in env.postings()] == ["Backend Engineer"]
    again = _invoke(app_, tmp_path, "sources", "extract", str(_REF))
    assert "0 jobs added, 0 not jobs, 0 still unread" in again.stdout
    assert len(engine.requests) == 2


def test_cli_extract_without_a_model_names_the_setting(
    env: _Env, tmp_path: Path
) -> None:
    _seed(env.db, _PROSE)
    engine = FakeAsyncEngine([extract_answer()])
    app_ = _app(env, tmp_path, engine, model=False)

    result = _invoke(app_, tmp_path, "sources", "extract")

    assert result.exit_code == 1
    assert result.stderr.startswith("error:")
    assert "[models].job_extract" in result.stderr
    assert "Traceback" not in result.output
    assert engine.requests == []


def test_cli_extract_lists_the_reason_of_each_unread_item(
    env: _Env, tmp_path: Path
) -> None:
    _seed(env.db, _PROSE)
    app_ = _app(env, tmp_path, FakeAsyncEngine([extract_answer(url=None)]), model=True)

    result = _invoke(app_, tmp_path, "sources", "extract")

    assert result.exit_code == 0
    assert "0 jobs added, 0 not jobs, 1 still unread" in result.stdout
    assert "the item has no usable http(s) address" in result.stdout


def test_search_prints_a_note_when_skipped_jobs_wait(
    env: _Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(env.db, _PROSE, _OTHER)

    async def collect(_self: Service, _sources: object, _on: object) -> CollectResult:
        return CollectResult(batches={}, failures=[])

    monkeypatch.setattr(Service, "_collect", collect)
    app_ = _app(env, tmp_path, FakeAsyncEngine(), model=False)

    result = _invoke(app_, tmp_path, "search", "--source", str(_REF))

    assert "note: 2 skipped jobs unread; run: ijob sources extract" in result.stderr


def test_cli_extract_shows_progress_while_it_reads(env: _Env, tmp_path: Path) -> None:
    _seed(env.db, _PROSE, _OTHER)
    engine = FakeAsyncEngine([extract_answer(), extract_answer(is_job=False)])

    result = _invoke(_app(env, tmp_path, engine, model=True), tmp_path, *_EXTRACT)

    assert result.exit_code == 0, result.output
    assert "read 0 of 2: 0 jobs, 0 not jobs, 0 unread" in result.stderr
    assert "read 2 of 2: 1 jobs, 1 not jobs, 0 unread" in result.stderr
    assert "read " not in result.stdout


def test_cli_extract_with_nothing_pending_shows_no_progress(
    env: _Env, tmp_path: Path
) -> None:
    _seed(env.db)

    result = _invoke(
        _app(env, tmp_path, FakeAsyncEngine([]), model=True), tmp_path, *_EXTRACT
    )

    assert result.exit_code == 0, result.output
    assert "read " not in result.stderr


def test_an_answer_in_the_wrong_form_is_asked_for_once_more(env: _Env) -> None:
    _seed(env.db, _PROSE)
    engine = FakeAsyncEngine([JUNK, extract_answer()])

    result = env.extract(engine)

    assert result.added == 1
    assert result.failed == ()
    assert len(engine.requests) == 2


def test_a_right_answer_in_marked_form_is_read_the_first_time(env: _Env) -> None:
    _seed(env.db, _PROSE)
    fields = {
        "is_job": True,
        "title": "Eng; GTM; Design",
        "company": "telli",
        "location": "Berlin",
        "url": "https://telli.example.test/careers",
    }
    engine = FakeAsyncEngine([f"[[ ## answer ## ]]\n{json.dumps(fields)}"])

    result = env.extract(engine)

    assert result.added == 1
    assert len(engine.requests) == 1
    assert [posting.title for posting in env.postings()] == ["Eng; GTM; Design"]
