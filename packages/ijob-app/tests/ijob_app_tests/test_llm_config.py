import tomllib
from importlib.resources import files
from pathlib import Path

import pytest
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import (
    EvalSettings,
    LlmSettings,
    PhoenixSettings,
    Task,
    TracingSettings,
)
from ijob_app.llm_config import (
    read_eval_settings,
    read_llm_settings,
    read_phoenix_settings,
    read_tracing_settings,
)
from ijob_core.errors import InvalidValueError


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_tables_absent(tmp_path: Path) -> None:
    path = _config(tmp_path, "[filters]\nrequire_salary = false\n")

    llm = read_llm_settings(path)
    tracing = read_tracing_settings(path)

    assert llm == LlmSettings(config_path=path)
    assert (llm.max_cost_usd, llm.concurrency, llm.timeout_s) == (0.50, 4, 120)
    assert llm.cli_path is None
    assert dict(llm.models) == {}
    assert tracing == TracingSettings()
    assert tracing.max_size_mb == 256


def test_defaults_when_file_absent(tmp_path: Path) -> None:
    path = tmp_path / "missing.toml"

    assert read_llm_settings(path) == LlmSettings(config_path=path)
    assert read_tracing_settings(path) == TracingSettings()


def test_models_parsed(tmp_path: Path) -> None:
    path = _config(tmp_path, '[models]\njudge = "openrouter/google/gemini-3.8-flash"\n')

    llm = read_llm_settings(path)

    assert dict(llm.models) == {
        Task.JUDGE: ModelRef(Provider.OPENROUTER, "google/gemini-3.8-flash")
    }
    assert llm.config_path == path


def test_models_accepts_source_recipe(tmp_path: Path) -> None:
    path = _config(
        tmp_path, '[models]\nsource_recipe = "claude-sdk/claude-sonnet-5-5"\n'
    )

    llm = read_llm_settings(path)

    assert dict(llm.models) == {
        Task.SOURCE_RECIPE: ModelRef(Provider.CLAUDE_SDK, "claude-sonnet-5-5")
    }


def test_new_tasks_are_configurable(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        "[models]\n"
        'source_agent = "claude-sdk/claude-sonnet-5-5"\n'
        'explore_agent = "claude-sdk/claude-sonnet-5-5"\n'
        'eval_grader = "openrouter/openai/gpt-6.1-sol"\n',
    )

    llm = read_llm_settings(path)

    assert dict(llm.models) == {
        Task.SOURCE_AGENT: ModelRef(Provider.CLAUDE_SDK, "claude-sonnet-5-5"),
        Task.EXPLORE_AGENT: ModelRef(Provider.CLAUDE_SDK, "claude-sonnet-5-5"),
        Task.EVAL_GRADER: ModelRef(Provider.OPENROUTER, "openai/gpt-6.1-sol"),
    }


def test_limits_and_cli_path_parsed(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        "[llm]\nmax_cost_usd = 2\nconcurrency = 8\ntimeout_s = 30\n"
        '[providers.claude-sdk]\ncli_path = "~/bin/claude"\n',
    )

    llm = read_llm_settings(path)

    assert (llm.max_cost_usd, llm.concurrency, llm.timeout_s) == (2, 8, 30)
    assert llm.cli_path == Path("~/bin/claude").expanduser()


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[llm]\nmax_cost_usd = 0\n", "llm.max_cost_usd"),
        ("[llm]\nmax_cost_usd = 101\n", "llm.max_cost_usd"),
        ("[llm]\nmax_cost_usd = true\n", "llm.max_cost_usd"),
        ("[llm]\nconcurrency = 0\n", "llm.concurrency"),
        ("[llm]\nconcurrency = 17\n", "llm.concurrency"),
        ("[llm]\nconcurrency = 2.5\n", "llm.concurrency"),
        ("[llm]\ntimeout_s = 9\n", "llm.timeout_s"),
        ("[llm]\ntimeout_s = 601\n", "llm.timeout_s"),
        ('[providers.claude-sdk]\ncli_path = ""\n', "providers.claude-sdk.cli_path"),
        ("[providers.claude-sdk]\ncli_path = 3\n", "providers.claude-sdk.cli_path"),
        ('[models]\njudge = "no-provider"\n', "models.judge"),
        ('[models]\njudge = "nope/some-model"\n', "models.judge"),
        ("[models]\njudge = 3\n", "models.judge"),
        ("llm = 3\n", "llm"),
    ],
)
def test_invalid_values_rejected(tmp_path: Path, text: str, key: str) -> None:
    path = _config(tmp_path, text)

    with pytest.raises(InvalidValueError) as raised:
        read_llm_settings(path)

    assert raised.value.field == key


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[tracing]\nmax_size_mb = 15\n", "tracing.max_size_mb"),
        ("[tracing]\nmax_size_mb = 10241\n", "tracing.max_size_mb"),
        ("[tracing]\nmax_size_mb = 16.5\n", "tracing.max_size_mb"),
    ],
)
def test_invalid_tracing_values_rejected(tmp_path: Path, text: str, key: str) -> None:
    path = _config(tmp_path, text)

    with pytest.raises(InvalidValueError) as raised:
        read_tracing_settings(path)

    assert raised.value.field == key


