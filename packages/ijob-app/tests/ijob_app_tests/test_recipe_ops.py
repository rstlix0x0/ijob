import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import override

import httpx
import pytest
from ijob_ai.errors import (
    CostUnknownError,
    LlmError,
    MissingModelError,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ijob_app import recipe_ops
from ijob_app.home import IjobHome
from ijob_app.llm_ops import (
    ItemOutcome,
    ItemStatus,
    LlmOptions,
    LlmRuntime,
    OperationReport,
    RunResult,
)
from ijob_app.recipe_errors import (
    NoTrialJobsError,
    RecipeCapReachedError,
    RecipeOutputError,
)
from ijob_app.recipe_ops import RecipeProposal, accept_recipe, analyze_operation
from ijob_core.model import SourceKind, SourceRef
from ijob_core.store import Store
from ijob_core.traces import TraceStore
from ijob_sources.errors import (
    FetchedPage,
    NotPublicAddressError,
    ReservedHostError,
    UnsupportedUrlError,
)
from ijob_sources.manual import manual_key
from ijob_sources.polite_http import ConditionalCache
from ijob_sources.public_host import public_host_guard
from ijob_sources.recipe import parse_recipe, recipe_text
from ijob_sources.recipe_document import RecipeFormat

from .conftest import ClientFactoryBuilder
from .llm_fakes import (
    JUNK,
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    Opaque,
    json_url_answer,
    recipe_answer,
    request_text,
    write_fake_cli,
    write_llm_config,
)
from .recipe_pages import JOBS_JSON, JS_ONLY_PAGE, LIST_PAGE

_HOST = "https://jobs.example.test"
_URL = f"{_HOST}/careers"
_JSON_URL = f"{_HOST}/api/jobs/"
_COMPANY = "Example"
_PUBLIC = "93.184.216.34"
_HTML_RECIPE: dict[str, object] = {
    "version": 1,
    "format": "html",
    "url": _URL,
    "items": "//li[@class='job']",
    "fields": {"title": ".//a/text()", "url": ".//a/@href"},
}
_JSON_RECIPE: dict[str, object] = {
    "version": 1,
    "format": "json",
    "url": _JSON_URL,
    "items": "jobs",
    "fields": {"title": "title", "url": "link", "location": "city"},
}
_INVALID_PREFIX = "the model's recipe is invalid: "

type _Page = tuple[str, bytes]


def _ref(url: str) -> SourceRef:
    return SourceRef(SourceKind.RECIPE, manual_key(url))


def _html(body: str) -> _Page:
    return "text/html; charset=utf-8", body.encode()


def _json(body: str) -> _Page:
    return "application/json", body.encode()


class _Site:
    """A mock web server: pages and redirects by path, an open robots.txt, a log."""

    def __init__(
        self, pages: Mapping[str, _Page], redirects: Mapping[str, str] | None = None
    ) -> None:
        self._pages = pages
        self._redirects = redirects or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        target = self._redirects.get(request.url.path)
        if target is not None:
            return httpx.Response(302, headers={"Location": target})
        page = self._pages.get(request.url.path)
        if page is None:
            return httpx.Response(404)
        content_type, body = page
        return httpx.Response(200, content=body, headers={"Content-Type": content_type})

    def fetched(self) -> list[str]:
        """Return the paths requested, robots.txt left out."""
        return [r.url.path for r in self.requests if r.url.path != "/robots.txt"]

    def fetched_urls(self) -> list[tuple[str, str]]:
        """Return the host and path requested, robots.txt left out."""
        return [
            (r.url.host, r.url.path)
            for r in self.requests
            if r.url.path != "/robots.txt"
        ]


async def _resolve(host: str, port: int) -> Sequence[str]:
    del port
    return ["10.0.0.1"] if host.startswith("internal.") else [_PUBLIC]


class _RaisingEngine(FakeAsyncEngine):
    def __init__(self, error: LlmError) -> None:
        super().__init__()
        self._error = error

    @override
    async def complete(self, _request: Opaque) -> Opaque:
        raise self._error


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


class _Runtimes:
    def __init__(self, home: IjobHome, tmp_path: Path) -> None:
        self._home = home
        self._cli = write_fake_cli(tmp_path / "bin")

    def __call__(
        self,
        engine: FakeAsyncEngine,
        *,
        source_recipe: str | None = MADE_UP_CLAUDE,
        max_cost_usd: float | None = None,
    ) -> LlmRuntime:
        write_llm_config(
            self._home.config,
            judge=None,
            source_recipe=source_recipe,
            max_cost_usd=max_cost_usd,
            concurrency=1,
            cli_path=self._cli.path,
        )
        options = LlmOptions(config_path=self._home.config, engines=FakeEngines(engine))
        return LlmRuntime.create(self._home, options)


@pytest.fixture
def runtimes(home: IjobHome, tmp_path: Path) -> _Runtimes:
    return _Runtimes(home, tmp_path)


def _analyze(
    runtime: LlmRuntime,
    site: _Site,
    client_factory_for: ClientFactoryBuilder,
    *,
    url: str = _URL,
    page: FetchedPage | None = None,
) -> RecipeProposal:
    async def run() -> RecipeProposal:
        cache: ConditionalCache | None = None
        client = client_factory_for(site)(cache)
        client.guard_requests(public_host_guard(_resolve))
        async with client:
            return await analyze_operation(
                runtime,
                client,
                ref=_ref(url),
                target_url=url,
                company=_COMPANY,
                page=page,
            )

    return asyncio.run(run())


def _list_site() -> _Site:
    return _Site({"/careers": _html(LIST_PAGE)})


def _js_site(api: _Page | None = None) -> _Site:
    return _Site(
        {"/careers": _html(JS_ONLY_PAGE), "/api/jobs/": api or _json(JOBS_JSON)}
    )


def _recipe_with(**changes: object) -> dict[str, object]:
    return {**_HTML_RECIPE, **changes}


def test_html_recipe_proposal(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder, home: IjobHome
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _list_site()

    proposal = _analyze(runtimes(engine), site, client_factory_for)

    assert [job.title for job in proposal.jobs] == ["Rust Engineer", "Go Engineer"]
    assert [job.url for job in proposal.jobs] == [
        f"{_HOST}/jobs/1",
        f"{_HOST}/jobs/2",
    ]
    assert proposal.invalid == 0
    assert proposal.ref == _ref(_URL)
    assert (proposal.target_url, proposal.company) == (_URL, _COMPANY)
    assert proposal.recipe_format is RecipeFormat.HTML
    assert proposal.recipe_url == _URL
    assert proposal.recipe == recipe_text(parse_recipe(json.dumps(_HTML_RECIPE)))
    assert proposal.model == MADE_UP_CLAUDE
    assert isinstance(proposal.report, OperationReport)
    assert proposal.report.done == 1
    assert proposal.report.spent == pytest.approx(0.001)
    assert len(engine.requests) == 1
    assert "Rust Engineer" in request_text(engine.requests[0])
    with TraceStore.open(home.traces) as traces:
        [summary] = traces.summaries(10)
    assert summary.operation == "ijob sources add"


def test_json_url_takes_two_calls(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine(
        [json_url_answer("/api/jobs/"), recipe_answer(_JSON_RECIPE)]
    )
    site = _js_site()

    proposal = _analyze(runtimes(engine), site, client_factory_for)

    assert site.fetched() == ["/careers", "/api/jobs/"]
    assert len(engine.requests) == 2
    assert "Rust Engineer" in request_text(engine.requests[1])
    assert proposal.recipe_format is RecipeFormat.JSON
    assert proposal.recipe_url == _JSON_URL
    assert proposal.target_url == _URL
    assert [job.title for job in proposal.jobs] == ["Rust Engineer", "Go Engineer"]
    assert proposal.jobs[0].url == f"{_HOST}/jobs/1"
    assert proposal.report.done == 1
    assert proposal.report.spent == pytest.approx(0.002)


def test_html_redirect_resolves_jobs_against_final_address(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _Site(
        {"/hiring": _html(LIST_PAGE)},
        redirects={"/careers": "https://careers.example.test/hiring"},
    )

    proposal = _analyze(runtimes(engine), site, client_factory_for)

    assert [job.url for job in proposal.jobs] == [
        "https://careers.example.test/jobs/1",
        "https://careers.example.test/jobs/2",
    ]
    assert proposal.target_url == _URL
    assert proposal.recipe_url == _URL
    assert json.loads(proposal.recipe)["url"] == _URL
    assert "/hiring" in request_text(engine.requests[0])


def test_json_redirects_resolve_against_final_addresses(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine(
        [json_url_answer("/api/jobs/"), recipe_answer(_JSON_RECIPE)]
    )
    site = _Site(
        {"/hiring": _html(JS_ONLY_PAGE), "/v2/jobs": _json(JOBS_JSON)},
        redirects={
            "/careers": "https://careers.example.test/hiring",
            "/api/jobs/": "https://api.example.test/v2/jobs",
        },
    )

    proposal = _analyze(runtimes(engine), site, client_factory_for)

    assert site.fetched_urls() == [
        ("jobs.example.test", "/careers"),
        ("careers.example.test", "/hiring"),
        ("careers.example.test", "/api/jobs/"),
        ("api.example.test", "/v2/jobs"),
    ]
    assert proposal.recipe_url == "https://careers.example.test/api/jobs/"
    assert proposal.target_url == _URL
    assert [job.url for job in proposal.jobs] == [
        "https://api.example.test/jobs/1",
        "https://api.example.test/jobs/2",
    ]


@pytest.mark.parametrize(
    "json_url",
    [
        "http://[bad/jobs",
        "//[x",
        "https://ex\u2100mple.test/jobs",
        "ftp://jobs.example.test/api",
        "mailto:jobs@example.test",
    ],
)
def test_unusable_json_url_is_recipe_output_error(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder, json_url: str
) -> None:
    engine = FakeAsyncEngine([json_url_answer(json_url)])
    site = _js_site()

    with pytest.raises(RecipeOutputError) as caught:
        _analyze(runtimes(engine), site, client_factory_for)

    assert str(caught.value).startswith(_INVALID_PREFIX)
    assert len(str(caught.value)) > len(_INVALID_PREFIX)
    assert site.fetched() == ["/careers"]
    assert len(engine.requests) == 1


def test_malformed_added_url_refused_before_fetch(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _list_site()

    with pytest.raises(UnsupportedUrlError) as caught:
        _analyze(runtimes(engine), site, client_factory_for, url="http://[bad/careers")

    assert caught.value.url == "http://[bad/careers"
    assert caught.value.reason
    assert site.requests == []
    assert engine.requests == []


def test_cap_before_second_call(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine(
        [json_url_answer("/api/jobs/"), recipe_answer(_JSON_RECIPE)]
    )
    runtime = runtimes(engine, max_cost_usd=0.0005)

    with pytest.raises(RecipeCapReachedError):
        _analyze(runtime, _js_site(), client_factory_for)

    assert len(engine.requests) == 1


def test_json_url_serving_html_refused(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([json_url_answer("/api/jobs/")])
    site = _js_site(api=_html(LIST_PAGE))

    with pytest.raises(UnsupportedUrlError) as caught:
        _analyze(runtimes(engine), site, client_factory_for)

    assert caught.value.reason == "expected JSON"
    assert len(engine.requests) == 1


def test_json_url_private_host_refused(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([json_url_answer("http://10.0.0.5/api/jobs/")])
    site = _js_site()

    with pytest.raises(NotPublicAddressError):
        _analyze(runtimes(engine), site, client_factory_for)

    assert site.fetched() == ["/careers"]
    assert len(engine.requests) == 1


def test_invalid_recipe_is_recipe_output_error(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_recipe_with(items="//li["))])

    with pytest.raises(RecipeOutputError) as caught:
        _analyze(runtimes(engine), _list_site(), client_factory_for)

    assert str(caught.value).startswith(_INVALID_PREFIX)
    assert len(str(caught.value)) > len(_INVALID_PREFIX)


def test_evaluation_error_in_trial_is_recipe_output_error(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_recipe_with(items="foo()"))])

    with pytest.raises(RecipeOutputError) as caught:
        _analyze(runtimes(engine), _list_site(), client_factory_for)

    assert str(caught.value).startswith(_INVALID_PREFIX)
    assert str(caught.value) == _INVALID_PREFIX + "Unregistered function"


def test_zero_jobs_in_trial(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_recipe_with(items="//li[@id='none']"))])

    with pytest.raises(NoTrialJobsError) as caught:
        _analyze(runtimes(engine), _list_site(), client_factory_for)

    assert str(caught.value) == "the proposed recipe read no jobs from the page"


def test_missing_model_before_fetch(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine()
    site = _list_site()

    with pytest.raises(MissingModelError):
        _analyze(runtimes(engine, source_recipe=None), site, client_factory_for)

    assert site.requests == []
    assert engine.requests == []


def test_added_url_fetched_under_guard(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _list_site()

    with pytest.raises(NotPublicAddressError):
        _analyze(
            runtimes(engine),
            site,
            client_factory_for,
            url="https://internal.example.test/careers",
        )

    assert site.requests == []
    assert engine.requests == []


@pytest.mark.parametrize(
    "error",
    [
        ProviderFailedError("boom"),
        ProviderUnavailableError("gone"),
        CostUnknownError(MADE_UP_CLAUDE),
    ],
)
def test_provider_errors_propagate(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder, error: LlmError
) -> None:
    with pytest.raises(type(error)) as caught:
        _analyze(runtimes(_RaisingEngine(error)), _list_site(), client_factory_for)

    assert str(caught.value) == str(error)


def test_unparseable_answer_is_recipe_output_error(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([JUNK])

    with pytest.raises(RecipeOutputError) as caught:
        _analyze(runtimes(engine), _list_site(), client_factory_for)

    assert str(caught.value).startswith(_INVALID_PREFIX)


@pytest.mark.parametrize(
    "answer",
    [
        recipe_answer(_JSON_RECIPE),
        json_url_answer(""),
    ],
)
def test_other_task_output_errors_are_recipe_output_errors(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder, answer: str
) -> None:
    engine = FakeAsyncEngine([answer])

    with pytest.raises(RecipeOutputError) as caught:
        _analyze(runtimes(engine), _list_site(), client_factory_for)

    assert str(caught.value).startswith(_INVALID_PREFIX)


def test_json_url_for_a_json_sample_is_recipe_output_error(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([json_url_answer("/api/jobs/"), json_url_answer("/x")])

    with pytest.raises(RecipeOutputError) as caught:
        _analyze(runtimes(engine), _js_site(), client_factory_for)

    assert str(caught.value).startswith(_INVALID_PREFIX)
    assert len(engine.requests) == 2


@pytest.mark.parametrize("url", ["/relative", ""])
def test_model_url_replaced_before_parse(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder, url: str
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_recipe_with(url=url))])

    proposal = _analyze(runtimes(engine), _list_site(), client_factory_for)

    assert proposal.recipe_url == _URL
    assert json.loads(proposal.recipe)["url"] == _URL
    assert len(proposal.jobs) == 2


@pytest.mark.parametrize(
    ("url", "page", "error"),
    [
        (
            "https://www.jobstreet.com/careers",
            _html(LIST_PAGE),
            ReservedHostError,
        ),
        (
            _URL,
            ("text/html", b"<html>" + b"a" * (5 * 1024 * 1024)),
            UnsupportedUrlError,
        ),
        (_URL, ("text/plain", b"Rust Engineer"), UnsupportedUrlError),
    ],
)
def test_added_url_refused_cases(
    runtimes: _Runtimes,
    client_factory_for: ClientFactoryBuilder,
    url: str,
    page: _Page,
    error: type[Exception],
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _Site({"/careers": page})

    with pytest.raises(error):
        _analyze(runtimes(engine), site, client_factory_for, url=url)

    assert engine.requests == []


def test_accept_recipe_stores_source_and_recipe(tmp_path: Path) -> None:
    at = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    proposal = RecipeProposal(
        ref=_ref(_URL),
        target_url=_URL,
        company=_COMPANY,
        recipe=recipe_text(parse_recipe(json.dumps(_HTML_RECIPE))),
        recipe_format=RecipeFormat.HTML,
        recipe_url=_URL,
        jobs=(),
        invalid=0,
        model=MADE_UP_CLAUDE,
        report=OperationReport(
            done=1, not_done=0, spent=0.001, cap=1.0, stopped_by=None
        ),
    )

    with Store.open(tmp_path / "ijob.db") as store:
        accept_recipe(store, proposal, at=at)
        row = store.source(proposal.ref)
        stored = store.recipe(proposal.ref)

    assert row is not None
    assert (row.company, row.target_url, row.removed) == (_COMPANY, _URL, False)
    assert stored is not None
    assert stored.recipe == proposal.recipe
    assert (stored.model, stored.created_at) == (MADE_UP_CLAUDE, at)


def test_given_page_is_not_fetched_and_resolves_jobs_against_its_final_address(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _list_site()
    page = FetchedPage(
        body=LIST_PAGE.encode(),
        content_type="text/html; charset=utf-8",
        final_url="https://careers.example.test/hiring",
    )

    proposal = _analyze(runtimes(engine), site, client_factory_for, page=page)

    assert site.requests == []
    assert [job.url for job in proposal.jobs] == [
        "https://careers.example.test/jobs/1",
        "https://careers.example.test/jobs/2",
    ]
    assert proposal.target_url == _URL
    assert proposal.recipe_url == _URL
    assert "/hiring" in request_text(engine.requests[0])


def test_given_page_still_fetches_the_json_address(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine(
        [json_url_answer("/api/jobs/"), recipe_answer(_JSON_RECIPE)]
    )
    site = _js_site()
    page = FetchedPage(
        body=JS_ONLY_PAGE.encode(), content_type="text/html", final_url=_URL
    )

    proposal = _analyze(runtimes(engine), site, client_factory_for, page=page)

    assert site.fetched() == ["/api/jobs/"]
    assert proposal.recipe_format is RecipeFormat.JSON


def test_given_page_of_another_type_is_refused_before_the_model(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    page = FetchedPage(body=b"Rust Engineer", content_type="text/plain", final_url=_URL)

    with pytest.raises(UnsupportedUrlError):
        _analyze(runtimes(engine), _list_site(), client_factory_for, page=page)

    assert engine.requests == []


def _notes(error: BaseException) -> list[str]:
    return list(getattr(error, "__notes__", []))


_SPENT_NOTE = "cost $0.0010 of $"


def test_failure_after_spending_carries_the_cost_note(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_recipe_with(items="//li["))])

    with pytest.raises(RecipeOutputError) as caught:
        _analyze(runtimes(engine), _list_site(), client_factory_for)

    [note] = _notes(caught.value)
    assert note.startswith(_SPENT_NOTE)
    assert re.fullmatch(r"cost \$0\.0010 of \$\d+\.\d\d", note)


@pytest.mark.parametrize(
    ("answers", "site", "error"),
    [
        pytest.param(
            [recipe_answer(_recipe_with(items="//li[@id='none']"))],
            _list_site,
            NoTrialJobsError,
            id="no-trial-jobs",
        ),
        pytest.param([JUNK], _list_site, RecipeOutputError, id="unusable-answer"),
        pytest.param(
            [json_url_answer("/api/jobs/")],
            lambda: _js_site(api=_html(LIST_PAGE)),
            UnsupportedUrlError,
            id="returned-sources-error",
        ),
        pytest.param(
            [json_url_answer("/api/jobs/"), json_url_answer("/x")],
            _js_site,
            RecipeOutputError,
            id="second-answer-unusable",
        ),
    ],
)
def test_every_failure_after_spending_carries_the_cost_note(
    runtimes: _Runtimes,
    client_factory_for: ClientFactoryBuilder,
    answers: list[str],
    site: Callable[[], _Site],
    error: type[Exception],
) -> None:
    with pytest.raises(error) as caught:
        _analyze(runtimes(FakeAsyncEngine(answers)), site(), client_factory_for)

    assert len(_notes(caught.value)) == 1
    assert _notes(caught.value)[0].startswith("cost $0.00")


def test_cap_reached_after_the_first_call_carries_the_cost_note(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    engine = FakeAsyncEngine(
        [json_url_answer("/api/jobs/"), recipe_answer(_JSON_RECIPE)]
    )

    with pytest.raises(RecipeCapReachedError) as caught:
        _analyze(runtimes(engine, max_cost_usd=0.0005), _js_site(), client_factory_for)

    assert _notes(caught.value) == ["cost $0.0010 of $0.00"]


@pytest.mark.parametrize(
    "error",
    [ProviderFailedError("boom"), CostUnknownError(MADE_UP_CLAUDE)],
)
def test_failure_before_any_spend_has_no_cost_note(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder, error: LlmError
) -> None:
    with pytest.raises(type(error)) as caught:
        _analyze(runtimes(_RaisingEngine(error)), _list_site(), client_factory_for)

    assert _notes(caught.value) == []


def test_item_the_cap_kept_from_starting_is_a_cap_error_not_a_runtime_error(
    runtimes: _Runtimes,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capped(
        runtime: LlmRuntime, _operation: str, items: Sequence[object], *_args: object
    ) -> RunResult[object, object]:
        report = OperationReport(
            done=0,
            not_done=1,
            spent=0.0,
            cap=runtime.settings.max_cost_usd,
            stopped_by="cap",
        )
        return RunResult((ItemOutcome(items[0], ItemStatus.CAP_REACHED),), report)

    monkeypatch.setattr(recipe_ops, "run_items", capped)
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])

    with pytest.raises(RecipeCapReachedError) as caught:
        _analyze(runtimes(engine), _list_site(), client_factory_for)

    assert _notes(caught.value) == []
    assert engine.requests == []
    assert str(caught.value) == (
        "cost cap reached before the first model call; raise [llm].max_cost_usd"
    )


def test_error_before_the_run_has_no_cost_note(
    runtimes: _Runtimes, client_factory_for: ClientFactoryBuilder
) -> None:
    with pytest.raises(NotPublicAddressError) as caught:
        _analyze(
            runtimes(FakeAsyncEngine()),
            _list_site(),
            client_factory_for,
            url="https://internal.example.test/careers",
        )

    assert _notes(caught.value) == []
