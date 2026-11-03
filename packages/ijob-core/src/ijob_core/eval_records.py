"""The records of the evaluation jury and of eval runs, and their reads and writes.

A label set (`EvalSet`) is the sample of postings that one rule text, one profile
and one jury share, with each case's pool, the votes the jurors cast, and the
labels drawn from them. A run (`EvalRun`) is one `ijob eval` command with its
metrics, spend and per-case results.

`EvalRecords` reads and writes these on the connection of the `Store` that
returns it. Votes and labels derived from votes are written with `INSERT OR
IGNORE`, so a stored one never changes. Votes live apart from the scores table,
so a juror's score never shows in the user's search list.
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from ijob_core.errors import InvalidValueError
from ijob_core.model import PostingId


class EvalTask(StrEnum):
    """The task a vote or label answers."""

    JUDGE = "judge"
    SCORER = "scorer"


class Bucket(StrEnum):
    """The pool a case was drawn from."""

    KEPT = "kept"
    REJECTED = "rejected"
    UNJUDGED = "unjudged"


class LabelOrigin(StrEnum):
    """Who decided a label."""

    AGREED = "agreed"
    USER = "user"


class RunStatus(StrEnum):
    """How far an eval run got."""

    RUNNING = "running"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


_SCORE_MIN: Final = 0
_SCORE_MAX: Final = 100


def _check_answer(
    task: EvalTask,
    *,
    keep: bool | None,
    rule: int | None,
    score: int | None,
    rule_optional: bool = False,
) -> None:
    """Check that the answer fields are the ones `task` uses, and in range.

    A judge answer that rejects needs a `rule` of at least 1, unless
    `rule_optional` lets it name none; a scorer answer needs a `score` from 0 to
    100.

    Raises:
        InvalidValueError: Naming the field that does not fit.
    """
    if task is EvalTask.JUDGE:
        if keep is None:
            raise InvalidValueError("keep", "is missing for a judge answer")
        if score is not None:
            raise InvalidValueError("score", "is set on a judge answer")
        if keep and rule is not None:
            raise InvalidValueError("rule", "is set on a keep answer")
        if not keep:
            missing = rule is None and not rule_optional
            if missing or (rule is not None and rule < 1):
                raise InvalidValueError("rule", "must be at least 1 on a reject answer")
        return
    if score is None:
        raise InvalidValueError("score", "is missing for a scorer answer")
    if not _SCORE_MIN <= score <= _SCORE_MAX:
        raise InvalidValueError(
            "score", f"{score} is outside {_SCORE_MIN} to {_SCORE_MAX}"
        )
    if keep is not None or rule is not None:
        raise InvalidValueError("keep", "is set on a scorer answer")


@dataclass(frozen=True, slots=True)
class EvalSet:
    """One label set.

    Attributes:
        id: The set's number.
        rules_sha256: The SHA-256 of the rules the set was drawn under.
        profile_sha256: The SHA-256 of the profile it scores against.
        jury: The jurors' model references, sorted.
        created_at: When the set was made, timezone-aware.
    """

    id: int
    rules_sha256: str
    profile_sha256: str
    jury: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One posting in a label set.

    Attributes:
        set_id: The set it belongs to.
        posting_id: The posting.
        position: Its place in the sample, from 0.
        bucket: The pool it was drawn from.
        input_sha256: The SHA-256 of the posting text the jurors read.
    """

    set_id: int
    posting_id: PostingId
    position: int
    bucket: Bucket
    input_sha256: str


