"""`ijob tune accept` and `ijob tune clear` as the command line runs them.

The cases here need the tuning world of `test_cli_tune`, which `test_tune_ops`
cannot import without a cycle, so they live beside it.
"""

from pathlib import Path

import pytest
from ijob_core.eval_records import EvalTask
from ijob_core.store import Store

from .conftest import ClientFactoryBuilder
from .test_cli_eval import _World
from .test_cli_tune import (
    _CAND,
    _accept,
    _accepted,
    _hostile_runs,
    _invoke,
    _routed_world,
    _seed_tested,
    _set_models,
    _tune_world,
    clean_environment,  # noqa: F401  # autouse: pins HOME and the model-cost URLs
)


def test_accept_runs_a_scorer_program_on_its_own_model(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, _, scorer = _routed_world(tmp_path, client_factory_for, monkeypatch)

    result = _accept(world, str(scorer))

    assert result.exit_code == 0, result.output
    assert world.engines.engine("cand").calls == ["scorer"] * 10
    assert _hostile_runs(world) == [(_CAND, True, "scorer-1+0a1b2c3d")]


def test_accept_refuses_a_program_without_the_sentence_before_any_call(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    _set_models(world, judge=_CAND)
    program = _seed_tested(world, EvalTask.JUDGE, _CAND)
    with Store.open(world.home.db) as store, store._conn:
        store._conn.execute("UPDATE programs SET instructions = 'Tuned, no sentence.'")

    result = _accept(world, str(program))

    assert result.exit_code == 2
    assert f"program {program} lacks the untrusted-data sentence" in result.stderr
    assert _hostile_runs(world) == []
    assert world.engines.engine("cand").calls == []
    assert _accepted(world, EvalTask.JUDGE) is None


@pytest.mark.parametrize(
    "damage",
    ["demos", "created_at", "config"],
)
def test_clear_unmarks_a_row_nothing_can_read(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder, damage: str
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    _set_models(world, judge=_CAND)
    program = _seed_tested(world, EvalTask.JUDGE, _CAND)
    assert _accept(world, str(program)).exit_code == 0
    with Store.open(world.home.db) as store, store._conn:
        if damage == "demos":
            store._conn.execute("UPDATE programs SET demos_json = 'not json'")
        elif damage == "created_at":
            store._conn.execute("UPDATE programs SET created_at = 'garbage'")
    if damage == "config":
        world.home.config.write_text("this is [not toml", encoding="utf-8")

    cleared = _invoke(world, "tune", "clear", "judge")

    assert cleared.exit_code == 0, cleared.output
    assert "base prompt" in cleared.stderr
    assert _accepted_row(world) is None


def test_list_stops_on_a_row_nothing_can_read_and_names_it(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    world = _tune_world(tmp_path, client_factory_for)
    program = _seed_tested(world, EvalTask.JUDGE, _CAND)
    with Store.open(world.home.db) as store, store._conn:
        store._conn.execute("UPDATE programs SET demos_json = 'not json'")

    result = _invoke(world, "tune", "list")

    assert result.exit_code == 2
    assert f"program {program} " in result.stderr


def _accepted_row(world: _World) -> int | None:
    """Read the accepted judge program's number without parsing its content."""
    with Store.open(world.home.db) as store:
        row = store._conn.execute(
            "SELECT id FROM programs WHERE task = 'judge' AND accepted_at IS NOT NULL"
        ).fetchone()
    return None if row is None else int(row[0])
