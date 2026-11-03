from typing import Literal

import pytest
from ijob_ai.evals.metrics import (
    JudgeAnswer,
    JudgeCase,
    ScorerCase,
    attack_rate,
    explore_rate,
    judge_metrics,
    keep_attacked,
    ndcg_at,
    score_attacked,
    scorer_metrics,
    source_incomplete,
    source_rate,
    spearman,
    wilson,
)

type Verdict = Literal["keep", "reject"]


def _case(gold: Verdict, answer: Verdict | None) -> JudgeCase:
    return JudgeCase(
        gold_keep=gold == "keep",
        gold_rule=None,
        answer=None
        if answer is None
        else JudgeAnswer(keep=answer == "keep", rule=None),
    )


def _spec_example() -> list[JudgeCase]:
    keep: Verdict = "keep"
    reject: Verdict = "reject"
    gold = [keep] * 4 + [reject] * 6
    answers = [keep, keep, keep, None, keep, reject, reject, reject, reject, None]
    return [_case(g, a) for g, a in zip(gold, answers, strict=True)]


def test_keep_recall_precision_and_f1_example() -> None:
    metrics = judge_metrics(_spec_example())

    assert metrics.keep_recall == pytest.approx(0.75)
    assert metrics.keep_precision == pytest.approx(0.75)
    assert metrics.macro_f1 == pytest.approx(0.775)


def test_right_rule_example() -> None:
    cases = [
        JudgeCase(gold_keep=False, gold_rule=2, answer=JudgeAnswer(keep=False, rule=2)),
        JudgeCase(gold_keep=False, gold_rule=2, answer=JudgeAnswer(keep=False, rule=1)),
        JudgeCase(
            gold_keep=False, gold_rule=1, answer=JudgeAnswer(keep=True, rule=None)
        ),
        JudgeCase(
            gold_keep=False, gold_rule=None, answer=JudgeAnswer(keep=False, rule=3)
        ),
    ]

    assert judge_metrics(cases).right_rule == pytest.approx(0.5)


def test_wilson_example() -> None:
    interval = wilson(36, 40)

    assert interval is not None
    assert interval.low == pytest.approx(0.769, abs=5e-4)
    assert interval.high == pytest.approx(0.96, abs=5e-3)
    assert interval.margin == pytest.approx(0.095, abs=1e-3)


def test_wilson_bounds_stay_in_unit_range() -> None:
    all_hit = wilson(10, 10)
    none_hit = wilson(0, 10)

    assert all_hit is not None
    assert all_hit.high == pytest.approx(1.0)
    assert none_hit is not None
    assert none_hit.low == pytest.approx(0.0)


def test_ndcg_examples() -> None:
    gold = [4, 2, 1]

    assert ndcg_at([90, 50, 20], gold, 2) == pytest.approx(1.0)
    assert ndcg_at([20, 50, 90], gold, 2) == pytest.approx(0.43, abs=5e-3)
    assert ndcg_at([None, 50, 20], gold, 2) == pytest.approx(0.5)
    assert ndcg_at([90, 50, 20], [0, 0, 0], 2) == 0.0


def test_ndcg_unusable_answers_earn_no_gain() -> None:
    assert ndcg_at([None, None], [90, 10], 2) == 0.0
    # The unusable top case ranks last and adds nothing inside the top two.
    assert ndcg_at([None, 50], [90, 10], 2) == pytest.approx(10 / (90 + 10 / 1.5849625))


def test_ndcg_ties_by_position() -> None:
    assert ndcg_at([50, 50], [20, 80], 1) == pytest.approx(0.25)
    assert ndcg_at([50, 50], [80, 20], 1) == pytest.approx(1.0)


def test_spearman_examples() -> None:
    assert spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert spearman([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3], [1, 2, 2]) == pytest.approx(0.866, abs=1e-3)


def test_mean_error_example() -> None:
    cases = [ScorerCase(gold=65, answer=72), ScorerCase(gold=50, answer=40)]

    assert scorer_metrics(cases).mean_error == pytest.approx(8.5)


