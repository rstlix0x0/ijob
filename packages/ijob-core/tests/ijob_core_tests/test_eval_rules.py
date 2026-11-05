from datetime import UTC, datetime

import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import Bucket, EvalTask, EvalVote, Pools
from ijob_core.eval_rules import (
    Agreed,
    NeedsReview,
    ReviewReason,
    Split,
    SpotCandidate,
    add_split,
    draw_cases,
    draw_more,
    judge_label,
    limit_split,
    rejection_rule,
    score_label,
    spot_order,
    spot_per_task,
)
from ijob_core.model import PostingId

_MOMENT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _pools(kept: int, rejected: int, unjudged: int) -> Pools:
    ids = iter(range(1, kept + rejected + unjudged + 1))
    return Pools(
        kept=tuple(PostingId(next(ids)) for _ in range(kept)),
        rejected=tuple(PostingId(next(ids)) for _ in range(rejected)),
        unjudged=tuple(PostingId(next(ids)) for _ in range(unjudged)),
    )


def _counts(picks: list[tuple[PostingId, Bucket]]) -> tuple[int, int, int]:
    buckets = [bucket for _, bucket in picks]
    return (
        buckets.count(Bucket.KEPT),
        buckets.count(Bucket.REJECTED),
        buckets.count(Bucket.UNJUDGED),
    )


def test_draw_is_stable_per_hashes() -> None:
    pools = _pools(100, 100, 100)

    first = draw_cases("rules-a", "profile-a", pools)

    assert draw_cases("rules-a", "profile-a", pools) == first
    assert draw_cases("rules-b", "profile-a", pools) != first
    assert draw_cases("rules-a", "profile-b", pools) != first


def test_draw_counts() -> None:
    picks = draw_cases("r", "p", _pools(100, 100, 100))

    assert _counts(picks) == (40, 40, 20)
    assert len({posting_id for posting_id, _ in picks}) == 100


def test_draw_orders_by_pool_and_draws_from_the_right_pool() -> None:
    pools = _pools(50, 50, 50)

    picks = draw_cases("r", "p", pools)

    assert [bucket for _, bucket in picks] == (
        [Bucket.KEPT] * 40 + [Bucket.REJECTED] * 40 + [Bucket.UNJUDGED] * 20
    )
    for posting_id, bucket in picks:
        assert (
            posting_id
            in {
                Bucket.KEPT: pools.kept,
                Bucket.REJECTED: pools.rejected,
                Bucket.UNJUDGED: pools.unjudged,
            }[bucket]
        )


def test_draw_tops_up_from_unjudged() -> None:
    picks = draw_cases("r", "p", _pools(12, 100, 100))

    assert _counts(picks) == (12, 40, 48)


def test_draw_takes_what_exists_when_pools_are_small() -> None:
    picks = draw_cases("r", "p", _pools(3, 2, 4))

    assert _counts(picks) == (3, 2, 4)


def test_draw_of_nothing_is_empty() -> None:
    assert draw_cases("r", "p", _pools(0, 0, 0)) == []


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, (0, 0, 1)),
        (2, (1, 1, 0)),
        (3, (1, 1, 1)),
        (5, (2, 2, 1)),
        (10, (4, 4, 2)),
        (25, (10, 10, 5)),
        (100, (40, 40, 20)),
    ],
)
def test_limit_split_table(n: int, expected: tuple[int, int, int]) -> None:
    assert limit_split(n, Split(100, 100, 100)) == Split(*expected)


@pytest.mark.parametrize("n", [0, -1, 101])
def test_limit_split_rejects_n_outside_range(n: int) -> None:
    with pytest.raises(InvalidValueError) as caught:
        limit_split(n, Split(100, 100, 100))

    assert caught.value.field == "limit"


def test_limit_short_bucket_uses_unjudged() -> None:
    assert limit_split(10, Split(1, 100, 100)) == Split(1, 4, 5)
    assert limit_split(10, Split(100, 0, 100)) == Split(4, 0, 6)


def test_limit_takes_what_exists() -> None:
    assert limit_split(10, Split(1, 1, 2)) == Split(1, 1, 2)


def test_limit_tops_up_from_any_pool_with_postings_left() -> None:
    assert limit_split(1, Split(5, 5, 0)) == Split(1, 0, 0)
    assert limit_split(1, Split(0, 5, 0)) == Split(0, 1, 0)
    assert limit_split(10, Split(100, 100, 0)) == Split(6, 4, 0)
    assert limit_split(10, Split(2, 3, 0)) == Split(2, 3, 0)


