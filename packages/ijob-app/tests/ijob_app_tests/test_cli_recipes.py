import dataclasses
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import override

import httpx
import pytest
import typer
from ijob_ai.errors import LlmError, ProviderFailedError
from ijob_app.cli import app, make_app
from ijob_app.llm_ops import LlmOptions
from ijob_app.recipe_ops import RecipeProposal
from ijob_app.service import Service
from ijob_core.model import SourceKind, parse_source_ref
from ijob_core.store import Store
from ijob_sources.errors import FetchedPage
from ijob_sources.manual import manual_key
from typer.testing import CliRunner, Result

from .conftest import ClientFactoryBuilder
from .llm_fakes import (
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    Opaque,
    json_url_answer,
    recipe_answer,
    write_fake_cli,
    write_llm_config,
)
from .recipe_pages import JS_ONLY_PAGE, LIST_PAGE

_HOST = "https://jobs.example.test"
_URL = f"{_HOST}/careers"
_KEY = manual_key(_URL)
_REF = f"recipe:{_KEY}"
_COMPANY = "Example"
_PUBLIC = "93.184.216.34"
_HTML_RECIPE: dict[str, object] = {
    "version": 1,
    "format": "html",
    "url": _URL,
    "items": "//li[@class='job']",
    "fields": {"title": ".//a/text()", "url": ".//a/@href"},
}
_OTHER_RECIPE: dict[str, object] = {**_HTML_RECIPE, "items": "//li"}
_JSON_RECIPE: dict[str, object] = {
    "version": 1,
    "format": "json",
    "url": f"{_HOST}/api/jobs/",
    "items": "jobs",
    "fields": {"title": "title", "url": "link"},
}
_JOB_LINES = [
    f"Rust Engineer\t{_COMPANY}\t-\t-\t{_HOST}/jobs/1",
    f"Go Engineer\t{_COMPANY}\t-\t-\t{_HOST}/jobs/2",
]
_HELLO = "<html><body><p>Hello</p></body></html>"
_NOTE = "note: set [models].source_recipe to let ijob learn this page"
_UNRECOGNIZED = f"error: cannot use {_URL}: no hiring system, job posting or feed found"
_CONFIRMATION = "error: confirmation needed; pass --yes"
_SPENT_LINE = "cost $0.0010 of $0.50"
_FEED_URL = "https://feed.example.test/rss.xml"
_JAZZHR = (
    Path(__file__).parents[3]
    / "ijob-sources"
    / "tests"
    / "ijob_sources_tests"
    / "fixtures"
    / "jsonld"
    / "jazzhr.html"
)
_JOB_PAGE_URL = f"{_HOST}/jobs/release-engineer"

type _Page = tuple[str, bytes]


def _html(body: str) -> _Page:
    return "text/html; charset=utf-8", body.encode()


def _json(body: str) -> _Page:
    return "application/json", body.encode()


class _Site:
    """A mock web server: each path answers its pages in turn; a log of requests."""

    def __init__(self, pages: Mapping[str, list[_Page]]) -> None:
        self._pages = pages
        self._served: dict[str, int] = {}
        self.requests: list[httpx.Request] = []
        self.robots_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            self.robots_requests += 1
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        self.requests.append(request)
        pages = self._pages.get(path)
        if pages is None:
            return httpx.Response(404)
        index = self._served.get(path, 0)
        self._served[path] = index + 1
        content_type, body = pages[min(index, len(pages) - 1)]
        return httpx.Response(200, content=body, headers={"Content-Type": content_type})

    def fetched(self) -> list[str]:
        """Return the paths requested, robots.txt left out."""
        return [request.url.path for request in self.requests]


def _careers(*pages: _Page) -> _Site:
    return _Site({"/careers": list(pages or [_html(LIST_PAGE)])})


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


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


