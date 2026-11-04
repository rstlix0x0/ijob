import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ijob_app.eval_report import (
    build_report,
    comparable_runs,
    jury_metrics,
    run_detail,
)
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    EvalLabel,
    EvalRecords,
    EvalResult,
    EvalRun,
    EvalTask,
    EvalVote,
    LabelOrigin,
    RunStatus,
)
from ijob_core.model import (
    ExternalId,
    PostingId,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.store import Store

_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_SOURCE = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_JURY = ("claude-sdk/a", "claude-sdk/b", "claude-sdk/c")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "ijob.db") as opened:
        opened.import_seeds([(_SOURCE, "Alpha")])
        opened.record(
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
                for number in range(10)
            ],
            seen_at=_AT,
        )
        yield opened


@pytest.fixture
def records(store: Store) -> EvalRecords:
    return store.evals


def _set_id(records: EvalRecords) -> int:
    return records.find_or_create_set("r", "p", _JURY, created_at=_AT).id


def _run(  # noqa: PLR0913  # one knob of a run per argument
    records: EvalRecords,
    suite: str = "judge",
    *,
    version: str = "judge-1",
    set_id: int | None = None,
    status: RunStatus = RunStatus.COMPLETE,
    limited: bool = False,
    metrics: dict[str, object] | str | None = None,
    spent: float = 0.0,
    model: str = "claude-sdk/haiku",
) -> EvalRun:
    started = records.start_run(
        suite, model, version, set_id, started_at=_AT, limited=limited
    )
    records.record_spend(started.id, spent_usd=spent, subscription_usd=0.0)
    text = metrics if isinstance(metrics, str) else json.dumps(metrics or {})
    records.finish_run(started.id, status, text, finished_at=_AT)
    run = records.run(started.id)
    assert run is not None
    return run


def _ids(store: Store) -> list[PostingId]:
    return sorted(posting.id for posting in store.postings(sources=[_SOURCE]))


def _vote(  # noqa: PLR0913  # one knob of a vote per argument
    records: EvalRecords,
    set_id: int,
    posting: PostingId,
    task: EvalTask,
    juror: str,
    *,
    keep: bool | None = None,
    score: int | None = None,
) -> None:
    records.put_vote(
        EvalVote(
            set_id=set_id,
            posting_id=posting,
            task=task,
            juror=juror,
            task_version="t-1",
            keep=keep,
            rule=None if keep is not False else 1,
            score=score,
            reasons=("because",),
            made_at=_AT,
        )
    )


def _judge_votes(
    records: EvalRecords, set_id: int, posting: PostingId, keeps: tuple[bool, ...]
) -> None:
    for juror, keep in zip(_JURY, keeps, strict=False):
        _vote(records, set_id, posting, EvalTask.JUDGE, juror, keep=keep)


def _scores(
    records: EvalRecords, set_id: int, posting: PostingId, *scores: int
) -> None:
    for juror, score in zip(_JURY, scores, strict=False):
        _vote(records, set_id, posting, EvalTask.SCORER, juror, score=score)


def _label(  # noqa: PLR0913  # one knob of a label per argument
    records: EvalRecords,
    set_id: int,
    posting: PostingId,
    *,
    keep: bool | None = None,
    score: int | None = None,
    origin: LabelOrigin = LabelOrigin.AGREED,
    spot_checked: bool = False,
) -> None:
    records.put_label(
        EvalLabel(
            set_id=set_id,
            posting_id=posting,
            task=EvalTask.JUDGE if keep is not None else EvalTask.SCORER,
            keep=keep,
            rule=None,
            score=score,
            origin=origin,
            spot_checked=spot_checked,
            decided_at=_AT,
        )
    )


