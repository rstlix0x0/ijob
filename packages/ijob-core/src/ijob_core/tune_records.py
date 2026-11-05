"""The frozen split of a label set into the parts tuning uses, and tuned programs.

`SplitPart` names the four parts a posting can fall in. `TunedProgram` is the
instructions and demos tuning produced for one task and model. `TuneRecords`
reads and writes both on the connection of the `Store` that returns it. A set's
split is written once and never changed or deleted; a program is never changed,
only marked accepted or unmarked.
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalTask
from ijob_core.model import PostingId


class SplitPart(StrEnum):
    """The part of the split a posting belongs to."""

    TRAIN = "train"
    VAL = "val"
    HELD_OUT = "held_out"
    UNUSED = "unused"


@dataclass(frozen=True, slots=True)
class TuneSplitRow:
    """One posting's part in the split of a label set.

    Attributes:
        set_id: The label set the split belongs to.
        posting_id: The posting.
        part: The part the posting fell in.
    """

    set_id: int
    posting_id: PostingId
    part: SplitPart


@dataclass(frozen=True, slots=True)
class TunedProgram:
    """The instructions and demos tuning produced for one task and model.

    Attributes:
        id: The row number.
        task: The task the program is for.
        model: The model reference it was tuned for.
        optimizer: The optimizer that produced it.
        base_version: The task version it was tuned from.
        set_id: The label set it was tuned on.
        bound_sha256: The rules hash for the judge, the profile hash for the
            scorer; the demos hold that text.
        instructions: The tuned instruction text, never empty.
        demos_json: The demos as a JSON list of objects.
        version: The task version a call with this program runs under.
        tune_run_id: The tune run that produced it, if one is recorded.
        created_at: When it was saved, timezone-aware.
        accepted_at: When it was accepted, or `None` while it is not.

    Raises:
        InvalidValueError: Naming the program's id, if the instructions are blank,
            the demos are not a JSON list of objects, or a time has no timezone.
    """

    id: int
    task: EvalTask
    model: str
    optimizer: str
    base_version: str
    set_id: int
    bound_sha256: str
    instructions: str
    demos_json: str
    version: str
    tune_run_id: int | None
    created_at: datetime
    accepted_at: datetime | None

    def __post_init__(self) -> None:
        """Reject content a stored program may not hold.

        Raises:
            InvalidValueError: Naming the field that does not fit and the id.
        """
        label = f"program {self.id}"
        _check_content(self.instructions, self.demos_json, label=label)
        for field, moment in (
            ("created_at", self.created_at),
            ("accepted_at", self.accepted_at),
        ):
            if moment is not None and moment.utcoffset() is None:
                raise InvalidValueError(field, f"{label} has no timezone")


_SELECT_PROGRAMS: Final = (
    "SELECT id, task, model, optimizer, base_version, set_id, bound_sha256, "
    "instructions, demos_json, version, tune_run_id, created_at, accepted_at "
    "FROM programs"
)


def _stamp(field: str, moment: datetime) -> str:
    """Render an aware `moment` as fixed-width ISO-8601 text in UTC."""
    if moment.utcoffset() is None:
        raise InvalidValueError(field, "has no timezone")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _from_text(text: str) -> datetime:
    """Read text written by `_stamp` as an aware UTC datetime."""
    return datetime.fromisoformat(text).astimezone(UTC)


def _check_content(instructions: str, demos_json: str, *, label: str) -> None:
    """Raise `InvalidValueError` unless the text and demos are well formed.

    Args:
        instructions: The instruction text, which must not be blank.
        demos_json: The demos, which must be a JSON list of objects.
        label: What the message names, such as `program 3`.
    """
    if not instructions.strip():
        raise InvalidValueError("instructions", f"{label} has empty instructions")
    try:
        demos = json.loads(demos_json)
    except ValueError as error:
        msg = f"{label} has demos that are not JSON"
        raise InvalidValueError("demos", msg) from error
    if not isinstance(demos, list) or not all(isinstance(d, dict) for d in demos):
        msg = f"{label} has demos that are not a list of objects"
        raise InvalidValueError("demos", msg)


def _program(row: tuple[object, ...]) -> TunedProgram:
    """Build a program from a row of `_SELECT_PROGRAMS`, or raise naming its id.

    Raises:
        InvalidValueError: Naming the id, if a value of the row cannot be read or
            its content is malformed.
    """
    accepted = row[12]
    run = row[10]
    try:
        return TunedProgram(
            id=int(str(row[0])),
            task=EvalTask(str(row[1])),
            model=str(row[2]),
            optimizer=str(row[3]),
            base_version=str(row[4]),
            set_id=int(str(row[5])),
            bound_sha256=str(row[6]),
            instructions=str(row[7]),
            demos_json=str(row[8]),
            version=str(row[9]),
            tune_run_id=None if run is None else int(str(run)),
            created_at=_from_text(str(row[11])),
            accepted_at=None if accepted is None else _from_text(str(accepted)),
        )
    except ValueError as error:
        msg = f"program {row[0]} has a value that cannot be read"
        raise InvalidValueError("program", msg) from error


class TuneRecords:
    """Reads and writes of the tuning split and tuned programs on one connection.

    Get one from `Store.tunes`; it shares the store's connection and its lifetime.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap the connection of an open store.

        Args:
            conn: The connection, on a database at the current schema version.
        """
        self._conn = conn

    def has_split(self, set_id: int) -> bool:
        """Return whether the set already has a frozen split."""
        row = self._conn.execute(
            "SELECT 1 FROM tune_splits WHERE set_id = ? LIMIT 1", (set_id,)
        ).fetchone()
        return row is not None

    def put_split(self, set_id: int, parts: Mapping[PostingId, SplitPart]) -> None:
        """Freeze the split of a set, in one transaction.

        The write lock is taken before the check, so no other writer can add rows
        to the set between the check and the insert.

        Args:
            set_id: The label set.
            parts: The part of each posting.

        Raises:
            InvalidValueError: If the set already has a split; it stays as it was.
            sqlite3.IntegrityError: If the set or a posting is unknown.
            sqlite3.Error: If the write fails.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        with self._conn:
            if self.has_split(set_id):
                raise InvalidValueError("set_id", f"set {set_id} already has a split")
            self._conn.executemany(
                "INSERT INTO tune_splits (set_id, posting_id, part) VALUES (?, ?, ?)",
                [
                    (set_id, posting_id, part.value)
                    for posting_id, part in parts.items()
                ],
            )

    def split(self, set_id: int) -> list[TuneSplitRow]:
        """Read the split of a set.

        Returns:
            The rows by posting number; empty when the set has no split.
        """
        rows = self._conn.execute(
            "SELECT posting_id, part FROM tune_splits WHERE set_id = ? "
            "ORDER BY posting_id",
            (set_id,),
        ).fetchall()
        return [
            TuneSplitRow(set_id, PostingId(posting_id), SplitPart(part))
            for posting_id, part in rows
        ]

    def save_program(  # noqa: PLR0913  # a program's whole row, one argument each
        self,
        *,
        task: EvalTask,
        model: str,
        optimizer: str,
        base_version: str,
        set_id: int,
        bound_sha256: str,
        instructions: str,
        demos_json: str,
        version: str,
        tune_run_id: int | None,
        created_at: datetime,
    ) -> TunedProgram:
        """Store a program, or return the row already holding its version and bound.

        The row for the same task, model, version and bound is returned
        untouched, so an identical result reuses it and an accepted program is
        never changed. The same version under another bound is a new row.

        Args:
            task: The task the program is for.
            model: The model reference it was tuned for.
            optimizer: The optimizer that produced it.
            base_version: The task version it was tuned from.
            set_id: The label set it was tuned on.
            bound_sha256: The rules or profile hash the demos hold.
            instructions: The instruction text, not blank.
            demos_json: The demos, a JSON list of objects.
            version: The version a call with the program runs under.
            tune_run_id: The tune run that produced it, if one is recorded.
            created_at: When it was saved, timezone-aware.

        Returns:
            The stored row.

        Raises:
            InvalidValueError: If the instructions are blank, the demos are not a
                JSON list of objects, or `created_at` has no timezone.
            sqlite3.IntegrityError: If the set is unknown.
            sqlite3.Error: If the write fails.
        """
        _check_content(instructions, demos_json, label="the program")
        stamp = _stamp("created_at", created_at)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO programs (task, model, optimizer, "
                "base_version, set_id, bound_sha256, instructions, demos_json, "
                "version, tune_run_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.value,
                    model,
                    optimizer,
                    base_version,
                    set_id,
                    bound_sha256,
                    instructions,
                    demos_json,
                    version,
                    tune_run_id,
                    stamp,
                ),
            )
            row = self._conn.execute(
                f"{_SELECT_PROGRAMS} WHERE task = ? AND model = ? AND version = ? "
                "AND bound_sha256 = ?",
                (task.value, model, version, bound_sha256),
            ).fetchone()
        return _program(row)

    def program(self, program_id: int) -> TunedProgram | None:
        """Read one program.

        Raises:
            InvalidValueError: Naming the id, if the row's demos are not a JSON
                list of objects or its instructions are empty.
        """
        row = self._conn.execute(
            f"{_SELECT_PROGRAMS} WHERE id = ?", (program_id,)
        ).fetchone()
        return None if row is None else _program(row)

    def programs(self, task: EvalTask) -> list[TunedProgram]:
        """Read the programs of a task by id.

        Raises:
            InvalidValueError: Naming the id of the first malformed row.
        """
        rows = self._conn.execute(
            f"{_SELECT_PROGRAMS} WHERE task = ? ORDER BY id",
            (task.value,),
        ).fetchall()
        return [_program(row) for row in rows]

    def accepted(self, task: EvalTask) -> TunedProgram | None:
        """Read the accepted program of a task, or `None` when there is none.

        Raises:
            InvalidValueError: Naming the id, if the accepted row is malformed.
        """
        row = self._conn.execute(
            f"{_SELECT_PROGRAMS} WHERE task = ? AND accepted_at IS NOT NULL",
            (task.value,),
        ).fetchone()
        return None if row is None else _program(row)

    def accept_program(self, program_id: int, *, accepted_at: datetime) -> None:
        """Accept a program and unmark any other accepted program of its task.

        Args:
            program_id: The program to accept.
            accepted_at: When it was accepted, timezone-aware.

        Raises:
            InvalidValueError: If there is no such program or `accepted_at` has no
                timezone.
            sqlite3.Error: If the write fails.
        """
        stamp = _stamp("accepted_at", accepted_at)
        with self._conn:
            row = self._conn.execute(
                "SELECT task FROM programs WHERE id = ?", (program_id,)
            ).fetchone()
            if row is None:
                msg = f"program {program_id} is unknown"
                raise InvalidValueError("program_id", msg)
            self._conn.execute(
                "UPDATE programs SET accepted_at = NULL "
                "WHERE task = ? AND id != ? AND accepted_at IS NOT NULL",
                (row[0], program_id),
            )
            self._conn.execute(
                "UPDATE programs SET accepted_at = ? WHERE id = ?",
                (stamp, program_id),
            )

    def clear_program(self, task: EvalTask) -> bool:
        """Unmark the accepted program of a task; the row itself stays.

        It reads no content, so it works on a malformed row.

        Returns:
            Whether a program was accepted.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE programs SET accepted_at = NULL "
                "WHERE task = ? AND accepted_at IS NOT NULL",
                (task.value,),
            )
        return cursor.rowcount > 0