class _Apps:
    def __init__(
        self, tmp_path: Path, client_factory_for: ClientFactoryBuilder
    ) -> None:
        self._tmp_path = tmp_path
        self._client_factory_for = client_factory_for

    def __call__(  # noqa: PLR0913 - one knob of the test setup each
        self,
        site: _Site,
        engine: FakeAsyncEngine | None = None,
        *,
        model: bool = True,
        interactive: bool = False,
        max_cost_usd: float | None = None,
        raw_config: str | None = None,
    ) -> typer.Typer:
        config = _home(self._tmp_path) / "config.toml"
        _home(self._tmp_path).mkdir(exist_ok=True)
        cli_path = write_fake_cli(self._tmp_path / "bin").path
        write_llm_config(
            config,
            judge=None,
            source_recipe=MADE_UP_CLAUDE if model else None,
            max_cost_usd=max_cost_usd,
            concurrency=1,
            cli_path=cli_path,
        )
        if raw_config is not None:
            config.write_text(raw_config, encoding="utf-8")
        engines = FakeEngines(engine or FakeAsyncEngine([recipe_answer(_HTML_RECIPE)]))

        def options(_home: object, config_path: Path) -> LlmOptions:
            return LlmOptions(config_path=config_path, engines=engines)

        return make_app(
            self._client_factory_for(site),
            llm_options=options,
            is_interactive=lambda: interactive,
            resolver=_resolve,
        )


@pytest.fixture
def apps(tmp_path: Path, client_factory_for: ClientFactoryBuilder) -> _Apps:
    return _Apps(tmp_path, client_factory_for)


def _invoke(
    app_: typer.Typer, tmp_path: Path, *args: str, stdin: str | None = None
) -> Result:
    argv = [*args, "--db", str(tmp_path / "ijob.db")]
    env = {"IJOB_HOME": str(_home(tmp_path))}
    return CliRunner().invoke(app_, argv, env=env, input=stdin)


def _add_url(
    app_: typer.Typer, tmp_path: Path, *extra: str, stdin: str | None = None
) -> Result:
    return _invoke(
        app_,
        tmp_path,
        "sources",
        "add-url",
        _URL,
        "--company",
        _COMPANY,
        *extra,
        stdin=stdin,
    )


def _recipe_refs(tmp_path: Path) -> list[str]:
    with Store.open(tmp_path / "ijob.db") as store:
        return [
            str(row.ref)
            for row in store.sources()
            if row.ref.kind is SourceKind.RECIPE and not row.removed
        ]


def _stored_recipe(tmp_path: Path, ref: str = _REF) -> dict[str, object] | None:
    with Store.open(tmp_path / "ijob.db") as store:
        stored = store.recipe(parse_source_ref(ref))
    return None if stored is None else json.loads(stored.recipe)


def test_add_url_without_model_prints_note(tmp_path: Path, apps: _Apps) -> None:
    engine = FakeAsyncEngine()
    app_ = apps(_careers(), engine, model=False)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [_UNRECOGNIZED, _NOTE]
    assert engine.requests == []
    assert _recipe_refs(tmp_path) == []


def test_add_url_non_interactive_needs_yes(tmp_path: Path, apps: _Apps) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _careers()
    app_ = apps(site, engine)

    result = _add_url(app_, tmp_path)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [_CONFIRMATION]
    assert engine.requests == []
    assert site.fetched() == ["/careers"]
    assert _recipe_refs(tmp_path) == []


def test_add_url_yes_stores_recipe(tmp_path: Path, apps: _Apps) -> None:
    app_ = apps(_careers())

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"recipe: html at {_URL}",
        "read 2 jobs, 0 invalid",
        *_JOB_LINES,
        f"added {_REF}",
    ]
    assert re.search(r"^cost \$0\.0010 of \$\d+\.\d\d$", result.stderr, re.MULTILINE)
    listing = _invoke(app_, tmp_path, "sources", "list").stdout.splitlines()
    assert any(line.startswith(f"{_REF}\t{_COMPANY}\tuser\t") for line in listing)
    assert _stored_recipe(tmp_path) == _HTML_RECIPE


def test_add_url_interactive_accepts(tmp_path: Path, apps: _Apps) -> None:
    app_ = apps(_careers(), interactive=True)

    result = _add_url(app_, tmp_path, stdin="YES\n")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[-1] == f"added {_REF}"
    assert _recipe_refs(tmp_path) == [_REF]


def test_add_url_interactive_declines(tmp_path: Path, apps: _Apps) -> None:
    app_ = apps(_careers(), interactive=True)

    result = _add_url(app_, tmp_path, stdin="n\n")

    assert result.exit_code == 1
    assert result.stdout.splitlines()[-1].endswith("not stored")
    assert f"added {_REF}" not in result.stdout
    assert _recipe_refs(tmp_path) == []


