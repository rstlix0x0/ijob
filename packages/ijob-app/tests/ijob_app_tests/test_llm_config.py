import tomllib
from importlib.resources import files
from pathlib import Path

import pytest
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import LlmSettings, Task, TracingSettings
from ijob_app.llm_config import read_llm_settings, read_tracing_settings
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
    assert (tracing.max_size_mb, tracing.langfuse_endpoint) == (256, None)


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


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://localhost:3000/", "http://localhost:3000"),
        ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
        ("https://[::1]:3000/", "https://[::1]:3000"),
    ],
)
def test_langfuse_endpoint_local_only(
    tmp_path: Path, endpoint: str, expected: str
) -> None:
    path = _config(tmp_path, f'[tracing]\nlangfuse_endpoint = "{endpoint}"\n')

    assert read_tracing_settings(path).langfuse_endpoint == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://cloud.langfuse.com",
        "ftp://localhost",
        "localhost:3000",
        "http://localhost:notaport",
        "",
    ],
)
def test_langfuse_endpoint_non_local_rejected(tmp_path: Path, endpoint: str) -> None:
    path = _config(tmp_path, f'[tracing]\nlangfuse_endpoint = "{endpoint}"\n')

    with pytest.raises(InvalidValueError) as raised:
        read_tracing_settings(path)

    assert raised.value.field == "tracing.langfuse_endpoint"


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
    "[llm]",
    "max_cost_usd",
    "concurrency",
    "timeout_s",
    "[providers.claude-sdk]",
    "cli_path",
    "[tracing]",
    "max_size_mb",
    "langfuse_endpoint",
)


def _template_lines() -> list[str]:
    template = files("ijob_app") / "data" / "config_template.toml"
    return template.read_text(encoding="utf-8").splitlines()


def test_template_parses_and_yields_defaults(tmp_path: Path) -> None:
    path = _config(tmp_path, "\n".join(_template_lines()))

    assert tomllib.loads(path.read_text(encoding="utf-8")) == {}
    assert read_llm_settings(path) == LlmSettings(config_path=path)
    assert read_tracing_settings(path) == TracingSettings()


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
    tracing = read_tracing_settings(enabled)

    assert set(llm.models) == set(Task)
    assert llm.cli_path == Path("/usr/local/bin/claude")
    assert tracing.langfuse_endpoint == "http://localhost:3000"
