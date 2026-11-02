from ijob_ai.errors import LlmError, TaskOutputError
from ijob_app.recipe_errors import (
    NoTrialJobsError,
    RecipeCapReachedError,
    RecipeOutputError,
)


def test_error_messages() -> None:
    assert str(RecipeOutputError("no items")) == (
        "the model's recipe is invalid: no items"
    )
    assert str(NoTrialJobsError()) == ("the proposed recipe read no jobs from the page")
    assert str(RecipeCapReachedError()) == (
        "cost cap reached before the JSON recipe; raise [llm].max_cost_usd"
    )
    assert str(RecipeCapReachedError(started=False)) == (
        "cost cap reached before the first model call; raise [llm].max_cost_usd"
    )


def test_recipe_output_error_is_a_task_output_error_that_keeps_its_parts() -> None:
    error = RecipeOutputError("no items")

    assert isinstance(error, TaskOutputError)
    assert error.task == "source_recipe"
    assert error.reason == "no items"


def test_the_other_errors_are_llm_errors() -> None:
    assert isinstance(NoTrialJobsError(), LlmError)
    assert isinstance(RecipeCapReachedError(), LlmError)
