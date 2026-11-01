"""Exceptions raised by the LLM layer."""

from pathlib import Path


class LlmError(Exception):
    """Root of every exception the LLM layer raises on its own.

    Validation failures stay `ijob_core.errors.InvalidValueError`; none of the
    classes here subclasses it.
    """


class MissingModelError(LlmError):
    """No model is configured for a task.

    Attributes:
        task: Name of the task that has no model.
        config_path: Configuration file that should name one.
    """

    def __init__(self, task: str, config_path: Path) -> None:
        """Build the error from the task and the configuration file.

        Args:
            task: Name of the task that has no model.
            config_path: Configuration file that should name one.
        """
        super().__init__(f"no model for {task}; set [models].{task} in {config_path}")
        self.task = task
        self.config_path = config_path


class ProviderUnavailableError(LlmError):
    """A provider cannot be used until the user fixes its setup.

    Attributes:
        reason: What is wrong and how to fix it.
    """

    def __init__(self, reason: str) -> None:
        """Build the error from the reason.

        Args:
            reason: What is wrong and how to fix it.
        """
        super().__init__(reason)
        self.reason = reason


class ProviderFailedError(LlmError):
    """A provider call failed or timed out.

    Attributes:
        reason: Why the call failed.
    """

    def __init__(self, reason: str) -> None:
        """Build the error from the reason.

        Args:
            reason: Why the call failed.
        """
        super().__init__(reason)
        self.reason = reason


class CostUnknownError(LlmError):
    """The cost of a call cannot be determined, so the budget cannot be kept.

    Attributes:
        model: The model reference whose cost is unknown.
    """

    def __init__(self, model: str) -> None:
        """Build the error from the model reference.

        Args:
            model: The model reference whose cost is unknown.
        """
        super().__init__(f"cost of {model} is unknown")
        self.model = model


class TaskOutputError(LlmError):
    """A task's model output could not be used.

    Attributes:
        task: Name of the task that produced the output.
        reason: Why the output is unusable.
    """

    def __init__(self, task: str, reason: str) -> None:
        """Build the error from the task and the reason.

        Args:
            task: Name of the task that produced the output.
            reason: Why the output is unusable.
        """
        super().__init__(f"{task} returned unusable output: {reason}")
        self.task = task
        self.reason = reason


class NothingActionableError(LlmError):
    """The operation has no postings to act on."""

    def __init__(self) -> None:
        """Build the error."""
        super().__init__("nothing to act on")


class NoCvError(LlmError):
    """The operation needs a CV and none is stored."""

    def __init__(self) -> None:
        """Build the error."""
        super().__init__("no CV is stored; add one first")


class CvTextError(LlmError):
    """The CV text could not be read or used.

    Attributes:
        reason: Why the CV text is unusable.
    """

    def __init__(self, reason: str) -> None:
        """Build the error from the reason.

        Args:
            reason: Why the CV text is unusable.
        """
        super().__init__(f"CV text is unusable: {reason}")
        self.reason = reason
