"""A stored recipe source, or a page no model is set to learn, loads no LLM library.

Each test runs one entry point in a fresh interpreter, over a database that holds
a recipe source and its recipe, and reads whether `dspy` or `claude_agent_sdk`
was imported.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ijob_core.model import parse_source_ref
from ijob_core.store import Store

_REF: Final = parse_source_ref("recipe:careers")
_URL: Final = "https://jobs.example.test/careers/"
_RECIPE: Final[dict[str, object]] = {
    "version": 1,
    "format": "html",
    "url": _URL,
    "items": "//li[@class='job']",
    "fields": {"title": ".//a", "url": ".//a/@href"},
}

# Shared by the scripts below: it reads the home and database paths from the
# arguments and defines a client factory that serves the recipe page from a mock
# transport and a resolver that answers every host with a public address.
_PRELUDE: Final = """\
import asyncio, sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from ijob_app.home import IjobHome
from ijob_app.service import open_service
from ijob_sources.polite_http import PoliteClient

HOME, DB = Path(sys.argv[1]), Path(sys.argv[2])
PAGE = (
    '<ul><li class="job"><a href="/careers/1/">Platform Engineer</a></li>'
    '<li class="job"><a href="/careers/2/">Data Engineer</a></li></ul>'
)
NOW = lambda: datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def handler(request):
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\\nAllow: /\\n")
    return httpx.Response(
        200, content=PAGE.encode(), headers={"Content-Type": "text/html"}
    )


def factory(cache):
    ticks = [0.0]

    async def sleep(seconds):
        ticks[0] += seconds

    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=lambda: ticks[0],
        sleep=sleep,
        cache=cache,
    )


async def resolve(host, port):
    return ["93.184.216.34"]


def open_stored():
    return open_service(
        IjobHome(HOME), DB, factory, now=NOW, use_cache=False, resolver=resolve
    )


def llm_loaded():
    return "dspy" in sys.modules or "claude_agent_sdk" in sys.modules
"""

_SEARCH: Final = (
    _PRELUDE
    + """\
from ijob_app.config import FilterFlags, read_filter_layers
from ijob_core.model import parse_source_ref

ref = parse_source_ref("recipe:careers")


async def main():
    layers = read_filter_layers(None, FilterFlags())
    with open_stored() as service:
        await service.refresh([ref])
        return [item async for item in service.search(layers, [ref])]


items = asyncio.run(main())
print(sorted(item.posting.title for item in items))
print(llm_loaded())
"""
)

_SOURCES_LIST: Final = (
    _PRELUDE
    + """\
from ijob_app.cli import make_app
from typer.testing import CliRunner

def refuse(cache):
    raise AssertionError("sources list made a request")

result = CliRunner().invoke(
    make_app(refuse, resolver=resolve),
    ["sources", "list", "--db", str(DB)],
    env={"IJOB_HOME": str(HOME)},
)
print(result.exit_code)
print("recipe:careers" in result.stdout)
print(llm_loaded())
"""
)

_ADD_URL_NO_MODEL: Final = (
    _PRELUDE
    + """\
from ijob_app.cli import make_app
from typer.testing import CliRunner

result = CliRunner().invoke(
    make_app(factory, resolver=resolve),
    [
        "sources", "add-url", "https://jobs.example.test/other",
        "--company", "Example Co", "--yes", "--db", str(DB),
    ],
    env={"IJOB_HOME": str(HOME)},
)
print(result.exit_code)
print("[models].source_recipe" in result.stderr)
print(llm_loaded())
"""
)

_TUI: Final = (
    _PRELUDE
    + """\
import ijob_app.tui.recipe
import ijob_app.tui.sources
from ijob_app.tui import IjobApp


async def main():
    with open_stored() as service:
        app = IjobApp(service, None)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            return app.query_one("#panes").current


print(asyncio.run(main()))
print(llm_loaded())
"""
)


def _store_recipe_source(db: Path) -> None:
    with Store.open(db) as store:
        store.set_recipe_source(
            _REF,
            company="Example Co",
            target_url=_URL,
            recipe=json.dumps(_RECIPE),
            model="made-up-model",
            at=datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
        )


def _run(script: str, tmp_path: Path) -> list[str]:
    """Run `script` in a new interpreter and return its output lines."""
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "ijob.db"
    _store_recipe_source(db)
    run = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [sys.executable, "-I", "-c", script, str(home), str(db)],
        env={**os.environ, "IJOB_HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.splitlines()


def test_search_with_recipe_source_loads_no_llm_library(tmp_path: Path) -> None:
    lines = _run(_SEARCH, tmp_path)

    assert lines[-2:] == ["['Data Engineer', 'Platform Engineer']", "False"]


def test_sources_list_with_recipe_source_loads_no_llm_library(tmp_path: Path) -> None:
    lines = _run(_SOURCES_LIST, tmp_path)

    assert lines[-3:] == ["0", "True", "False"]


def test_add_url_without_model_loads_no_llm_library(tmp_path: Path) -> None:
    lines = _run(_ADD_URL_NO_MODEL, tmp_path)

    assert lines[-3:] == ["1", "True", "False"]


def test_tui_start_with_recipe_source_loads_no_llm_library(tmp_path: Path) -> None:
    lines = _run(_TUI, tmp_path)

    assert lines[-2:] == ["sources", "False"]


def test_recipe_ops_does_not_load_llm_text() -> None:
    script = (
        "import sys, ijob_app.recipe_ops; print('ijob_app.llm_text' in sys.modules)"
    )
    run = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["False"]