def test_add_url_prompt_text(tmp_path: Path, apps: _Apps) -> None:
    app_ = apps(_careers(), interactive=True)

    result = _add_url(app_, tmp_path, stdin="y\n")

    assert "Store this source? [y/N] " in result.stdout
    assert "[y/N]:" not in result.stdout


def test_add_url_already_added(tmp_path: Path, apps: _Apps) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    app_ = apps(_careers(), engine)
    _add_url(app_, tmp_path, "--yes")

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        f"error: {_REF} is already added; run: ijob sources reanalyze {_REF}"
    ]
    assert len(engine.requests) == 1


def test_add_url_invalid_recipe(tmp_path: Path, apps: _Apps) -> None:
    engine = FakeAsyncEngine([recipe_answer({**_HTML_RECIPE, "items": "//li["})])
    app_ = apps(_careers(), engine)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    [line, cost] = result.stderr.splitlines()
    assert line.startswith("error: the model's recipe is invalid: ")
    assert cost == _SPENT_LINE
    assert _recipe_refs(tmp_path) == []


def test_add_url_no_jobs(tmp_path: Path, apps: _Apps) -> None:
    engine = FakeAsyncEngine(
        [recipe_answer({**_HTML_RECIPE, "items": "//li[@id='none']"})]
    )
    app_ = apps(_careers(), engine)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "error: the proposed recipe read no jobs from the page",
        _SPENT_LINE,
    ]
    assert _recipe_refs(tmp_path) == []


def test_add_url_cap_before_json_call(tmp_path: Path, apps: _Apps) -> None:
    engine = FakeAsyncEngine(
        [json_url_answer("/api/jobs/"), recipe_answer(_JSON_RECIPE)]
    )
    site = _Site(
        {
            "/careers": [_html(JS_ONLY_PAGE)],
            "/api/jobs/": [_json('{"jobs": []}')],
        }
    )
    app_ = apps(site, engine, max_cost_usd=0.0005)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "error: cost cap reached before the JSON recipe; raise [llm].max_cost_usd",
        "cost $0.0010 of $0.00",
    ]
    assert len(engine.requests) == 1
    assert _recipe_refs(tmp_path) == []


def test_add_url_provider_failure(tmp_path: Path, apps: _Apps) -> None:
    app_ = apps(_careers(), _RaisingEngine(ProviderFailedError("boom")))

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    [line] = result.stderr.splitlines()
    assert line.startswith("error: ")
    assert "boom" in line
    assert "Traceback" not in result.stderr
    assert _recipe_refs(tmp_path) == []


def test_add_url_private_host(tmp_path: Path, apps: _Apps) -> None:
    host = "internal.example.test"
    site = _Site({"/careers": [_html(_HELLO)]})
    app_ = apps(site)

    result = _invoke(
        app_,
        tmp_path,
        "sources",
        "add-url",
        f"http://{host}/careers",
        "--company",
        _COMPANY,
        "--yes",
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        f"error: http://{host}/robots.txt is not a public address"
    ]
    assert _recipe_refs(tmp_path) == []