def test_tracing_size_bounds_accepted(tmp_path: Path) -> None:
    low = _config(tmp_path, "[tracing]\nmax_size_mb = 16\n")
    assert read_tracing_settings(low).max_size_mb == 16

    high = _config(tmp_path, "[tracing]\nmax_size_mb = 10240\n")
    assert read_tracing_settings(high).max_size_mb == 10240


def test_unreadable_config_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, "[llm\n")

    with pytest.raises(InvalidValueError) as raised:
        read_llm_settings(path)

    assert raised.value.field == "config"


def test_old_langfuse_key_is_an_unknown_key(tmp_path: Path) -> None:
    path = _config(tmp_path, '[tracing]\nlangfuse_endpoint = "http://localhost:3000"\n')

    with pytest.raises(InvalidValueError) as raised:
        read_tracing_settings(path)

    assert raised.value.field == "tracing.langfuse_endpoint"
    assert raised.value.reason == "unknown key"


def test_phoenix_endpoint_defaults_to_local(tmp_path: Path) -> None:
    for path in (tmp_path / "missing.toml", _config(tmp_path, "[llm]\n")):
        settings = read_phoenix_settings(path)

        assert settings == PhoenixSettings()
        assert settings.endpoint == "http://127.0.0.1:6006"


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://localhost:6006/", "http://localhost:6006"),
        ("http://127.0.0.1:6006", "http://127.0.0.1:6006"),
        ("https://[::1]:6006/", "https://[::1]:6006"),
    ],
)
def test_phoenix_endpoint_local_only(
    tmp_path: Path, endpoint: str, expected: str
) -> None:
    path = _config(tmp_path, f'[phoenix]\nendpoint = "{endpoint}"\n')

    assert read_phoenix_settings(path).endpoint == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://app.phoenix.arize.com",
        "ftp://localhost",
        "localhost:6006",
        "http://localhost:notaport",
        "",
    ],
)
def test_phoenix_endpoint_non_local_rejected(tmp_path: Path, endpoint: str) -> None:
    path = _config(tmp_path, f'[phoenix]\nendpoint = "{endpoint}"\n')

    with pytest.raises(InvalidValueError) as raised:
        read_phoenix_settings(path)

    assert raised.value.field == "phoenix.endpoint"


def test_phoenix_endpoint_must_be_a_string(tmp_path: Path) -> None:
    path = _config(tmp_path, "[phoenix]\nendpoint = 6006\n")

    with pytest.raises(InvalidValueError) as raised:
        read_phoenix_settings(path)

    assert raised.value.field == "phoenix.endpoint"


def test_phoenix_unknown_key_is_refused(tmp_path: Path) -> None:
    path = _config(tmp_path, "[phoenix]\nfoo = 1\n")

    with pytest.raises(InvalidValueError) as raised:
        read_phoenix_settings(path)

    assert raised.value.field == "phoenix.foo"
    assert raised.value.reason == "unknown key"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[llm]\nfoo = 1\n", "llm.foo"),
        ('[models]\nranker = "claude-sdk/x"\n', "models.ranker"),
        ('[providers.other]\ncli_path = "x"\n', "providers.other"),
        ('[providers.claude-sdk]\nfoo = "x"\n', "providers.claude-sdk.foo"),
    ],
)
def test_unknown_keys_rejected(tmp_path: Path, text: str, key: str) -> None:
    path = _config(tmp_path, text)

    with pytest.raises(InvalidValueError) as raised:
        read_llm_settings(path)

    assert raised.value.field == key
    assert raised.value.reason == "unknown key"


def test_unknown_tracing_key_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, "[tracing]\nfoo = 1\n")

    with pytest.raises(InvalidValueError) as raised:
        read_tracing_settings(path)

    assert raised.value.field == "tracing.foo"


_NEW_KEYS = (
    "[models]",
    "cv_profile",
    "instructions",
    "judge",
    "scorer",
    "source_recipe",
    "job_extract",
    "source_agent",
    "explore_agent",
    "eval_grader",
    "[llm]",
    "max_cost_usd",
    "concurrency",
    "timeout_s",
    "[providers.claude-sdk]",
    "cli_path",
    "[tracing]",
    "max_size_mb",
    "[phoenix]",
    "endpoint",
    "[eval]",
    "jury",
    "total_cost_usd",
)


