import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]
_IMAGE = "image: arizephoenix/phoenix:21.0.0"


def _text(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def _shipped_files() -> list[Path]:
    paths = [_ROOT / "README.md", _ROOT / ".env.example", _ROOT / "compose.yaml"]
    paths += [path for path in (_ROOT / "docs").rglob("*") if path.is_file()]
    for source in (_ROOT / "packages").glob("*/src"):
        paths += [path for path in source.rglob("*") if path.is_file()]
    return paths


def test_langfuse_is_named_nowhere() -> None:
    hits: list[str] = []
    for path in _shipped_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "langfuse" in text.lower():
            hits.append(str(path.relative_to(_ROOT)))

    assert hits == []


def test_compose_binds_only_to_localhost() -> None:
    compose = _text("compose.yaml")

    assert "127.0.0.1:6006:6006" in compose
    assert _IMAGE in compose
    assert compose.count("ports:") == 1
    services = compose.split("services:", 1)[1].split("\nvolumes:", 1)[0]
    assert re.findall(r"^ {2}(\w[\w-]*):\s*$", services, re.MULTILINE) == ["phoenix"]
    assert "phoenix_data:/mnt/data" in compose


@pytest.mark.parametrize(
    "line",
    [
        'restart: "no"',
        "mem_limit: 2g",
        'PHOENIX_TELEMETRY_ENABLED: "false"',
        'PHOENIX_ALLOW_EXTERNAL_RESOURCES: "false"',
        'PHOENIX_ENABLE_MCP_SERVER: "false"',
        'PHOENIX_ENABLE_AUTH: "False"',
        "PHOENIX_WORKING_DIR: /mnt/data",
    ],
)
def test_compose_turns_off_what_ijob_does_not_use(line: str) -> None:
    assert line in [entry.strip() for entry in _text("compose.yaml").splitlines()]


def test_env_example_says_nothing_loads_it() -> None:
    text = _text(".env.example")

    assert "OPENROUTER_API_KEY" in text
    assert "OTEL_" not in text
    assert "export" in text
