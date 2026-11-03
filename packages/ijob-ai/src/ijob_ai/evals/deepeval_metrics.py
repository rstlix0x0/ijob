"""DeepEval metrics for the evaluation suites, behind one lazy loader.

DeepEval is heavy, prints, reads `.env` files and emits warnings on import, so
this is the only module that touches it, and only inside `load_deepeval`. The
loader opts out of telemetry and `.env` loading before the import, and defines
every subclass inside a block that silences DeepEval's warnings. Metrics are
called directly with `_show_indicator=False`; `evaluate()` is never used, as it
prints banners and writes run data to the working directory.

`KeepMatch` and `ScoreClose` use no model. `Grader` lets DeepEval's G-Eval call
a model through `run_call`, so every grade is metered, traced and charged to
the run's budget.
"""
# ruff: noqa: ANN401  # DeepEval's overridden methods take *args and **kwargs

import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any, Final, Protocol, Self, cast, override

import dspy

from ijob_ai.errors import CapReachedError, TaskOutputError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.settings import Task

GRADER_VERSION: Final = "eval-grader-v1"
FAITHFUL_STEPS: Final[tuple[str, ...]] = (
    "List each fact the output states about the posting, the rules or the candidate.",
    "Check that each fact appears in the input.",
    "Give 10 if all appear, and take off in proportion to those that do not.",
)
KEEP: Final = "keep"
REJECT: Final = "reject"
DEFAULT_TOLERANCE: Final = 20
PASS_THRESHOLD: Final = 0.5

_EMPTY_REASON = "the reason is empty"
_FAITHFUL_NAME = "Faithful"
_GRADER_INSTRUCTIONS = "Follow the prompt and answer in the output fields."
_PROMPT_FIELD = "prompt"
_ANSWER_FIELD = "answer"


class Metric(Protocol):
    """A DeepEval metric after `a_measure`, as ijob reads it."""

    score: float | None
    success: bool | None
    reason: str | None

    async def a_measure(self, test_case: Any, *, _show_indicator: bool = ...) -> float:
        """Score a test case and fill `score`, `success` and `reason`."""
        ...


class GraderModel(Protocol):
    """The model DeepEval's G-Eval calls to grade a case."""

    def generate(self, *args: Any, **kwargs: Any) -> str:
        """Refuse: ijob calls the model asynchronously only."""
        ...

    async def a_generate(self, prompt: str, schema: type[Any] | None = None) -> Any:
        """Answer a prompt as a `schema` instance, or as text without one."""
        ...

    def get_model_name(self) -> str:
        """Return the name of the model."""
        ...


@dataclass(frozen=True, slots=True)
class CaseResult:
    """What a metric said about one case.

    Attributes:
        score: The metric's score, 0.0 to 1.0.
        passed: Whether the score met the metric's threshold.
        reason: The metric's reason, or None when it gives none.
    """

    score: float
    passed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class DeepEval:
    """The DeepEval pieces ijob uses, built once by `load_deepeval`.

    Attributes:
        keep_match: Builds a `KeepMatch`.
        score_close: Builds a `ScoreClose` with the given tolerance in points.
        grader: Builds a `Grader` from the LM, provider, budget, timeout in
            seconds and model name.
        faithful: Builds the G-Eval metric that grades facts against the input,
            on a grader.
        case: Builds an `LLMTestCase` from input, actual output and expected
            output.
        g_eval_class: The `GEval` class.
    """

    keep_match: Callable[[], Metric]
    score_close: Callable[[int], Metric]
    grader: Callable[[dspy.LM, Provider, Budget, float, str], GraderModel]
    faithful: Callable[[GraderModel], Metric]
    case: Callable[[str, str, str | None], object]
    g_eval_class: type[Any]


def _number(text: str | None) -> int | None:
    """Read `text` as a whole number, or None when it is not one."""
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


