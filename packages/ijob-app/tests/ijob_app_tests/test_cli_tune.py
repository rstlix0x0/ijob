import asyncio
import re
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
from ijob_ai.tasks.versions import JUDGE_UNTRUSTED, SCORER_UNTRUSTED
from ijob_app.eval_suites import Suite
from ijob_app.tune_ops import full_run
from ijob_app.tune_report import COST_RATE_NOTE, COST_SOURCE, MARGIN_NOTE
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalTask,
    LabelOrigin,
    RunStatus,
)
from ijob_core.store import Store
from ijob_core.tune_records import SplitPart
from rich.console import Console
from typer.testing import CliRunner, Result

from .conftest import ClientFactoryBuilder
from .eval_fakes import JurorEngine, JurorEngines, serve_openrouter_by_kind
from .llm_fakes import Opaque, score_answer, verdict_answer
from .test_cli_eval import _A, _B, _ROUTED, _SOURCE, _more_postings, _World
from .test_tune_grid import _script, _Scripted

_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_BASE = "claude-sdk/base"
_CAND = "claude-sdk/cand"
_TUNER = "claude-sdk/tuner"
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
    models: tuple[str, ...] = (),
    max_cost: float | None = None,
    total_cost: float | None = None,
    run_cost: float | None = None,
) -> _World:
    world = _World(
        tmp_path,
        clients,
        max_cost=max_cost,
        total_cost=total_cost,
        tuner=_TUNER,
    )
    _more_postings(world, 70)
    _label_everything(world)
    lines = ["", "[tune]"]
    lines.append("candidates = [" + ", ".join(f'"{ref}"' for ref in candidates) + "]")
    lines.append("models = [" + ", ".join(f'"{ref}"' for ref in models) + "]")
    if run_cost is not None:
        lines.append(f"max_cost_usd = {run_cost}")
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


# --- tune run and tune list -------------------------------------------------

_REWRITE = "Decide the posting with care."


def _run(
    world: _World, *args: str, interactive: bool = False, stdin: str | None = None
) -> Result:
    return CliRunner().invoke(
        world.app(interactive=interactive),
        ["tune", "run", *args],
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
        input=stdin,
    )


def _listed(world: _World) -> Result:
    return _invoke(world, "tune", "list")


def _ready_world(  # noqa: PLR0913
    tmp_path: Path,
    clients: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
    *,
    models: tuple[str, ...] = (_CAND,),
    candidates: tuple[str, ...] = (_CAND,),
    run_cost: float | None = None,
) -> _World:
    """A world whose split is frozen and whose models have full runs."""
    world = _tune_world(
        tmp_path,
        clients,
        candidates=candidates,
        models=models,
        run_cost=run_cost,
    )
    if _ROUTED in candidates:
        _routed(monkeypatch)
    assert _screen(world, "--yes").exit_code == 0
    return world


def _tune_rows(world: _World) -> list[tuple[str, RunStatus]]:
    with Store.open(world.home.db) as store:
        return [
            (run.model, run.status) for run in store.evals.runs() if run.suite == "tune"
        ]


def _program_count(world: _World) -> int:
    with Store.open(world.home.db) as store:
        return sum(len(store.tunes.programs(task)) for task in EvalTask)


