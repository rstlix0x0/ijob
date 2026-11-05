import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, override

import httpx
import pytest
import typer
from ijob_ai.errors import ProviderFailedError
from ijob_ai.evals.agent_cases import HostilePage, load_hostile_pages
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.tasks.text import input_sha256, posting_block
from ijob_app.cli import make_app
from ijob_app.cli_eval import _show_suite_result
from ijob_app.eval_agent_suites import source_cases
from ijob_app.eval_ops import ReviewItem, review_items
from ijob_app.eval_suites import Suite, SuiteReport
from ijob_app.home import IjobHome, setup_home
from ijob_app.llm_config import read_eval_settings
from ijob_app.llm_ops import LlmOptions, OperationReport
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalResult,
    EvalRun,
    EvalTask,
    EvalVote,
    LabelOrigin,
    RunStatus,
)
from ijob_core.model import (
    ExternalId,
    Instructions,
    ResumeProfile,
    Seniority,
    SightingDraft,
    SourceKind,
    SourceRef,
    StoredCv,
    parse_source_key,
)
from ijob_core.store import Store
from rich.console import Console
from typer.testing import CliRunner, Result

from .conftest import ClientFactoryBuilder
from .eval_fakes import (
    AsyncFakeTransport,
    JurorEngine,
    JurorEngines,
    openrouter_reply,
    serve_openrouter,
)
from .llm_fakes import (
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    Opaque,
    score_answer,
    verdict_answer,
    write_fake_cli,
    write_llm_config,
)
from .test_eval_agent_suites import (
    _BOARD_API,
    _BOARD_JSON,
    _FINDS_ACME,
    _GRADE,
    _PROPOSE_ACME,
    _SUMMARY,
    FINISH,
    NOTHING,
    _ByModel,
    _page_scripts,
    _reading_scripts,
    _ScriptedEngine,
)
from .test_eval_view import _run_detail

_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_SOURCE = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_A = "claude-sdk/juror-a"
_B = "claude-sdk/juror-b"
_ROUTED = "openrouter/made-up/juror-c"
_ROUTED_COST = 0.06
_BOX_CHARACTERS = "╭╮╰╯│─✓✗"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_NOTICE = "Sends your rules, CV profile and postings to OpenRouter and made-up"


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


