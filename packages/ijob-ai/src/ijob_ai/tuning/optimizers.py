"""The three optimizers, built with the settings tuning runs them under.

`build_optimizer` returns a `BuiltOptimizer` whose `compile` takes the same
arguments for every optimizer. Optuna, which MIPROv2 needs, loads only inside
`build_optimizer`. This module imports DSPy, so it is imported only when a
tuning is about to run.
"""

from dataclasses import dataclass
from typing import Any, Final

import dspy
from dspy.teleprompt import GEPA, BootstrapFewShotWithRandomSearch, MIPROv2

from ijob_ai.llm.settings import Task
from ijob_ai.tuning.metrics import demo_threshold, float_metric, gepa_metric
from ijob_ai.tuning.names import Optimizer

__all__ = [
    "AUTO_LEVEL",
    "BOOTSTRAPPED_DEMOS",
    "CANDIDATE_PROGRAMS",
    "LABELED_DEMOS",
    "MAX_ERRORS",
    "MIPRO_MAX_VAL",
    "BuiltOptimizer",
    "Optimizer",
    "build_optimizer",
]

BOOTSTRAPPED_DEMOS: Final = 2
LABELED_DEMOS: Final = 0
CANDIDATE_PROGRAMS: Final = 6
MAX_ERRORS: Final = 1_000_000
AUTO_LEVEL: Final = "light"
MIPRO_MAX_VAL: Final = 50


@dataclass(frozen=True, slots=True)
class BuiltOptimizer:
    """An optimizer ready to compile a program.

    Attributes:
        optimizer: Which optimizer this is.
        inner: The DSPy optimizer that does the work.
    """

    optimizer: Optimizer
    inner: Any

    def compile(self, student: Any, *, trainset: list[Any], valset: list[Any]) -> Any:  # noqa: ANN401  # dspy programs are untyped
        """Optimize `student` on `trainset`, choosing between candidates on `valset`.

        MIPROv2 scores each candidate on all of `valset` only up to
        `MIPRO_MAX_VAL` examples. Under `auto` it ignores its `minibatch` argument
        and turns minibatching on above that size, so a larger `valset` is refused
        before any call is made.

        Args:
            student: The program to optimize.
            trainset: Examples to learn from.
            valset: Examples to choose between candidates on.

        Returns:
            The optimized program.

        Raises:
            ValueError: If this is MIPROv2 and `valset` is over `MIPRO_MAX_VAL`.
        """
        if self.optimizer is Optimizer.MIPRO:
            if len(valset) > MIPRO_MAX_VAL:
                msg = (
                    f"mipro takes at most {MIPRO_MAX_VAL} val examples, "
                    f"got {len(valset)}"
                )
                raise ValueError(msg)
            return self.inner.compile(
                student, trainset=trainset, valset=valset, minibatch=False
            )
        return self.inner.compile(student, trainset=trainset, valset=valset)


def build_optimizer(
    optimizer: Optimizer,
    task: Task,
    *,
    tuner_lm: dspy.BaseLM | None,
    num_threads: int,
    task_lm: dspy.BaseLM | None = None,
) -> BuiltOptimizer:
    """Build an optimizer with its tuning settings.

    Args:
        optimizer: Which optimizer to build.
        task: `Task.JUDGE` or `Task.SCORER`; it picks the metric and the demo
            threshold.
        tuner_lm: The LM that reflects (GEPA) or proposes (MIPROv2); Bootstrap
            ignores it and accepts None.
        num_threads: How many examples run at once.
        task_lm: The LM the task runs on. MIPROv2 runs its trials on it and
            ignores the ambient LM; the other optimizers use the ambient LM.

    Returns:
        The built optimizer.

    Raises:
        ValueError: If `tuner_lm` is None for GEPA or MIPROv2, if `task_lm` is
            None for MIPROv2, or if `task` has no tuning metric.
    """
    threshold = demo_threshold(task)
    if optimizer is Optimizer.BOOTSTRAP:
        return BuiltOptimizer(
            optimizer,
            BootstrapFewShotWithRandomSearch(
                metric=float_metric(task),
                max_bootstrapped_demos=BOOTSTRAPPED_DEMOS,
                max_labeled_demos=LABELED_DEMOS,
                num_candidate_programs=CANDIDATE_PROGRAMS,
                num_threads=num_threads,
                max_errors=MAX_ERRORS,
                metric_threshold=threshold,
            ),
        )
    if tuner_lm is None:
        msg = f"{optimizer.value} needs a tuner model"
        raise ValueError(msg)
    if optimizer is Optimizer.GEPA:
        return BuiltOptimizer(
            optimizer,
            GEPA(
                metric=gepa_metric(task),
                auto=AUTO_LEVEL,
                reflection_lm=tuner_lm,
                num_threads=num_threads,
            ),
        )
    if task_lm is None:
        msg = "mipro needs the task model"
        raise ValueError(msg)
    import optuna  # noqa: F401, PLC0415  # loads only here; a missing install fails here

    return BuiltOptimizer(
        optimizer,
        MIPROv2(
            metric=float_metric(task),
            prompt_model=tuner_lm,
            task_model=task_lm,
            auto=AUTO_LEVEL,
            max_bootstrapped_demos=BOOTSTRAPPED_DEMOS,
            max_labeled_demos=LABELED_DEMOS,
            num_threads=num_threads,
            max_errors=MAX_ERRORS,
            metric_threshold=threshold,
        ),
    )