@pytest.mark.parametrize(
    ("url", "page", "message"),
    [
        (
            "https://www.jobstreet.com/careers",
            _html(LIST_PAGE),
            "error: www.jobstreet.com is read only by its own source kind",
        ),
        (
            "https://www.japan-dev.com/jobs",
            _html(LIST_PAGE),
            "error: www.japan-dev.com is read only by its own source kind",
        ),
        (
            "http://169.254.169.254/careers",
            _html(LIST_PAGE),
            "error: http://169.254.169.254/robots.txt is not a public address",
        ),
        (
            "http://127.0.0.1/careers",
            _html(LIST_PAGE),
            "error: http://127.0.0.1/robots.txt is not a public address",
        ),
        (
            _URL,
            ("text/html", b"<html>" + b"a" * (5 * 1024 * 1024)),
            f"error: cannot use {_URL}: the page is larger than 5 MiB",
        ),
        (
            _URL,
            ("text/plain", b"Rust Engineer"),
            (
                f"error: cannot use {_URL}: "
                "the media type 'text/plain' is neither JSON nor HTML"
            ),
        ),
    ],
    ids=["jobstreet", "japandev", "metadata", "loopback", "over-5-mib", "plain-text"],
)
def test_add_url_refused_cases(
    tmp_path: Path, apps: _Apps, url: str, page: _Page, message: str
) -> None:
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _Site({"/careers": [page], "/jobs": [page]})
    app_ = apps(site, engine)

    result = _invoke(
        app_,
        tmp_path,
        "sources",
        "add-url",
        url,
        "--company",
        _COMPANY,
        "--yes",
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [message]
    assert "Traceback" not in result.stderr
    assert engine.requests == []
    assert _recipe_refs(tmp_path) == []
    if "169.254" in url or "127.0.0.1" in url or "jobstreet" in url or "japan" in url:
        assert site.requests == []
        assert site.robots_requests == 0


def test_add_url_malformed_url_is_readable_and_fetches_nothing(
    tmp_path: Path, apps: _Apps
) -> None:
    site = _careers()
    app_ = apps(site)

    result = _invoke(
        app_,
        tmp_path,
        "sources",
        "add-url",
        "http://[bad/careers",
        "--company",
        _COMPANY,
        "--yes",
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "error: cannot use http://[bad/careers: Invalid IPv6 URL"
    ]
    assert site.requests == []
    assert site.robots_requests == 0


def test_add_url_reads_an_unrecognized_page_and_its_robots_txt_once(
    tmp_path: Path, apps: _Apps
) -> None:
    site = _careers()
    app_ = apps(site)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 0, result.stderr
    assert site.fetched() == ["/careers"]
    assert site.robots_requests == 1


def test_add_url_follows_a_redirect_when_it_learns_the_page_once(
    tmp_path: Path, apps: _Apps
) -> None:
    pages = {"/careers/": [_html(LIST_PAGE)]}

    class _Redirecting(_Site):
        @override
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/careers":
                self.requests.append(request)
                return httpx.Response(301, headers={"Location": "/careers/"})
            return super().__call__(request)

    site = _Redirecting(pages)
    app_ = apps(site)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 0, result.stderr
    assert site.fetched() == ["/careers", "/careers/"]
    assert f"recipe: html at {_URL}" in result.stdout
    assert _recipe_refs(tmp_path) == [f"recipe:{manual_key(_URL + '/')}"]


def test_add_url_json_address_is_fetched_after_the_page_is_reused(
    tmp_path: Path, apps: _Apps
) -> None:
    engine = FakeAsyncEngine(
        [json_url_answer("/api/jobs/"), recipe_answer(_JSON_RECIPE)]
    )
    site = _Site(
        {
            "/careers": [_html(JS_ONLY_PAGE)],
            "/api/jobs/": [_json('{"jobs": [{"title": "Rust", "link": "/j/1"}]}')],
        }
    )
    app_ = apps(site, engine)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 0, result.stderr
    assert site.fetched() == ["/careers", "/api/jobs/"]
    assert site.robots_requests == 2


def test_add_url_failure_after_spending_prints_the_cost_after_the_error(
    tmp_path: Path, apps: _Apps
) -> None:
    engine = FakeAsyncEngine([recipe_answer({**_HTML_RECIPE, "items": "//li["})])
    app_ = apps(_careers(), engine)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    [error, cost] = result.stderr.splitlines()
    assert error.startswith("error: the model's recipe is invalid: ")
    assert cost == "cost $0.0010 of $0.50"
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


def test_add_url_failure_before_any_spend_prints_no_cost(
    tmp_path: Path, apps: _Apps
) -> None:
    app_ = apps(_careers(), _RaisingEngine(ProviderFailedError("boom")))

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    [line] = result.stderr.splitlines()
    assert "boom" in line
    assert "cost" not in result.stderr


def test_reanalyze_failure_after_spending_prints_the_cost_after_the_error(
    tmp_path: Path, apps: _Apps
) -> None:
    _seed_recipe(tmp_path)
    engine = FakeAsyncEngine([recipe_answer({**_HTML_RECIPE, "items": "//li["})])
    app_ = apps(_careers(), engine)

    result = _reanalyze(app_, tmp_path, _REF, "--yes")

    assert result.exit_code == 1
    [error, cost] = result.stderr.splitlines()
    assert error.startswith("error: the model's recipe is invalid: ")
    assert cost == "cost $0.0010 of $0.50"


def test_add_url_learns_recipes_runtime_error(tmp_path: Path, apps: _Apps) -> None:
    config = f'[models]\nsource_recipe = "{MADE_UP_CLAUDE}"\n\n[llm]\nbogus_key = 1\n'
    app_ = apps(_careers(), raw_config=config)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 2
    [line] = result.stderr.splitlines()
    assert line.startswith("error: ")
    assert "Traceback" not in result.stderr
    assert _recipe_refs(tmp_path) == []


def test_add_url_job_posting_unchanged(tmp_path: Path, apps: _Apps) -> None:
    engine = FakeAsyncEngine()
    site = _Site({"/jobs/release-engineer": [("text/html", _JAZZHR.read_bytes())]})
    app_ = apps(site, engine, model=False)

    result = _invoke(
        app_,
        tmp_path,
        "sources",
        "add-url",
        _JOB_PAGE_URL,
        "--company",
        "Eclipse Foundation",
    )

    assert result.exit_code == 0, result.stderr
    ref = f"jobposting:{manual_key(_JOB_PAGE_URL)}"
    assert result.stdout == f"added {ref}\n"
    assert result.stderr == ""
    assert engine.requests == []


def test_add_url_empty_feed_unchanged(tmp_path: Path, apps: _Apps) -> None:
    feed = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        "</channel></rss>"
    )
    app_ = apps(_Site({"/rss.xml": [("application/rss+xml", feed.encode())]}))

    result = _invoke(
        app_, tmp_path, "sources", "add-url", _FEED_URL, "--company", _COMPANY
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        f"error: cannot use {_FEED_URL}: no jobs; cannot verify the company"
    ]