def test_comparable_excludes_incomplete_limited_and_other_versions(
    records: EvalRecords,
) -> None:
    set_id = _set_id(records)
    other_set = records.find_or_create_set("r2", "p", _JURY, created_at=_AT).id
    old = _run(records, version="judge-0", set_id=set_id)
    kept = [_run(records, set_id=set_id), _run(records, set_id=set_id)]
    stopped = _run(records, set_id=set_id, status=RunStatus.INCOMPLETE)
    partial = _run(records, set_id=set_id, limited=True)
    elsewhere = _run(records, set_id=other_set)
    untouched = _run(records, "hostile", version="hostile-1")

    found = comparable_runs(records.runs(), suite="judge", set_id=set_id)

    assert [run.id for run in found.runs] == [run.id for run in kept]
    assert found.task_version == "judge-1"
    assert found.others == 4
    assert len(found.other_ids) == 4
    left_out = {old.id, stopped.id, partial.id, elsewhere.id}
    assert left_out.isdisjoint(run.id for run in found.runs)
    assert untouched.id not in {run.id for run in found.runs}


def test_suite_without_labels_compares_without_a_set(records: EvalRecords) -> None:
    first = _run(records, "hostile", version="hostile-1")
    second = _run(records, "hostile", version="hostile-1", model="claude-sdk/opus")

    found = comparable_runs(records.runs(), suite="hostile", set_id=None)

    assert [run.id for run in found.runs] == [first.id, second.id]
    assert found.others == 0


def test_a_suite_with_no_comparable_run_has_no_version(records: EvalRecords) -> None:
    set_id = _set_id(records)
    _run(records, set_id=set_id, status=RunStatus.INCOMPLETE)

    found = comparable_runs(records.runs(), suite="judge", set_id=set_id)

    assert found.runs == ()
    assert found.task_version is None
    assert found.others == 1


def test_agreement_example(store: Store, records: EvalRecords) -> None:
    set_id = _set_id(records)
    ids = _ids(store)
    for posting in ids[:7]:
        _judge_votes(records, set_id, posting, (True, True, True))
    for posting in ids[7:]:
        _judge_votes(records, set_id, posting, (True, False, True))

    assert jury_metrics(records, records.sets()[0]).agreement == pytest.approx(0.7)


def test_agreement_is_none_without_a_complete_pair(
    store: Store, records: EvalRecords
) -> None:
    set_id = _set_id(records)
    _judge_votes(records, set_id, _ids(store)[0], (True, True))

    assert jury_metrics(records, records.sets()[0]).agreement is None


def test_overruled_examples(store: Store, records: EvalRecords) -> None:
    set_id = _set_id(records)
    ids = _ids(store)
    # votes keep, reject, reject; the user keeps: counted
    _judge_votes(records, set_id, ids[0], (True, False, False))
    _label(records, set_id, ids[0], keep=True, origin=LabelOrigin.USER)
    # votes keep, reject, reject; the user rejects: with the majority
    _judge_votes(records, set_id, ids[1], (True, False, False))
    _label(records, set_id, ids[1], keep=False, origin=LabelOrigin.USER)
    # a tie has no majority: not counted
    _judge_votes(records, set_id, ids[2], (True, False))
    _label(records, set_id, ids[2], keep=True, origin=LabelOrigin.USER)
    # votes 72, 65, 40; the user says 65: the median, not counted
    _scores(records, set_id, ids[3], 72, 65, 40)
    _label(records, set_id, ids[3], score=65, origin=LabelOrigin.USER)
    # votes 72, 65, 40; the user says 50: counted
    _scores(records, set_id, ids[4], 72, 65, 40)
    _label(records, set_id, ids[4], score=50, origin=LabelOrigin.USER)
    # an agreed label is nobody's override
    _judge_votes(records, set_id, ids[5], (True, True, True))
    _label(records, set_id, ids[5], keep=True)

    assert jury_metrics(records, records.sets()[0]).overruled == 2


def test_spot_check_example(store: Store, records: EvalRecords) -> None:
    set_id = _set_id(records)
    ids = _ids(store)
    for posting in ids[:9]:
        _label(records, set_id, posting, keep=True, spot_checked=True)
    _label(
        records,
        set_id,
        ids[9],
        keep=False,
        origin=LabelOrigin.USER,
        spot_checked=True,
    )

    assert jury_metrics(records, records.sets()[0]).spot_check == pytest.approx(0.9)