def _template_lines() -> list[str]:
    template = files("ijob_app") / "data" / "config_template.toml"
    return template.read_text(encoding="utf-8").splitlines()


def test_template_parses_and_yields_defaults(tmp_path: Path) -> None:
    path = _config(tmp_path, "\n".join(_template_lines()))

    assert tomllib.loads(path.read_text(encoding="utf-8")) == {}
    assert read_llm_settings(path) == LlmSettings(config_path=path)
    assert read_tracing_settings(path) == TracingSettings()
    assert read_phoenix_settings(path) == PhoenixSettings()
    assert read_eval_settings(path) == EvalSettings()


def test_template_mentions_every_new_key() -> None:
    comments = "\n".join(line for line in _template_lines() if line.startswith("#"))

    for key in _NEW_KEYS:
        assert key in comments


def test_template_new_settings_are_valid_once_uncommented(tmp_path: Path) -> None:
    lines = _template_lines()
    start = lines.index("# [models]")
    enabled = _config(
        tmp_path,
        "\n".join(
            line.removeprefix("# ")
            for line in lines[start:]
            if line.startswith("# ") and ("=" in line or line.startswith("# ["))
        ),
    )

    llm = read_llm_settings(enabled)
    evals = read_eval_settings(enabled)

    assert set(llm.models) == set(Task)
    assert evals.jury is not None
    assert 2 <= len(evals.jury) <= 5
    assert llm.cli_path == Path("/usr/local/bin/claude")
    assert read_phoenix_settings(enabled).endpoint == "http://127.0.0.1:6006"


_JURY = (
    '"claude-sdk/claude-opus-5-5", '
    '"openrouter/openai/gpt-6.1-sol", '
    '"openrouter/google/gemini-4-argon"'
)


def test_eval_defaults(tmp_path: Path) -> None:
    for path in (
        tmp_path / "missing.toml",
        _config(tmp_path, "[filters]\nrequire_salary = false\n"),
    ):
        evals = read_eval_settings(path)

        assert evals.jury is None
        assert (evals.max_cost_usd, evals.total_cost_usd) == (4, 10)


def test_eval_settings_parsed(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        f"[eval]\njury = [{_JURY}]\nmax_cost_usd = 2.5\ntotal_cost_usd = 100\n",
    )

    evals = read_eval_settings(path)

    assert evals.jury == (
        ModelRef(Provider.CLAUDE_SDK, "claude-opus-5-5"),
        ModelRef(Provider.OPENROUTER, "openai/gpt-6.1-sol"),
        ModelRef(Provider.OPENROUTER, "google/gemini-4-argon"),
    )
    assert (evals.max_cost_usd, evals.total_cost_usd) == (2.5, 100)


def test_eval_unknown_key_is_refused(tmp_path: Path) -> None:
    path = _config(tmp_path, "[eval]\nfoo = 1\n")

    with pytest.raises(InvalidValueError) as raised:
        read_eval_settings(path)

    assert raised.value.field == "eval.foo"


@pytest.mark.parametrize(
    "jury",
    [
        '["claude-sdk/claude-opus-5-5"]',
        "[" + ", ".join(f'"claude-sdk/m{i}"' for i in range(6)) + "]",
        '["claude-sdk/m", "claude-sdk/m"]',
        '"claude-sdk/m"',
        "[1, 2]",
        "[]",
    ],
)
def test_jury_size_and_duplicates_are_refused(tmp_path: Path, jury: str) -> None:
    path = _config(tmp_path, f"[eval]\njury = {jury}\n")

    with pytest.raises(InvalidValueError) as raised:
        read_eval_settings(path)

    assert raised.value.field == "eval.jury"


def test_jury_bad_reference_is_refused(tmp_path: Path) -> None:
    path = _config(tmp_path, '[eval]\njury = ["claude-sdk/m", "nope/m"]\n')

    with pytest.raises(InvalidValueError) as raised:
        read_eval_settings(path)

    assert raised.value.field == "eval.jury"


@pytest.mark.parametrize("key", ["max_cost_usd", "total_cost_usd"])
@pytest.mark.parametrize("value", ["0", "-1", "101", '"4"', "true"])
def test_eval_cost_ranges(tmp_path: Path, key: str, value: str) -> None:
    path = _config(tmp_path, f"[eval]\n{key} = {value}\n")

    with pytest.raises(InvalidValueError) as raised:
        read_eval_settings(path)

    assert raised.value.field == f"eval.{key}"
