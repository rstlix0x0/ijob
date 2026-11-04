from pathlib import Path

import pytest
from ijob_ai.errors import (
    CapReachedError,
    CostUnknownError,
    CvTextError,
    LlmError,
    MissingModelError,
    NoCvError,
    NothingActionableError,
    ProviderFailedError,
    ProviderUnavailableError,
    TaskOutputError,
)
from ijob_core.errors import InvalidValueError

LLM_ERRORS: list[type[LlmError]] = [
    MissingModelError,
    ProviderUnavailableError,
    ProviderFailedError,
    CostUnknownError,
    TaskOutputError,
    NothingActionableError,
    NoCvError,
    CvTextError,
    CapReachedError,
]


def test_missing_model_message() -> None:
    error = MissingModelError("judge", Path("/x/config.toml"))
    assert str(error) == "no model for judge; set [models].judge in /x/config.toml"
    assert error.task == "judge"
    assert error.config_path == Path("/x/config.toml")


@pytest.mark.parametrize("error_type", LLM_ERRORS)
def test_all_llm_errors_share_root(error_type: type[LlmError]) -> None:
    assert issubclass(error_type, LlmError)
    assert not issubclass(error_type, InvalidValueError)


def test_llm_error_is_an_exception() -> None:
    assert issubclass(LlmError, Exception)


def test_provider_unavailable_keeps_reason() -> None:
    error = ProviderUnavailableError("OPENROUTER_API_KEY is not set")
    assert error.reason == "OPENROUTER_API_KEY is not set"
    assert str(error) == "OPENROUTER_API_KEY is not set"


def test_provider_failed_keeps_reason() -> None:
    error = ProviderFailedError("timed out")
    assert error.reason == "timed out"
    assert str(error) == "timed out"


def test_cost_unknown_names_the_model() -> None:
    error = CostUnknownError("openrouter/x/y")
    assert error.model == "openrouter/x/y"
    assert "openrouter/x/y" in str(error)


def test_task_output_error_names_task_and_reason() -> None:
    error = TaskOutputError("judge", "no verdict")
    assert error.task == "judge"
    assert error.reason == "no verdict"
    assert str(error) == "judge returned unusable output: no verdict"


def test_nothing_actionable_message() -> None:
    assert str(NothingActionableError())


def test_no_cv_message_points_at_the_fix() -> None:
    assert "cv" in str(NoCvError()).lower()


def test_cv_text_error_keeps_reason() -> None:
    error = CvTextError("the PDF has no text layer")
    assert error.reason == "the PDF has no text layer"
    assert "the PDF has no text layer" in str(error)


def test_cap_reached_is_an_llm_error() -> None:
    with pytest.raises(LlmError):
        raise CapReachedError


def test_cap_reached_has_a_message() -> None:
    assert str(CapReachedError())


def test_cap_reached_keeps_a_message_it_is_given() -> None:
    assert str(CapReachedError("three runs is the limit")) == "three runs is the limit"


def test_a_cap_reached_subclass_can_pass_its_own_message() -> None:
    class StoppedEarlyError(CapReachedError):
        def __init__(self) -> None:
            super().__init__("stopped early")

    assert str(StoppedEarlyError()) == "stopped early"