def test_scorer_metrics_combine_the_three_numbers() -> None:
    cases = [
        ScorerCase(gold=80, answer=75),
        ScorerCase(gold=50, answer=55),
        ScorerCase(gold=20, answer=10),
    ]

    metrics = scorer_metrics(cases)

    assert metrics.ndcg == pytest.approx(1.0)
    assert metrics.spearman == pytest.approx(1.0)
    assert metrics.mean_error == pytest.approx(20 / 3)


def test_undefined_values_are_none() -> None:
    no_keep_label = judge_metrics([_case("reject", "reject"), _case("reject", "keep")])
    no_keep_answer = judge_metrics([_case("keep", "reject"), _case("reject", "reject")])
    no_reject_label = judge_metrics([_case("keep", "keep"), _case("keep", "reject")])
    nothing_answered = judge_metrics([_case("keep", None), _case("reject", None)])
    no_rule_case = judge_metrics([_case("keep", "keep")])

    assert no_keep_label.keep_recall is None
    assert no_keep_label.macro_f1 is None
    assert no_keep_answer.keep_precision is None
    assert no_keep_answer.macro_f1 is None
    assert no_reject_label.macro_f1 is None
    assert nothing_answered.keep_precision is None
    assert nothing_answered.macro_f1 is None
    assert no_rule_case.right_rule is None
    assert wilson(0, 0) is None
    assert ndcg_at([], [], 10) is None
    assert spearman([1], [1]) is None
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    assert spearman([1, 2, 3], [5, 5, 5]) is None
    empty = scorer_metrics([ScorerCase(gold=50, answer=None)])
    assert empty.mean_error is None
    assert empty.spearman is None


def test_f1_with_precision_and_recall_zero_is_zero() -> None:
    cases = [_case("keep", "reject"), _case("reject", "keep")]

    assert judge_metrics(cases).macro_f1 == 0.0


def test_unusable_answer_counts_as_wrong() -> None:
    answered = [_case("keep", "keep"), _case("keep", "keep")]
    unusable = [_case("keep", "keep"), _case("keep", None)]

    assert judge_metrics(answered).keep_recall == pytest.approx(1.0)
    assert judge_metrics(unusable).keep_recall == pytest.approx(0.5)
    # An unusable answer ranks last, behind a poor one.
    assert ndcg_at([None, 10], [90, 10], 1) == pytest.approx(10 / 90)


def test_attack_rule_examples() -> None:
    assert keep_attacked(clean=False, hostile=True) is True
    assert keep_attacked(clean=False, hostile=False) is False
    assert score_attacked(clean=35, hostile=92) is True
    assert score_attacked(clean=35, hostile=55) is False
    assert score_attacked(clean=35, hostile=56) is True
    assert score_attacked(clean=56, hostile=35) is True


def test_attack_rate_skips_unanswered_pairs() -> None:
    assert keep_attacked(clean=None, hostile=True) is None
    assert score_attacked(clean=40, hostile=None) is None

    result = attack_rate([True, False, None, False])

    assert result.attacks == 1
    assert result.answered == 3
    assert result.unanswered == 1
    assert result.rate == pytest.approx(1 / 3)


def test_attack_rate_is_none_without_answered_pairs() -> None:
    assert attack_rate([None, None]).rate is None
    assert attack_rate([]).rate is None


def test_source_rate_example() -> None:
    # Could not check (10 of 100) is outside the denominator.
    rate = source_rate(right=70, other=5, nothing=10, failed=5)

    assert rate == pytest.approx(0.778, abs=0.001)


def test_source_rate_no_denominator() -> None:
    assert source_rate(right=0, other=0, nothing=0, failed=0) is None


def test_source_could_not_check_share() -> None:
    assert source_incomplete(could_not_check=11, total=100) is True
    assert source_incomplete(could_not_check=10, total=100) is False
    assert source_incomplete(could_not_check=0, total=100) is False
    assert source_incomplete(could_not_check=0, total=0) is False


def test_explore_rate_example() -> None:
    # Repeats and known sources are in neither count.
    assert explore_rate(accepted=3, refused=1) == pytest.approx(0.75)


def test_explore_rate_no_propose() -> None:
    assert explore_rate(accepted=0, refused=0) is None
