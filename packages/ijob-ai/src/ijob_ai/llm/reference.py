"""Model references of the form `<provider>/<model>`."""

from dataclasses import dataclass
from enum import StrEnum
from typing import override

from ijob_core.errors import InvalidValueError


class Provider(StrEnum):
    """An LLM provider ijob can call."""

    CLAUDE_SDK = "claude-sdk"
    OPENROUTER = "openrouter"


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A provider and the model to call on it.

    Attributes:
        provider: The provider that serves the model.
        model: The provider's name for the model; it may itself contain `/`.
    """

    provider: Provider
    model: str

    @override
    def __str__(self) -> str:
        """Return the reference as `<provider>/<model>`."""
        return f"{self.provider}/{self.model}"


def parse_model_ref(value: str, key: str) -> ModelRef:
    """Parse `<provider>/<model>`, splitting at the first `/`.

    Args:
        value: The reference text, such as `openrouter/google/gemini-3.8-flash`.
        key: The configuration key the text came from, named in errors.

    Returns:
        The parsed reference.

    Raises:
        InvalidValueError: If `value` has no provider, names an unknown
            provider, or has a blank model.
    """
    provider_text, separator, model = value.partition("/")
    if not separator:
        raise InvalidValueError(
            key, f"{value!r} has no provider; use <provider>/<model>"
        )
    try:
        provider = Provider(provider_text)
    except ValueError:
        known = ", ".join(p.value for p in Provider)
        raise InvalidValueError(
            key, f"unknown provider {provider_text!r}; use one of {known}"
        ) from None
    if not model.strip():
        raise InvalidValueError(key, f"{value!r} has a blank model")
    return ModelRef(provider, model)
