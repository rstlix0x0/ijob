"""Claude Agent SDK engines for `dspy.LM`: isolated options, request mapping, metering.

`ClaudeAsyncEngine` runs one tool-less `claude_agent_sdk.query()` per call and
writes what the call cost into the current call meter. `ClaudeSyncEngine` only
satisfies `dspy.LM`'s need for a sync engine; ijob calls models asynchronously.
"""

import json
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NoReturn, Protocol

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    Message,
    ResultMessage,
)
from dspy.lm15 import Message as LmMessage
from dspy.lm15 import Request, Response, TextPart, Usage

from ijob_ai.errors import (
    CostUnknownError,
    ProviderFailedError,
    ProviderUnavailableError,
    TaskOutputError,
)
from ijob_ai.llm.meter import CallMeter, current_meter
from ijob_ai.llm.reference import ModelRef

_ONE_TEXT_USER_MESSAGE = "claude-sdk engine takes one text user message"
_EXAMPLES_HEADER = "Worked examples, as JSON turns:"
_ANSWER_HEADER = "Now answer this one:"
_ASYNC_ONLY = "claude-sdk runs async only"
_OUTPUT_LABEL = "claude call"


@asynccontextmanager
async def _closed(
    messages: AsyncIterator[Message],
) -> AsyncGenerator[AsyncIterator[Message]]:
    """Give the messages, then end the call that produces them if it is still on."""
    try:
        yield messages
    finally:
        if isinstance(messages, AsyncGenerator):
            await messages.aclose()


class SdkQuery(Protocol):
    """The part of `claude_agent_sdk.query` that the engine calls."""

    def __call__(
        self, *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Message]:
        """Run one claude call and yield its messages."""
        ...


def build_options(
    *, cli_path: Path, model: str, system_prompt: str, cwd: Path
) -> ClaudeAgentOptions:
    """Build the options that keep a claude call to a single, tool-less turn.

    The call reads no settings, loads no tools or MCP servers, persists no
    session, and blanks the API-key and auth-token variables so the CLI stays
    on the user's login. Thinking is off: each task fills one typed answer, and
    thinking would only make it slower.

    Args:
        cli_path: The resolved `claude` executable.
        model: The model name from configuration.
        system_prompt: The system text, passed on the command line.
        cwd: Working directory for the call; a throwaway directory.

    Returns:
        The options for one `claude_agent_sdk.query()` call.
    """
    return ClaudeAgentOptions(
        cli_path=cli_path,
        setting_sources=[],
        tools=[],
        max_turns=1,
        strict_mcp_config=True,
        permission_mode="dontAsk",
        verbatim_prompts=True,
        extra_args={"no-session-persistence": None},
        env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""},
        cwd=cwd,
        model=model,
        system_prompt=system_prompt,
        thinking={"type": "disabled"},
    )


def request_prompt(request: Request) -> tuple[str, str]:
    """Split a request into its system text and its prompt text.

    One user message is sent as it is. Few-shot turns, alternating `user` and
    `assistant` and ending on `user`, become one prompt: every turn but the
    last as a JSON list of `{"role", "content"}` objects, then the last user
    turn. JSON escaping keeps the text of a turn from ending that turn.

    Args:
        request: The request to map.

    Returns:
        The system text (empty when the request has none) and the prompt text.

    Raises:
        ProviderFailedError: The messages are not text-only turns alternating
            `user`, `assistant` and ending on `user`, or the system parts are
            not all text.
    """
    messages = request.messages
    for index, message in enumerate(messages):
        if message.role != ("user" if index % 2 == 0 else "assistant"):
            raise ProviderFailedError(_ONE_TEXT_USER_MESSAGE)
    if len(messages) % 2 == 0:
        raise ProviderFailedError(_ONE_TEXT_USER_MESSAGE)
    system = request.system
    if system is None:
        system_text = ""
    elif isinstance(system, str):
        system_text = system
    else:
        system_text = _join_text(system)
    texts = [_join_text(message.parts) for message in messages]
    if len(texts) == 1:
        return system_text, texts[0]
    examples = [
        {"role": message.role, "content": text}
        for message, text in zip(messages[:-1], texts[:-1], strict=True)
    ]
    prompt = (
        f"{_EXAMPLES_HEADER}\n{json.dumps(examples, ensure_ascii=False)}\n\n"
        f"{_ANSWER_HEADER}\n{texts[-1]}"
    )
    return system_text, prompt


def _join_text(parts: tuple[object, ...]) -> str:
    """Concatenate text parts, refusing any other kind of part."""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, TextPart):
            raise ProviderFailedError(_ONE_TEXT_USER_MESSAGE)
        texts.append(part.text)
    return "".join(texts)


def _count(usage: dict[str, object] | None, key: str) -> int | None:
    """Read a token count from a result's usage mapping, if it holds one."""
    value = usage.get(key) if usage is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _input_tokens(usage: dict[str, object] | None) -> int | None:
    """Count the tokens sent: fresh input plus cache writes plus cache reads.

    The CLI reports cached tokens apart from `input_tokens`. A missing cache
    key counts as 0; a missing `input_tokens` leaves the count unknown.
    """
    fresh = _count(usage, "input_tokens")
    if fresh is None:
        return None
    created = _count(usage, "cache_creation_input_tokens") or 0
    read = _count(usage, "cache_read_input_tokens") or 0
    return fresh + created + read


