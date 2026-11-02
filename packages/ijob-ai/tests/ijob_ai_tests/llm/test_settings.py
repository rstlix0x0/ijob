import dataclasses
from pathlib import Path

import pytest
from ijob_ai.errors import MissingModelError
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import LlmSettings, Task, TracingSettings

_CONFIG = Path("/home/user/.ijob/config.toml")
_JUDGE = ModelRef(Provider.CLAUDE_SDK, "claude-haiku-5-5")


def test_task_values() -> None:
    assert [t.value for t in Task] == [
        "cv_profile",
        "instructions",
        "judge",
        "scorer",
        "source_recipe",
    ]


def test_settings_defaults() -> None:
    settings = LlmSettings(config_path=_CONFIG)
    assert settings.max_cost_usd == 0.50
    assert settings.concurrency == 4
    assert settings.timeout_s == 120
    assert settings.cli_path is None
    assert settings.models == {}
    tracing = TracingSettings()
    assert tracing.max_size_mb == 256
    assert tracing.langfuse_endpoint is None


def test_model_for_returns_configured_model() -> None:
    settings = LlmSettings(config_path=_CONFIG, models={Task.JUDGE: _JUDGE})
    assert settings.model_for(Task.JUDGE) == _JUDGE


def test_model_for_missing_raises() -> None:
    settings = LlmSettings(config_path=_CONFIG, models={Task.JUDGE: _JUDGE})
    with pytest.raises(MissingModelError) as caught:
        settings.model_for(Task.SCORER)
    assert "scorer" in str(caught.value)
    assert str(_CONFIG) in str(caught.value)
    assert caught.value.config_path == _CONFIG


def test_settings_are_frozen() -> None:
    settings = LlmSettings(config_path=_CONFIG)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.concurrency = 8  # type: ignore[misc]
    tracing = TracingSettings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        tracing.max_size_mb = 1  # type: ignore[misc]