@dataclass(frozen=True, slots=True)
class EvalVote:
    """One juror's answer on one case.

    A judge vote sets `keep` (and `rule` when rejecting) and no `score`; a scorer
    vote sets `score` alone.

    Attributes:
        set_id: The set it belongs to.
        posting_id: The posting.
        task: The task answered.
        juror: The juror's model reference.
        task_version: The version of the task that answered.
        keep: Whether the posting is kept, for a judge vote.
        rule: The 1-based number of the rejecting rule, if any.
        score: The fit from 0 to 100, for a scorer vote.
        reasons: The reasons given.
        made_at: When the juror answered, timezone-aware.
    """

    set_id: int
    posting_id: PostingId
    task: EvalTask
    juror: str
    task_version: str
    keep: bool | None
    rule: int | None
    score: int | None
    reasons: tuple[str, ...]
    made_at: datetime

    def __post_init__(self) -> None:
        """Reject answer fields that do not fit the task or are out of range.

        Raises:
            InvalidValueError: Naming the field that does not fit.
        """
        _check_answer(self.task, keep=self.keep, rule=self.rule, score=self.score)


@dataclass(frozen=True, slots=True)
class EvalLabel:
    """The decided answer for one case and task.

    Attributes:
        set_id: The set it belongs to.
        posting_id: The posting.
        task: The task answered.
        keep: Whether the posting is kept, for a judge label.
        rule: The 1-based number of the rejecting rule, if any.
        score: The fit from 0 to 100, for a scorer label.
        origin: Whether the jurors agreed or the user decided.
        spot_checked: Whether the user looked at an agreed label.
        decided_at: When it was decided, timezone-aware.
    """

    set_id: int
    posting_id: PostingId
    task: EvalTask
    keep: bool | None
    rule: int | None
    score: int | None
    origin: LabelOrigin
    spot_checked: bool
    decided_at: datetime

    def __post_init__(self) -> None:
        """Reject answer fields that do not fit the task or are out of range.

        A judge label that rejects may name no rule, as when the jurors reject
        for different rules; a judge vote may not.

        Raises:
            InvalidValueError: Naming the field that does not fit.
        """
        _check_answer(
            self.task,
            keep=self.keep,
            rule=self.rule,
            score=self.score,
            rule_optional=True,
        )


@dataclass(frozen=True, slots=True)
class EvalRun:
    """One `ijob eval` command.

    Attributes:
        id: The run's number.
        suite: The suite run, `jury` included.
        model: The model under test.
        task_version: The task version tested; `judge-1+scorer-1` for both tasks.
        set_id: The label set used, or `None` for a suite without labels.
        status: How far the run got. A read shows a run still `running` as
            `incomplete`; only the value `start_run` returns is `running`.
        limited: Whether the run covered only part of its cases.
        metrics: The run's metrics as JSON text.
        spent_usd: OpenRouter spend in US dollars, which counts toward the caps.
        subscription_usd: claude-sdk spend in US dollars, which does not.
        started_at: When the run began, timezone-aware.
        finished_at: When it ended, or `None` while it has not.
    """

    id: int
    suite: str
    model: str
    task_version: str
    set_id: int | None
    status: RunStatus
    limited: bool
    metrics: str
    spent_usd: float
    subscription_usd: float
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class EvalResult:
    """One case's outcome in a run.

    Attributes:
        run_id: The run it belongs to.
        case_key: The case, as the suite names it.
        passed: Whether the answer matched.
        answer: The model's answer as JSON text.
        detail: A note on the outcome; blank when there is none.
        latency_ms: How long the answer took, in milliseconds.
    """

    run_id: int
    case_key: str
    passed: bool
    answer: str
    detail: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class Pools:
    """The postings in each pool, as ids in ascending order.

    Attributes:
        kept: Postings whose newest verdict keeps them.
        rejected: Postings whose newest verdict rejects them.
        unjudged: Postings with no verdict under the rules.
    """

    kept: tuple[PostingId, ...]
    rejected: tuple[PostingId, ...]
    unjudged: tuple[PostingId, ...]


def _stamp(field: str, moment: datetime) -> str:
    """Render an aware `moment` as fixed-width ISO-8601 text in UTC."""
    if moment.utcoffset() is None:
        raise InvalidValueError(field, "has no timezone")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _from_text(text: str) -> datetime:
    """Read text written by `_stamp` as an aware UTC datetime."""
    return datetime.fromisoformat(text).astimezone(UTC)


def _bool_or_none(value: int | None) -> bool | None:
    """Read a nullable 0/1 column."""
    return None if value is None else bool(value)


