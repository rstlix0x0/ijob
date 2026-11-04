"""LLM, tracing and eval settings read from the user's config file.

The `[models]`, `[llm]`, `[providers.claude-sdk]`, `[tracing]` and `[eval]`
tables become `LlmSettings`, `TracingSettings` and `EvalSettings`. A missing
file or table gives the defaults; an unknown key or an out-of-range value
raises `InvalidValueError` naming the key.
"""

import tomllib
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from ijob_ai.llm.reference import ModelRef, parse_model_ref
from ijob_ai.llm.settings import EvalSettings, LlmSettings, Task, TracingSettings
from ijob_core.errors import InvalidValueError

type _Table = dict[str, object]

_LLM_KEYS: Final = frozenset({"max_cost_usd", "concurrency", "timeout_s"})
_CLAUDE_SDK: Final = "claude-sdk"
_CLAUDE_SDK_KEYS: Final = frozenset({"cli_path"})
_TRACING_KEYS: Final = frozenset({"max_size_mb", "langfuse_endpoint"})
_EVAL_KEYS: Final = frozenset({"jury", "max_cost_usd", "total_cost_usd"})
_JURY_SIZE: Final = range(2, 6)
_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_COST_USD: Final = 100
_CONCURRENCY: Final = range(1, 17)
_TIMEOUT_S: Final = range(10, 601)
_MAX_SIZE_MB: Final = range(16, 10_241)


def read_llm_settings(config_path: Path) -> LlmSettings:
    """Read the model, limit and provider settings of the LLM layer.

    Args:
        config_path: The user's config file; it need not exist.

    Returns:
        The settings, with defaults for every absent table or key.

    Raises:
        InvalidValueError: If the file cannot be read or parsed, a table holds
            an unknown key, or a value is out of range.
    """
    config = _read(config_path)
    defaults = LlmSettings(config_path=config_path)
    models = _models(_section(config, "models"))
    llm = _section(config, "llm")
    _only_keys(llm, _LLM_KEYS, "llm")
    providers = _section(config, "providers")
    _only_keys(providers, frozenset({_CLAUDE_SDK}), "providers")
    claude_sdk_key = f"providers.{_CLAUDE_SDK}"
    claude_sdk = _section(providers, _CLAUDE_SDK, claude_sdk_key)
    _only_keys(claude_sdk, _CLAUDE_SDK_KEYS, claude_sdk_key)
    return LlmSettings(
        config_path=config_path,
        models=models,
        max_cost_usd=_max_cost(llm.get("max_cost_usd"), defaults.max_cost_usd),
        concurrency=_bounded_int(
            llm.get("concurrency"),
            "llm.concurrency",
            _CONCURRENCY,
            defaults.concurrency,
        ),
        timeout_s=_bounded_int(
            llm.get("timeout_s"), "llm.timeout_s", _TIMEOUT_S, int(defaults.timeout_s)
        ),
        cli_path=_cli_path(claude_sdk.get("cli_path"), f"{claude_sdk_key}.cli_path"),
    )


def read_tracing_settings(config_path: Path) -> TracingSettings:
    """Read the trace store and Langfuse settings.

    Args:
        config_path: The user's config file; it need not exist.

    Returns:
        The settings, with defaults for every absent table or key.

    Raises:
        InvalidValueError: If the file cannot be read or parsed, the table
            holds an unknown key, a value is out of range, or the Langfuse
            endpoint is not a local `http` or `https` URL.
    """
    tracing = _section(_read(config_path), "tracing")
    _only_keys(tracing, _TRACING_KEYS, "tracing")
    defaults = TracingSettings()
    return TracingSettings(
        max_size_mb=_bounded_int(
            tracing.get("max_size_mb"),
            "tracing.max_size_mb",
            _MAX_SIZE_MB,
            defaults.max_size_mb,
        ),
        langfuse_endpoint=_endpoint(tracing.get("langfuse_endpoint")),
    )


