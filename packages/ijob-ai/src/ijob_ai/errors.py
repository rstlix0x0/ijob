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


class CapReachedError(LlmError):
    """A run reached its spending cap and stopped before another model call."""

    def __init__(self, message: str = "the spending cap was reached") -> None:
        """Build the error.

        Args:
            message: What to tell the user; a subclass may pass its own.
        """
        super().__init__(message)


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
    """There is nothing to act on.

    Raised when an operation has no postings to act on, or when the user's
    instructions give neither a filter nor a rule.
    """

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


class PhoenixError(LlmError):
    """Root of the failures of talking to Phoenix.

    Each kind exits through the same handler as any other `LlmError`.
    """


class PhoenixUnreachableError(PhoenixError):
    """Nothing answers at the Phoenix endpoint.

    Attributes:
        url: The endpoint that did not answer.
    """

    def __init__(self, url: str) -> None:
        """Build the error from the endpoint.

        Args:
            url: The endpoint that did not answer.
        """
        super().__init__(f"Phoenix is not running at {url}; run docker compose up -d")
        self.url = url


class PhoenixHttpError(PhoenixError):
    """Phoenix answered a REST call with status 400 or above.

    Attributes:
        status: The HTTP status.
        body: The first 200 characters of the reply.
    """

    def __init__(self, status: int, body: str) -> None:
        """Build the error from the status and the reply.

        Args:
            status: The HTTP status.
            body: The reply text; only its first 200 characters are kept.
        """
        self.status = status
        self.body = body[:200]
        super().__init__(f"Phoenix answered {status}: {self.body}")


class PhoenixReplyError(PhoenixError):
    """Phoenix answered a REST call with a body ijob cannot read.

    Attributes:
        path: The route that was called.
    """

    def __init__(self, path: str) -> None:
        """Build the error from the route.

        Args:
            path: The route that was called.
        """
        super().__init__(
            f"Phoenix answered {path} with a reply ijob cannot read; "
            "check that port 6006 is Phoenix 21"
        )
        self.path = path


class SpanBatchRefusedError(PhoenixError):
    """The span exporter reported failure for a batch.

    The exporter returns only success or failure, so the cause is not known.

    Attributes:
        url: The endpoint the batch was sent to.
    """

    def __init__(self, url: str) -> None:
        """Build the error from the endpoint.

        Args:
            url: The endpoint the batch was sent to.
        """
        super().__init__(
            f"sending spans to {url} failed; run docker compose logs phoenix"
        )
        self.url = url
