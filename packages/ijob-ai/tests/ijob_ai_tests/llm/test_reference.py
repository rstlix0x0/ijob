import dataclasses

import pytest
from ijob_ai.llm.reference import ModelRef, Provider, parse_model_ref
from ijob_core.errors import InvalidValueError


def test_provider_values() -> None:
    assert [p.value for p in Provider] == ["claude-sdk", "openrouter"]
    assert Provider("claude-sdk") is Provider.CLAUDE_SDK


@pytest.mark.parametrize(
    ("value", "provider", "model"),
    [
        ("claude-sdk/claude-haiku-5-5", Provider.CLAUDE_SDK, "claude-haiku-5-5"),
        (
            "openrouter/google/gemini-3.8-flash",
            Provider.OPENROUTER,
            "google/gemini-3.8-flash",
        ),
    ],
)
def test_parse_model_ref_table(value: str, provider: Provider, model: str) -> None:
    ref = parse_model_ref(value, "models.judge")
    assert ref == ModelRef(provider, model)
    assert str(ref) == value


@pytest.mark.parametrize(
    "value",
    ["openrouter/", "anthropic/claude-haiku-5-5", "claude-haiku-5-5"],
)
def test_parse_model_ref_table_errors(value: str) -> None:
    with pytest.raises(InvalidValueError) as raised:
        parse_model_ref(value, "models.judge")
    assert raised.value.field == "models.judge"


def test_parse_model_ref_rejects_blank_model_with_spaces() -> None:
    with pytest.raises(InvalidValueError):
        parse_model_ref("claude-sdk/  ", "models.scorer")


def test_model_ref_is_frozen() -> None:
    ref = ModelRef(Provider.OPENROUTER, "a/b")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.model = "c"  # type: ignore[misc]