@pytest.mark.parametrize("n", [1, 2, 3, 5, 10, 25, 99, 100])
@pytest.mark.parametrize(
    "available",
    [
        Split(0, 0, 100),
        Split(100, 0, 0),
        Split(0, 100, 0),
        Split(3, 2, 1),
        Split(50, 0, 7),
    ],
)
def test_limit_chooses_n_whenever_n_exist(n: int, available: Split) -> None:
    got = limit_split(n, available)
    total = available.kept + available.rejected + available.unjudged

    assert got.kept + got.rejected + got.unjudged == min(n, total)
    assert got.kept <= available.kept
    assert got.rejected <= available.rejected
    assert got.unjudged <= available.unjudged


def _judge(
    *, keep: bool, rule: int | None = None, juror: str = "j", posting: int = 1
) -> EvalVote:
    return EvalVote(
        set_id=1,
        posting_id=PostingId(posting),
        task=EvalTask.JUDGE,
        juror=juror,
        task_version="judge-1",
        keep=keep,
        rule=rule,
        score=None,
        reasons=(),
        made_at=_MOMENT,
    )


def _keep() -> EvalVote:
    return _judge(keep=True)


def _reject(rule: int) -> EvalVote:
    return _judge(keep=False, rule=rule)


def _scorer(score: int, juror: str = "j") -> EvalVote:
    return EvalVote(
        set_id=1,
        posting_id=PostingId(1),
        task=EvalTask.SCORER,
        juror=juror,
        task_version="scorer-1",
        keep=None,
        rule=None,
        score=score,
        reasons=(),
        made_at=_MOMENT,
    )


def test_judge_label_table() -> None:
    keep = _keep()

    assert judge_label([keep, keep, keep], 3) == Agreed(
        keep=True, rule=None, score=None
    )
    assert judge_label([_reject(2), _reject(2), _reject(1)], 3) == (
        Agreed(keep=False, rule=2, score=None)
    )
    assert judge_label([_reject(1), _reject(2), _reject(3)], 3) == (
        Agreed(keep=False, rule=None, score=None)
    )
    assert judge_label([keep, _reject(2), _reject(2)], 3) == NeedsReview(
        ReviewReason.SPLIT
    )
    assert judge_label([_reject(2), _reject(2)], 3) == NeedsReview(
        ReviewReason.FEWER_VOTES
    )


def test_judge_label_refuses_scorer_votes() -> None:
    with pytest.raises(InvalidValueError) as caught:
        judge_label([_scorer(50)], 1)

    assert caught.value.field == "task"


def test_score_label_refuses_judge_votes() -> None:
    with pytest.raises(InvalidValueError) as caught:
        score_label([_keep()], 1)

    assert caught.value.field == "task"


def test_judge_label_without_votes_stays_unlabelled() -> None:
    assert judge_label([], 3) is None


def test_judge_label_with_one_juror() -> None:
    assert judge_label([_reject(3)], 1) == Agreed(keep=False, rule=3, score=None)


def test_judge_label_fewer_votes_wins_over_split() -> None:
    assert judge_label([_keep(), _reject(1)], 3) == NeedsReview(
        ReviewReason.FEWER_VOTES
    )


def test_judge_label_rule_needs_a_majority_of_rejecting_votes() -> None:
    votes = [_reject(1), _reject(1), _reject(2), _reject(2)]

    assert judge_label(votes, 4) == Agreed(keep=False, rule=None, score=None)


def test_rejection_rule() -> None:
    assert rejection_rule([2, 2]) == 2
    assert rejection_rule([2, 2, 1]) == 2
    assert rejection_rule([1, 2, 3]) is None
    assert rejection_rule([1, 2]) is None
    assert rejection_rule([4]) == 4
    assert rejection_rule([]) is None


def test_score_label_table() -> None:
    def votes(*scores: int) -> list[EvalVote]:
        return [_scorer(score) for score in scores]

    assert score_label(votes(72, 65, 40), 3) == NeedsReview(ReviewReason.SPREAD)
    assert score_label(votes(60, 50, 40), 3) == Agreed(None, None, 50)
    assert score_label(votes(61, 50, 40), 3) == NeedsReview(ReviewReason.SPREAD)
    assert score_label(votes(50, 50, 50), 3) == Agreed(None, None, 50)
    assert score_label(votes(70, 30), 3) == NeedsReview(ReviewReason.FEWER_VOTES)


def test_score_label_is_the_median_low() -> None:
    assert score_label([_scorer(50), _scorer(60)], 2) == Agreed(None, None, 50)
    assert score_label([_scorer(55), _scorer(40), _scorer(45)], 3) == Agreed(
        None, None, 45
    )


def test_score_label_without_votes_stays_unlabelled() -> None:
    assert score_label([], 3) is None


