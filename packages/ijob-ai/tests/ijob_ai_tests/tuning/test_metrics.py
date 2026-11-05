import dspy
import pytest
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import ScoreOut, VerdictOut
from ijob_ai.tuning.metrics import (
    demo_threshold,
    float_metric,
    gepa_metric,
    judge_score,
    scorer_score,
)


def verdict(*, keep: bool, rule: int | None = None, reason: str = "fits") -> VerdictOut:
    return VerdictOut(keep=keep, reason=reason, rule=rule)


def score_out(value: int) -> ScoreOut:
    return ScoreOut(score=value, reasons=["close"])


def test_judge_score_examples() -> None:
    assert judge_score(verdict(keep=True), label=True) == 1
    assert judge_score(verdict(keep=False, rule=1), label=False) == 1
    assert judge_score(verdict(keep=True), label=False) == 0
    assert judge_score(verdict(keep=False, rule=1), label=True) == 0


def test_judge_score_reads_keep_only() -> None:
    odd = verdict(keep=True, rule=3, reason="rejected by rule 3")
    assert judge_score(odd, label=True) == 1


def test_scorer_score_examples() -> None:
    assert scorer_score(score_out(80), 40) == pytest.approx(0.6)
    assert scorer_score(score_out(100), 0) == 0


def test_gepa_metric_returns_score_and_feedback() -> None:
    metric = gepa_metric(Task.JUDGE)
    gold = dspy.Example(label=True)
    pred = dspy.Prediction(verdict=verdict(keep=False, rule=1))
    result = metric(gold, pred, None, "predict", None)
    assert isinstance(result, dspy.Prediction)
    assert result.score == 0
    assert "reject" in result.feedback
    scorer = gepa_metric(Task.SCORER)(
        dspy.Example(label=40), dspy.Prediction(score=score_out(80)), None, None, None
    )
    assert scorer.score == pytest.approx(0.6)
    assert "40" in scorer.feedback


def test_float_metric_returns_a_float() -> None:
    judge = float_metric(Task.JUDGE)(
        dspy.Example(label=True), dspy.Prediction(verdict=verdict(keep=True))
    )
    scorer = float_metric(Task.SCORER)(
        dspy.Example(label=40), dspy.Prediction(score=score_out(80))
    )
    assert type(judge) is float
    assert type(scorer) is float
    assert judge == 1
    assert scorer == pytest.approx(0.6)


def test_demo_thresholds() -> None:
    assert demo_threshold(Task.JUDGE) == 1.0
    assert demo_threshold(Task.SCORER) == 0.8


def test_other_tasks_have_no_metric() -> None:
    with pytest.raises(ValueError, match="no tuning metric"):
        demo_threshold(Task.INSTRUCTIONS)