def test_sources_help_names_reanalyze() -> None:
    result = CliRunner().invoke(app, ["sources", "--help"])

    assert result.exit_code == 0
    assert "reanalyze" in result.stdout


_SEEN = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _seed_recipe(tmp_path: Path, *, company: str = "Other Co") -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.set_recipe_source(
            parse_source_ref(_REF),
            company=company,
            target_url=_URL,
            recipe=json.dumps(_OTHER_RECIPE),
            model="old-model",
            at=_SEEN,
        )


def _reanalyze(
    app_: typer.Typer,
    tmp_path: Path,
    ref: str = _REF,
    *extra: str,
    stdin: str | None = None,
) -> Result:
    return _invoke(app_, tmp_path, "sources", "reanalyze", ref, *extra, stdin=stdin)


def test_reanalyze_replaces_recipe(tmp_path: Path, apps: _Apps) -> None:
    _seed_recipe(tmp_path)
    app_ = apps(_careers())

    result = _reanalyze(app_, tmp_path, _REF, "--yes")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"recipe: html at {_URL}",
        "read 2 jobs, 0 invalid",
        *(line.replace(_COMPANY, "Other Co") for line in _JOB_LINES),
        f"added {_REF}",
    ]
    assert re.search(r"^cost \$0\.0010 of ", result.stderr, re.MULTILINE)
    assert _stored_recipe(tmp_path) == _HTML_RECIPE
    listing = _invoke(app_, tmp_path, "sources", "list").stdout.splitlines()
    assert any(line.startswith(f"{_REF}\tOther Co\tuser\t") for line in listing)


def test_reanalyze_bad_ref(tmp_path: Path, apps: _Apps) -> None:
    site = _careers()
    app_ = apps(site)

    result = _reanalyze(app_, tmp_path, "nonsense", "--yes")

    assert result.exit_code == 2
    assert result.stderr.startswith("error: ")
    assert site.requests == []


def test_reanalyze_unknown_ref(tmp_path: Path, apps: _Apps) -> None:
    app_ = apps(_careers())

    result = _reanalyze(app_, tmp_path, "recipe:nothing", "--yes")

    assert result.exit_code == 2
    assert result.stderr.splitlines() == ["error: unknown source recipe:nothing"]


def test_reanalyze_removed_ref(tmp_path: Path, apps: _Apps) -> None:
    _seed_recipe(tmp_path)
    app_ = apps(_careers())
    _invoke(app_, tmp_path, "sources", "remove", _REF)

    result = _reanalyze(app_, tmp_path, _REF, "--yes")

    assert result.exit_code == 2
    assert result.stderr.splitlines() == [f"error: unknown source {_REF}"]


