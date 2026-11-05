import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalTask
from ijob_core.model import (
    ExternalId,
    PostingId,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.store import _MIGRATIONS, Store
from ijob_core.tune_records import SplitPart, TunedProgram, TuneSplitRow

_A = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_AT = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
_NAIVE = _AT.replace(tzinfo=None)


def _store(path: Path) -> Store:
    store = Store.open(path / "ijob.db")
    store.import_seeds([(_A, "Alpha")])
    store.record(
        [
            SightingDraft(
                source=_A,
                external_id=ExternalId(str(number)),
                title=f"Engineer {number}",
                company="Acme",
                location="Remote",
                url=f"https://example.com/jobs/{number}",
                posted_at=None,
                raw="{}",
            )
            for number in range(1, 5)
        ],
        seen_at=_AT,
    )
    return store


def _set(store: Store, rules: str = "r") -> int:
    return store.evals.find_or_create_set(rules, "p", ("a", "b"), created_at=_AT).id


_PARTS = {
    PostingId(1): SplitPart.TRAIN,
    PostingId(2): SplitPart.VAL,
    PostingId(3): SplitPart.HELD_OUT,
    PostingId(4): SplitPart.UNUSED,
}


def test_split_round_trips(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        assert not store.tunes.has_split(set_id)

        store.tunes.put_split(set_id, _PARTS)

        assert store.tunes.has_split(set_id)
        assert store.tunes.split(set_id) == [
            TuneSplitRow(set_id, posting_id, part)
            for posting_id, part in _PARTS.items()
        ]


def test_a_frozen_split_cannot_be_replaced(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        store.tunes.put_split(set_id, _PARTS)
        before = store.tunes.split(set_id)

        with pytest.raises(InvalidValueError, match="already"):
            store.tunes.put_split(set_id, {PostingId(1): SplitPart.VAL})

        assert store.tunes.split(set_id) == before


def test_a_second_writer_cannot_merge_rows_into_a_split_being_frozen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ijob.db"
    outcomes: list[str] = []

    def other_writer(statement: str) -> None:
        # Runs between put_split's check and its insert.
        if not statement.startswith("INSERT INTO tune_splits"):
            return
        with closing(sqlite3.connect(path, timeout=0)) as other:
            try:
                with other:
                    other.execute(
                        "INSERT INTO tune_splits (set_id, posting_id, part) "
                        "VALUES (1, 4, 'train')"
                    )
            except sqlite3.OperationalError as error:
                outcomes.append(str(error))
            else:
                outcomes.append("written")

    with _store(tmp_path) as store:
        set_id = _set(store)
        assert set_id == 1
        store._conn.set_trace_callback(other_writer)
        store.tunes.put_split(set_id, {PostingId(1): SplitPart.TRAIN})
        store._conn.set_trace_callback(None)

        assert outcomes == ["database is locked"]
        assert [row.posting_id for row in store.tunes.split(set_id)] == [PostingId(1)]


def test_a_new_set_gets_its_own_split(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        first = _set(store, "r1")
        second = _set(store, "r2")
        store.tunes.put_split(first, _PARTS)

        store.tunes.put_split(second, {PostingId(1): SplitPart.HELD_OUT})

        assert len(store.tunes.split(first)) == 4
        assert store.tunes.split(second) == [
            TuneSplitRow(second, PostingId(1), SplitPart.HELD_OUT)
        ]


def test_tune_splits_migrates_an_older_schema(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        for statements in _MIGRATIONS[:8]:
            for statement in statements:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO postings (title, company, location, url, first_seen, "
            "last_seen) VALUES ('Engineer', 'Acme', 'Remote', "
            "'https://example.com/jobs/1', ?, ?)",
            (_AT.isoformat(), _AT.isoformat()),
        )
        conn.execute("PRAGMA user_version = 8")

    with Store.open(tmp_path / "ijob.db") as store:
        set_id = _set(store)
        store.tunes.put_split(set_id, {PostingId(1): SplitPart.TRAIN})
        assert store.posting(PostingId(1)) is not None

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(_MIGRATIONS)


def _save(  # noqa: PLR0913  # one keyword per field a test varies
    store: Store,
    set_id: int,
    *,
    task: EvalTask = EvalTask.JUDGE,
    model: str = "claude-sdk/claude-haiku-5-5",
    version: str = "judge-2+1a2b3c4d",
    instructions: str = "Decide.",
    demos_json: str = '[{"posting": "p"}]',
    bound_sha256: str = "b" * 64,
) -> TunedProgram:
    return store.tunes.save_program(
        task=task,
        model=model,
        optimizer="gepa",
        base_version="judge-2",
        set_id=set_id,
        bound_sha256=bound_sha256,
        instructions=instructions,
        demos_json=demos_json,
        version=version,
        tune_run_id=4,
        created_at=_AT,
    )


def test_program_round_trips(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)

        saved = _save(store, set_id)

        assert saved == TunedProgram(
            id=saved.id,
            task=EvalTask.JUDGE,
            model="claude-sdk/claude-haiku-5-5",
            optimizer="gepa",
            base_version="judge-2",
            set_id=set_id,
            bound_sha256="b" * 64,
            instructions="Decide.",
            demos_json='[{"posting": "p"}]',
            version="judge-2+1a2b3c4d",
            tune_run_id=4,
            created_at=_AT,
            accepted_at=None,
        )
        assert store.tunes.program(saved.id) == saved
        assert store.tunes.programs(EvalTask.JUDGE) == [saved]
        assert store.tunes.programs(EvalTask.SCORER) == []
        assert store.tunes.program(saved.id + 1) is None


def test_an_identical_program_reuses_its_row(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        first = _save(store, set_id)
        store.tunes.accept_program(first.id, accepted_at=_AT)

        again = _save(store, set_id, instructions="Other text.")

        assert again.id == first.id
        assert again.instructions == "Decide."
        assert again.accepted_at == _AT
        assert len(store.tunes.programs(EvalTask.JUDGE)) == 1


def test_the_same_text_under_another_bound_is_a_new_row(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        first_set = _set(store, "r1")
        second_set = _set(store, "r2")
        first = _save(store, first_set)
        store.tunes.accept_program(first.id, accepted_at=_AT)

        second = _save(store, second_set, bound_sha256="c" * 64)

        assert second.id != first.id
        assert (second.bound_sha256, second.set_id) == ("c" * 64, second_set)
        assert second.accepted_at is None
        kept = store.tunes.program(first.id)
        assert kept is not None
        assert (kept.bound_sha256, kept.set_id) == ("b" * 64, first_set)
        assert kept.accepted_at == _AT
        assert len(store.tunes.programs(EvalTask.JUDGE)) == 2


def test_accepting_replaces_the_old_program(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        old = _save(store, set_id)
        new = _save(store, set_id, version="judge-2+99999999")
        scorer = _save(store, set_id, task=EvalTask.SCORER, version="scorer-1+11111111")
        store.tunes.accept_program(old.id, accepted_at=_AT)
        store.tunes.accept_program(scorer.id, accepted_at=_AT)

        store.tunes.accept_program(new.id, accepted_at=_AT + timedelta(hours=1))

        accepted = store.tunes.accepted(EvalTask.JUDGE)
        assert accepted is not None
        assert accepted.id == new.id
        assert accepted.accepted_at == _AT + timedelta(hours=1)
        old_now = store.tunes.program(old.id)
        assert old_now is not None
        assert old_now.accepted_at is None
        kept = store.tunes.accepted(EvalTask.SCORER)
        assert kept is not None
        assert kept.id == scorer.id


def test_a_failed_accept_leaves_the_old_program_accepted(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        old = _save(store, set_id)
        new = _save(store, set_id, version="judge-2+99999999")
        store.tunes.accept_program(old.id, accepted_at=_AT)
        with closing(sqlite3.connect(tmp_path / "ijob.db")) as conn, conn:
            conn.execute(
                "CREATE TRIGGER refuse_accept BEFORE UPDATE OF accepted_at "
                "ON programs WHEN NEW.accepted_at IS NOT NULL "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )

        with pytest.raises(sqlite3.IntegrityError, match="refused"):
            store.tunes.accept_program(new.id, accepted_at=_AT + timedelta(hours=1))

        accepted = store.tunes.accepted(EvalTask.JUDGE)
        assert accepted is not None
        assert (accepted.id, accepted.accepted_at) == (old.id, _AT)
        with closing(sqlite3.connect(tmp_path / "ijob.db")) as conn:
            row = conn.execute(
                "SELECT accepted_at FROM programs WHERE id = ?", (old.id,)
            ).fetchone()
        assert row[0] is not None


def test_accepting_with_a_naive_time_is_refused(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        program = _save(store, _set(store))

        with pytest.raises(InvalidValueError, match="accepted_at"):
            store.tunes.accept_program(program.id, accepted_at=_NAIVE)

        assert store.tunes.accepted(EvalTask.JUDGE) is None


def test_saving_with_a_naive_time_is_refused(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        with pytest.raises(InvalidValueError, match="created_at"):
            store.tunes.save_program(
                task=EvalTask.JUDGE,
                model="m",
                optimizer="gepa",
                base_version="judge-2",
                set_id=set_id,
                bound_sha256="b" * 64,
                instructions="Decide.",
                demos_json="[]",
                version="judge-2+1a2b3c4d",
                tune_run_id=None,
                created_at=_NAIVE,
            )
        assert store.tunes.programs(EvalTask.JUDGE) == []


def test_saving_for_an_unknown_set_is_refused(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _save(store, 999)
        assert store.tunes.programs(EvalTask.JUDGE) == []


def test_accepting_an_unknown_program_is_refused(tmp_path: Path) -> None:
    with _store(tmp_path) as store, pytest.raises(InvalidValueError, match="42"):
        store.tunes.accept_program(42, accepted_at=_AT)


def test_clear_removes_only_the_mark(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        program = _save(store, set_id)
        store.tunes.accept_program(program.id, accepted_at=_AT)

        assert store.tunes.clear_program(EvalTask.JUDGE)

        assert store.tunes.accepted(EvalTask.JUDGE) is None
        row = store.tunes.program(program.id)
        assert row is not None
        assert row.accepted_at is None
        assert not store.tunes.clear_program(EvalTask.JUDGE)


def test_clear_leaves_the_other_tasks_program_accepted(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        judge = _save(store, set_id)
        scorer = _save(store, set_id, task=EvalTask.SCORER, version="scorer-1+11111111")
        store.tunes.accept_program(judge.id, accepted_at=_AT)
        store.tunes.accept_program(scorer.id, accepted_at=_AT)

        assert store.tunes.clear_program(EvalTask.JUDGE)

        assert store.tunes.accepted(EvalTask.JUDGE) is None
        kept = store.tunes.accepted(EvalTask.SCORER)
        assert kept is not None
        assert kept.id == scorer.id


def _program_row(
    *,
    instructions: str = "Decide.",
    demos_json: str = "[]",
    created_at: datetime = _AT,
    accepted_at: datetime | None = None,
) -> TunedProgram:
    return TunedProgram(
        id=3,
        task=EvalTask.JUDGE,
        model="m",
        optimizer="gepa",
        base_version="judge-2",
        set_id=1,
        bound_sha256="b" * 64,
        instructions=instructions,
        demos_json=demos_json,
        version="judge-2+1a2b3c4d",
        tune_run_id=None,
        created_at=created_at,
        accepted_at=accepted_at,
    )


@pytest.mark.parametrize(
    ("build", "field"),
    [
        (lambda: _program_row(instructions=" "), "instructions"),
        (lambda: _program_row(demos_json="not json"), "demos"),
        (lambda: _program_row(demos_json='["x"]'), "demos"),
        (lambda: _program_row(demos_json='{"a": 1}'), "demos"),
        (lambda: _program_row(created_at=_NAIVE), "created_at"),
        (lambda: _program_row(accepted_at=_NAIVE), "accepted_at"),
    ],
    ids=[
        "blank-instructions",
        "demos-not-json",
        "demos-list-of-strings",
        "demos-an-object",
        "naive-created-at",
        "naive-accepted-at",
    ],
)
def test_a_malformed_program_cannot_be_built(
    build: Callable[[], TunedProgram], field: str
) -> None:
    with pytest.raises(InvalidValueError, match="program 3") as raised:
        build()

    assert raised.value.field == field


@pytest.mark.parametrize(
    ("demos_json", "instructions"),
    [
        ("not json", "Decide."),
        ('["x"]', "Decide."),
        ('{"a": 1}', "Decide."),
        ("[]", " "),
    ],
)
def test_malformed_program_names_its_id(
    tmp_path: Path, demos_json: str, instructions: str
) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        program = _save(store, set_id)
        store.tunes.accept_program(program.id, accepted_at=_AT)
        with closing(sqlite3.connect(tmp_path / "ijob.db")) as conn, conn:
            conn.execute(
                "UPDATE programs SET demos_json = ?, instructions = ?",
                (demos_json, instructions),
            )
        pattern = f"program {program.id} "

        with pytest.raises(InvalidValueError, match=pattern):
            store.tunes.program(program.id)
        with pytest.raises(InvalidValueError, match=pattern):
            store.tunes.accepted(EvalTask.JUDGE)
        with pytest.raises(InvalidValueError, match=pattern):
            store.tunes.programs(EvalTask.JUDGE)
        assert store.tunes.clear_program(EvalTask.JUDGE)
        assert store.tunes.accepted(EvalTask.JUDGE) is None


def test_a_malformed_program_is_not_saved(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        set_id = _set(store)
        with pytest.raises(InvalidValueError, match="demos"):
            _save(store, set_id, demos_json='["x"]')
        with pytest.raises(InvalidValueError, match="instructions"):
            _save(store, set_id, instructions="")
        assert store.tunes.programs(EvalTask.JUDGE) == []


def test_programs_migrates_an_older_schema(tmp_path: Path) -> None:
    path = tmp_path / "ijob.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        for statements in _MIGRATIONS[:9]:
            for statement in statements:
                conn.execute(statement)
        conn.execute("PRAGMA user_version = 9")

    with Store.open(path) as store:
        set_id = _set(store)
        assert _save(store, set_id).id == 1

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(_MIGRATIONS)
        assert len(_MIGRATIONS) == 10