def test_spot_check_is_none_when_none_answered(
    store: Store, records: EvalRecords
) -> None:
    set_id = _set_id(records)
    _label(records, set_id, _ids(store)[0], keep=True)

    assert jury_metrics(records, records.sets()[0]).spot_check is None


def test_report_lists_each_suite_and_leaves_the_jury_runs_out(
    records: EvalRecords,
) -> None:
    set_id = _set_id(records)
    _run(records, set_id=set_id, metrics={"keep_recall": 0.9})
    _run(records, "jury", version="x")
    _run(records, "hostile", version="hostile-1")

    report = build_report(
        records, current=records.sets()[0], labels_ready=True, cap=10.0
    )

    assert [suite.suite for suite in report.suites] == ["judge", "hostile"]
    assert report.suites[0].lines[0].metrics == {"keep_recall": 0.9}
    assert not report.empty


def test_report_of_a_store_with_no_run_is_empty(records: EvalRecords) -> None:
    report = build_report(records, current=None, labels_ready=False, cap=10.0)

    assert report.empty
    assert report.suites == ()


def test_report_gap_under_the_margin_is_noise(records: EvalRecords) -> None:
    set_id = _set_id(records)
    _run(records, set_id=set_id, metrics={"keep_recall": 0.90, "margin": 0.09})
    _run(records, set_id=set_id, metrics={"keep_recall": 0.86, "margin": 0.09})

    report = build_report(
        records, current=records.sets()[0], labels_ready=True, cap=10.0
    )

    assert report.suites[0].noise == pytest.approx(0.09)


def test_report_gap_over_the_margin_is_not_noise(records: EvalRecords) -> None:
    set_id = _set_id(records)
    _run(records, set_id=set_id, metrics={"keep_recall": 0.90, "margin": 0.05})
    _run(records, set_id=set_id, metrics={"keep_recall": 0.70, "margin": 0.05})

    report = build_report(
        records, current=records.sets()[0], labels_ready=True, cap=10.0
    )

    assert report.suites[0].noise is None


def test_report_totals_spend_of_every_run(records: EvalRecords) -> None:
    _run(records, "hostile", version="hostile-1", spent=1.25)
    _run(records, "hostile", version="hostile-1", spent=2.35)

    report = build_report(records, current=None, labels_ready=False, cap=10.0)

    assert report.spent == pytest.approx(3.6)
    assert report.cap == 10.0


def test_report_reads_a_jury_run_in_the_spend_too(records: EvalRecords) -> None:
    _run(records, "jury", version="x", spent=0.5)
    _run(records, "hostile", version="hostile-1", spent=0.25)

    report = build_report(records, current=None, labels_ready=False, cap=10.0)

    assert report.spent == pytest.approx(0.75)


def test_run_detail_reads_speed_and_results_of_any_status(
    records: EvalRecords,
) -> None:
    run = _run(records, status=RunStatus.INCOMPLETE, metrics={"keep_recall": 0.5})
    for number, (passed, millis) in enumerate(
        [(True, 1000), (False, 2000), (True, 3000), (True, 4000)]
    ):
        records.put_result(EvalResult(run.id, f"c{number}", passed, "{}", "", millis))

    detail = run_detail(records, run.id)

    assert detail.run.status is RunStatus.INCOMPLETE
    assert detail.metrics == {"keep_recall": 0.5}
    assert detail.p50_ms == pytest.approx(2500)
    assert detail.p95_ms == pytest.approx(4000)
    assert [result.case_key for result in detail.failed] == ["c1"]


def test_run_detail_of_an_unknown_run_is_refused(records: EvalRecords) -> None:
    with pytest.raises(InvalidValueError, match="42"):
        run_detail(records, 42)


def test_metrics_that_are_not_numbers_are_ignored(records: EvalRecords) -> None:
    run = _run(
        records,
        metrics={"keep_recall": 0.5, "note": "x", "flag": True, "none": None},
    )

    assert run_detail(records, run.id).metrics == {"keep_recall": 0.5}


def test_metrics_that_are_not_json_read_as_empty(records: EvalRecords) -> None:
    run = _run(records, metrics="[not json")

    assert run_detail(records, run.id).metrics == {}
