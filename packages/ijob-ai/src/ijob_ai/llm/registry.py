"""Process hygiene for DSPy and the registry of one `dspy.LM` per task.

This module imports DSPy, so it is imported only when a model is about to be
used.
"""

import os
from collections.abc import Callable
from functools import cache

import dspy
from dspy.clients.engines.base import AsyncEngine, Engine

from ijob_ai.errors import ProviderUnavailableError
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import LlmSettings, Task

_COST_MAP_FLAG = "LITELLM_LOCAL_MODEL_COST_MAP"

# Each of these silently moves a call to another backend or a remote price fetch.
_REFUSED_VARIABLES = (
    "LITELLM_MODEL_COST_MAP_URL",
    "OPENROUTER_API_BASE",
    "OPENROUTER_BASE_URL",
)

_OPENROUTER_KEY = "OPENROUTER_API_KEY"

type EngineFactory = Callable[[ModelRef], tuple[Engine, AsyncEngine]]


@cache
def _configure_dspy() -> None:
    """Turn off DSPy's own disk and memory caches, once per process."""
    dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)


def prepare_environment() -> None:
    """Make DSPy run offline and without its own cache.

    Sets the local price-map flag, refuses the variables that redirect a call,
    and turns DSPy's caches off. Every call repeats the checks; only the first
    configures DSPy.

    Raises:
        ProviderUnavailableError: If a variable that redirects a call is set.
    """
    os.environ[_COST_MAP_FLAG] = "True"
    for name in _REFUSED_VARIABLES:
        if name in os.environ:
            msg = f"{name} is set; unset it, because ijob calls providers directly"
            raise ProviderUnavailableError(msg)
    _configure_dspy()


class Registry:
    """The `dspy.LM` of each task, built on first use and kept."""

    def __init__(self, settings: LlmSettings, claude_engines: EngineFactory) -> None:
        """Create a registry that has built nothing yet.

        Args:
            settings: Settings that name each task's model.
            claude_engines: Builds the sync and async engine for a claude-sdk
                model; called once per claude-sdk task, when its LM is first built.
        """
        self._settings = settings
        self._claude_engines = claude_engines
        self._lms: dict[Task, dspy.LM] = {}

    def lm_for(self, task: Task) -> dspy.LM:
        """Return the task's `dspy.LM`, building it on first use.

        The LM never caches and never retries, so every billed attempt is seen.

        Args:
            task: The task about to call a model.

        Returns:
            The task's LM; the same object on every call.

        Raises:
            MissingModelError: If no model is configured for `task`.
            ProviderUnavailableError: If the environment is not usable, or
                OpenRouter is chosen and `OPENROUTER_API_KEY` is not set.
        """
        existing = self._lms.get(task)
        if existing is not None:
            return existing
        ref = self._settings.model_for(task)
        prepare_environment()
        lm = self._build(ref)
        self._lms[task] = lm
        return lm

    def _build(self, ref: ModelRef) -> dspy.LM:
        if ref.provider is Provider.OPENROUTER:
            if _OPENROUTER_KEY not in os.environ:
                msg = f"{_OPENROUTER_KEY} is not set; export it to use {ref}"
                raise ProviderUnavailableError(msg)
            return dspy.LM(str(ref), cache=False, num_retries=0)
        engine, async_engine = self._claude_engines(ref)
        return dspy.LM(
            str(ref),
            engine=engine,
            async_engine=async_engine,
            cache=False,
            num_retries=0,
        )