def test_reanalyze_other_kind(tmp_path: Path, apps: _Apps) -> None:
    with Store.open(tmp_path / "ijob.db") as store:
        store.add_source(parse_source_ref("greenhouse:x"), "X")
    app_ = apps(_careers())

    result = _reanalyze(app_, tmp_path, "greenhouse:x", "--yes")

    assert result.exit_code == 2
    assert result.stderr.splitlines() == ["error: greenhouse:x has no recipe"]


def test_reanalyze_missing_model(tmp_path: Path, apps: _Apps) -> None:
    _seed_recipe(tmp_path)
    app_ = apps(_careers(), model=False)

    result = _reanalyze(app_, tmp_path, _REF, "--yes")

    assert result.exit_code == 1
    [line] = result.stderr.splitlines()
    assert line.startswith("error: ")
    assert "source_recipe" in line
    assert _stored_recipe(tmp_path) == _OTHER_RECIPE


def test_reanalyze_needs_yes_non_interactive(tmp_path: Path, apps: _Apps) -> None:
    _seed_recipe(tmp_path)
    engine = FakeAsyncEngine([recipe_answer(_HTML_RECIPE)])
    site = _careers()
    app_ = apps(site, engine)

    result = _reanalyze(app_, tmp_path)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [_CONFIRMATION]
    assert engine.requests == []
    assert site.requests == []
    assert _stored_recipe(tmp_path) == _OTHER_RECIPE


def test_reanalyze_declined_keeps_old_recipe(tmp_path: Path, apps: _Apps) -> None:
    _seed_recipe(tmp_path)
    app_ = apps(_careers(), interactive=True)

    result = _reanalyze(app_, tmp_path, stdin="n\n")

    assert result.exit_code == 1
    assert "Store this source? [y/N] " in result.stdout
    assert result.stdout.splitlines()[-1].endswith("not stored")
    assert _stored_recipe(tmp_path) == _OTHER_RECIPE


def _fail_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def locked(_store: Store, *_args: object, **_kwargs: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(Store, "set_recipe_source", locked)


def test_add_url_store_failure_is_readable(
    tmp_path: Path, apps: _Apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_ = apps(_careers())
    _fail_store(monkeypatch)

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == "error: database is locked"
    assert "Traceback" not in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_reanalyze_store_failure_is_readable(
    tmp_path: Path, apps: _Apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_recipe(tmp_path)
    app_ = apps(_careers())
    _fail_store(monkeypatch)

    result = _reanalyze(app_, tmp_path, _REF, "--yes")

    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == "error: database is locked"
    assert _stored_recipe(tmp_path) == _OTHER_RECIPE


def test_add_url_prints_report_notes_on_stderr(
    tmp_path: Path, apps: _Apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyze = Service.analyze_url

    async def noisy(
        self: Service, url: str, company: str, **kwargs: FetchedPage | None
    ) -> RecipeProposal:
        proposal = await analyze(self, url, company, **kwargs)
        report = dataclasses.replace(
            proposal.report, warnings=("warning: tracing is off",), dropped_spans=3
        )
        return dataclasses.replace(proposal, report=report)

    monkeypatch.setattr(Service, "analyze_url", noisy)
    app_ = apps(_careers())

    result = _add_url(app_, tmp_path, "--yes")

    assert result.exit_code == 0, result.stderr
    assert result.stderr.splitlines()[-2:] == [
        "warning: tracing is off",
        "warning: 3 spans not stored",
    ]
    assert "warning" not in result.stdout


def test_reanalyze_missing_home_beats_bad_ref(tmp_path: Path, apps: _Apps) -> None:
    app_ = apps(_careers())
    missing = tmp_path / "nowhere"

    result = CliRunner().invoke(
        app_,
        ["sources", "reanalyze", "nonsense", "--db", str(tmp_path / "ijob.db")],
        env={"IJOB_HOME": str(missing)},
    )

    assert result.exit_code == 1
    [line] = result.stderr.splitlines()
    assert line.startswith("error: ")
    assert "nonsense" not in line
    assert not missing.exists()


def test_reanalyze_bad_ref_creates_no_database(tmp_path: Path, apps: _Apps) -> None:
    app_ = apps(_careers())

    result = _reanalyze(app_, tmp_path, "nonsense", "--yes")

    assert result.exit_code == 2
    assert not (tmp_path / "ijob.db").exists()