@pytest.mark.usefixtures("on_a_terminal")
def test_run_asks_with_the_estimate(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _ready_world(
        tmp_path,
        client_factory_for,
        monkeypatch,
        models=(_ROUTED,),
        candidates=(_ROUTED,),
    )
    runs = world.runs()
    _script(monkeypatch, _Scripted(2, rewrite=_REWRITE))

    declined = _run(world, "--optimizer", "bootstrap", interactive=True, stdin="\n")
    closed = _run(world, "--optimizer", "bootstrap")

    assert declined.exit_code == 1
    assert "Tune · 2 runs" in declined.stdout
    assert _ROUTED in declined.stdout
    assert "Estimate" in declined.stdout
    assert "cap $4.00 per run" in declined.stdout
    assert "[tune].max_cost_usd" in declined.stdout
    assert "Sends your rules, CV profile and postings to OpenRouter" in declined.stdout
    assert "the run was not started" in declined.stderr
    assert closed.exit_code == 1
    assert "confirmation needed; pass --yes" in closed.stderr
    assert world.runs() == runs
    assert _tune_rows(world) == []


def test_run_and_list_write_no_split(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tune_world(tmp_path, client_factory_for, models=(_CAND,))

    refused = _run(world, "--yes")
    listed = _listed(world)

    assert refused.exit_code == 1
    assert "tune screen" in refused.stderr
    assert listed.exit_code == 0, listed.output
    assert _split_rows(world) == 0
    assert world.runs() == 0

    ready = _ready_world(tmp_path / "again", client_factory_for, monkeypatch)
    frozen = _split_rows(ready)
    _script(monkeypatch, _Scripted(2, rewrite=_REWRITE))
    assert _run(ready, "--yes", "--optimizer", "bootstrap").exit_code == 0
    assert _listed(ready).exit_code == 0
    assert _split_rows(ready) == frozen == 100


def test_run_without_models_says_to_read_the_report(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    result = _run(world, "--yes")

    assert result.exit_code == 1
    assert "tune report" in result.stderr
    assert "tune.models" in result.stderr


def test_run_tunes_tests_and_shows_one_table(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _ready_world(tmp_path, client_factory_for, monkeypatch)
    _script(monkeypatch, _Scripted(2, rewrite=_REWRITE))

    result = _run(world, "--yes", "--optimizer", "bootstrap")

    assert result.exit_code == 0, result.output
    assert [line.split()[:4] for line in result.stdout.splitlines()] == [
        ["judge", _CAND, "bootstrap", "tuned"],
        ["scorer", _CAND, "bootstrap", "tuned"],
    ]
    assert [status for _, status in _tune_rows(world)] == [RunStatus.COMPLETE] * 2
    assert _program_count(world) == 2


def test_progress_goes_to_stderr(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _ready_world(tmp_path, client_factory_for, monkeypatch)
    _script(monkeypatch, _Scripted(2, rewrite=_REWRITE))

    result = _run(world, "--yes", "--optimizer", "bootstrap", "--task", "judge")

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1
    assert result.stdout.split()[0] == "judge"
    assert f"judge bootstrap {_CAND}" in result.stderr
    assert "estimate" in result.stderr
    assert "calls" not in result.stdout


def test_an_incomplete_run_retries_next_time(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _ready_world(tmp_path, client_factory_for, monkeypatch)
    only = ("--yes", "--optimizer", "bootstrap", "--task", "judge")
    _script(monkeypatch, _Scripted(2, raises=RuntimeError("it broke")))

    first = _run(world, *only)
    _script(monkeypatch, _Scripted(2, rewrite=_REWRITE))
    second = _run(world, *only)
    third = _run(world, *only)

    assert first.exit_code == 1
    assert "incomplete" in first.stdout
    assert "compile raised" in first.stdout
    assert second.exit_code == 0, second.output
    assert [status for _, status in _tune_rows(world)] == [
        RunStatus.INCOMPLETE,
        RunStatus.COMPLETE,
    ]
    assert third.exit_code == 0
    assert "every combination is already done" in third.stderr
    assert len(_tune_rows(world)) == 2


def test_again_runs_a_done_combination_again(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _ready_world(tmp_path, client_factory_for, monkeypatch)
    only = ("--yes", "--optimizer", "bootstrap", "--task", "judge")
    _script(monkeypatch, _Scripted(2, rewrite=_REWRITE))
    assert _run(world, *only).exit_code == 0

    again = _run(world, *only, "--again")

    assert again.exit_code == 0, again.output
    assert len(_tune_rows(world)) == 2
    assert _program_count(world) == 1


def test_run_closes_a_stopped_grid_with_exit_1(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _ready_world(tmp_path, client_factory_for, monkeypatch)
    _script(monkeypatch, _Scripted(3, rewrite=_REWRITE))
    world.engines = JurorEngines(
        {"cand": JurorEngine(raises=ProviderUnavailableError("cli is missing"))}
    )

    result = _run(world, "--yes", "--optimizer", "bootstrap")

    assert result.exit_code == 1
    assert "cli is missing" in result.stderr
    assert "scorer" in result.stdout
    assert "not run" in result.stdout


def test_run_with_a_bad_option_exits_2(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for, models=(_CAND,))

    bad_model = _run(world, "--yes", "--model", "nonsense")
    bad_optimizer = _run(world, "--yes", "--optimizer", "nope")

    assert bad_model.exit_code == 2
    assert "--model" in bad_model.stderr
    assert bad_optimizer.exit_code == 2


def test_run_help_lists_its_flags(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    result = _invoke(world, "tune", "run", "--help")

    assert result.exit_code == 0
    for flag in ("--model", "--optimizer", "--task", "--again", "--yes", "--db"):
        assert flag in result.stdout


def _seed_programs(world: _World, *, tested: bool) -> None:
    """Save a tested and an untested judge program for the world's model."""
    with Store.open(world.home.db) as store:
        eval_set = store.evals.sets()[0]
        for number, version in enumerate(("judge-2+aaaaaaaa", "judge-2+bbbbbbbb")):
            program = store.tunes.save_program(
                task=EvalTask.JUDGE,
                model=_CAND,
                optimizer="gepa",
                base_version="judge-2",
                set_id=eval_set.id,
                bound_sha256=eval_set.rules_sha256,
                instructions=f"Tuned. {JUDGE_UNTRUSTED}",
                demos_json="[]",
                version=version,
                tune_run_id=None,
                created_at=_AT,
            )
            if tested and number == 0:
                run = store.evals.start_run(
                    "judge",
                    _CAND,
                    program.version,
                    eval_set.id,
                    started_at=_AT,
                    limited=True,
                )
                store.evals.finish_run(
                    run.id, RunStatus.COMPLETE, "{}", finished_at=_AT
                )


def test_list_shows_status(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _ready_world(tmp_path, client_factory_for, monkeypatch)
    _seed_programs(world, tested=True)

    result = _listed(world)

    assert result.exit_code == 0, result.output
    rows = [line.split() for line in result.stdout.splitlines()]
    assert rows == [
        ["1", "judge", _CAND, "gepa", "judge-2+aaaaaaaa", "tested"],
        ["2", "judge", _CAND, "gepa", "judge-2+bbbbbbbb", "untested"],
    ]


@pytest.mark.usefixtures("on_a_terminal")
def test_list_draws_a_box_on_a_terminal(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _ready_world(tmp_path, client_factory_for, monkeypatch)
    _seed_programs(world, tested=True)

    result = _listed(world)

    assert result.exit_code == 0, result.output
    assert "Programs · 2" in result.stdout
    assert "untested" in result.stdout


def test_list_writes_nothing(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tune_world(tmp_path, client_factory_for, models=(_CAND,))

    empty = _listed(world)

    assert empty.exit_code == 0, empty.output
    assert "no tuned programs yet" in empty.stderr + empty.stdout
    assert world.runs() == 0
    assert _split_rows(world) == 0
    ready = _ready_world(tmp_path / "again", client_factory_for, monkeypatch)
    _seed_programs(ready, tested=True)
    before = (ready.runs(), _split_rows(ready), _program_count(ready))

    assert _listed(ready).exit_code == 0

    assert (ready.runs(), _split_rows(ready), _program_count(ready)) == before


# --- tune report ------------------------------------------------------------


def _report(world: _World, *args: str) -> Result:
    return _invoke(world, "tune", "report", *args)


def _held_out_counts(world: _World) -> tuple[int, int]:
    """Count the held-out postings and the keeps among them, from the store."""
    with Store.open(world.home.db) as store:
        eval_set = store.evals.sets()[0]
        held = {
            row.posting_id
            for row in store.tunes.split(eval_set.id)
            if row.part is SplitPart.HELD_OUT
        }
        keeps = sum(
            1
            for label in store.evals.labels(eval_set.id, EvalTask.JUDGE)
            if label.posting_id in held and label.keep
        )
    return len(held), keeps


def _tuned_world(
    tmp_path: Path,
    clients: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> _World:
    """A world with a screened split and a tuned, tested program per task."""
    world = _ready_world(tmp_path, clients, monkeypatch)
    _script(monkeypatch, _Scripted(2, rewrite=_REWRITE))
    assert _run(world, "--yes", "--optimizer", "bootstrap").exit_code == 0
    return world


def test_report_writes_no_split(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    result = _report(world)

    assert result.exit_code == 0, result.output
    assert "tune screen" in result.stdout
    assert "Choosing" not in result.stdout
    assert "Held-out" not in result.stdout
    assert COST_SOURCE in result.stdout
    assert _split_rows(world) == 0
    assert world.runs() == 0
    assert world.engines.engine("base").calls == []


def test_cli_report_prints_both_sections(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tuned_world(tmp_path, client_factory_for, monkeypatch)
    runs, splits = world.runs(), _split_rows(world)
    postings, keeps = _held_out_counts(world)

    result = _report(world)

    assert result.exit_code == 0, result.output
    assert postings > 0
    assert f"Held-out ({postings} postings, {keeps} keeps)" in result.stdout
    assert "Choosing" in result.stdout
    assert "program 1 (bootstrap)" in result.stdout
    assert result.stdout.index("Choosing") < result.stdout.index(COST_RATE_NOTE)
    assert result.stdout.index("Held-out (") < result.stdout.index(MARGIN_NOTE)
    assert COST_SOURCE in result.stdout
    assert (world.runs(), _split_rows(world)) == (runs, splits)


@pytest.mark.usefixtures("on_a_terminal")
def test_report_draws_boxes_on_a_terminal(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tuned_world(tmp_path, client_factory_for, monkeypatch)
    postings, keeps = _held_out_counts(world)

    result = _report(world)

    assert result.exit_code == 0, result.output
    assert "Choosing (train and val only)" in result.stdout
    assert f"Held-out ({postings} postings, {keeps} keeps)" in result.stdout
    assert COST_SOURCE in " ".join(result.stdout.split())


def test_report_out_writes_the_same_tables_over_the_old_file(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tuned_world(tmp_path, client_factory_for, monkeypatch)
    out = tmp_path / "results.md"
    out.write_text("old text\n", encoding="utf-8")

    result = _report(world, "--out", str(out))

    assert result.exit_code == 0, result.output
    written = out.read_text(encoding="utf-8")
    assert written == result.stdout
    assert "old text" not in written
    assert re.search(
        r"^## Held-out \(\d+ postings, \d+ keeps\)$", written, re.MULTILINE
    )
    assert str(out) in result.stderr


def test_report_out_to_a_missing_folder_exits_1(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    out = tmp_path / "nowhere" / "results.md"

    result = _report(world, "--out", str(out))

    assert result.exit_code == 1
    assert str(out) in result.stderr
    assert not out.exists()


@pytest.mark.usefixtures("on_a_terminal")
def test_report_out_writes_the_file_on_a_terminal_too(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _tuned_world(tmp_path, client_factory_for, monkeypatch)
    out = tmp_path / "results.md"

    result = _report(world, "--out", str(out))

    assert result.exit_code == 0, result.output
    written = out.read_text(encoding="utf-8")
    assert re.search(
        r"^## Held-out \(\d+ postings, \d+ keeps\)$", written, re.MULTILINE
    )
    assert "##" not in result.stdout


def test_report_out_to_a_folder_exits_1(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    out = tmp_path / "results"
    out.mkdir()

    result = _report(world, "--out", str(out))

    assert result.exit_code == 1
    assert result.stderr.startswith(f"error: cannot write {out}: ")


def test_report_out_to_a_read_only_file_exits_1(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    out = tmp_path / "results.md"
    out.write_text("old\n", encoding="utf-8")
    out.chmod(0o444)

    result = _report(world, "--out", str(out))

    assert result.exit_code == 1
    assert result.stderr.startswith(f"error: cannot write {out}: ")
    assert out.read_text(encoding="utf-8") == "old\n"


def test_report_without_a_baseline_exits_2(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for, baseline=None)

    result = _report(world)

    assert result.exit_code == 2
    assert "tune.baseline" in result.stderr


# --- tune accept and tune clear ---------------------------------------------


def _set_models(world: _World, **models: str) -> None:
    """Name the model of each task under `[models]` in the world's config."""
    text = world.home.config.read_text(encoding="utf-8")
    named = "".join(f'{task} = "{ref}"\n' for task, ref in models.items())
    world.home.config.write_text(
        text.replace("[models]\n", f"[models]\n{named}", 1), encoding="utf-8"
    )


def _seed_tested(
    world: _World, task: EvalTask, model: str, *, tested: bool = True
) -> int:
    """Save a program of `task` for `model`, with a complete held-out run if asked."""
    with Store.open(world.home.db) as store:
        eval_set = store.evals.sets()[0]
        judge = task is EvalTask.JUDGE
        base = "judge-2" if judge else "scorer-1"
        program = store.tunes.save_program(
            task=task,
            model=model,
            optimizer="gepa",
            base_version=base,
            set_id=eval_set.id,
            bound_sha256=eval_set.rules_sha256 if judge else eval_set.profile_sha256,
            instructions=f"Tuned. {JUDGE_UNTRUSTED if judge else SCORER_UNTRUSTED}",
            demos_json="[]",
            version=f"{base}+0a1b2c3d",
            tune_run_id=None,
            created_at=_AT,
        )
        if tested:
            run = store.evals.start_run(
                task.value,
                model,
                program.version,
                eval_set.id,
                started_at=_AT,
                limited=True,
            )
            store.evals.finish_run(run.id, RunStatus.COMPLETE, "{}", finished_at=_AT)
        return program.id


def _accept(
    world: _World, *args: str, interactive: bool = False, stdin: str | None = None
) -> Result:
    return CliRunner().invoke(
        world.app(interactive=interactive),
        ["tune", "accept", *args],
        env={"IJOB_HOME": str(world.home.root), "COLUMNS": "120"},
        input=stdin,
    )


def _accepted(world: _World, task: EvalTask) -> int | None:
    with Store.open(world.home.db) as store:
        program = store.tunes.accepted(task)
    return None if program is None else program.id


def _hostile_runs(world: _World) -> list[tuple[str, bool, str]]:
    with Store.open(world.home.db) as store:
        return [
            (run.model, run.limited, run.task_version)
            for run in store.evals.runs()
            if run.suite == "hostile"
        ]


def _flipping() -> JurorEngines:
    """Engines whose `cand` flips its first judge verdict on the hostile posting."""
    return JurorEngines(
        {
            "cand": JurorEngine(
                judge=[verdict_answer(keep=False, rule=1), verdict_answer()]
            )
        }
    )


def test_accept_marks_a_program_after_ten_answers_in_five_pairs(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    _set_models(world, judge=_CAND)
    program = _seed_tested(world, EvalTask.JUDGE, _CAND)

    result = _accept(world, str(program))

    assert result.exit_code == 0, result.output
    assert world.engines.engine("cand").calls == ["judge"] * 10
    assert _accepted(world, EvalTask.JUDGE) == program
    assert _hostile_runs(world) == [(_CAND, True, "judge-2+0a1b2c3d")]
    assert re.search(r"^pairs +5$", result.stdout, re.MULTILINE)
    assert re.search(r"^attacks +0$", result.stdout, re.MULTILINE)
    assert re.search(r"^result +accepted$", result.stdout, re.MULTILINE)
    assert "may hold your rules" not in result.stderr + result.stdout


def test_accept_fails_on_an_attack_and_changes_nothing(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    _set_models(world, judge=_CAND)
    program = _seed_tested(world, EvalTask.JUDGE, _CAND)
    world.engines = _flipping()

    result = _accept(world, str(program))

    assert result.exit_code == 1
    assert "attacks 1" in result.stderr
    assert "unanswered 0" in result.stderr
    assert "run 2" in result.stderr  # run 1 is the program's held-out test
    assert "ijob eval report 2 --failures" in result.stderr
    assert re.search(r"^result +not accepted$", result.stdout, re.MULTILINE)
    assert _accepted(world, EvalTask.JUDGE) is None


def test_attacks_on_the_other_task_do_not_count(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    _set_models(world, judge=_CAND, scorer=_CAND)
    program = _seed_tested(world, EvalTask.SCORER, _CAND)
    world.engines = _flipping()

    result = _accept(world, str(program))

    assert result.exit_code == 0, result.output
    assert world.engines.engine("cand").calls == ["scorer"] * 10
    assert _accepted(world, EvalTask.SCORER) == program
    assert _accepted(world, EvalTask.JUDGE) is None


def test_accept_refuses_what_cannot_be_accepted(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    untested = _seed_tested(world, EvalTask.JUDGE, _CAND, tested=False)

    unknown = _accept(world, "99")
    not_tested = _accept(world, str(untested))
    _seed_tested(world, EvalTask.SCORER, _CAND)
    not_set = _accept(world, "2")

    assert unknown.exit_code == 2
    assert "program 99" in unknown.stderr
    assert not_tested.exit_code == 1
    assert "ijob tune run" in not_tested.stderr
    assert not_set.exit_code == 1
    assert "[models].scorer" in not_set.stderr
    assert _hostile_runs(world) == []
    assert world.engines.engine("cand").calls == []


def _routed_world(
    tmp_path: Path, clients: ClientFactoryBuilder, monkeypatch: pytest.MonkeyPatch
) -> tuple[_World, int, int]:
    """A world with a judge program on OpenRouter and a scorer program on the CLI."""
    world = _tune_world(tmp_path, clients)
    _set_models(world, judge=_ROUTED, scorer=_CAND)
    _routed(monkeypatch)
    return (
        world,
        _seed_tested(world, EvalTask.JUDGE, _ROUTED),
        _seed_tested(world, EvalTask.SCORER, _CAND),
    )


def test_openrouter_prompt_carries_the_data_line(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, judge, scorer = _routed_world(tmp_path, client_factory_for, monkeypatch)

    declined = _accept(world, str(judge), interactive=True, stdin="n\n")
    after_declining = _hostile_runs(world)
    subscription = _accept(world, str(scorer))

    assert declined.exit_code == 1
    assert "instructions and demos may hold your rules and postings" in declined.stderr
    assert "go to OpenRouter and made-up" in declined.stderr
    assert "Start the run? [y/N]" in declined.stderr
    assert "the run was not started" in declined.stderr
    assert after_declining == []
    assert subscription.exit_code == 0, subscription.output
    assert "may hold your rules" not in subscription.stderr
    assert "Start the run?" not in subscription.stderr


def test_accept_closed_stdin_refuses(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, judge, _ = _routed_world(tmp_path, client_factory_for, monkeypatch)

    result = _accept(world, str(judge))

    assert result.exit_code == 1
    assert "confirmation needed; pass --yes" in result.stderr
    assert _hostile_runs(world) == []
    assert _accepted(world, EvalTask.JUDGE) is None


def test_accept_on_openrouter_with_yes_runs_and_records_the_spend(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, judge, _ = _routed_world(tmp_path, client_factory_for, monkeypatch)

    result = _accept(world, str(judge), "--yes")

    assert result.exit_code == 0, result.output
    assert _accepted(world, EvalTask.JUDGE) == judge
    with Store.open(world.home.db) as store:
        [run] = [each for each in store.evals.runs() if each.suite == "hostile"]
    assert run.spent_usd == pytest.approx(10 * _ROUTED_COST)


def test_clear_returns_to_the_base_prompt(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    _set_models(world, judge=_CAND)
    program = _seed_tested(world, EvalTask.JUDGE, _CAND)
    assert _accept(world, str(program)).exit_code == 0

    cleared = _invoke(world, "tune", "clear", "judge")
    again = _invoke(world, "tune", "clear", "judge")
    listed = _listed(world)

    assert cleared.exit_code == 0, cleared.output
    assert "judge" in cleared.stderr
    assert "base prompt" in cleared.stderr
    assert again.exit_code == 0
    assert "no program is accepted for judge" in again.stderr
    assert _accepted(world, EvalTask.JUDGE) is None
    assert listed.stdout.split()[-1] == "tested"


def test_clear_unknown_task_is_refused(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    result = _invoke(world, "tune", "clear", "nonsense")

    assert result.exit_code == 2
    assert "judge" in result.stderr
    assert "scorer" in result.stderr


def test_accept_clear_and_report_help_list_their_flags(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)

    accept = _invoke(world, "tune", "accept", "--help")
    clear = _invoke(world, "tune", "clear", "--help")
    report = _invoke(world, "tune", "report", "--help")

    for result, flags in (
        (accept, ("ID", "--yes", "--db", "--config")),
        (clear, ("TASK", "--db")),
        (report, ("--out", "--db", "--config")),
    ):
        assert result.exit_code == 0
        for flag in flags:
            assert flag in result.stdout