_SELECT_SET = "SELECT id, rules_sha256, profile_sha256, jury, created_at FROM eval_sets"
_SELECT_RUN = (
    "SELECT id, suite, model, task_version, set_id, "
    "CASE status WHEN 'running' THEN 'incomplete' ELSE status END, limited, "
    "metrics, spent_usd, subscription_usd, started_at, finished_at FROM eval_runs"
)


def _eval_set(row: tuple[int, str, str, str, str]) -> EvalSet:
    """Build a set from a row of `_SELECT_SET`."""
    return EvalSet(
        row[0], row[1], row[2], tuple(json.loads(row[3])), _from_text(row[4])
    )


def _eval_run(row: tuple[object, ...]) -> EvalRun:
    """Build a run from a row of `_SELECT_RUN`."""
    finished = row[11]
    return EvalRun(
        id=int(str(row[0])),
        suite=str(row[1]),
        model=str(row[2]),
        task_version=str(row[3]),
        set_id=None if row[4] is None else int(str(row[4])),
        status=RunStatus(str(row[5])),
        limited=bool(row[6]),
        metrics=str(row[7]),
        spent_usd=float(str(row[8])),
        subscription_usd=float(str(row[9])),
        started_at=_from_text(str(row[10])),
        finished_at=None if finished is None else _from_text(str(finished)),
    )


