"""Scores for the judge and scorer tasks, in the shape each optimizer wants.

Every metric reads its answer from the prediction and its label from the
example's `label` field. This module imports DSPy only for `Prediction`, so
nothing heavier loads with it.
"""

from collections.abc import Callable
from typing import Any, Final

import dspy

from ijob_ai.llm.settings import Task
from ijob_ai.tasks.outputs import ScoreOut, VerdictOut

JUDGE_DEMO_THRESHOLD: Final = 1.0
SCORER_DEMO_THRESHOLD: Final = 0.8
_SCORE_RANGE: Final = 100

type FloatMetric = Callable[..., float]
type GepaMetric = Callable[..., dspy.Prediction]


def judge_score(answer: VerdictOut, label: bool) -> float:  # noqa: FBT001  # a score is a plain comparison of two booleans
    """Score a verdict against the label, reading `keep` only.

    Args:
        answer: The model's verdict.
        label: The labelled keep.

    Returns:
        1.0 when `keep` matches the label, else 0.0.
    """
    return 1.0 if answer.keep == label else 0.0


def scorer_score(answer: ScoreOut, label: int) -> float:
    """Score a fit score against the label by how far apart they are.

    Args:
        answer: The model's score.
        label: The labelled score, 0 to 100.

    Returns:
        `1 - abs(answer - label) / 100`.
    """
    return 1 - abs(answer.score - label) / _SCORE_RANGE


def _score_of(task: Task, gold: Any, pred: Any) -> float:  # noqa: ANN401  # dspy examples and predictions are untyped
    """Score `pred` against `gold` for `task`, or raise `ValueError`."""
    if task is Task.JUDGE:
        return judge_score(pred.verdict, gold.label)
    if task is Task.SCORER:
        return scorer_score(pred.score, gold.label)
    msg = f"{task.value} has no tuning metric"
    raise ValueError(msg)


def _feedback(task: Task, gold: Any, pred: Any, score: float) -> str:  # noqa: ANN401  # dspy examples and predictions are untyped
    """Say in words how `pred` compares with the label."""
    if task is Task.JUDGE:
        verdict = "keep" if pred.verdict.keep else "reject"
        label = "keep" if gold.label else "reject"
        if score == 1.0:
            return f"Correct: the posting should be {label}."
        return f"Wrong: the answer was {verdict} but the posting should be {label}."
    return (
        f"The score was {pred.score.score} and the right score is {gold.label}, "
        f"{abs(pred.score.score - gold.label)} points apart."
    )


def gepa_metric(task: Task) -> GepaMetric:
    """Build the five-argument metric GEPA calls.

    Args:
        task: `Task.JUDGE` or `Task.SCORER`.

    Returns:
        A function of `(gold, pred, trace, pred_name, pred_trace)` giving a
        `Prediction` with `score` and `feedback`.

    Raises:
        ValueError: If `task` has no tuning metric.
    """
    demo_threshold(task)

    def metric(
        gold: Any,  # noqa: ANN401  # dspy examples are untyped
        pred: Any,  # noqa: ANN401  # dspy predictions are untyped
        trace: Any = None,  # noqa: ANN401, ARG001  # part of the five-argument contract
        pred_name: str | None = None,  # noqa: ARG001  # part of the five-argument contract
        pred_trace: Any = None,  # noqa: ANN401, ARG001  # part of the five-argument contract
    ) -> dspy.Prediction:
        score = _score_of(task, gold, pred)
        return dspy.Prediction(score=score, feedback=_feedback(task, gold, pred, score))

    return metric


def float_metric(task: Task) -> FloatMetric:
    """Build the metric the other optimizers call, giving a plain float.

    A `Prediction` is always truthy to Bootstrap, so only a float can fail a
    demo.

    Args:
        task: `Task.JUDGE` or `Task.SCORER`.

    Returns:
        A function of `(gold, pred, trace)` giving the score.

    Raises:
        ValueError: If `task` has no tuning metric.
    """
    demo_threshold(task)

    def metric(
        gold: Any,  # noqa: ANN401  # dspy examples are untyped
        pred: Any,  # noqa: ANN401  # dspy predictions are untyped
        trace: Any = None,  # noqa: ANN401, ARG001  # part of the metric contract
    ) -> float:
        return _score_of(task, gold, pred)

    return metric


def demo_threshold(task: Task) -> float:
    """Return the score a bootstrapped example must reach to become a demo.

    Args:
        task: `Task.JUDGE` or `Task.SCORER`.

    Returns:
        1.0 for the judge, so a wrong answer never becomes a demo, and 0.8 for
        the scorer, the 20 points its suite allows.

    Raises:
        ValueError: If `task` has no tuning metric.
    """
    if task is Task.JUDGE:
        return JUDGE_DEMO_THRESHOLD
    if task is Task.SCORER:
        return SCORER_DEMO_THRESHOLD
    msg = f"{task.value} has no tuning metric"
    raise ValueError(msg)