@cache
def load_deepeval() -> DeepEval:
    """Import DeepEval and define the metrics, once.

    Sets `DEEPEVAL_TELEMETRY_OPT_OUT` and `DEEPEVAL_DISABLE_DOTENV` to `1`
    before the import, then imports and defines every subclass with warnings
    silenced.

    Returns:
        The DeepEval pieces; the same object on every call.
    """
    os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"
    os.environ["DEEPEVAL_DISABLE_DOTENV"] = "1"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from deepeval.metrics import BaseMetric, GEval  # noqa: PLC0415  # lazy, see module doc
        from deepeval.models import DeepEvalBaseLLM  # noqa: PLC0415
        from deepeval.test_case import LLMTestCase, SingleTurnParams  # noqa: PLC0415

        class KeepMatch(BaseMetric):  # type: ignore[no-untyped-call]
            """Passes when the answer is the same keep or reject as the label."""

            def __init__(self) -> None:
                self.threshold = 1.0
                self.async_mode = True
                self.verbose_mode = False

            def _grade(self, test_case: LLMTestCase) -> float:
                answer = (test_case.actual_output or "").strip().lower()
                label = (test_case.expected_output or "").strip().lower()
                self.score = (
                    1.0 if answer in {KEEP, REJECT} and answer == label else 0.0
                )
                self.success = self.score == 1.0
                self.reason = f"answered {answer!r}, label {label!r}"
                return self.score

            @override
            def measure(
                self, test_case: LLMTestCase, *args: Any, **kwargs: Any
            ) -> float:
                return self._grade(test_case)

            @override
            async def a_measure(
                self, test_case: LLMTestCase, *args: Any, **kwargs: Any
            ) -> float:
                return self._grade(test_case)

            @override
            def is_successful(self) -> bool:
                return bool(self.success)

            @property
            @override
            def __name__(self) -> str:
                return "Keep match"

        class ScoreClose(BaseMetric):  # type: ignore[no-untyped-call]
            """Passes when the answered score is within a tolerance of the label."""

            def __init__(self, tolerance: int) -> None:
                self.tolerance = tolerance
                self.threshold = 1.0
                self.async_mode = True
                self.verbose_mode = False

            def _grade(self, test_case: LLMTestCase) -> float:
                answer = _number(test_case.actual_output)
                label = _number(test_case.expected_output)
                close = (
                    answer is not None
                    and label is not None
                    and abs(answer - label) <= self.tolerance
                )
                self.score = 1.0 if close else 0.0
                self.success = close
                self.reason = (
                    f"answered {answer}, label {label}, within {self.tolerance}"
                )
                return self.score

            @override
            def measure(
                self, test_case: LLMTestCase, *args: Any, **kwargs: Any
            ) -> float:
                return self._grade(test_case)

            @override
            async def a_measure(
                self, test_case: LLMTestCase, *args: Any, **kwargs: Any
            ) -> float:
                return self._grade(test_case)

            @override
            def is_successful(self) -> bool:
                return bool(self.success)

            @property
            @override
            def __name__(self) -> str:
                return "Score close"

        class Grader(DeepEvalBaseLLM):  # type: ignore[no-untyped-call]
            """G-Eval's model: asynchronous only, through `run_call`."""

            def __init__(
                self,
                lm: dspy.LM,
                provider: Provider,
                budget: Budget,
                timeout_s: float,
                model_name: str,
            ) -> None:
                self._lm = lm
                self._provider = provider
                self._budget = budget
                self._timeout_s = timeout_s
                super().__init__(model_name)

            @override
            def load_model(self, *args: Any, **kwargs: Any) -> Self:
                return self

            @override
            def generate(self, *args: Any, **kwargs: Any) -> str:
                msg = "the grader answers through a_generate only"
                raise NotImplementedError(msg)

            @override
            async def a_generate(
                self,
                prompt: str,
                schema: type[Any] | None = None,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                if not self._budget.try_start():
                    raise CapReachedError
                fields = (
                    {
                        name: field.annotation
                        for name, field in schema.model_fields.items()
                    }
                    if schema is not None
                    else {_ANSWER_FIELD: str}
                )
                signature = dspy.make_signature(
                    {
                        _PROMPT_FIELD: (str, dspy.InputField()),
                        **{
                            name: (annotation, dspy.OutputField())
                            for name, annotation in fields.items()
                        },
                    },
                    _GRADER_INSTRUCTIONS,
                )
                prediction = await run_call(
                    dspy.Predict(signature),
                    self._lm,
                    inputs={_PROMPT_FIELD: prompt},
                    provider=self._provider,
                    budget=self._budget,
                    timeout_s=self._timeout_s,
                    task=Task.EVAL_GRADER,
                    task_version=GRADER_VERSION,
                    posting_id=None,
                )
                if schema is None:
                    return str(prediction[_ANSWER_FIELD])
                return schema(**{name: prediction[name] for name in fields})

            @override
            def get_model_name(self, *args: Any, **kwargs: Any) -> str:
                return str(self.name)

    def make_grader(
        lm: dspy.LM, provider: Provider, budget: Budget, timeout_s: float, name: str
    ) -> GraderModel:
        return Grader(lm, provider, budget, timeout_s, name)

    def make_faithful(grader: GraderModel) -> Metric:
        return GEval(
            name=_FAITHFUL_NAME,
            evaluation_steps=list(FAITHFUL_STEPS),
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            model=cast(DeepEvalBaseLLM, grader),
            threshold=PASS_THRESHOLD,
            async_mode=True,
            verbose_mode=False,
        )

    def make_case(input_text: str, actual: str, expected: str | None) -> object:
        return LLMTestCase(
            input=input_text, actual_output=actual, expected_output=expected
        )

    return DeepEval(
        keep_match=KeepMatch,
        score_close=ScoreClose,
        grader=make_grader,
        faithful=make_faithful,
        case=make_case,
        g_eval_class=GEval,
    )


def keep_match() -> Metric:
    """Build the metric that passes on the same keep or reject as the label.

    Returns:
        A `KeepMatch`; a case carries `keep` or `reject` as actual and
        expected output.
    """
    return load_deepeval().keep_match()


def score_close(tolerance: int = DEFAULT_TOLERANCE) -> Metric:
    """Build the metric that passes when the score is near the label.

    Args:
        tolerance: Largest allowed difference, in points.

    Returns:
        A `ScoreClose`; a case carries whole-number text as actual and
        expected output.
    """
    return load_deepeval().score_close(tolerance)


def make_grader(
    *,
    lm: dspy.LM,
    provider: Provider,
    budget: Budget,
    timeout_s: float,
    model_name: str,
) -> GraderModel:
    """Build the model DeepEval's G-Eval uses to grade reasons.

    Each grade makes one call through `run_call` as task `EVAL_GRADER`, and the
    call's span records `GRADER_VERSION`.

    Args:
        lm: The grader's LM.
        provider: The provider serving `lm`.
        budget: The operation's budget, charged with each grade.
        timeout_s: Seconds a grade may take.
        model_name: The grader's model reference, for display.

    Returns:
        The grader. Its `generate` raises `NotImplementedError`; a grade raises
        `CapReachedError` when the budget has reached its cap, and the errors
        of `run_call` otherwise.
    """
    return load_deepeval().grader(lm, provider, budget, timeout_s, model_name)


async def measure_case(
    metric: Metric,
    *,
    input: str,  # noqa: A002  # DeepEval's own name for the case input
    actual_output: str,
    expected_output: str | None = None,
) -> CaseResult:
    """Run a metric on one case, without printing or writing anything.

    Args:
        metric: The metric to run.
        input: The case input.
        actual_output: The answer being measured.
        expected_output: The label, for metrics that compare to one.

    Returns:
        The score, whether it passed, and the metric's reason.
    """
    test_case = load_deepeval().case(input, actual_output, expected_output)
    score = await metric.a_measure(test_case, _show_indicator=False)
    return CaseResult(score=score, passed=bool(metric.success), reason=metric.reason)


async def grade_reason(
    grader: GraderModel,
    *,
    input: str,  # noqa: A002  # DeepEval's own name for the case input
    actual_output: str,
) -> CaseResult:
    """Grade whether a reason states only facts found in its input.

    Args:
        grader: The model that grades, from `make_grader`.
        input: The numbered rules or profile JSON, then the posting block.
        actual_output: The reason being graded.

    Returns:
        The score from 0.0 to 1.0, passing at 0.5 or above, with the grader's
        reason. An empty or blank ``actual_output`` fails with score 0.0 and
        no model call, so it costs nothing.

    Raises:
        CapReachedError: The grader's budget has reached its cap.
        TaskOutputError: The grader's answer could not be used, including a
            score outside 0.0 to 1.0 after scaling.
    """
    if not actual_output.strip():
        return CaseResult(score=0.0, passed=False, reason=_EMPTY_REASON)
    metric = load_deepeval().faithful(grader)
    result = await measure_case(metric, input=input, actual_output=actual_output)
    if not 0.0 <= result.score <= 1.0:
        msg = f"grade {result.score} is outside 0.0 to 1.0"
        raise TaskOutputError(Task.EVAL_GRADER.value, msg)
    return result
