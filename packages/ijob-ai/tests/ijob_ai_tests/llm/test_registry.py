import asyncio
import os
from pathlib import Path
from typing import Any

import dspy
import pytest
from ijob_ai.errors import MissingModelError, ProviderUnavailableError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.call import run_call
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.registry import Registry, prepare_environment
from ijob_ai.llm.settings import LlmSettings, Task

from .fakes import FakeAsyncEngine, FakeEngine, Judge

CLAUDE = ModelRef(Provider.CLAUDE_SDK, "claude-haiku-5-5")
OPENROUTER = ModelRef(Provider.OPENROUTER, "google/gemini-3.8-flash")
OVERRIDES = ["LITELLM_MODEL_COST_MAP_URL", "OPENROUTER_API_BASE", "OPENROUTER_BASE_URL"]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (*OVERRIDES, "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def settings(tmp_path: Path, **models: ModelRef) -> LlmSettings:
    return LlmSettings(
        config_path=tmp_path / "config.toml",
        models={Task(task): ref for task, ref in models.items()},
    )


def test_prepare_environment_sets_cost_map_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")
    prepare_environment()
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


@pytest.mark.parametrize("name", OVERRIDES)
def test_prepare_environment_refuses_overrides(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "https://example.invalid")
    with pytest.raises(ProviderUnavailableError, match=name):
        prepare_environment()


def test_prepare_environment_disables_dspy_cache(tmp_path: Path) -> None:
    prepare_environment()
    engine = FakeAsyncEngine()
    registry = Registry(
        settings(tmp_path, judge=CLAUDE), lambda _ref: (FakeEngine(), engine)
    )

    asyncio.run(
        run_call(
            dspy.Predict(Judge),
            registry.lm_for(Task.JUDGE),
            inputs={"posting": "AAA"},
            provider=Provider.CLAUDE_SDK,
            budget=Budget(1.0),
            timeout_s=5,
            task=Task.JUDGE,
            task_version="v1",
            posting_id=None,
        )
    )

    assert len(engine.requests) == 1
    assert not (tmp_path / ".dspy_cache").exists()
    assert dspy.cache.enable_disk_cache is False
    assert dspy.cache.enable_memory_cache is False


def test_lm_for_openrouter_without_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[tuple[Any, ...]] = []
    monkeypatch.setattr(dspy, "LM", lambda *args, **_kwargs: built.append(args))
    registry = Registry(
        settings(tmp_path, judge=OPENROUTER), lambda _ref: pytest.fail("factory")
    )

    with pytest.raises(ProviderUnavailableError, match="OPENROUTER_API_KEY"):
        registry.lm_for(Task.JUDGE)

    assert built == []


def test_lm_for_openrouter_builds_one_lm_per_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-for-tests")
    registry = Registry(
        settings(tmp_path, judge=OPENROUTER), lambda _ref: pytest.fail("factory")
    )

    first = registry.lm_for(Task.JUDGE)

    assert registry.lm_for(Task.JUDGE) is first
    assert first.model == "openrouter/google/gemini-3.8-flash"
    assert first.num_retries == 0
    assert first.cache is False


def test_lm_for_claude_uses_injected_engines(tmp_path: Path) -> None:
    calls: list[ModelRef] = []
    sync_engine = FakeEngine()
    async_engine = FakeAsyncEngine()

    def factory(ref: ModelRef) -> tuple[FakeEngine, FakeAsyncEngine]:
        calls.append(ref)
        return sync_engine, async_engine

    registry = Registry(settings(tmp_path, judge=CLAUDE), factory)

    first = registry.lm_for(Task.JUDGE)
    second = registry.lm_for(Task.JUDGE)

    assert first is second
    assert calls == [CLAUDE]
    assert first.model == "claude-sdk/claude-haiku-5-5"
    assert first._async_engine_spec is async_engine
    assert first.num_retries == 0
    assert first.cache is False


def test_lm_for_missing_model_raises(tmp_path: Path) -> None:
    registry = Registry(settings(tmp_path), lambda _ref: pytest.fail("factory"))
    with pytest.raises(MissingModelError, match="judge"):
        registry.lm_for(Task.JUDGE)


def test_lm_for_ref_is_cached_per_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-for-tests")
    other = ModelRef(Provider.OPENROUTER, "google/gemini-3.8-pro")
    registry = Registry(settings(tmp_path), lambda _ref: pytest.fail("factory"))

    first = registry.lm_for_ref(OPENROUTER)

    assert registry.lm_for_ref(ModelRef(Provider.OPENROUTER, OPENROUTER.model)) is first
    assert registry.lm_for_ref(other) is not first
    assert first.model == "openrouter/google/gemini-3.8-flash"


def test_lm_for_still_uses_the_task_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-for-tests")
    registry = Registry(
        settings(tmp_path, judge=OPENROUTER), lambda _ref: pytest.fail("factory")
    )

    by_task = registry.lm_for(Task.JUDGE)

    assert by_task.model == "openrouter/google/gemini-3.8-flash"
    assert registry.lm_for(Task.JUDGE) is by_task
    assert registry.lm_for_ref(OPENROUTER) is by_task
