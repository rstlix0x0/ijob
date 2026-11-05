import asyncio
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import override

import pytest
from ijob_ai.errors import (
    CostUnknownError,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ijob_ai.llm.reference import parse_model_ref
from ijob_ai.tasks.text import input_sha256, posting_block
from ijob_app.eval_suites import Suite
from ijob_app.tune_ops import full_run
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalTask,
    LabelOrigin,
    RunStatus,
)
from ijob_core.store import Store
from rich.console import Console
from typer.testing import CliRunner, Result

from .conftest import ClientFactoryBuilder
from .eval_fakes import JurorEngine, JurorEngines, serve_openrouter_by_kind
from .llm_fakes import Opaque, score_answer, verdict_answer
from .test_cli_eval import _A, _B, _ROUTED, _SOURCE, _more_postings, _World

_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_BASE = "claude-sdk/base"
_CAND = "claude-sdk/cand"
_ROUTED_COST = 0.01


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


def _label_everything(world: _World) -> None:
    """Label 100 postings for both tasks, half kept, as the user would."""
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
        for position, posting in enumerate(postings):
            store.evals.put_cases(
                eval_set.id,
                [
                    EvalCase(
                        eval_set.id,
                        posting.id,
                        position,
                        Bucket.KEPT,
                        input_sha256(posting_block(posting)),
                    )
                ],
            )
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
                        origin=LabelOrigin.USER,
                        spot_checked=False,
                        decided_at=_AT,
                    )
                )


def _tune_world(  # noqa: PLR0913  # one knob of the setup per argument
    tmp_path: Path,
    clients: ClientFactoryBuilder,
    *,
    candidates: tuple[str, ...] = (),
    baseline: str | None = _BASE,
    max_cost: float | None = None,
    total_cost: float | None = None,
) -> _World:
    world = _World(tmp_path, clients, max_cost=max_cost, total_cost=total_cost)
    _more_postings(world, 70)
    _label_everything(world)
    lines = ["", "[tune]"]
    lines.append("candidates = [" + ", ".join(f'"{ref}"' for ref in candidates) + "]")
    if baseline is not None:
        lines.append(f'baseline = "{baseline}"')
    with world.home.config.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return world


def _screen(
    world: _World, *args: str, interactive: bool = False, stdin: str | None = None
) -> Result:
    return CliRunner().invoke(
        world.app(interactive=interactive),
        ["tune", "screen", *args],
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
        input=stdin,
    )


def _invoke(world: _World, *args: str) -> Result:
    return CliRunner().invoke(
        world.app(interactive=False),
        list(args),
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
    )


def _split_rows(world: _World) -> int:
    with Store.open(world.home.db) as store:
        return sum(len(store.tunes.split(each.id)) for each in store.evals.sets())


