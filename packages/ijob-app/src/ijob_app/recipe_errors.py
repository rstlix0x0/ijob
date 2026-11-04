"""The errors of recipe analysis.

`RecipeCapReachedError` and `RecipeOutputError` can be raised inside the
analysis worker, so they must be `LlmError`s: `run_items` records an `LlmError`
from a worker as a failed item and the analysis re-raises it for the caller,
while any other exception from a worker would reach the caller wrapped in an
exception group. `RecipeCapReachedError` is also a `CapReachedError`, so a
caller that treats every cap alike, such as an agent run storing its stop,
handles it without knowing the recipe. `NoTrialJobsError` is an `LlmError` too,
though a page that reads no jobs is not a model failure.

This module imports `ijob_ai.errors`, so `ijob_app.recipe_ops` imports it only
inside the functions that need it; a search that learns no recipe loads no
`ijob_ai` module.
"""

from ijob_ai.errors import CapReachedError, LlmError, TaskOutputError
from ijob_ai.llm.settings import Task


class RecipeOutputError(TaskOutputError):
    """The model's recipe, or its answer, cannot be used.

    Unlike `TaskOutputError`, the message names the recipe, not the task.
    """

    def __init__(self, reason: str) -> None:
        """Build the error from the reason.

        Args:
            reason: Why the recipe is unusable.
        """
        super().__init__(Task.SOURCE_RECIPE.value, reason)
        self.args = (f"the model's recipe is invalid: {reason}",)


class NoTrialJobsError(LlmError):
    """The proposed recipe read no job from the page it was learned on."""

    def __init__(self) -> None:
        """Build the error."""
        super().__init__("the proposed recipe read no jobs from the page")


class RecipeCapReachedError(CapReachedError):
    """The cost cap stopped the analysis before a model call it needed."""

    def __init__(self, *, started: bool = True) -> None:
        """Build the error.

        Args:
            started: Whether the first model call was made. `True` means the cap
                was reached after it, before the JSON recipe; `False` means the
                cap stopped the analysis before any call.
        """
        where = "the JSON recipe" if started else "the first model call"
        super().__init__(f"cost cap reached before {where}; raise [llm].max_cost_usd")