def _meter_result(
    meter: CallMeter, result: ResultMessage, assistant_model: str | None
) -> None:
    """Record a finished call in the meter, whether or not it was an error."""
    meter.completed = True
    meter.cost_usd = result.total_cost_usd
    meter.input_tokens = _input_tokens(result.usage)
    meter.output_tokens = _count(result.usage, "output_tokens")
    meter.model = assistant_model


def _error_reason(result: ResultMessage) -> str:
    """Say why an error result failed, from what the CLI reported."""
    detail = result.result or "; ".join(result.errors or []) or result.subtype
    return f"claude reported an error: {detail}"


class ClaudeAsyncEngine:
    """Answers `dspy.LM` requests through the Claude Agent SDK.

    Every call is one single-turn query with no tools, settings or session
    (`build_options`), run in a throwaway working directory.
    """

    def __init__(
        self,
        model_ref: ModelRef,
        cli_path: Path,
        *,
        query: SdkQuery = claude_agent_sdk.query,
    ) -> None:
        """Bind the engine to a model and a `claude` executable.

        Args:
            model_ref: The model to call; its model part is sent to the CLI.
            cli_path: The resolved, version-checked `claude` executable.
            query: Runs one query; replaced in tests.
        """
        self._model_ref = model_ref
        self._cli_path = cli_path
        self._query = query

    async def complete(self, request: Request) -> Response:
        """Run one claude call and meter its cost.

        The current call meter is filled from the finished call's result before
        any error about that result is raised.

        Args:
            request: Text turns alternating `user`, `assistant` and ending on
                `user`, with optional system text.

        Returns:
            The answer, with the cost under `provider_data["usage"]["cost"]`.

        Raises:
            ProviderUnavailableError: The `claude` executable was not found.
            ProviderFailedError: The request has an unsupported shape, the SDK or
                the CLI failed, no result arrived, or the result is an error.
            CostUnknownError: The result carries no cost.
            TaskOutputError: The result carries no text.
        """
        system_text, prompt = request_prompt(request)
        meter = current_meter()
        assistant_model: str | None = None
        result: ResultMessage | None = None
        with tempfile.TemporaryDirectory(prefix="ijob-claude-") as scratch:
            options = build_options(
                cli_path=self._cli_path,
                model=self._model_ref.model,
                system_prompt=system_text,
                cwd=Path(scratch),
            )
            try:
                async with _closed(
                    self._query(prompt=prompt, options=options)
                ) as messages:
                    async for message in messages:
                        if isinstance(message, AssistantMessage):
                            assistant_model = message.model
                        elif isinstance(message, ResultMessage):
                            # The answer is here: stop, rather than wait for the
                            # CLI to exit, which can take half a minute.
                            result = message
                            _meter_result(meter, message, assistant_model)
                            break
            except CLINotFoundError as exc:
                raise ProviderUnavailableError(str(exc)) from exc
            except ClaudeSDKError as exc:
                reason = f"{type(exc).__name__}: {exc}"
                raise ProviderFailedError(reason) from exc
        if result is None:
            reason = "claude returned no result"
            raise ProviderFailedError(reason)
        if result.total_cost_usd is None:
            raise CostUnknownError(str(self._model_ref))
        if result.is_error:
            raise ProviderFailedError(_error_reason(result))
        if result.result is None:
            raise TaskOutputError(_OUTPUT_LABEL, "the result has no text")
        return Response(
            id=None,
            model=assistant_model or self._model_ref.model,
            message=LmMessage.assistant(result.result),
            finish_reason="stop",
            usage=Usage(
                input_tokens=_input_tokens(result.usage),
                output_tokens=_count(result.usage, "output_tokens"),
            ),
            provider_data={"usage": {"cost": result.total_cost_usd}},
        )

    def stream(self, request: Request) -> NoReturn:
        """Refuse: streaming is not supported.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release nothing; each call owns and removes its own resources."""


class ClaudeSyncEngine:
    """The sync half of the pair `dspy.LM` requires; it never answers."""

    def complete(self, _request: Request) -> NoReturn:
        """Refuse: claude-sdk runs async only.

        Raises:
            ProviderUnavailableError: Always.
        """
        raise ProviderUnavailableError(_ASYNC_ONLY)

    def stream(self, request: Request) -> NoReturn:
        """Refuse: streaming is not supported.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release nothing."""


def claude_engines(
    ref: ModelRef, cli_path: Path
) -> tuple[ClaudeSyncEngine, ClaudeAsyncEngine]:
    """Build the engine pair for a claude-sdk model.

    Args:
        ref: The claude-sdk model reference.
        cli_path: The resolved `claude` executable.

    Returns:
        The sync and async engines to hand to `dspy.LM`.
    """
    return ClaudeSyncEngine(), ClaudeAsyncEngine(ref, cli_path)
