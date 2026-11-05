"""Settings of the LLM layer: tasks, models, limits, tracing and evals."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from ijob_core.errors import InvalidValueError

from ijob_ai.errors import MissingModelError
from ijob_ai.llm.reference import ModelRef

_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})


def check_local_endpoint(value: str, key: str) -> None:
    """Refuse an endpoint that is not a local `http` or `https` URL.

    Args:
        value: The URL to check.
        key: Setting name to put in the error.

    Raises:
        InvalidValueError: If `value` is not a valid URL, its scheme is not
            `http` or `https`, or its host is not `localhost`, `127.0.0.1` or
            `::1`.
    """
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as error:
        raise InvalidValueError(key, f"{value!r} is not a valid URL") from error
    if parts.scheme not in {"http", "https"}:
        raise InvalidValueError(key, f"{value!r} is not an http or https URL")
    if parts.hostname not in _LOCAL_HOSTS:
        hosts = ", ".join(sorted(_LOCAL_HOSTS))
        raise InvalidValueError(key, f"host must be one of {hosts}")


class Task(StrEnum):
    """A kind of model call; each one is configured with its own model."""

    CV_PROFILE = "cv_profile"
    EVAL_GRADER = "eval_grader"
    EXPLORE_AGENT = "explore_agent"
    INSTRUCTIONS = "instructions"
    JOB_EXTRACT = "job_extract"
    JUDGE = "judge"
    SCORER = "scorer"
    SOURCE_AGENT = "source_agent"
    SOURCE_RECIPE = "source_recipe"
    TUNER = "tuner"


@dataclass(frozen=True, slots=True, kw_only=True)
class LlmSettings:
    """Everything the LLM layer reads from configuration.

    Attributes:
        config_path: Configuration file the settings came from, named in errors.
        models: Model per task; a task may be absent.
        max_cost_usd: Spending cap for one operation, in US dollars.
        concurrency: How many model calls may run at once.
        timeout_s: Seconds one model call may take.
        cli_path: Path to the `claude` CLI, or None to find it on `PATH`.
    """

    config_path: Path
    models: Mapping[Task, ModelRef] = field(default_factory=dict[Task, ModelRef])
    max_cost_usd: float = 0.50
    concurrency: int = 4
    timeout_s: float = 120
    cli_path: Path | None = None

    def model_for(self, task: Task) -> ModelRef:
        """Return the model configured for a task.

        Args:
            task: The task about to call a model.

        Returns:
            The task's model.

        Raises:
            MissingModelError: If no model is configured for `task`.
        """
        model = self.models.get(task)
        if model is None:
            raise MissingModelError(task.value, self.config_path)
        return model


@dataclass(frozen=True, slots=True)
class TracingSettings:
    """How much the layer keeps of what it records about its calls.

    Attributes:
        max_size_mb: Size at which the local trace store starts dropping the
            oldest traces, in megabytes.
    """

    max_size_mb: int = 256


@dataclass(frozen=True, slots=True)
class PhoenixSettings:
    """Where the local Phoenix instance listens.

    Attributes:
        endpoint: Base URL of the Phoenix server; always a local `http` or
            `https` address.

    Raises:
        InvalidValueError: If the endpoint is not a local `http` or `https`
            URL.
    """

    endpoint: str = "http://127.0.0.1:6006"

    def __post_init__(self) -> None:
        """Refuse an endpoint that is not on this machine."""
        check_local_endpoint(self.endpoint, "phoenix.endpoint")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvalSettings:
    """What the eval commands read from configuration.

    Attributes:
        jury: Two to five different models that vote on labels, or None when
            none is configured.
        max_cost_usd: Spending cap for one eval command, in US dollars.
        total_cost_usd: Spending cap across all eval commands, in US dollars.
    """

    jury: tuple[ModelRef, ...] | None = None
    max_cost_usd: float = 4
    total_cost_usd: float = 10


@dataclass(frozen=True, slots=True, kw_only=True)
class TuneSettings:
    """What the tuning commands read from configuration.

    Attributes:
        candidates: Models the screening step tries, in the order written.
        baseline: Model the candidates are compared with, or None when absent.
        models: Models a tuning run covers, in the order written.
        max_cost_usd: Spending cap for one tuning run, in US dollars.
    """

    candidates: tuple[ModelRef, ...] = ()
    baseline: ModelRef | None = None
    models: tuple[ModelRef, ...] = ()
    max_cost_usd: float = 2