def _routed(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_openrouter_by_kind(
        monkeypatch,
        judge=verdict_answer(),
        scorer=score_answer(70),
        cost=_ROUTED_COST,
    )


@pytest.mark.usefixtures("on_a_terminal")
def test_screen_asks_with_the_estimate(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for, candidates=(_ROUTED,))

    result = _screen(world, interactive=True, stdin="\n")

    assert result.exit_code == 1
    assert "Screen · 4 runs" in result.stdout
    assert _ROUTED in result.stdout
    assert "Estimate" in result.stdout
    assert "cap $4.00 per run" in result.stdout
    assert "$10.00 left of $10.00" in result.stdout
    assert "the screen was not started" in result.stderr
    assert world.runs() == 0
    assert _split_rows(world) == 0


def test_claude_only_screen_closed_stdin_freezes_nothing(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    result = _screen(world)

    assert result.exit_code == 1
    assert "confirmation needed; pass --yes" in result.stderr
    assert _split_rows(world) == 0
    assert world.runs() == 0
    assert world.calls() == 0


def test_nothing_missing_closed_stdin_freezes_nothing(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    assert _screen(world, "--yes").exit_code == 0
    with Store.open(world.home.db) as store, store._conn:
        store._conn.execute("DELETE FROM tune_splits")
    assert _split_rows(world) == 0

    result = _screen(world)

    assert result.exit_code == 1
    assert "confirmation needed; pass --yes" in result.stderr
    assert _split_rows(world) == 0


@pytest.mark.usefixtures("on_a_terminal")
def test_claude_only_screen_no_freezes_nothing(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    result = _screen(world, interactive=True, stdin="n\n")

    assert result.exit_code == 1
    assert "frozen for good" in result.stderr
    assert _split_rows(world) == 0
    assert world.runs() == 0


def test_claude_only_screen_yes_flag_freezes(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    result = _screen(world, "--yes")

    assert result.exit_code == 0, result.output
    assert _split_rows(world) == 100
    assert world.runs() == 2


def test_existing_split_on_claude_needs_no_question(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    assert _screen(world, "--yes").exit_code == 0
    _forget_run(world, Suite.SCORER, _BASE)

    result = _screen(world)

    assert result.exit_code == 0, result.output
    assert world.runs() == 2
    assert _split_rows(world) == 100


def test_screen_closed_stdin_refuses(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for, candidates=(_ROUTED,))

    result = _screen(world)

    assert result.exit_code == 1
    assert "confirmation needed; pass --yes" in result.stderr
    assert world.runs() == 0
    assert world.calls() == 0
    assert _split_rows(world) == 0


def test_screen_no_freezes_nothing(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for, candidates=(_ROUTED,))

    result = _screen(world, interactive=True, stdin="n\n")

    assert result.exit_code == 1
    assert "Start the screen? [y/N]" in result.stderr
    assert _split_rows(world) == 0
    assert world.runs() == 0


class _Watching(JurorEngine):
    """An engine that notes whether the split existed when its first call came."""

    def __init__(self, db: Path) -> None:
        super().__init__()
        self._db = db
        self.frozen_at_first_call: bool | None = None

    @override
    async def complete(self, request: Opaque) -> Opaque:
        if self.frozen_at_first_call is None:
            with closing(sqlite3.connect(self._db)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM tune_splits"
                ).fetchone()
            self.frozen_at_first_call = count[0] > 0
        return await super().complete(request)


def test_screen_yes_freezes_then_runs(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    watcher = _Watching(world.home.db)
    world.engines = JurorEngines({"base": watcher})

    result = _screen(world, "--yes")

    assert result.exit_code == 0, result.output
    assert watcher.frozen_at_first_call is True
    assert _split_rows(world) == 100


def test_screen_writes_full_runs(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tune_world(tmp_path, client_factory_for, candidates=(_ROUTED,))
    _routed(monkeypatch)

    result = _screen(world, "--yes")

    assert result.exit_code == 0, result.output
    assert world.runs() == 4
    with Store.open(world.home.db) as store:
        eval_set = store.evals.sets()[0]
        for ref in (_BASE, _ROUTED):
            for task in EvalTask:
                model = parse_model_ref(ref, "model")
                assert full_run(store, task, model, eval_set) is not None
        spent = [run.spent_usd for run in store.evals.runs()]
    assert sum(1 for each in spent if each > 0) == 2


def _forget_run(world: _World, suite: Suite, model: str) -> None:
    """Delete the run of `suite` on `model`, as if it had never been made."""
    with Store.open(world.home.db) as store, store._conn:
        row = store._conn.execute(
            "SELECT id FROM eval_runs WHERE suite = ? AND model = ?",
            (suite.value, model),
        ).fetchone()
        store._conn.execute("DELETE FROM eval_results WHERE run_id = ?", (row[0],))
        store._conn.execute("DELETE FROM eval_runs WHERE id = ?", (row[0],))


def test_screen_rerun_keeps_the_split_and_runs_only_what_is_missing(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    assert _screen(world, "--yes").exit_code == 0
    with Store.open(world.home.db) as store:
        rows = store.tunes.split(store.evals.sets()[0].id)
    _forget_run(world, Suite.SCORER, _BASE)
    calls = world.engines.engine("base").calls.count("scorer")

    result = _screen(world, "--yes")

    assert result.exit_code == 0, result.output
    assert world.runs() == 2
    assert world.engines.engine("base").calls.count("judge") == 100
    assert world.engines.engine("base").calls.count("scorer") == calls + 100
    with Store.open(world.home.db) as store:
        assert store.tunes.split(store.evals.sets()[0].id) == rows


def test_screen_plan_lines_show_the_cap_per_run_and_the_recorded_spending_left(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tune_world(tmp_path, client_factory_for, candidates=(_ROUTED,))
    _routed(monkeypatch)
    assert _screen(world, "--yes").exit_code == 0
    _forget_run(world, Suite.JUDGE, _ROUTED)
    with Store.open(world.home.db) as store:
        left = 10.0 - store.evals.total_spend()
    assert left < 10.0 - 0.005

    result = _screen(world, "--yes")

    assert result.exit_code == 0, result.output
    plan = {
        line.split()[0]: line.split()[1:]
        for line in result.stderr.splitlines()
        if line.startswith(("cap_usd_per_run", "left_usd"))
    }
    assert plan["cap_usd_per_run"] == ["4.00"]
    assert plan["left_usd"] == [f"{left:.2f}"]


def test_screen_with_nothing_missing_runs_nothing(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    assert _screen(world, "--yes").exit_code == 0
    calls = world.calls()

    result = _screen(world)

    assert result.exit_code == 0, result.output
    assert "every model already has a full run" in result.stderr
    assert world.calls() == calls
    assert world.runs() == 2


def test_screen_stops_on_spent_budget(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tune_world(
        tmp_path, client_factory_for, candidates=(_ROUTED,), total_cost=0.03
    )
    _routed(monkeypatch)

    result = _screen(world, "--yes")

    assert result.exit_code == 1
    assert "eval spending is used up" in result.stderr
    assert world.runs() == 1
    assert world.engines.engine("base").calls == []


def test_screen_stops_on_provider_unavailable(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    world.engines = JurorEngines(
        {"base": JurorEngine(raises=ProviderUnavailableError("cli is missing"))}
    )

    result = _screen(world, "--yes")

    assert result.exit_code == 1
    assert "cli is missing" in result.stderr
    assert world.runs() == 1
    assert world.engines.engine("base").calls.count("scorer") == 0
    assert "scorer claude-sdk/base not run" in result.stdout


def test_screen_stops_on_provider_failure(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    world.engines = JurorEngines(
        {"base": JurorEngine(raises=ProviderFailedError("boom"))}
    )

    result = _screen(world, "--yes")

    assert result.exit_code == 1
    assert "boom" in result.stderr
    assert world.runs() == 1
    assert world.engines.engine("base").calls.count("scorer") == 0


def test_screen_stops_when_a_cost_is_unknown(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    world.engines = JurorEngines({"base": JurorEngine(raises=CostUnknownError(_BASE))})

    result = _screen(world, "--yes")

    assert result.exit_code == 1
    assert f"cost of {_BASE} is unknown" in result.stderr
    assert world.runs() == 1
    assert world.engines.engine("base").calls.count("scorer") == 0
    assert "scorer claude-sdk/base not run" in result.stdout


def test_screen_stop_text_is_literal_with_a_piped_stdout_and_a_terminal_stderr(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Console, "is_terminal", property(lambda _self: True))
    monkeypatch.setattr("ijob_app.cli_output._stderr_is_terminal", lambda: True)
    world = _tune_world(tmp_path, client_factory_for)
    world.engines = JurorEngines(
        {"base": JurorEngine(raises=ProviderFailedError("boom; run: rm -rf x"))}
    )

    result = _screen(world, "--yes")

    assert result.exit_code == 1
    stop = next(line for line in result.stderr.splitlines() if "boom" in line)
    assert "fix" not in stop
    assert "run: rm -rf x" in stop


class _Interrupting(JurorEngine):
    """An engine that sends Ctrl-C when it gets its `at`th request."""

    def __init__(self, at: int) -> None:
        super().__init__()
        self._at = at

    @override
    async def complete(self, request: Opaque) -> Opaque:
        if len(self.calls) + 1 == self._at:
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0)
        return await super().complete(request)


def test_ctrl_c_keeps_the_running_pair_incomplete_and_runs_no_later_pair(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for, candidates=("claude-sdk/cand",))
    interrupting = _Interrupting(at=150)
    world.engines = JurorEngines({"cand": interrupting})

    result = _screen(world, "--yes")

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert "judge claude-sdk/cand run 1 complete" in lines
    assert "scorer claude-sdk/cand run 2 interrupted, kept incomplete" in lines
    assert "judge claude-sdk/base not run" in lines
    assert "scorer claude-sdk/base not run" in lines
    assert "note: stopped: interrupted" in result.stderr
    assert world.engines.engine("base").calls == []
    with Store.open(world.home.db) as store:
        runs = [(run.suite, run.model, run.status) for run in store.evals.runs()]
    assert runs == [
        ("judge", "claude-sdk/cand", RunStatus.COMPLETE),
        ("scorer", "claude-sdk/cand", RunStatus.INCOMPLETE),
    ]
    assert _split_rows(world) == 100


@pytest.mark.usefixtures("on_a_terminal")
def test_screen_exits_1_on_an_incomplete_run(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tune_world(
        tmp_path, client_factory_for, candidates=(_ROUTED,), max_cost=0.05
    )
    _routed(monkeypatch)

    result = _screen(world, "--yes")

    assert result.exit_code == 1
    assert "incomplete" in result.stdout
    with Store.open(world.home.db) as store:
        statuses = [run.status for run in store.evals.runs()]
    assert RunStatus.INCOMPLETE in statuses
    assert RunStatus.COMPLETE in statuses


def test_screen_without_a_baseline_exits_2(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for, baseline=None)

    result = _screen(world, "--yes")

    assert result.exit_code == 2
    assert "tune.baseline" in result.stderr
    assert world.runs() == 0


def test_screen_help_lists_its_flags(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    result = _invoke(world, "tune", "screen", "--help")

    assert result.exit_code == 0
    for flag in ("--yes", "--db", "--config"):
        assert flag in result.stdout


def test_eval_commands_write_no_split(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    report = _invoke(world, "eval", "report")
    label = _invoke(world, "eval", "label", "--limit", "10", "--yes")

    assert report.exit_code == 0, report.output
    assert label.exit_code in (0, 1), label.output
    assert _split_rows(world) == 0


def test_cli_import_leaves_tuning_unloaded() -> None:
    probe = (
        "import sys;"
        "import ijob_app.cli;"
        "loaded = [m for m in ('dspy', 'optuna') if m in sys.modules];"
        "assert not loaded, loaded"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
