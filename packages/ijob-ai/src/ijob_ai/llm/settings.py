"""Settings of the LLM layer: tasks, models, limits and tracing."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ijob_ai.errors import MissingModelError
from ijob_ai.llm.reference import ModelRef


class Task(StrEnum):
    """A kind of model call; each one is configured with its own model."""

    CV_PROFILE = "cv_profile"
    INSTRUCTIONS = "instructions"
    JUDGE = "judge"
    SCORER = "scorer"
    SOURCE_RECIPE = "source_recipe"


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
    """Where and how much the layer records about its calls.

    Attributes:
        max_size_mb: Size at which the local trace store starts dropping the
            oldest traces, in megabytes.
        langfuse_endpoint: Langfuse endpoint to export spans to, or None to
            keep traces local.
    """

    max_size_mb: int = 256
    langfuse_endpoint: str | None = None