def _candidates(judge: int, scorer: int) -> list[SpotCandidate]:
    return [
        SpotCandidate(PostingId(n), EvalTask.JUDGE) for n in range(1, judge + 1)
    ] + [SpotCandidate(PostingId(n), EvalTask.SCORER) for n in range(1, scorer + 1)]


def test_spot_order_prefers_five_per_task() -> None:
    order = spot_order("seed", _candidates(30, 30), 5)

    assert len(order) == 10
    assert [c.task for c in order].count(EvalTask.JUDGE) == 5
    assert [c.task for c in order].count(EvalTask.SCORER) == 5


def test_spot_order_fills_from_the_other_task_when_one_is_short() -> None:
    order = spot_order("seed", _candidates(2, 30), 5)

    assert len(order) == 10
    assert [c.task for c in order].count(EvalTask.JUDGE) == 2
    assert [c.task for c in order].count(EvalTask.SCORER) == 8


def test_spot_order_offers_fewer_than_ten_when_fewer_exist() -> None:
    assert len(spot_order("seed", _candidates(3, 4), 5)) == 7


def test_spot_order_counts_labels_already_checked() -> None:
    order = spot_order(
        "seed",
        _candidates(30, 30),
        5,
        checked=[EvalTask.JUDGE] * 5 + [EvalTask.SCORER] * 2,
    )

    assert len(order) == 3
    assert [c.task for c in order] == [EvalTask.SCORER] * 3


def test_spot_order_is_empty_once_ten_are_checked() -> None:
    assert (
        spot_order("seed", _candidates(30, 30), 5, checked=[EvalTask.JUDGE] * 10) == []
    )


def test_spot_order_depends_on_the_seed() -> None:
    assert spot_order("a", _candidates(30, 30), 5) != spot_order(
        "b", _candidates(30, 30), 5
    )


def test_spot_order_holds_as_labels_grow() -> None:
    small = _candidates(30, 30)
    grown = small + [
        SpotCandidate(PostingId(n), task)
        for n in range(31, 61)
        for task in (EvalTask.JUDGE, EvalTask.SCORER)
    ]

    before = spot_order("seed", small, 5, checked=[])
    after = spot_order("seed", grown, 5, checked=[])

    both = [c for c in before if c in after]
    assert both == [c for c in after if c in before]
    assert len(both) >= 2


def test_spot_order_resumes_after_a_quit() -> None:
    candidates = _candidates(30, 30)
    first = spot_order("seed", candidates, 5)

    answered = first[:3]
    rest = [c for c in candidates if c not in answered]
    resumed = spot_order("seed", rest, 5, checked=[c.task for c in answered])

    assert resumed == first[3:]


@pytest.mark.parametrize(
    ("n", "available", "expected"),
    [
        (100, (118, 515, 28404), (80, 10, 10)),
        (5, (118, 515, 28404), (4, 1, 0)),
        (2, (118, 515, 28404), (2, 0, 0)),
        (1, (118, 515, 28404), (1, 0, 0)),
        (100, (30, 50, 500), (30, 10, 60)),
        (100, (30, 50, 5), (30, 50, 5)),
    ],
)
def test_add_split_rows(
    n: int, available: tuple[int, int, int], expected: tuple[int, int, int]
) -> None:
    assert add_split(n, Split(*available)) == Split(*expected)


@pytest.mark.parametrize("n", [0, 101])
def test_add_split_range(n: int) -> None:
    with pytest.raises(InvalidValueError, match="1 to 100"):
        add_split(n, Split(100, 100, 100))


def test_draw_more_is_seeded() -> None:
    pools = _pools(200, 200, 200)

    first = draw_more("rules", "profile", 100, pools, 50)

    assert draw_more("rules", "profile", 100, pools, 50) == first
    assert draw_more("rules", "profile", 150, pools, 50) != first
    assert _counts(first) == (40, 5, 5)
    assert len({posting_id for posting_id, _ in first}) == 50


@pytest.mark.parametrize(
    ("cases", "expected"), [(100, 5), (119, 5), (120, 6), (200, 10)]
)
def test_spot_per_task_counts(cases: int, expected: int) -> None:
    assert spot_per_task(cases) == expected


def test_spot_order_takes_more_for_a_larger_set() -> None:
    order = spot_order(
        "seed",
        _candidates(30, 30),
        10,
        checked=[EvalTask.JUDGE] * 5 + [EvalTask.SCORER] * 5,
    )

    assert len(order) == 10
    assert [c.task for c in order].count(EvalTask.JUDGE) == 5
    assert [c.task for c in order].count(EvalTask.SCORER) == 5