def read_eval_settings(config_path: Path) -> EvalSettings:
    """Read the jury and spending caps of the eval commands.

    Args:
        config_path: The user's config file; it need not exist.

    Returns:
        The settings; an absent jury reads as None, and absent caps as 4 and
        10 US dollars.

    Raises:
        InvalidValueError: If the file cannot be read or parsed, the table
            holds an unknown key, the jury is not two to five different valid
            model references, or a cap is not above 0 and at most 100.
    """
    table = _section(_read(config_path), "eval")
    _only_keys(table, _EVAL_KEYS, "eval")
    defaults = EvalSettings()
    return EvalSettings(
        jury=_jury(table.get("jury")),
        max_cost_usd=_cost(
            table.get("max_cost_usd"), "eval.max_cost_usd", defaults.max_cost_usd
        ),
        total_cost_usd=_cost(
            table.get("total_cost_usd"), "eval.total_cost_usd", defaults.total_cost_usd
        ),
    )


def _read(path: Path) -> _Table:
    """Read the config file; an absent file reads as empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        raise InvalidValueError("config", f"{path}: {error}") from error
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise InvalidValueError("config", f"{path}: {error}") from error


def _section(parent: _Table, name: str, key: str | None = None) -> _Table:
    """Return the table `name` of `parent`, empty when absent."""
    value = parent.get(name, {})
    if not isinstance(value, dict):
        raise InvalidValueError(key or name, "not a table")
    return value


def _only_keys(table: _Table, allowed: frozenset[str], prefix: str) -> None:
    """Reject a key the table is not meant to hold."""
    for key in table:
        if key not in allowed:
            raise InvalidValueError(f"{prefix}.{key}", "unknown key")


def _models(table: _Table) -> dict[Task, ModelRef]:
    """Parse `[models]`, one reference per configured task."""
    models: dict[Task, ModelRef] = {}
    for name, value in table.items():
        key = f"models.{name}"
        try:
            task = Task(name)
        except ValueError:
            raise InvalidValueError(key, "unknown key") from None
        if not isinstance(value, str):
            raise InvalidValueError(key, f"{value!r} is not a string")
        models[task] = parse_model_ref(value, key)
    return models


def _max_cost(value: object, default: float) -> float:
    """Read `[llm].max_cost_usd`: a number above 0 and at most 100."""
    return _cost(value, "llm.max_cost_usd", default)


def _cost(value: object, key: str, default: float) -> float:
    """Read a spending cap: a number above 0 and at most 100."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidValueError(key, f"{value!r} is not a number")
    if not 0 < value <= _MAX_COST_USD:
        raise InvalidValueError(key, f"{value!r} is not above 0 and at most 100")
    return float(value)


def _jury(value: object) -> tuple[ModelRef, ...] | None:
    """Read `[eval].jury`: two to five different model references."""
    key = "eval.jury"
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidValueError(key, f"{value!r} is not a list")
    if len(value) not in _JURY_SIZE:
        raise InvalidValueError(
            key,
            f"{len(value)} entries; use between {_JURY_SIZE.start} and "
            f"{_JURY_SIZE.stop - 1}",
        )
    jury: list[ModelRef] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidValueError(key, f"{item!r} is not a string")
        ref = parse_model_ref(item, key)
        if ref in jury:
            raise InvalidValueError(key, f"{item!r} appears twice")
        jury.append(ref)
    return tuple(jury)


def _bounded_int(value: object, key: str, allowed: range, default: int) -> int:
    """Read an integer that must lie within `allowed`."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidValueError(key, f"{value!r} is not an integer")
    if value not in allowed:
        raise InvalidValueError(
            key, f"{value} is not between {allowed.start} and {allowed.stop - 1}"
        )
    return value


def _cli_path(value: object, key: str) -> Path | None:
    """Read `cli_path`, expanding `~`; its existence is checked at use."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidValueError(key, f"{value!r} is not a path")
    return Path(value).expanduser()


def _endpoint(value: object) -> str | None:
    """Read `langfuse_endpoint`: a local `http` or `https` URL."""
    key = "tracing.langfuse_endpoint"
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError(key, f"{value!r} is not a string")
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
    return value.removesuffix("/")
