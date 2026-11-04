"""Span attribute names and values that ijob records for model and tool calls.

The `gen_ai.*` names follow the OpenTelemetry GenAI convention; ijob owns the
strings itself rather than importing them from the pre-release incubating
semantic-conventions package. The `ijob.*` names are ijob's own.
"""

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ijob_ai.llm.reference import Provider

PROVIDER_NAME: Final = "gen_ai.provider.name"
OPERATION_NAME: Final = "gen_ai.operation.name"
REQUEST_MODEL: Final = "gen_ai.request.model"
RESPONSE_MODEL: Final = "gen_ai.response.model"
INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
COST: Final = "gen_ai.usage.cost"

TASK: Final = "ijob.task"
TASK_VERSION: Final = "ijob.task_version"
POSTING_ID: Final = "ijob.posting_id"
OUTCOME: Final = "ijob.outcome"
PROMPT: Final = "ijob.prompt"
COMPLETION: Final = "ijob.completion"
TOOL_NAME: Final = "ijob.tool.name"
TOOL_OUTCOME: Final = "ijob.tool.outcome"
TOOL_VIOLATION: Final = "ijob.tool.violation"

PROVIDER_NAMES: Final = MappingProxyType(
    {
        Provider.CLAUDE_SDK: "anthropic",
        Provider.OPENROUTER: "openrouter",
    }
)
"""The `gen_ai.provider.name` value for each of ijob's providers."""


class Outcome(StrEnum):
    """How a model call ended, recorded as `ijob.outcome`."""

    OK = "ok"
    TASK_OUTPUT_ERROR = "task_output_error"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    COST_UNKNOWN = "cost_unknown"
    TIMEOUT = "timeout"