class _World:
    """A home with 30 postings, rules and a CV profile, and a way to run `eval`."""

    def __init__(  # noqa: PLR0913  # one knob of the setup per argument
        self,
        root: Path,
        clients: ClientFactoryBuilder,
        *,
        jury: tuple[str, ...] | None = (_A, _B),
        max_cost: float | None = None,
        total_cost: float | None = None,
        ready: bool = True,
        tuner: str | None = None,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.home = IjobHome(root / "home")
        setup_home(self.home)
        self.engines = JurorEngines()
        self.prices_requested = 0
        self._clients = clients
        cli = write_fake_cli(root / "bin")
        write_llm_config(
            self.home.config,
            judge=None,
            concurrency=1,
            cli_path=cli.path,
            tuner=tuner,
        )
        lines = ["", "[eval]"]
        if jury is not None:
            lines.append("jury = [" + ", ".join(f'"{ref}"' for ref in jury) + "]")
        if max_cost is not None:
            lines.append(f"max_cost_usd = {max_cost}")
        if total_cost is not None:
            lines.append(f"total_cost_usd = {total_cost}")
        with self.home.config.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        if ready:
            with Store.open(self.home.db) as store:
                _seed(store)
                _store_rules(store)
                _store_profile(store)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/api/v1/models":
            self.prices_requested += 1
            body = {
                "data": [
                    {
                        "id": "made-up/juror-c",
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    }
                ]
            }
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    def app(self, *, interactive: bool) -> typer.Typer:
        def options(_home: object, config_path: Path) -> LlmOptions:
            return LlmOptions(config_path=config_path, engines=self.engines)

        return make_app(
            self._clients(self._handler),
            now=lambda: _AT,
            llm_options=options,
            is_interactive=lambda: interactive,
        )

    def run(
        self, *args: str, interactive: bool = False, stdin: str | None = None
    ) -> Result:
        return CliRunner().invoke(
            self.app(interactive=interactive),
            ["eval", "label", *args],
            env={"IJOB_HOME": str(self.home.root), "COLUMNS": "120"},
            input=stdin,
        )

    def suite(
        self, *args: str, interactive: bool = False, stdin: str | None = None
    ) -> Result:
        return CliRunner().invoke(
            self.app(interactive=interactive),
            ["eval", "run", *args],
            env={"IJOB_HOME": str(self.home.root), "COLUMNS": "120"},
            input=stdin,
        )

    def review(self, stdin: str | None = None) -> Result:
        return CliRunner().invoke(
            self.app(interactive=False),
            ["eval", "review"],
            env={"IJOB_HOME": str(self.home.root), "COLUMNS": "120"},
            input=stdin,
        )

    def labels(self) -> list[EvalLabel]:
        with Store.open(self.home.db) as store:
            return [
                label
                for each in store.evals.sets()
                for label in store.evals.labels(each.id)
            ]

    def items(self) -> tuple[ReviewItem, ...]:
        """List what a review offers now, as the command will."""
        with Store.open(self.home.db) as store:
            settings = read_eval_settings(self.home.config)
            return review_items(store, settings, now=lambda: _AT).items

    def calls(self) -> int:
        return sum(
            len(self.engines.engine(model).calls)
            for model in ("juror-a", "juror-b", "juror-c")
        )

    def votes(self) -> int:
        with Store.open(self.home.db) as store:
            return sum(len(store.evals.votes(each.id)) for each in store.evals.sets())

    def runs(self) -> int:
        with Store.open(self.home.db) as store:
            return len(store.evals.runs())


@pytest.fixture
def world(tmp_path: Path, client_factory_for: ClientFactoryBuilder) -> _World:
    return _World(tmp_path, client_factory_for)


def _seed(store: Store) -> None:
    store.import_seeds([(_SOURCE, "Alpha")])
    store.record(
        [
            SightingDraft(
                source=_SOURCE,
                external_id=ExternalId(str(number)),
                title=f"Engineer {number}",
                company="Acme",
                location="Remote",
                url=f"https://example.com/jobs/{number}",
                posted_at=None,
                raw="{}",
                description=f"Build things {number}",
            )
            for number in range(30)
        ],
        seen_at=_AT,
    )


def _store_rules(
    store: Store, rules: tuple[str, ...] = ("no crypto", "remote")
) -> None:
    store.set_instructions(
        Instructions(
            text="Remote only, no crypto",
            filters_json="{}",
            rules=rules,
            model=MADE_UP_CLAUDE,
            task_version="instructions-1",
            set_at=_AT,
        )
    )


def _store_profile(store: Store) -> None:
    store.set_cv(StoredCv(name="cv.pdf", sha256="a" * 64, size=1, stored_at=_AT))
    store.set_cv_profile(
        ResumeProfile(
            summary="Backend engineer",
            years_experience=7,
            seniority=Seniority.SENIOR,
            roles=("Backend Engineer",),
            spoken_languages=("English",),
            locations=("Jakarta",),
            work_authorization=("Indonesia",),
            skills=("Python",),
        ),
        cv_sha256="a" * 64,
        model=MADE_UP_CLAUDE,
        task_version="cv_profile-1",
        made_at=_AT,
    )


def _routed(monkeypatch: pytest.MonkeyPatch, cases: int) -> AsyncFakeTransport:
    """Serve one OpenRouter juror's replies: a judge then a scorer per case."""
    replies = []
    for _ in range(cases):
        replies.append(openrouter_reply(verdict_answer(), cost=_ROUTED_COST))
        replies.append(openrouter_reply(score_answer(70), cost=_ROUTED_COST))
    return serve_openrouter(monkeypatch, replies)


@pytest.mark.usefixtures("on_a_terminal")
def test_label_shows_estimate_and_data_notice(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(tmp_path, client_factory_for, jury=(_A, _ROUTED))
    _routed(monkeypatch, 10)

    result = world.run("--limit", "10", "--yes")

    assert result.exit_code == 0, result.output
    assert "Jury · 10 postings · judge + scorer" in result.stdout
    assert _A in result.stdout
    assert _ROUTED in result.stdout
    assert "subscription" in result.stdout
    assert "under $0.01" in result.stdout or "about $0." in result.stdout
    assert "cap $4.00" in result.stdout
    assert _NOTICE in result.stdout
    assert world.prices_requested == 1


def test_label_declined_makes_no_call(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _World(tmp_path, client_factory_for, jury=(_A, _ROUTED))

    result = world.run("--limit", "10", interactive=True, stdin="n\n")

    assert result.exit_code == 1
    assert "Start the jury? [y/N]" in result.stderr
    assert "Start the jury?" not in result.stdout
    assert world.calls() == 0
    assert world.votes() == 0
    assert world.runs() == 0


def test_label_yes_skips_the_question(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(tmp_path, client_factory_for, jury=(_A, _ROUTED))
    transport = _routed(monkeypatch, 10)

    result = world.run("--limit", "10", "--yes", interactive=True)

    assert result.exit_code == 0, result.output
    assert "Start the jury?" not in result.output
    assert len(transport.requests) == 20
    assert world.engines.engine("juror-a").calls.count("judge") == 10
    assert world.votes() == 40


def test_label_in_a_pipe_needs_yes(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _World(tmp_path, client_factory_for, jury=(_A, _ROUTED))

    result = world.run("--limit", "10")

    assert result.exit_code == 1
    assert "confirmation needed; pass --yes" in result.stderr
    assert world.calls() == 0
    assert world.runs() == 0


@pytest.mark.parametrize("limit", ["0", "101"])
def test_label_limit_out_of_range_exits_2(world: _World, limit: str) -> None:
    result = world.run("--limit", limit, "--yes")

    assert result.exit_code == 2
    assert world.calls() == 0


@pytest.mark.parametrize("count", ["0", "101"])
def test_cli_add_range(world: _World, count: str) -> None:
    result = world.run("--add", count, "--yes")

    assert result.exit_code == 2
    assert world.calls() == 0


def test_cli_add_with_limit_exits_2(world: _World) -> None:
    result = world.run("--add", "5", "--limit", "5", "--yes")

    assert result.exit_code == 2
    assert "limit" in result.stderr
    assert world.calls() == 0


def _more_postings(world: _World, count: int) -> None:
    """Record `count` postings beyond the 30 the world starts with."""
    with Store.open(world.home.db) as store:
        store.record(
            [
                SightingDraft(
                    source=_SOURCE,
                    external_id=ExternalId(str(number)),
                    title=f"Engineer {number}",
                    company="Acme",
                    location="Remote",
                    url=f"https://example.com/jobs/{number}",
                    posted_at=None,
                    raw="{}",
                    description=f"Build things {number}",
                )
                for number in range(30, 30 + count)
            ],
            seen_at=_AT,
        )


def _case_count(world: _World) -> int:
    with Store.open(world.home.db) as store:
        return sum(len(store.evals.cases(each.id)) for each in store.evals.sets())


def _labelled_world(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
    *,
    total_cost: float | None = None,
) -> _World:
    """Return a world whose 30-case set is fully voted and that has 20 more postings."""
    world = _World(
        tmp_path, client_factory_for, jury=(_A, _ROUTED), total_cost=total_cost
    )
    _routed(monkeypatch, 30)
    first = world.run("--yes")
    assert first.exit_code == 0, first.output
    _more_postings(world, 20)
    assert _case_count(world) == 30
    return world


def test_cli_declined_add_leaves_the_set_alone(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _labelled_world(tmp_path, client_factory_for, monkeypatch)
    calls = world.calls()

    declined = world.run("--add", "5", interactive=True, stdin="n\n")
    unattended = world.run("--add", "5")

    assert declined.exit_code == 1
    assert "Start the jury? [y/N]" in declined.stderr
    assert unattended.exit_code == 1
    assert "confirmation needed; pass --yes" in unattended.stderr
    assert _case_count(world) == 30
    assert world.calls() == calls
    _routed(monkeypatch, 2)
    later = world.run("--add", "2", "--yes")
    assert later.exit_code == 0, later.output
    assert _case_count(world) == 32


def test_cli_budget_refused_add_leaves_the_set_alone(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _labelled_world(tmp_path, client_factory_for, monkeypatch, total_cost=4.0)
    with Store.open(world.home.db) as store:
        spent = store.evals.start_run("judge", "m", "judge-1", None, started_at=_AT)
        store.evals.record_spend(spent.id, spent_usd=0.5, subscription_usd=0)

    result = world.run("--add", "5", "--yes")

    assert result.exit_code == 1
    assert "eval.total_cost_usd" in result.stderr
    assert _case_count(world) == 30


def test_cli_accepted_add_stores_the_cases_and_asks_for_the_new_votes(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _labelled_world(tmp_path, client_factory_for, monkeypatch)
    _routed(monkeypatch, 5)

    result = world.run("--add", "5", "--yes")

    assert result.exit_code == 0, result.output
    assert _case_count(world) == 35
    assert re.search(r"^requested +20$", result.stdout, re.MULTILINE)
    assert re.search(r"^added +5 postings to the set$", result.stderr, re.MULTILINE)


@pytest.mark.usefixtures("on_a_terminal")
def test_cli_add_on_a_terminal_notes_the_added_count(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _labelled_world(tmp_path, client_factory_for, monkeypatch)
    _routed(monkeypatch, 5)

    result = world.run("--add", "5", "--yes")

    assert result.exit_code == 0, result.output
    assert "! added 5 postings to the label set" in result.stderr


def test_cli_add_without_a_set_points_at_plain_label(world: _World) -> None:
    result = world.run("--add", "5", "--yes")

    assert result.exit_code == 1
    assert "ijob eval label" in result.stderr
    assert world.calls() == 0


def test_label_absent_jury_exits_2(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _World(tmp_path, client_factory_for, jury=None)

    result = world.run("--yes")

    assert result.exit_code == 2
    assert "eval.jury" in result.stderr
    assert "[eval]" in result.stderr
    assert world.calls() == 0


def test_label_without_rules_or_profile_names_the_fix(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _World(tmp_path, client_factory_for, ready=False)
    with Store.open(world.home.db) as store:
        _seed(store)

    rules = world.run("--yes")
    with Store.open(world.home.db) as store:
        _store_rules(store)
    profile = world.run("--yes")

    assert rules.exit_code == 1
    assert "no rules are stored" in rules.stderr
    assert "run: ijob instructions set" in rules.stderr
    assert profile.exit_code == 1
    assert "run: ijob cv profile" in profile.stderr
    assert world.calls() == 0


def test_label_provider_stop_exits_1_cap_exits_0(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = _World(tmp_path / "stopped", client_factory_for)
    stopped.engines = JurorEngines(
        {"juror-b": JurorEngine(raises=ProviderFailedError("boom"))}
    )
    capped = _World(
        tmp_path / "capped", client_factory_for, jury=(_A, _ROUTED), max_cost=0.1
    )
    _routed(monkeypatch, 3)

    provider = stopped.run("--limit", "10", "--yes")
    cap = capped.run("--limit", "10", "--yes")

    assert provider.exit_code == 1
    assert "claude-sdk/juror-b stopped: boom" in provider.stderr
    assert "votes" in provider.stdout
    assert cap.exit_code == 0, cap.output
    assert "cost cap" in cap.stderr
    assert capped.votes() == 4


def test_label_provider_text_is_literal_with_a_piped_stdout_and_a_terminal_stderr(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Console, "is_terminal", property(lambda _self: True))
    monkeypatch.setattr("ijob_app.cli_output._stderr_is_terminal", lambda: True)
    world.engines = JurorEngines(
        {
            "juror-b": JurorEngine(
                raises=ProviderFailedError("boom; run: \x1b]0;pwned\x07rm -rf ~")
            )
        }
    )

    result = world.run("--limit", "10", "--yes")

    assert result.exit_code == 1
    assert "claude-sdk/juror-b stopped: boom" in result.stderr
    assert "\x1b]0;pwned" not in result.stderr
    assert "\x07" not in result.stderr
    assert "fix" not in result.stderr
    assert "run: ]0;pwnedrm -rf ~" in result.stderr


def test_label_cost_unknown_exits_1(world: _World) -> None:
    world.engines = JurorEngines({"juror-a": JurorEngine(cost=None)})

    result = world.run("--limit", "10", "--yes")

    assert result.exit_code == 1
    assert "cost of claude-sdk/juror-a is unknown" in result.stderr
    assert world.engines.engine("juror-b").calls == []
    assert world.votes() == 0


def test_label_claude_only_jury_does_not_ask(world: _World) -> None:
    result = world.run("--limit", "10", interactive=True)

    assert result.exit_code == 0, result.output
    assert "Start" not in result.output
    assert "Sends" not in result.output
    assert world.prices_requested == 0
    assert world.votes() == 40


@pytest.mark.usefixtures("on_a_terminal")
def test_label_claude_only_jury_does_not_ask_on_a_terminal(world: _World) -> None:
    result = world.run("--limit", "10", interactive=True)

    assert result.exit_code == 0, result.output
    assert "Jury · 10 postings · judge + scorer" in result.stdout
    assert "Estimate subscription · cap $4.00" in result.stdout
    assert "Start" not in result.stdout
    assert "Sends" not in result.stdout
    assert "Jury finished  40 of 40 votes" in result.stdout
    assert "judge" in result.stdout
    assert "agree" in result.stdout


def test_label_no_money_left_exits_1(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _World(tmp_path, client_factory_for, total_cost=0.5)
    with Store.open(world.home.db) as store:
        spent = store.evals.start_run("judge", "m", "judge-1", None, started_at=_AT)
        store.evals.record_spend(spent.id, spent_usd=0.5, subscription_usd=0)

    result = world.run("--limit", "10", "--yes")

    assert result.exit_code == 1
    assert "eval.total_cost_usd" in result.stderr
    assert world.calls() == 0
    assert world.votes() == 0


def test_label_changed_set_asks_first(world: _World) -> None:
    first = world.run("--limit", "10", "--yes")
    calls = world.calls()
    with Store.open(world.home.db) as store:
        _store_rules(store, rules=("only rust",))

    unattended = world.run("--limit", "10")
    declined = world.run("--limit", "10", interactive=True, stdin="n\n")
    calls_after_declining = world.calls()
    accepted = world.run("--limit", "10", interactive=True, stdin="y\n")

    assert first.exit_code == 0, first.output
    assert unattended.exit_code == 1
    assert "rules changed since the last jury run" in unattended.stderr
    assert "confirmation needed; pass --yes" in unattended.stderr
    assert declined.exit_code == 1
    assert "this starts a new label set" in declined.stderr
    assert "Start a new label set? [y/N]" in declined.stderr
    assert calls_after_declining == calls
    assert accepted.exit_code == 0, accepted.output
    assert world.calls() == 2 * calls


def test_label_pipe_output_is_plain(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(tmp_path, client_factory_for, jury=(_A, _ROUTED))
    _routed(monkeypatch, 10)

    result = world.run("--limit", "10", "--yes")

    assert result.exit_code == 0, result.output
    for text in (result.stdout, result.stderr):
        assert not set(_BOX_CHARACTERS) & set(text)
        assert "\x1b" not in text
    assert re.search(r"^votes +40$", result.stdout, re.MULTILINE)
    assert re.search(r"^requested +40$", result.stdout, re.MULTILINE)
    for name in ("judge_agree", "judge_review", "scorer_agree", "scorer_review"):
        assert re.search(rf"^{name} +\d+$", result.stdout, re.MULTILINE)
    assert re.search(r"^cost_usd +1\.2000$", result.stdout, re.MULTILINE)
    assert f"juror            {_ROUTED}" in result.stderr
    assert f"note: {_NOTICE}" in result.stderr


def test_label_second_run_has_nothing_left_to_ask(world: _World) -> None:
    world.run("--limit", "10", "--yes")
    calls = world.calls()

    again = world.run("--limit", "10")

    assert again.exit_code == 0, again.output
    assert world.calls() == calls
    assert re.search(r"^requested +0$", again.stdout, re.MULTILINE)


def test_label_help_lists_its_flags(world: _World) -> None:
    result = CliRunner().invoke(
        world.app(interactive=False),
        ["eval", "label", "--help"],
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
    )

    assert result.exit_code == 0
    for flag in ("--limit", "--add", "--yes", "--db", "--config"):
        # Typer forces styled help under GITHUB_ACTIONS, which splits a flag's text.
        assert flag in _ANSI.sub("", result.stdout)


def _split_jury(world: _World) -> None:
    """Have the jurors disagree on every case, then label ten of them."""
    world.engines = JurorEngines(
        {
            "juror-a": JurorEngine(
                judge=[verdict_answer(reason="remote allowed")],
                scorer=[score_answer(72, ("skills match",))],
            ),
            "juror-b": JurorEngine(
                judge=[verdict_answer(keep=False, rule=1, reason="office")],
                scorer=[score_answer(40, ("field differs",))],
            ),
        }
    )
    assert world.run("--limit", "10", "--yes").exit_code == 0


def _agree_jury(world: _World) -> None:
    """Have the jurors agree on every case (keep, score 70), then label ten."""
    assert world.run("--limit", "10", "--yes").exit_code == 0


def _same_answers(items: tuple[ReviewItem, ...]) -> list[str]:
    """Give, for each spot check, the answer that matches the agreed label."""
    answers = []
    for item in items:
        assert item.agreed is not None
        answers.append("k" if item.task is EvalTask.JUDGE else f"{item.agreed.score}")
    return answers


type _Summary = set[tuple[str, bool | None, int | None, int | None, str]]


def _summary(labels: list[EvalLabel]) -> _Summary:
    return {
        (label.task, label.keep, label.rule, label.score, label.origin)
        for label in labels
    }


def _first_title(output: str) -> str:
    match = re.search(r"Engineer \d+", output)
    assert match is not None, output
    return match.group()


@pytest.mark.usefixtures("on_a_terminal")
def test_review_judge_keys(world: _World) -> None:
    _split_jury(world)

    result = world.review("k\n50\nr\nq\n")

    assert result.exit_code == 0, result.output
    assert "Review 1 of 20 · judge" in result.stdout
    assert "Review 3 of 20 · judge" in result.stdout
    assert "rule 1: office" in result.stdout
    assert "Your rule 1: no crypto" in result.stdout
    assert _summary(world.labels()) == {
        ("judge", True, None, None, "user"),
        ("scorer", None, None, 50, "user"),
        ("judge", False, 1, None, "user"),
    }


@pytest.mark.usefixtures("on_a_terminal")
def test_review_score_input(world: _World) -> None:
    _split_jury(world)

    result = world.review("k\n65\nk\n\nq\n")

    assert result.exit_code == 0, result.output
    assert "Review 2 of 20 · scorer · spread 32" in result.stdout
    assert "Enter takes 40" in result.stdout
    scores = sorted(label.score for label in world.labels() if label.score is not None)
    assert scores == [40, 65]


@pytest.mark.usefixtures("on_a_terminal")
def test_review_invalid_input_asks_again(world: _World) -> None:
    _split_jury(world)

    result = world.review("101\nabc\nk\n101\nabc\n-1\n 7 \nq\n")

    assert result.exit_code == 0, result.output
    assert result.stdout.count("Your call") == 4
    assert result.stdout.count("Your score") == 4
    assert _summary(world.labels()) == {
        ("judge", True, None, None, "user"),
        ("scorer", None, None, 7, "user"),
    }


@pytest.mark.usefixtures("on_a_terminal")
def test_review_enter_on_a_judge_case_asks_again(world: _World) -> None:
    _split_jury(world)

    result = world.review("\n\nq\n")

    assert result.exit_code == 0, result.output
    assert result.stdout.count("Your call") == 3
    assert world.labels() == []


@pytest.mark.usefixtures("on_a_terminal")
def test_review_keys_are_per_task(world: _World) -> None:
    _split_jury(world)

    result = world.review("40\nk\nk\n40\nq\n")

    assert result.exit_code == 0, result.output
    assert result.stdout.count("Your call") == 3
    assert result.stdout.count("Your score") == 2
    assert len(world.labels()) == 2


@pytest.mark.usefixtures("on_a_terminal")
def test_review_skip_stays_unlabelled(world: _World) -> None:
    _split_jury(world)

    first = world.review("s\nq\n")
    second = world.review("q\n")

    assert first.exit_code == 0, first.output
    assert world.labels() == []
    assert "Review 1 of 20" in second.stdout
    assert _first_title(second.stdout) == _first_title(first.stdout)


@pytest.mark.usefixtures("on_a_terminal")
def test_review_quit_keeps_answers(world: _World) -> None:
    _split_jury(world)
    calls = world.calls()

    quit_ = world.review("k\nq\n")
    ended = world.review("50\n")

    assert quit_.exit_code == 0, quit_.output
    assert ended.exit_code == 0, ended.output
    assert len(world.labels()) == 2
    assert world.calls() == calls


@pytest.mark.usefixtures("on_a_terminal")
def test_review_open_shows_the_posting(world: _World) -> None:
    _split_jury(world)

    result = world.review("o\nq\n")

    assert result.exit_code == 0, result.output
    assert "POSTING" in result.stdout
    assert "Build things" in result.stdout
    assert "https://example.com/jobs/" in result.stdout
    assert result.stdout.count("Your call") == 2
    assert world.labels() == []


def test_review_in_a_pipe_exits_1(world: _World) -> None:
    _split_jury(world)

    result = world.review("k\n")

    assert result.exit_code == 1
    assert "needs a terminal" in result.stderr
    assert world.labels() == []


@pytest.mark.usefixtures("on_a_terminal")
def test_review_nothing_to_do_exits_0(world: _World) -> None:
    before = world.review("")
    _agree_jury(world)
    answered = world.review("\n".join(_same_answers(world.items())) + "\n")
    after = world.review("")

    assert before.exit_code == 0, before.output
    assert "Nothing to review" in before.stdout
    assert answered.exit_code == 0, answered.output
    assert after.exit_code == 0, after.output
    assert "Nothing to review" in after.stdout
    assert "Your call" not in after.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_review_skips_changed_and_removed_postings(world: _World) -> None:
    _split_jury(world)
    with Store.open(world.home.db) as store:
        for case in store.evals.cases(store.evals.sets()[0].id)[:2]:
            store.set_description(case.posting_id, "rewritten after the draw")

    result = world.review("q\n")

    assert result.exit_code == 0, result.output
    assert "Review 1 of 16" in result.stdout
    assert "2 postings changed or gone since the draw" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_review_counts_skipped_postings_when_nothing_is_left(world: _World) -> None:
    _split_jury(world)
    with Store.open(world.home.db) as store:
        for case in store.evals.cases(store.evals.sets()[0].id):
            store.set_description(case.posting_id, "rewritten after the draw")

    result = world.review("")

    assert result.exit_code == 0, result.output
    assert "10 postings changed or gone since the draw" in result.stdout
    assert "Nothing to review" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_review_missing_vote_is_offered(world: _World) -> None:
    world.engines = JurorEngines(
        {"juror-b": JurorEngine(raises=ProviderFailedError("boom"))}
    )
    assert world.run("--limit", "10", "--yes").exit_code == 1

    result = world.review("q\n")

    assert result.exit_code == 0, result.output
    assert "Review 1 of 20 · judge · 1 of 2 votes" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_spot_check_stops_at_ten(world: _World) -> None:
    _agree_jury(world)

    first = world.review("q\n")
    result = world.review("\n".join(_same_answers(world.items())) + "\n")

    assert "Review 1 of 10 ·" in first.stdout
    assert "· spot check" in first.stdout
    assert result.exit_code == 0, result.output
    labels = world.labels()
    assert sum(label.spot_checked for label in labels) == 10
    assert {label.origin for label in labels} == {LabelOrigin.AGREED}
    assert world.items() == ()


@pytest.mark.usefixtures("on_a_terminal")
def test_spot_check_resumes_after_quit(world: _World) -> None:
    _agree_jury(world)
    answers = _same_answers(world.items())

    world.review("\n".join(answers[:3]) + "\nq\n")
    result = world.review("q\n")

    assert "Review 1 of 7 ·" in result.stdout
    assert sum(label.spot_checked for label in world.labels()) == 3


@pytest.mark.usefixtures("on_a_terminal")
def test_spot_check_different_answer_is_user_origin(world: _World) -> None:
    _agree_jury(world)
    (item, *_) = world.items()
    different = "r" if item.task is EvalTask.JUDGE else "10"

    result = world.review(f"{different}\nq\n")

    assert result.exit_code == 0, result.output
    (changed,) = [label for label in world.labels() if label.spot_checked]
    assert changed.origin is LabelOrigin.USER
    assert changed.posting_id == item.posting.id
    assert changed.task is item.task


@pytest.mark.usefixtures("on_a_terminal")
def test_spot_check_same_answer_stays_agreed(world: _World) -> None:
    _agree_jury(world)
    (item, *_) = world.items()

    world.review(f"{_same_answers((item,))[0]}\nq\n")

    (checked,) = [label for label in world.labels() if label.spot_checked]
    assert checked.origin is LabelOrigin.AGREED
    assert checked.posting_id == item.posting.id


@pytest.mark.usefixtures("on_a_terminal")
def test_review_without_a_set_exits_1(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    no_jury = _World(tmp_path / "a", client_factory_for, jury=None)
    no_rules = _World(tmp_path / "b", client_factory_for, ready=False)
    with Store.open(no_rules.home.db) as store:
        _seed(store)
    no_profile = _World(tmp_path / "c", client_factory_for, ready=False)
    with Store.open(no_profile.home.db) as store:
        _seed(store)
        _store_rules(store)

    jury = no_jury.review()
    rules = no_rules.review()
    profile = no_profile.review()

    assert jury.exit_code == 1
    assert "eval.jury" in jury.stdout + jury.stderr
    assert rules.exit_code == 1
    assert "ijob instructions set" in rules.stdout + rules.stderr
    assert profile.exit_code == 1
    assert "ijob cv profile" in profile.stdout + profile.stderr


def test_review_help_lists_its_flags(world: _World) -> None:
    result = CliRunner().invoke(
        world.app(interactive=False),
        ["eval", "review", "--help"],
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
    )

    assert result.exit_code == 0
    for flag in ("--db", "--config"):
        # Typer forces styled help under GITHUB_ACTIONS, which splits a flag's text.
        assert flag in _ANSI.sub("", result.stdout)


def _report(world: _World, *args: str) -> Result:
    return CliRunner().invoke(
        world.app(interactive=False),
        ["eval", "report", *args],
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
    )


def _identify_set(world: _World) -> int:
    """Store the label set that the world's jury, rules and profile identify."""
    with Store.open(world.home.db) as store:
        instructions = store.instructions()
        profile = store.cv_profile()
        assert instructions is not None
        assert profile is not None
        return store.evals.find_or_create_set(
            instructions.rules_sha256,
            profile.profile.sha256(),
            (_A, _B),
            created_at=_AT,
        ).id


def _case(
    key: str, *, passed: bool, answer: str = "{}", detail: str = "", ms: int = 5
) -> EvalResult:
    """Build a case result for `_add_run`, which gives it the run's number."""
    return EvalResult(0, key, passed, answer, detail, ms)


def _add_run(  # noqa: PLR0913  # one knob of a stored run per argument
    world: _World,
    suite: str = "judge",
    *,
    set_id: int | None = None,
    metrics: dict[str, float] | None = None,
    status: RunStatus = RunStatus.COMPLETE,
    spent: float = 0.0,
    model: str = "claude-sdk/haiku",
    results: tuple[EvalResult, ...] = (),
) -> int:
    with Store.open(world.home.db) as store:
        records = store.evals
        run = records.start_run(suite, model, f"{suite}-1", set_id, started_at=_AT)
        records.record_spend(run.id, spent_usd=spent, subscription_usd=0.0)
        for result in results:
            records.put_result(
                EvalResult(
                    run.id,
                    result.case_key,
                    result.passed,
                    result.answer,
                    result.detail,
                    result.latency_ms,
                )
            )
        records.finish_run(run.id, status, json.dumps(metrics or {}), finished_at=_AT)
        return run.id


@pytest.mark.usefixtures("on_a_terminal")
def test_report_empty_store_exits_0(world: _World) -> None:
    result = _report(world)

    assert result.exit_code == 0, result.output
    assert "No eval run yet" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_report_run_detail(world: _World) -> None:
    set_id = _identify_set(world)
    run_id = _add_run(
        world,
        set_id=set_id,
        status=RunStatus.INCOMPLETE,
        metrics={"keep_recall": 0.9, "margin": 0.09, "unusable": 3},
        spent=0.1,
        results=(
            _case("c1", passed=True, ms=6400),
            _case("c2", passed=True, ms=11200),
        ),
    )

    result = _report(world, str(run_id))

    assert result.exit_code == 0, result.output
    assert f"run {run_id}" in result.stdout
    assert "incomplete" in result.stdout
    assert "Keep recall" in result.stdout
    assert "0.90" in result.stdout
    assert "±0.09" in result.stdout
    assert "$0.10" in result.stdout
    assert "8.8s" in result.stdout
    assert "p95 11.2s" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_report_shows_a_table_per_suite_with_jury_and_spend(world: _World) -> None:
    set_id = _identify_set(world)
    _add_run(world, set_id=set_id, metrics={"keep_recall": 0.9}, spent=3.0)
    _add_run(world, set_id=set_id, metrics={"keep_recall": 0.5}, spent=0.6)
    _add_run(world, set_id=set_id, status=RunStatus.INCOMPLETE)
    _add_run(world, "hostile")

    result = _report(world)

    assert result.exit_code == 0, result.output
    assert "Judge runs · labels v1 · judge-1" in result.stdout
    assert "Hostile runs" in result.stdout
    assert "Jury: agreement - · overruled 0 · spot check -" in result.stdout
    assert "OpenRouter spent $3.60 of $10.00 · 1 other run not compared" in (
        result.stdout
    )


@pytest.mark.usefixtures("on_a_terminal")
def test_report_shows_the_jury_line_from_votes(world: _World) -> None:
    set_id = _identify_set(world)
    _add_run(world, set_id=set_id, metrics={"keep_recall": 0.9})
    with Store.open(world.home.db) as store:
        posting = store.postings(sources=[_SOURCE])[0].id
        for juror, keep in ((_A, True), (_B, True)):
            store.evals.put_vote(
                EvalVote(
                    set_id,
                    posting,
                    EvalTask.JUDGE,
                    juror,
                    "t-1",
                    keep,
                    None,
                    None,
                    (),
                    _AT,
                )
            )

    result = _report(world)

    assert "Jury: agreement 1.00 · overruled 0 · spot check -" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_report_without_a_set_lists_label_free_suites(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    bare = _World(tmp_path, client_factory_for, jury=None, ready=False)
    _add_run(bare, "hostile")
    _add_run(bare, "source-agent")
    _add_run(bare, "judge")

    result = _report(bare)

    assert result.exit_code == 0, result.output
    assert "Hostile runs" in result.stdout
    assert "Source agent runs" in result.stdout
    assert "Judge runs" not in result.stdout
    assert "Labels need setup" in result.stdout
    assert "Jury:" not in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_report_unknown_run_exits_2(world: _World) -> None:
    result = _report(world, "42")

    assert result.exit_code == 2
    assert "42" in result.stdout + result.stderr


@pytest.mark.usefixtures("on_a_terminal")
def test_report_failures_needs_a_run(world: _World) -> None:
    assert _report(world, "--failures").exit_code == 2


@pytest.mark.usefixtures("on_a_terminal")
def test_report_failures_lists_answers(world: _World) -> None:
    hostile = "[bold red]gone[/] \x1b]0;pwned\x07"
    run_id = _add_run(
        world,
        "hostile",
        results=(
            _case(
                "case-1",
                passed=False,
                answer=hostile,
                detail="[link=https://e.test]d[/link]",
            ),
            _case("case-2", passed=True, answer="fine"),
        ),
    )

    result = _report(world, str(run_id), "--failures")

    assert result.exit_code == 0, result.output
    assert "case-1" in result.stdout
    assert "case-2" not in result.stdout
    assert "[bold red]gone[/]" in result.stdout
    assert "[link=https://e.test]d[/link]" in result.stdout
    assert "\x1b]0;" not in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_report_with_no_failure_says_so(world: _World) -> None:
    run_id = _add_run(world, "hostile", results=(_case("c", passed=True),))

    result = _report(world, str(run_id), "--failures")

    assert result.exit_code == 0
    assert "No failed case" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_report_shows_the_noise_note(world: _World) -> None:
    set_id = _identify_set(world)
    _add_run(world, set_id=set_id, metrics={"keep_recall": 0.90, "margin": 0.09})
    _add_run(world, set_id=set_id, metrics={"keep_recall": 0.86, "margin": 0.09})

    result = _report(world)

    assert "Gaps under ±0.09 may be noise." in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_report_shows_total_spend(world: _World) -> None:
    _add_run(world, "hostile", spent=1.25)
    _add_run(world, "hostile", spent=2.35)

    result = _report(world)

    assert "OpenRouter spent $3.60 of $10.00" in result.stdout


def test_report_pipe_prints_plain_lines(world: _World) -> None:
    set_id = _identify_set(world)
    run_id = _add_run(
        world,
        set_id=set_id,
        metrics={"keep_recall": 0.9, "margin": 0.09},
        spent=0.1,
        results=(_case("c1", passed=False, answer="[x]", detail="why", ms=6400),),
    )

    listed = _report(world)
    detail = _report(world, str(run_id))
    failures = _report(world, str(run_id), "--failures")

    for result in (listed, detail, failures):
        assert result.exit_code == 0, result.output
        assert not any(char in result.stdout for char in _BOX_CHARACTERS)
    assert re.search(rf"^run +{run_id} .*judge-1", listed.stdout, re.MULTILINE)
    assert re.search(r"^spent_usd +0\.1000$", listed.stdout, re.MULTILINE)
    assert re.search(r"^keep_recall +0\.9000$", detail.stdout, re.MULTILINE)
    assert re.search(r"^p50_s +6\.4$", detail.stdout, re.MULTILINE)
    assert re.search(r"^c1 +\[x\] +why$", failures.stdout, re.MULTILINE)


def test_report_help_lists_its_flags(world: _World) -> None:
    result = CliRunner().invoke(
        world.app(interactive=False),
        ["eval", "report", "--help"],
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
    )

    assert result.exit_code == 0
    for flag in ("--failures", "--db", "--config"):
        # Typer forces styled help under GITHUB_ACTIONS, which splits a flag's text.
        assert flag in _ANSI.sub("", result.stdout)


_UNDER_TEST = "claude-sdk/under-test"
_ROUTED_MODEL = "openrouter/made-up/juror-c"
_RUN_NOTICE = "Sends your rules, CV profile and postings to OpenRouter and made-up"


def _label_cases(world: _World, count: int = 12) -> None:
    """Store the label set of the world's jury with `count` labelled cases."""
    with Store.open(world.home.db) as store:
        instructions = store.instructions()
        profile = store.cv_profile()
        assert instructions is not None
        assert profile is not None
        eval_set = store.evals.find_or_create_set(
            instructions.rules_sha256,
            profile.profile.sha256(),
            (_A, _B),
            created_at=_AT,
        )
        postings = sorted(store.postings(sources=[_SOURCE]), key=lambda each: each.id)
        postings = postings[:count]
        third = max(1, count // 3)
        buckets = (Bucket.KEPT, Bucket.REJECTED, Bucket.UNJUDGED)
        store.evals.put_cases(
            eval_set.id,
            [
                EvalCase(
                    eval_set.id,
                    posting.id,
                    position,
                    buckets[min(position // third, 2)],
                    input_sha256(posting_block(posting)),
                )
                for position, posting in enumerate(postings)
            ],
        )
        for position, posting in enumerate(postings):
            keep = position % 2 == 0
            for task in EvalTask:
                store.evals.put_label(
                    EvalLabel(
                        set_id=eval_set.id,
                        posting_id=posting.id,
                        task=task,
                        keep=keep if task is EvalTask.JUDGE else None,
                        rule=None if keep or task is EvalTask.SCORER else 1,
                        score=position * 7 % 101 if task is EvalTask.SCORER else None,
                        origin=LabelOrigin.AGREED,
                        spot_checked=False,
                        decided_at=_AT,
                    )
                )


def _grader(world: _World, ref: str) -> None:
    """Name the `reasons` grader in the world's config."""
    with world.home.config.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[models]\neval_grader = "{ref}"\n')


def _routed_answers(monkeypatch: pytest.MonkeyPatch, suite: str, cases: int) -> None:
    """Serve the OpenRouter model's answers for `cases` cases of a suite."""
    text = verdict_answer() if suite == "judge" else score_answer(70)
    serve_openrouter(
        monkeypatch,
        [openrouter_reply(text, cost=_ROUTED_COST) for _ in range(cases)],
    )


@pytest.mark.usefixtures("on_a_terminal")
def test_run_judge_screen(world: _World) -> None:
    _label_cases(world)

    result = world.suite("judge", "--model", _UNDER_TEST)

    assert result.exit_code == 0, result.output
    assert "Judge · under-test · 12 cases · run 1" in result.stdout
    recall = _screen_row(result.stdout, "Keep recall")
    assert "1.00" in recall
    assert "±0." in recall
    assert "main" in recall
    for name in ("Keep precision", "Macro F1", "Right rule cited"):
        assert name in result.stdout
    assert "Unusable answers" in result.stdout
    assert "of 12" in result.stdout
    assert "Cost" in result.stdout
    assert "Latency" in result.stdout
    assert "p95" in result.stdout
    assert "ijob eval report 1 --failures" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_run_scorer_screen(world: _World) -> None:
    _label_cases(world)

    result = world.suite("scorer", "--model", _UNDER_TEST)

    assert result.exit_code == 0, result.output
    assert "Scorer · under-test · 12 cases · run 1" in result.stdout
    ndcg = _screen_row(result.stdout, "NDCG@10")
    assert "main" in ndcg
    for name in ("Spearman", "Mean score error", "Unusable answers", "Cost", "Latency"):
        assert name in result.stdout
    assert "ijob eval report 1 --failures" in result.stdout


@pytest.mark.parametrize("suite", ["judge", "scorer"])
def test_run_provider_stop_exits_1_cap_exits_0(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
    suite: str,
) -> None:
    stopped = _World(tmp_path / "stopped", client_factory_for)
    stopped.engines = JurorEngines(
        {"under-test": JurorEngine(raises=ProviderFailedError("boom"))}
    )
    capped = _World(tmp_path / "capped", client_factory_for, max_cost=0.1)
    _label_cases(stopped)
    _label_cases(capped)
    _routed_answers(monkeypatch, suite, 3)

    provider = stopped.suite(suite, "--model", _UNDER_TEST)
    cap = capped.suite(suite, "--model", _ROUTED_MODEL, "--yes")

    assert provider.exit_code == 1
    assert f"{_UNDER_TEST} stopped: boom" in provider.stderr
    assert cap.exit_code == 0, cap.output
    assert "cost cap" in cap.stderr
    with Store.open(capped.home.db) as store:
        assert [run.status for run in store.evals.runs()] == [RunStatus.INCOMPLETE]


def test_run_provider_text_is_literal_with_a_piped_stdout_and_a_terminal_stderr(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Console, "is_terminal", property(lambda _self: True))
    monkeypatch.setattr("ijob_app.cli_output._stderr_is_terminal", lambda: True)
    _label_cases(world)
    world.engines = JurorEngines(
        {
            "under-test": JurorEngine(
                raises=ProviderFailedError("boom; run: \x1b]0;pwned\x07rm -rf ~")
            )
        }
    )

    result = world.suite("judge", "--model", _UNDER_TEST)

    assert result.exit_code == 1
    assert f"{_UNDER_TEST} stopped: boom" in result.stderr
    assert "\x1b]0;pwned" not in result.stderr
    assert "\x07" not in result.stderr
    stop = next(line for line in result.stderr.splitlines() if "boom" in line)
    assert "fix" not in stop
    assert "run: ]0;pwnedrm -rf ~" in stop


@pytest.mark.parametrize("stopped_by", ["provider", "cost_unknown"])
def test_run_stop_text_is_literal_whichever_stop_came_first(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stopped_by: Literal["provider", "cost_unknown"],
) -> None:
    monkeypatch.setattr(Console, "is_terminal", property(lambda _self: True))
    monkeypatch.setattr("ijob_app.cli_output._stderr_is_terminal", lambda: True)
    report = SuiteReport(
        run_id=1,
        suite=Suite.JUDGE,
        model=ModelRef(Provider.CLAUDE_SDK, "under-test"),
        status=RunStatus.INCOMPLETE,
        operation=OperationReport(
            done=0, not_done=1, spent=0.0, cap=4.0, stopped_by=stopped_by
        ),
        metrics={},
        skipped=0,
        stop_error=ProviderFailedError("boom; run: \x1b]0;pwned\x07rm -rf ~"),
        subscription_spent=0.0,
    )

    _show_suite_result(_run_detail("judge", {}), report)

    stderr = capsys.readouterr().err
    assert "\x1b]0;pwned" not in stderr
    stop = next(line for line in stderr.splitlines() if "boom" in line)
    assert "fix" not in stop
    assert "run: ]0;pwnedrm -rf ~" in stop


def test_run_cost_unknown_exits_1(world: _World) -> None:
    _label_cases(world)
    world.engines = JurorEngines({"under-test": JurorEngine(cost=None)})

    result = world.suite("judge", "--model", _UNDER_TEST)

    assert result.exit_code == 1
    assert f"cost of {_UNDER_TEST} is unknown" in result.stderr
    assert world.engines.engine("under-test").calls == ["judge"]


def test_hostile_asks_but_names_no_data(world: _World) -> None:
    result = world.suite(
        "hostile", "--model", _ROUTED_MODEL, interactive=True, stdin="n\n"
    )

    assert result.exit_code == 1
    assert "Start the run? [y/N]" in result.stderr
    assert "Sends" not in result.output
    assert "OpenRouter" not in result.output
    assert world.prices_requested == 1
    assert world.runs() == 0


def test_run_pipe_prints_key_values(world: _World) -> None:
    _label_cases(world)

    result = world.suite("judge", "--model", _UNDER_TEST)

    assert result.exit_code == 0, result.output
    assert not any(char in result.stdout for char in _BOX_CHARACTERS)
    for name, value in (
        ("keep_recall", "1.00"),
        ("keep_precision", "[0-9.]+"),
        ("macro_f1", "-"),
        ("unusable", "0"),
        ("cost_usd", "[0-9.]+"),
    ):
        assert re.search(rf"^{name} +{value}$", result.stdout, re.MULTILINE), name
    assert "ijob eval report 1 --failures" in result.stderr


@pytest.mark.parametrize("model", [_ROUTED_MODEL, _UNDER_TEST])
def test_paid_run_asks_first(world: _World, model: str) -> None:
    _label_cases(world)
    if model == _UNDER_TEST:
        for suite in ("judge", "scorer"):
            assert world.suite(suite, "--model", _UNDER_TEST).exit_code == 0
        _grader(world, _ROUTED_MODEL)
        arguments = ("reasons", "--model", _UNDER_TEST)
    else:
        arguments = ("judge", "--model", model)
    calls = world.calls()

    result = world.suite(*arguments, interactive=True, stdin="n\n")

    assert result.exit_code == 1
    assert "Start the run? [y/N]" in result.stderr
    assert "Start the run?" not in result.stdout
    assert world.calls() == calls


@pytest.mark.usefixtures("on_a_terminal")
def test_paid_run_names_the_data_and_vendors(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _label_cases(world)
    _routed_answers(monkeypatch, "judge", 12)

    result = world.suite("judge", "--model", _ROUTED_MODEL, "--yes")

    assert result.exit_code == 0, result.output
    assert _RUN_NOTICE in result.stdout
    assert "Estimate" in result.stdout
    assert "cap $4.00" in result.stdout


def test_claude_only_run_does_not_ask(world: _World) -> None:
    _label_cases(world)

    result = world.suite("judge", "--model", _UNDER_TEST, interactive=True)

    assert result.exit_code == 0, result.output
    assert "Start" not in result.output
    assert "Sends" not in result.output
    assert world.prices_requested == 0


def test_paid_run_in_a_pipe_needs_yes(world: _World) -> None:
    _label_cases(world)

    result = world.suite("judge", "--model", _ROUTED_MODEL)

    assert result.exit_code == 1
    assert "confirmation needed; pass --yes" in result.stderr
    assert _RUN_NOTICE in result.stderr
    assert world.runs() == 0


def _hostile_world(world: _World) -> None:
    """Have the model flip its verdict on the first hostile pair only."""
    world.engines = JurorEngines(
        {
            "under-test": JurorEngine(
                judge=[verdict_answer(keep=False, rule=1), verdict_answer()]
            )
        }
    )


@pytest.mark.usefixtures("on_a_terminal")
def test_hostile_miss_exits_1(world: _World) -> None:
    _hostile_world(world)

    result = world.suite("hostile", "--model", _UNDER_TEST)

    assert result.exit_code == 1
    assert "Hostile · under-test · 10 pairs · run 1" in result.stdout
    assert "✗ changed" in result.stdout
    assert "1 of 10 attacks changed the answer" in result.stdout
    assert "target is 0" in result.stdout


def test_hostile_clean_run_exits_0(world: _World) -> None:
    result = world.suite("hostile", "--model", _UNDER_TEST)

    assert result.exit_code == 0, result.output
    assert re.search(r"^attacks +0$", result.stdout, re.MULTILINE)
    assert re.search(r"^answered +10$", result.stdout, re.MULTILINE)


def test_hostile_limit_exits_2(world: _World) -> None:
    result = world.suite("hostile", "--model", _UNDER_TEST, "--limit", "5")

    assert result.exit_code == 2
    assert "limit" in result.stderr
    assert world.calls() == 0
    assert world.runs() == 0


@pytest.mark.parametrize("suite", ["judge", "scorer", "reasons"])
@pytest.mark.parametrize("limit", ["0", "101"])
def test_limit_out_of_range_exits_2(world: _World, suite: str, limit: str) -> None:
    _label_cases(world)
    _grader(world, _ROUTED_MODEL)

    result = world.suite(suite, "--model", _UNDER_TEST, "--limit", limit)

    assert result.exit_code == 2
    assert "limit" in result.stderr
    assert world.calls() == 0
    assert world.runs() == 0


def test_unknown_suite_exits_2(world: _World) -> None:
    result = world.suite("bogus")

    assert result.exit_code == 2
    for name in ("judge", "scorer", "reasons", "hostile"):
        assert name in result.stderr
    assert world.runs() == 0


def test_model_not_in_price_list_exits_2(world: _World) -> None:
    _label_cases(world)

    result = world.suite("judge", "--model", "openrouter/made-up/missing", "--yes")

    assert result.exit_code == 2
    assert "made-up/missing" in result.stderr
    assert "price list" in result.stderr
    assert world.runs() == 0


def test_run_without_a_label_set_names_the_fix(world: _World) -> None:
    result = world.suite("judge", "--model", _UNDER_TEST)

    assert result.exit_code == 1
    assert "run: ijob eval label" in result.stderr
    assert world.calls() == 0


def test_run_help_lists_its_flags(world: _World) -> None:
    result = CliRunner().invoke(
        world.app(interactive=False),
        ["eval", "run", "--help"],
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
    )

    assert result.exit_code == 0
    for flag in ("--model", "--limit", "--yes", "--db", "--config"):
        # Typer forces styled help under GITHUB_ACTIONS, which splits a flag's text.
        assert flag in _ANSI.sub("", result.stdout)
    assert "judge" in result.stdout


class _InterruptedEngine(JurorEngine):
    """A juror engine that answers `answers` calls, then gets a Ctrl-C."""

    def __init__(self, answers: int) -> None:
        super().__init__()
        self._answers = answers

    @override
    async def complete(self, request: Opaque) -> Opaque:
        if len(self.calls) >= self._answers:
            raise KeyboardInterrupt
        return await super().complete(request)


@pytest.mark.usefixtures("on_a_terminal")
def test_label_ctrl_c_keeps_the_votes_and_exits_0(world: _World) -> None:
    world.engines = JurorEngines({"juror-a": _InterruptedEngine(answers=3)})

    result = world.run("--limit", "10", "--yes")

    assert result.exit_code == 0, result.output
    assert "Stopped" in result.stdout
    assert world.votes() > 0
    assert f"{world.votes()} votes saved" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
@pytest.mark.parametrize(("suite", "status"), [("judge", 0), ("hostile", 1)])
def test_run_ctrl_c_stores_the_run_incomplete(
    world: _World, suite: str, status: int
) -> None:
    _label_cases(world)
    world.engines = JurorEngines({"under-test": _InterruptedEngine(answers=3)})

    result = world.suite(suite, "--model", _UNDER_TEST)

    assert result.exit_code == status, result.output
    with Store.open(world.home.db) as store:
        runs = store.evals.runs()
        stored = len(store.evals.results(runs[0].id))
    assert "Stopped" in result.stdout
    assert stored > 0
    assert f"{stored} cases stored" in result.stdout
    assert [run.status for run in runs] == [RunStatus.INCOMPLETE]


# --- the agent suites ----------------------------------------------------------

_AGENT = "claude-sdk/made-up-agent"
_AGENT_GRADER = "claude-sdk/made-up-grader"
_ROUTED_AGENT = "openrouter/made-up/agent"
_AGENT_MODELS = (
    f'source_agent = "{_AGENT}"\n'
    f'explore_agent = "{_AGENT}"\n'
    f'eval_grader = "{_AGENT_GRADER}"\n'
)
_LIMIT_NOTE = "These cases are easy: most board names are simple company slugs."


class _AgentWorld(_World):
    """A home with no postings, agent models configured and scripted agents."""

    def __init__(
        self,
        root: Path,
        clients: ClientFactoryBuilder,
        agents: _ByModel,
        *,
        max_cost: float | None = None,
    ) -> None:
        super().__init__(root, clients, jury=None, max_cost=max_cost, ready=False)
        self.agents = agents
        with self.home.config.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[models]\n{_AGENT_MODELS}")

    @override
    def _handler(self, request: httpx.Request) -> httpx.Response:
        if f"{request.url.host}{request.url.path}" == _BOARD_API:
            return httpx.Response(
                200, content=_BOARD_JSON, headers={"Content-Type": "application/json"}
            )
        return super()._handler(request)

    @override
    def app(self, *, interactive: bool) -> typer.Typer:
        agents = self.agents

        def options(_home: object, config_path: Path) -> LlmOptions:
            return LlmOptions(config_path=config_path, engines=agents)

        return make_app(
            self._clients(self._handler),
            now=lambda: _AT,
            llm_options=options,
            is_interactive=lambda: interactive,
            resolver=_resolve_public,
        )

    def stored_runs(self) -> list[EvalRun]:
        with Store.open(self.home.db) as store:
            return store.evals.runs()


def _screen_row(stdout: str, label: str) -> str:
    """Give the first line of a screen that holds `label`."""
    return next(line for line in stdout.splitlines() if label in line)


async def _resolve_public(host: str, port: int) -> Sequence[str]:
    del host, port
    return ["93.184.216.34"]


def _source_agents(*, touching: Sequence[HostilePage] = ()) -> _ByModel:
    """Script the source agent to find nothing, and to touch some pages' targets."""
    pages = load_hostile_pages()
    scripts = _reading_scripts()
    scripts[source_cases(1)[0].company] = NOTHING
    scripts |= _page_scripts(pages, touching=touching)
    return _ByModel({"made-up-agent": _ScriptedEngine(scripts)})


def _explore_agents(*, grade: str = _GRADE) -> _ByModel:
    """Script the explore agent to find and propose one board, graded by `grade`."""
    texts = (*_FINDS_ACME, _PROPOSE_ACME, FINISH, _SUMMARY)
    return _ByModel(
        {
            "made-up-agent": FakeAsyncEngine(texts),
            "made-up-grader": FakeAsyncEngine([grade]),
        }
    )


@pytest.fixture
def agent_world(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> _AgentWorld:
    return _AgentWorld(tmp_path, client_factory_for, _source_agents())


@pytest.mark.parametrize("suite", ["source-agent", "explore-agent"])
def test_agent_suite_asks_but_names_no_data(
    agent_world: _AgentWorld, suite: str
) -> None:
    result = agent_world.suite(
        suite, "--model", _ROUTED_AGENT, interactive=True, stdin="n\n"
    )

    assert result.exit_code == 1
    assert "Start the run? [y/N]" in result.stderr
    assert "Sends" not in result.output
    assert "OpenRouter" not in result.output
    assert agent_world.prices_requested == 0
    assert agent_world.runs() == 0


@pytest.mark.usefixtures("on_a_terminal")
def test_agent_suite_paid_estimate_reads_up_to_the_cap(
    agent_world: _AgentWorld,
) -> None:
    result = agent_world.suite(
        "source-agent",
        "--model",
        _ROUTED_AGENT,
        "--limit",
        "2",
        interactive=True,
        stdin="n\n",
    )

    assert result.exit_code == 1
    assert "Source agent · 5 cases" in result.stdout
    assert f"{_ROUTED_AGENT}" in result.stdout
    assert "up to the cap" in result.stdout
    assert "Estimate up to the cap · cap $4.00" in result.stdout
    assert agent_world.prices_requested == 0


def test_agent_suite_paid_estimate_reads_up_to_the_cap_in_a_pipe(
    agent_world: _AgentWorld,
) -> None:
    result = agent_world.suite("source-agent", "--model", _ROUTED_AGENT, "--limit", "2")

    assert result.exit_code == 1
    assert re.search(r"^estimate +up to the cap$", result.stderr, re.MULTILINE)
    assert re.search(
        rf"^model +{_ROUTED_AGENT} +up to the cap$", result.stderr, re.MULTILINE
    )
    assert "confirmation needed; pass --yes" in result.stderr
    assert "Sends" not in result.stderr
    assert agent_world.prices_requested == 0
    assert agent_world.runs() == 0


@pytest.mark.usefixtures("on_a_terminal")
def test_claude_only_agent_run_does_not_ask(agent_world: _AgentWorld) -> None:
    result = agent_world.suite("source-agent", "--limit", "1", interactive=True)

    assert result.exit_code == 0, result.output
    assert "Start" not in result.output
    assert "subscription" in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_source_suite_screen(agent_world: _AgentWorld) -> None:
    result = agent_world.suite("source-agent", "--limit", "1")

    assert result.exit_code == 0, result.output
    assert "Source agent · made-up-agent · 1 company · run 1" in result.stdout
    for line in (
        "Found the right source",
        "Other source verified",
        "Nothing found",
        "Could not check",
        "Rule violations",
        "Steps",
        "Cost per company",
    ):
        assert line in result.stdout
    found = _screen_row(result.stdout, "Found the right")
    assert found.split()[-4:-1] == ["0.00", "±0.40", "main"]
    assert "target 0" in result.stdout
    assert "median" in result.stdout
    assert "of 1" in result.stdout
    assert _LIMIT_NOTE in result.stdout
    assert "Hostile" not in result.stdout
    [run] = agent_world.stored_runs()
    assert run.suite == "source-agent"
    assert run.limited is True
    assert run.status is RunStatus.COMPLETE


def test_source_suite_in_a_pipe_is_plain(agent_world: _AgentWorld) -> None:
    result = agent_world.suite("source-agent", "--limit", "1")

    assert result.exit_code == 0, result.output
    assert not any(char in result.stdout for char in _BOX_CHARACTERS)
    for name, value in (
        ("found_right", "0.00"),
        ("other", "0"),
        ("nothing", "1"),
        ("could_not_check", "0"),
        ("violations", "0"),
        ("steps_median", "[0-9]+"),
        ("cost_per_company", "[0-9.]+"),
        ("failed", "0"),
        ("cost_usd", "[0-9.]+"),
    ):
        assert re.search(rf"^{name} +{value}$", result.stdout, re.MULTILINE), name
    assert "ijob eval report 1 --failures" in result.stderr


@pytest.mark.usefixtures("on_a_terminal")
def test_explore_suite_screen(
    client_factory_for: ClientFactoryBuilder, tmp_path: Path
) -> None:
    world = _AgentWorld(tmp_path, client_factory_for, _explore_agents())

    result = world.suite("explore-agent", "--limit", "1")

    assert result.exit_code == 0, result.output
    assert "Explore agent · made-up-agent · 1 scenario · run 1" in result.stdout
    accepted = _screen_row(result.stdout, "Accepted proposals")
    assert accepted.split()[-4:-1] == ["1.00", "±0.40", "main"]
    for name in (
        "Refused proposals",
        "Instruction following",
        "Ungraded scenarios",
        "Rule violations",
        "Steps",
        "Cost per scenario",
    ):
        assert name in result.stdout
    following = _screen_row(result.stdout, "following")
    assert "0.80" in following
    assert "ijob eval report 1 --failures" in result.stdout
    assert _LIMIT_NOTE not in result.stdout
    [run] = world.stored_runs()
    assert run.suite == "explore-agent"
    assert run.status is RunStatus.COMPLETE


def test_explore_suite_in_a_pipe_is_plain(
    client_factory_for: ClientFactoryBuilder, tmp_path: Path
) -> None:
    world = _AgentWorld(tmp_path, client_factory_for, _explore_agents())

    result = world.suite("explore-agent", "--limit", "1")

    assert result.exit_code == 0, result.output
    assert not any(char in result.stdout for char in _BOX_CHARACTERS)
    for name, value in (
        ("accepted_rate", "1.00"),
        ("accepted", "1"),
        ("refused", "0"),
        ("following", "0.80"),
        ("violations", "0"),
        ("ungraded", "0"),
    ):
        assert re.search(rf"^{name} +{value}$", result.stdout, re.MULTILINE), name


def test_agent_suite_violation_exits_1(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    pages = load_hostile_pages()
    world = _AgentWorld(
        tmp_path, client_factory_for, _source_agents(touching=[pages[0]])
    )

    result = world.suite("source-agent", "--limit", "1")

    assert result.exit_code == 1
    assert re.search(
        rf"^violations +{len(pages[0].injected_targets)}$", result.stdout, re.MULTILINE
    )
    [run] = world.stored_runs()
    assert run.status is RunStatus.COMPLETE


@pytest.mark.usefixtures("on_a_terminal")
def test_agent_suite_violation_screen_marks_the_target(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _AgentWorld(
        tmp_path, client_factory_for, _source_agents(touching=load_hostile_pages()[:1])
    )

    result = world.suite("source-agent", "--limit", "1")

    assert result.exit_code == 1
    violations = _screen_row(result.stdout, "Rule violations")
    assert "target 0" in violations
    assert violations.split()[2] != "0"


def test_agent_suite_incomplete_without_a_stop_says_so_in_a_pipe(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    unread = _ByModel({"made-up-agent": _ScriptedEngine({})})
    world = _AgentWorld(tmp_path, client_factory_for, unread)

    result = world.suite("source-agent", "--limit", "1")

    assert result.exit_code == 1
    [run] = world.stored_runs()
    assert run.status is RunStatus.INCOMPLETE
    assert "the run is kept as incomplete" in result.stderr
    assert "ijob eval report 1 --failures" in result.stderr


@pytest.mark.parametrize("suite", ["source-agent", "explore-agent"])
def test_agent_suite_incomplete_exits_1(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
    suite: str,
) -> None:
    world = _AgentWorld(tmp_path, client_factory_for, _source_agents(), max_cost=0.05)
    serve_openrouter(
        monkeypatch, [openrouter_reply(FINISH, cost=_ROUTED_COST) for _ in range(5)]
    )

    result = world.suite(suite, "--model", _ROUTED_AGENT, "--yes")

    assert result.exit_code == 1, result.output
    assert "stopped at the cost cap" in result.stderr
    [run] = world.stored_runs()
    assert run.status is RunStatus.INCOMPLETE


@pytest.mark.parametrize(
    ("suite", "limit"),
    [
        ("source-agent", "0"),
        ("source-agent", "151"),
        ("explore-agent", "0"),
        ("explore-agent", "6"),
    ],
)
def test_agent_suite_limit_out_of_range_exits_2(
    agent_world: _AgentWorld, suite: str, limit: str
) -> None:
    result = agent_world.suite(suite, "--limit", limit)

    assert result.exit_code == 2
    assert "limit" in result.stderr
    assert agent_world.runs() == 0


@pytest.mark.parametrize(
    ("suite", "limit"), [("source-agent", "150"), ("explore-agent", "5")]
)
def test_agent_suite_limit_edges_pass_the_plan(
    agent_world: _AgentWorld, suite: str, limit: str
) -> None:
    result = agent_world.suite(suite, "--model", _ROUTED_AGENT, "--limit", limit)

    assert result.exit_code == 1  # it stops to ask, in a pipe
    assert "confirmation needed; pass --yes" in result.stderr


def test_agent_suite_without_a_model_names_the_key(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _AgentWorld(tmp_path, client_factory_for, _source_agents())
    world.home.config.write_text("", encoding="utf-8")

    result = world.suite("explore-agent")

    assert result.exit_code == 1
    assert "explore_agent" in result.stderr
    assert world.runs() == 0


def test_explore_suite_without_a_grader_names_the_key(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _AgentWorld(tmp_path, client_factory_for, _explore_agents())
    text = world.home.config.read_text(encoding="utf-8")
    world.home.config.write_text(
        text.replace(f'eval_grader = "{_AGENT_GRADER}"\n', ""), encoding="utf-8"
    )

    result = world.suite("explore-agent", "--limit", "1")

    assert result.exit_code == 1
    assert "eval_grader" in result.stderr
    assert world.runs() == 0


def test_explore_suite_refuses_the_grader_as_the_model(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _AgentWorld(tmp_path, client_factory_for, _explore_agents())

    result = world.suite("explore-agent", "--model", _AGENT_GRADER)

    assert result.exit_code == 2
    assert "grader" in result.stderr
    assert world.runs() == 0


class _InterruptedAgent(FakeAsyncEngine):
    """An agent engine that gets a Ctrl-C on its first call."""

    @override
    async def complete(self, request: Opaque) -> Opaque:
        raise KeyboardInterrupt


@pytest.mark.usefixtures("on_a_terminal")
def test_agent_suite_ctrl_c_stores_the_run_incomplete_and_exits_1(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _AgentWorld(
        tmp_path,
        client_factory_for,
        _ByModel({"made-up-agent": _InterruptedAgent()}),
    )

    result = world.suite("source-agent", "--limit", "1")

    assert result.exit_code == 1, result.output
    assert "Stopped" in result.stdout
    [run] = world.stored_runs()
    assert run.status is RunStatus.INCOMPLETE


@pytest.mark.usefixtures("on_a_terminal")
def test_agent_suites_appear_in_report(agent_world: _AgentWorld) -> None:
    _add_run(
        agent_world,
        "source-agent",
        metrics={"found_right": 0.84, "margin": 0.07, "nothing": 16, "violations": 0},
        model="claude-sdk/sonnet",
    )
    _add_run(
        agent_world,
        "explore-agent",
        metrics={"accepted_rate": 0.75, "violations": 0},
        model="claude-sdk/sonnet",
    )
    limited = agent_world.suite("source-agent", "--limit", "1")
    assert limited.exit_code == 0, limited.output

    result = _report(agent_world)

    assert result.exit_code == 0, result.output
    assert "Source agent runs · source-agent-1" in result.stdout
    assert "Explore agent runs · explore-agent-1" in result.stdout
    for header in ("RIGHT", "NONE", "VIOL", "ACCEPT"):
        assert header in result.stdout
    row = _screen_row(result.stdout, "0.84")
    assert "sonnet" in row
    assert "1 other run not compared" in result.stdout


def test_agent_suites_appear_in_the_report_pipe(agent_world: _AgentWorld) -> None:
    run_id = _add_run(
        agent_world,
        "source-agent",
        metrics={"found_right": 0.84, "nothing": 16, "violations": 0},
    )

    result = _report(agent_world)

    assert result.exit_code == 0, result.output
    assert re.search(
        rf"^run +{run_id}  source-agent  .*  source-agent-1  .*found_right=0\.8400",
        result.stdout,
        re.MULTILINE,
    )


def test_run_help_lists_the_agent_suites_and_their_limits(
    agent_world: _AgentWorld,
) -> None:
    result = CliRunner().invoke(
        agent_world.app(interactive=False),
        ["eval", "run", "--help"],
        env={"IJOB_HOME": str(agent_world.home.root), "COLUMNS": "200"},
    )

    assert result.exit_code == 0
    text = _ANSI.sub("", result.stdout)
    assert "source-agent" in text
    assert "explore-agent" in text
    assert "150" in text
    assert "1 to 5" in text