class EvalRecords:
    """Reads and writes of the jury and run records on one connection.

    Get one from `Store.evals`; it shares the store's connection and its lifetime.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap the connection of an open store.

        Args:
            conn: The connection, on a database at the current schema version.
        """
        self._conn = conn

    def find_or_create_set(
        self,
        rules_sha256: str,
        profile_sha256: str,
        jury: Iterable[str],
        *,
        created_at: datetime,
    ) -> EvalSet:
        """Return the set for these rules, profile and jury, making it if new.

        The jury is compared as a sorted list, so its order does not matter. A set
        that exists keeps its own `created_at`.

        Args:
            rules_sha256: The SHA-256 of the rules.
            profile_sha256: The SHA-256 of the profile.
            jury: The jurors' model references.
            created_at: When the set is made, timezone-aware.

        Returns:
            The existing or new set.

        Raises:
            InvalidValueError: If `created_at` has no timezone.
            sqlite3.Error: If the write fails.
        """
        stamp = _stamp("created_at", created_at)
        jury_json = json.dumps(sorted(jury))
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO eval_sets (rules_sha256, profile_sha256, "
                "jury, created_at) VALUES (?, ?, ?, ?)",
                (rules_sha256, profile_sha256, jury_json, stamp),
            )
        row = self._conn.execute(
            f"{_SELECT_SET} WHERE rules_sha256 = ? AND profile_sha256 = ? AND jury = ?",
            (rules_sha256, profile_sha256, jury_json),
        ).fetchone()
        return _eval_set(row)

    def sets(self) -> list[EvalSet]:
        """Read every label set.

        Returns:
            The sets by number, oldest first; empty when none exists.
        """
        rows = self._conn.execute(f"{_SELECT_SET} ORDER BY id").fetchall()
        return [_eval_set(row) for row in rows]

    def put_cases(self, set_id: int, cases: Iterable[EvalCase]) -> None:
        """Store the cases of a set, leaving any already stored as they are.

        Args:
            set_id: The set the cases belong to.
            cases: The cases to store.

        Raises:
            sqlite3.IntegrityError: If the set or a posting is unknown.
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO eval_cases (set_id, posting_id, position, "
                "bucket, input_sha256) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        set_id,
                        case.posting_id,
                        case.position,
                        case.bucket.value,
                        case.input_sha256,
                    )
                    for case in cases
                ],
            )

    def cases(self, set_id: int) -> list[EvalCase]:
        """Read the cases of a set.

        Args:
            set_id: The set to read.

        Returns:
            The cases in sample order; empty for an unknown set.
        """
        rows = self._conn.execute(
            "SELECT set_id, posting_id, position, bucket, input_sha256 "
            "FROM eval_cases WHERE set_id = ? ORDER BY position",
            (set_id,),
        ).fetchall()
        return [
            EvalCase(row[0], PostingId(row[1]), row[2], Bucket(row[3]), row[4])
            for row in rows
        ]

    def put_vote(self, vote: EvalVote) -> bool:
        """Store a vote unless one exists for its set, posting, task and juror.

        Args:
            vote: The vote to store.

        Returns:
            True if the vote was stored, False if an earlier one stands.

        Raises:
            InvalidValueError: If `vote.made_at` has no timezone.
            sqlite3.IntegrityError: If the set or the posting is unknown.
            sqlite3.Error: If the write fails.
        """
        stamp = _stamp("made_at", vote.made_at)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO eval_votes (set_id, posting_id, task, juror, "
                "task_version, keep, rule, score, reasons, made_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    vote.set_id,
                    vote.posting_id,
                    vote.task.value,
                    vote.juror,
                    vote.task_version,
                    None if vote.keep is None else int(vote.keep),
                    vote.rule,
                    vote.score,
                    json.dumps(vote.reasons),
                    stamp,
                ),
            )
        return cursor.rowcount == 1

    def votes(self, set_id: int, task: EvalTask | None = None) -> list[EvalVote]:
        """Read the votes of a set.

        Args:
            set_id: The set to read.
            task: Read only this task's votes, or every task's when `None`.

        Returns:
            The votes by posting, task and juror; empty for an unknown set.
        """
        wanted = None if task is None else task.value
        rows = self._conn.execute(
            "SELECT set_id, posting_id, task, juror, task_version, keep, rule, "
            "score, reasons, made_at FROM eval_votes "
            "WHERE set_id = ? AND (? IS NULL OR task = ?) "
            "ORDER BY posting_id, task, juror",
            (set_id, wanted, wanted),
        ).fetchall()
        return [
            EvalVote(
                set_id=row[0],
                posting_id=PostingId(row[1]),
                task=EvalTask(row[2]),
                juror=row[3],
                task_version=row[4],
                keep=_bool_or_none(row[5]),
                rule=row[6],
                score=row[7],
                reasons=tuple(json.loads(row[8])),
                made_at=_from_text(row[9]),
            )
            for row in rows
        ]

    def put_label(self, label: EvalLabel) -> bool:
        """Store a label for its set, posting and task.

        A label the jurors agreed on never replaces one that is stored. A label the
        user decided replaces whatever is stored.

        Args:
            label: The label to store.

        Returns:
            True if the label was stored, False if an existing one stands.

        Raises:
            InvalidValueError: If `label.decided_at` has no timezone.
            sqlite3.IntegrityError: If the set or the posting is unknown.
            sqlite3.Error: If the write fails.
        """
        stamp = _stamp("decided_at", label.decided_at)
        verb = "REPLACE" if label.origin is LabelOrigin.USER else "IGNORE"
        with self._conn:
            cursor = self._conn.execute(
                f"INSERT OR {verb} INTO eval_labels (set_id, posting_id, task, keep, "  # noqa: S608  # fixed word
                "rule, score, origin, spot_checked, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    label.set_id,
                    label.posting_id,
                    label.task.value,
                    None if label.keep is None else int(label.keep),
                    label.rule,
                    label.score,
                    label.origin.value,
                    int(label.spot_checked),
                    stamp,
                ),
            )
        return cursor.rowcount == 1

    def mark_spot_checked(
        self, set_id: int, posting_id: PostingId, task: EvalTask
    ) -> bool:
        """Set the spot check mark on a stored label, changing nothing else.

        Args:
            set_id: The set the label belongs to.
            posting_id: The posting.
            task: The task the label answers.

        Returns:
            True if a label was marked, False if none is stored.

        Raises:
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE eval_labels SET spot_checked = 1 "
                "WHERE set_id = ? AND posting_id = ? AND task = ?",
                (set_id, posting_id, task.value),
            )
        return cursor.rowcount == 1

    def labels(self, set_id: int, task: EvalTask | None = None) -> list[EvalLabel]:
        """Read the labels of a set.

        Args:
            set_id: The set to read.
            task: Read only this task's labels, or every task's when `None`.

        Returns:
            The labels by posting and task; empty for an unknown set.
        """
        wanted = None if task is None else task.value
        rows = self._conn.execute(
            "SELECT set_id, posting_id, task, keep, rule, score, origin, "
            "spot_checked, decided_at FROM eval_labels "
            "WHERE set_id = ? AND (? IS NULL OR task = ?) "
            "ORDER BY posting_id, task",
            (set_id, wanted, wanted),
        ).fetchall()
        return [
            EvalLabel(
                set_id=row[0],
                posting_id=PostingId(row[1]),
                task=EvalTask(row[2]),
                keep=_bool_or_none(row[3]),
                rule=row[4],
                score=row[5],
                origin=LabelOrigin(row[6]),
                spot_checked=bool(row[7]),
                decided_at=_from_text(row[8]),
            )
            for row in rows
        ]

    def start_run(  # noqa: PLR0913  # the run's identifying fields
        self,
        suite: str,
        model: str,
        task_version: str,
        set_id: int | None,
        *,
        started_at: datetime,
        limited: bool = False,
    ) -> EvalRun:
        """Record that a run has started, with no spend yet.

        Args:
            suite: The suite being run.
            model: The model under test.
            task_version: The task version tested.
            set_id: The label set used, or `None` for a suite without labels.
            started_at: When the run began, timezone-aware.
            limited: Whether the run covers only part of its cases.

        Returns:
            The new run, `running`.

        Raises:
            InvalidValueError: If `started_at` has no timezone.
            sqlite3.IntegrityError: If `set_id` names no set.
            sqlite3.Error: If the write fails.
        """
        stamp = _stamp("started_at", started_at)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO eval_runs (suite, model, task_version, set_id, status, "
                "limited, started_at) VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (suite, model, task_version, set_id, int(limited), stamp),
            )
        return EvalRun(
            id=int(cursor.lastrowid or 0),
            suite=suite,
            model=model,
            task_version=task_version,
            set_id=set_id,
            status=RunStatus.RUNNING,
            limited=limited,
            metrics="{}",
            spent_usd=0.0,
            subscription_usd=0.0,
            started_at=_from_text(stamp),
            finished_at=None,
        )

    def record_spend(
        self, run_id: int, *, spent_usd: float, subscription_usd: float
    ) -> None:
        """Set a run's spend to the totals so far.

        Call it after each item, so an interrupted run keeps its count. The
        OpenRouter total may only grow: a lower one would raise the room left under
        `eval.total_cost_usd`.

        Args:
            run_id: The run.
            spent_usd: OpenRouter spend so far.
            subscription_usd: claude-sdk spend so far.

        Raises:
            InvalidValueError: If either amount is negative, `spent_usd` is below
                the total stored, or `run_id` names no run.
            sqlite3.Error: If the write fails.
        """
        if spent_usd < 0:
            raise InvalidValueError("spent_usd", "is negative")
        if subscription_usd < 0:
            raise InvalidValueError("subscription_usd", "is negative")
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE eval_runs SET spent_usd = ?, subscription_usd = ? "
                "WHERE id = ? AND spent_usd <= ?",
                (spent_usd, subscription_usd, run_id, spent_usd),
            )
        if cursor.rowcount == 0:
            if self._conn.execute(
                "SELECT 1 FROM eval_runs WHERE id = ?", (run_id,)
            ).fetchone():
                raise InvalidValueError(
                    "spent_usd", f"{spent_usd} is below the spend stored for the run"
                )
            raise InvalidValueError("run_id", f"{run_id} names no run")

    def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        metrics: str,
        *,
        finished_at: datetime,
    ) -> None:
        """Close a run with its status and metrics, once.

        Args:
            run_id: The run.
            status: `complete` or `incomplete`.
            metrics: The run's metrics as JSON text.
            finished_at: When the run ended, timezone-aware.

        Raises:
            InvalidValueError: If `finished_at` has no timezone, `status` is
                `running`, `run_id` names no run, or the run is already finished.
            sqlite3.Error: If the write fails.
        """
        if status is RunStatus.RUNNING:
            raise InvalidValueError("status", "cannot close a run as running")
        stamp = _stamp("finished_at", finished_at)
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE eval_runs SET status = ?, metrics = ?, finished_at = ? "
                "WHERE id = ? AND status = 'running'",
                (status.value, metrics, stamp, run_id),
            )
        if cursor.rowcount == 0:
            if self._conn.execute(
                "SELECT 1 FROM eval_runs WHERE id = ?", (run_id,)
            ).fetchone():
                raise InvalidValueError("status", "the run is already finished")
            raise InvalidValueError("run_id", f"{run_id} names no run")

    def run(self, run_id: int) -> EvalRun | None:
        """Read one run as another command sees it.

        A run still `running` reads as `incomplete`, whether or not its process is
        alive.

        Args:
            run_id: The run.

        Returns:
            The run, or `None` if there is none.
        """
        row = self._conn.execute(
            f"{_SELECT_RUN} WHERE id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else _eval_run(row)

    def runs(self) -> list[EvalRun]:
        """Read every run as another command sees it, oldest first.

        A run still `running` reads as `incomplete`.

        Returns:
            All runs by number.
        """
        rows = self._conn.execute(f"{_SELECT_RUN} ORDER BY id").fetchall()
        return [_eval_run(row) for row in rows]

    def total_spend(self) -> float:
        """Sum the OpenRouter spend of every run, `running` ones included.

        Returns:
            The total in US dollars; 0 when no run is recorded.
        """
        total = self._conn.execute("SELECT SUM(spent_usd) FROM eval_runs").fetchone()[0]
        return float(total or 0.0)

    def put_result(self, result: EvalResult) -> None:
        """Store a case's result, replacing one for the same run and case.

        Args:
            result: The result to keep.

        Raises:
            sqlite3.IntegrityError: If the run is unknown.
            sqlite3.Error: If the write fails.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO eval_results (run_id, case_key, passed, "
                "answer, detail, latency_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    result.run_id,
                    result.case_key,
                    int(result.passed),
                    result.answer,
                    result.detail,
                    result.latency_ms,
                ),
            )

    def results(self, run_id: int) -> list[EvalResult]:
        """Read the results of a run.

        Args:
            run_id: The run.

        Returns:
            The results by case key; empty for an unknown run.
        """
        rows = self._conn.execute(
            "SELECT run_id, case_key, passed, answer, detail, latency_ms "
            "FROM eval_results WHERE run_id = ? ORDER BY case_key",
            (run_id,),
        ).fetchall()
        return [
            EvalResult(row[0], row[1], bool(row[2]), row[3], row[4], row[5])
            for row in rows
        ]

    def pools(self, rules_sha256: str) -> Pools:
        """Sort every posting by its newest verdict under the given rules.

        The newest verdict of any model decides. When two verdicts share the newest
        time, the one that keeps wins. A posting with no verdict under the rules is
        unjudged.

        Args:
            rules_sha256: The SHA-256 of the rules the verdicts applied.

        Returns:
            The ids in each pool, ascending.
        """
        rows = self._conn.execute(
            "SELECT p.id, (SELECT v.keep FROM verdicts v "
            "WHERE v.posting_id = p.id AND v.rules_sha256 = ? "
            "ORDER BY v.made_at DESC, v.keep DESC LIMIT 1) "
            "FROM postings p ORDER BY p.id",
            (rules_sha256,),
        ).fetchall()
        kept = tuple(PostingId(row[0]) for row in rows if row[1] == 1)
        rejected = tuple(PostingId(row[0]) for row in rows if row[1] == 0)
        unjudged = tuple(PostingId(row[0]) for row in rows if row[1] is None)
        return Pools(kept=kept, rejected=rejected, unjudged=unjudged)
