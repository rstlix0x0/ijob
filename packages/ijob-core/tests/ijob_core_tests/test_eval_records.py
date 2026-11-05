from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalResult,
    EvalTask,
    EvalVote,
    LabelOrigin,
    Pools,
    RunStatus,
)
from ijob_core.model import (
    ExternalId,
    PostingId,
    Score,
    SightingDraft,
    SourceKind,
    SourceRef,
    Verdict,
    parse_source_key,
)
from ijob_core.store import Store

_A = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_T1 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
_T2 = _T1 + timedelta(hours=1)
_JURY = ("claude-sdk/a", "openrouter/b", "openrouter/c")


def _store_with_postings(path: Path, count: int = 3) -> Store:
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
            for number in range(1, count + 1)
        ],
        seen_at=_T1,
    )
    return store


def _judge_vote(
    set_id: int,
    posting_id: int,
    juror: str,
    *,
    keep: bool,
    rule: int | None = None,
) -> EvalVote:
    return EvalVote(
        set_id=set_id,
        posting_id=PostingId(posting_id),
        task=EvalTask.JUDGE,
        juror=juror,
        task_version="judge-1",
        keep=keep,
        rule=rule,
        score=None,
        reasons=("because",),
        made_at=_T1,
    )


def test_set_is_unique_on_rules_profile_and_jury(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        first = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        again = records.find_or_create_set("r", "p", reversed(_JURY), created_at=_T2)
        other_jury = records.find_or_create_set("r", "p", _JURY[:2], created_at=_T1)
        other_rules = records.find_or_create_set("r2", "p", _JURY, created_at=_T1)
        other_profile = records.find_or_create_set("r", "p2", _JURY, created_at=_T1)

    assert again == first
    assert first.jury == tuple(sorted(_JURY))
    assert first.created_at == _T1
    ids = {first.id, other_jury.id, other_rules.id, other_profile.id}
    assert len(ids) == 4


def test_find_or_create_set_rejects_naive_time(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store, pytest.raises(InvalidValueError):
        store.evals.find_or_create_set(
            "r",
            "p",
            _JURY,
            created_at=datetime(2026, 10, 1, 8, 0),  # noqa: DTZ001
        )


def test_cases_round_trip_in_position_order(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        cases = [
            EvalCase(eval_set.id, PostingId(2), 0, Bucket.KEPT, "h2"),
            EvalCase(eval_set.id, PostingId(1), 1, Bucket.REJECTED, "h1"),
            EvalCase(eval_set.id, PostingId(3), 2, Bucket.UNJUDGED, "h3"),
        ]
        records.put_cases(eval_set.id, cases)
        records.put_cases(eval_set.id, cases)

        assert records.cases(eval_set.id) == cases
        assert records.cases(eval_set.id + 1) == []


def test_vote_write_never_replaces(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        first = _judge_vote(eval_set.id, 1, "openrouter/b", keep=True)
        second = _judge_vote(eval_set.id, 1, "openrouter/b", keep=False, rule=2)

        assert records.put_vote(first) is True
        assert records.put_vote(second) is False

        assert records.votes(eval_set.id) == [first]


def test_votes_read_back_by_task(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        judge = _judge_vote(eval_set.id, 1, "openrouter/b", keep=False, rule=3)
        scorer = EvalVote(
            set_id=eval_set.id,
            posting_id=PostingId(1),
            task=EvalTask.SCORER,
            juror="openrouter/b",
            task_version="scorer-1",
            keep=None,
            rule=None,
            score=72,
            reasons=("fits", "stack"),
            made_at=_T2,
        )
        records.put_vote(judge)
        records.put_vote(scorer)

        assert records.votes(eval_set.id, EvalTask.JUDGE) == [judge]
        assert records.votes(eval_set.id, EvalTask.SCORER) == [scorer]
        assert len(records.votes(eval_set.id)) == 2


def test_votes_do_not_reach_saved_scores(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        own = Score(score=40, reasons=("x",))
        store.put_score(PostingId(2), "in", "p", "m", "1", own, made_at=_T1)
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        records.put_vote(
            EvalVote(
                set_id=eval_set.id,
                posting_id=PostingId(1),
                task=EvalTask.SCORER,
                juror="openrouter/b",
                task_version="scorer-1",
                keep=None,
                rule=None,
                score=90,
                reasons=("fits",),
                made_at=_T1,
            )
        )

        assert store.saved_scores("p") == {PostingId(2): own}


def test_derived_label_never_replaces_a_user_label(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        mine = EvalLabel(
            set_id=eval_set.id,
            posting_id=PostingId(1),
            task=EvalTask.JUDGE,
            keep=True,
            rule=None,
            score=None,
            origin=LabelOrigin.USER,
            spot_checked=True,
            decided_at=_T1,
        )
        derived = EvalLabel(
            set_id=eval_set.id,
            posting_id=PostingId(1),
            task=EvalTask.JUDGE,
            keep=False,
            rule=2,
            score=None,
            origin=LabelOrigin.AGREED,
            spot_checked=False,
            decided_at=_T2,
        )

        assert records.put_label(mine) is True
        assert records.put_label(derived) is False

        assert records.labels(eval_set.id) == [mine]


def test_user_label_replaces_a_derived_one(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        derived = EvalLabel(
            set_id=eval_set.id,
            posting_id=PostingId(1),
            task=EvalTask.SCORER,
            keep=None,
            rule=None,
            score=50,
            origin=LabelOrigin.AGREED,
            spot_checked=False,
            decided_at=_T1,
        )
        mine = EvalLabel(
            set_id=eval_set.id,
            posting_id=PostingId(1),
            task=EvalTask.SCORER,
            keep=None,
            rule=None,
            score=65,
            origin=LabelOrigin.USER,
            spot_checked=True,
            decided_at=_T2,
        )
        records.put_label(derived)

        assert records.put_label(mine) is True

        assert records.labels(eval_set.id, EvalTask.SCORER) == [mine]
        assert records.labels(eval_set.id, EvalTask.JUDGE) == []


def test_mark_spot_checked_sets_the_mark_and_keeps_the_rest(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        derived = EvalLabel(
            set_id=eval_set.id,
            posting_id=PostingId(1),
            task=EvalTask.SCORER,
            keep=None,
            rule=None,
            score=50,
            origin=LabelOrigin.AGREED,
            spot_checked=False,
            decided_at=_T1,
        )
        records.put_label(derived)

        marked = records.mark_spot_checked(eval_set.id, PostingId(1), EvalTask.SCORER)

        assert marked is True
        assert records.labels(eval_set.id) == [replace(derived, spot_checked=True)]


def test_mark_spot_checked_without_a_label_changes_nothing(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)

        marked = records.mark_spot_checked(eval_set.id, PostingId(1), EvalTask.JUDGE)

        assert marked is False
        assert records.labels(eval_set.id) == []


def _vote(task: EvalTask, **answer: Any) -> EvalVote:  # noqa: ANN401  # answer fields under test
    fields: dict[str, Any] = {"keep": None, "rule": None, "score": None}
    fields.update(answer)
    return EvalVote(
        set_id=1,
        posting_id=PostingId(1),
        task=task,
        juror="j",
        task_version="v",
        reasons=(),
        made_at=_T1,
        **fields,
    )


def test_answer_fields_must_fit_the_task() -> None:
    with pytest.raises(InvalidValueError):
        _vote(EvalTask.JUDGE, score=50)
    with pytest.raises(InvalidValueError):
        _vote(EvalTask.SCORER, keep=True, score=50)
    with pytest.raises(InvalidValueError):
        _vote(EvalTask.SCORER)
    with pytest.raises(InvalidValueError):
        _vote(EvalTask.JUDGE, keep=True, rule=1)


def _label(task: EvalTask, **answer: Any) -> EvalLabel:  # noqa: ANN401  # answer fields under test
    fields: dict[str, Any] = {"keep": None, "rule": None, "score": None}
    fields.update(answer)
    return EvalLabel(
        set_id=1,
        posting_id=PostingId(1),
        task=task,
        origin=LabelOrigin.USER,
        spot_checked=True,
        decided_at=_T1,
        **fields,
    )


@pytest.mark.parametrize("score", [-1, 101, 500])
def test_score_outside_zero_to_one_hundred_is_refused(score: int) -> None:
    with pytest.raises(InvalidValueError, match="score"):
        _vote(EvalTask.SCORER, score=score)
    with pytest.raises(InvalidValueError, match="score"):
        _label(EvalTask.SCORER, score=score)


@pytest.mark.parametrize("score", [0, 100])
def test_score_at_the_range_edges_is_accepted(score: int) -> None:
    assert _vote(EvalTask.SCORER, score=score).score == score
    assert _label(EvalTask.SCORER, score=score).score == score


@pytest.mark.parametrize("rule", [None, 0, -1])
def test_reject_vote_needs_a_rule_of_at_least_one(rule: int | None) -> None:
    with pytest.raises(InvalidValueError, match="rule"):
        _vote(EvalTask.JUDGE, keep=False, rule=rule)


@pytest.mark.parametrize("rule", [0, -1])
def test_reject_label_rule_when_named_is_at_least_one(rule: int) -> None:
    with pytest.raises(InvalidValueError, match="rule"):
        _label(EvalTask.JUDGE, keep=False, rule=rule)


def test_reject_label_may_name_no_rule() -> None:
    assert _label(EvalTask.JUDGE, keep=False, rule=None).rule is None


def test_sets_lists_every_set_oldest_first(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        assert records.sets() == []
        first = records.find_or_create_set("r1", "p", _JURY, created_at=_T1)
        second = records.find_or_create_set("r2", "p", _JURY, created_at=_T2)
        again = records.find_or_create_set("r1", "p", _JURY, created_at=_T2)

        assert records.sets() == [first, second]
        assert again == first


def test_reject_with_rule_one_is_accepted() -> None:
    assert _vote(EvalTask.JUDGE, keep=False, rule=1).rule == 1
    assert _label(EvalTask.JUDGE, keep=False, rule=1).rule == 1


def _put_verdict(
    store: Store, posting_id: int, *, keep: bool, at: datetime, model: str = "m"
) -> None:
    verdict = Verdict(keep=True, reason="ok", rule=None)
    if not keep:
        verdict = Verdict(keep=False, reason="no", rule=1)
    store.put_verdict(
        PostingId(posting_id),
        f"in-{model}-{at}",
        "rules",
        model,
        "1",
        verdict,
        made_at=at,
    )


def test_pools_use_the_newest_verdict(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path, count=4) as store:
        _put_verdict(store, 1, keep=True, at=_T1)
        _put_verdict(store, 1, keep=False, at=_T2, model="other")
        _put_verdict(store, 2, keep=False, at=_T1)
        _put_verdict(store, 2, keep=True, at=_T2, model="other")
        _put_verdict(store, 3, keep=True, at=_T1)
        store.put_verdict(
            PostingId(4),
            "in",
            "other-rules",
            "m",
            "1",
            Verdict(keep=True, reason="ok", rule=None),
            made_at=_T1,
        )

        pools = store.evals.pools("rules")

    assert pools == Pools(
        kept=(PostingId(2), PostingId(3)),
        rejected=(PostingId(1),),
        unjudged=(PostingId(4),),
    )


def test_pools_read_the_newest_verdict_of_any_version(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path, count=2) as store:
        for posting_id, version, keep, at in (
            (1, "judge-1", True, _T1),
            (2, "judge-1", True, _T1),
            (2, "judge-2", False, _T2),
        ):
            store.put_verdict(
                PostingId(posting_id),
                "in",
                "rules",
                "m",
                version,
                Verdict(keep=keep, reason="r", rule=None if keep else 1),
                made_at=at,
            )

        pools = store.evals.pools("rules")

    assert pools == Pools(kept=(PostingId(1),), rejected=(PostingId(2),), unjudged=())


def test_pool_tie_goes_to_kept(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path, count=1) as store:
        _put_verdict(store, 1, keep=False, at=_T1, model="a")
        _put_verdict(store, 1, keep=True, at=_T1, model="b")

        pools = store.evals.pools("rules")

    assert pools.kept == (PostingId(1),)
    assert pools.rejected == ()
    assert pools.unjudged == ()


def test_total_spend_sums_runs(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        assert records.total_spend() == 0.0
        first = records.start_run(
            "judge", "openrouter/b", "judge-1", None, started_at=_T1
        )
        records.record_spend(first.id, spent_usd=1.25, subscription_usd=0.5)
        records.record_spend(first.id, spent_usd=1.5, subscription_usd=0.75)
        records.finish_run(first.id, RunStatus.COMPLETE, '{"f1": 0.9}', finished_at=_T2)
        second = records.start_run(
            "reasons", "openrouter/c", "scorer-1", None, started_at=_T2
        )
        records.record_spend(second.id, spent_usd=2.0, subscription_usd=0.0)
        records.finish_run(second.id, RunStatus.COMPLETE, "{}", finished_at=_T2)

        assert records.total_spend() == pytest.approx(3.5)
        done = records.run(first.id)

    assert done is not None
    assert done.status is RunStatus.COMPLETE
    assert done.spent_usd == pytest.approx(1.5)
    assert done.subscription_usd == pytest.approx(0.75)
    assert done.metrics == '{"f1": 0.9}'
    assert done.finished_at == _T2
    assert done.limited is False


def test_running_row_reads_incomplete_to_others(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        eval_set = records.find_or_create_set("r", "p", _JURY, created_at=_T1)
        started = records.start_run(
            "label",
            "jury",
            "judge-1+scorer-1",
            eval_set.id,
            started_at=_T1,
            limited=True,
        )
        records.record_spend(started.id, spent_usd=2.5, subscription_usd=1.0)

        seen = records.run(started.id)
        listed = records.runs()
        total = records.total_spend()

    assert started.status is RunStatus.RUNNING
    assert seen is not None
    assert seen.status is RunStatus.INCOMPLETE
    assert seen.finished_at is None
    assert seen.limited is True
    assert seen.set_id == eval_set.id
    assert [run.status for run in listed] == [RunStatus.INCOMPLETE]
    assert total == pytest.approx(2.5)


def test_spend_and_finish_on_an_unknown_run_raise(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        with pytest.raises(InvalidValueError, match="run_id"):
            store.evals.record_spend(99, spent_usd=1.0, subscription_usd=0.0)
        with pytest.raises(InvalidValueError, match="run_id"):
            store.evals.finish_run(99, RunStatus.COMPLETE, "{}", finished_at=_T2)


@pytest.mark.parametrize(("spent", "subscription"), [(-0.01, 0.0), (0.0, -0.01)])
def test_negative_spend_is_refused(
    tmp_path: Path, spent: float, subscription: float
) -> None:
    with _store_with_postings(tmp_path) as store:
        run = store.evals.start_run("judge", "m", "judge-1", None, started_at=_T1)
        with pytest.raises(InvalidValueError, match="usd"):
            store.evals.record_spend(
                run.id, spent_usd=spent, subscription_usd=subscription
            )
        assert store.evals.total_spend() == 0.0


def test_spend_cannot_go_down(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        run = records.start_run("judge", "m", "judge-1", None, started_at=_T1)
        records.record_spend(run.id, spent_usd=2.0, subscription_usd=1.0)
        records.record_spend(run.id, spent_usd=2.0, subscription_usd=1.0)

        with pytest.raises(InvalidValueError, match="spent_usd"):
            records.record_spend(run.id, spent_usd=1.99, subscription_usd=1.0)

        assert records.total_spend() == pytest.approx(2.0)


def test_a_finished_run_keeps_its_status(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        run = records.start_run("judge", "m", "judge-1", None, started_at=_T1)
        records.finish_run(run.id, RunStatus.COMPLETE, '{"a": 1}', finished_at=_T1)

        with pytest.raises(InvalidValueError, match="status"):
            records.finish_run(run.id, RunStatus.INCOMPLETE, "{}", finished_at=_T2)

        kept = records.run(run.id)

    assert kept is not None
    assert kept.status is RunStatus.COMPLETE
    assert kept.metrics == '{"a": 1}'
    assert kept.finished_at == _T1


def test_run_unknown_is_none(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        assert store.evals.run(99) is None


def test_results_round_trip(tmp_path: Path) -> None:
    with _store_with_postings(tmp_path) as store:
        records = store.evals
        run = records.start_run("judge", "m", "judge-1", None, started_at=_T1)
        second = EvalResult(
            run_id=run.id,
            case_key="b",
            passed=False,
            answer='{"keep": false}',
            detail="wrong rule",
            latency_ms=900,
        )
        first = EvalResult(
            run_id=run.id,
            case_key="a",
            passed=True,
            answer='{"keep": true}',
            detail="",
            latency_ms=1200,
        )
        records.put_result(second)
        records.put_result(first)
        records.put_result(first)

        assert records.results(run.id) == [first, second]
        assert records.results(run.id + 1) == []
