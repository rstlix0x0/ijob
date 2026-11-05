import re
from pathlib import Path

import pytest
import typer
from ijob_app.cli import app

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


_PUBLISHED = re.compile(r"^127\.0\.0\.1:\d+:\d+(/\w+)?$")


def _published_ports(compose: str) -> list[str]:
    """Return every entry under a `ports:` key, with quotes removed."""
    entries: list[str] = []
    lines = compose.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "ports:":
            continue
        indent = len(line) - len(line.lstrip())
        for entry in lines[index + 1 :]:
            if len(entry) - len(entry.lstrip()) <= indent and entry.strip():
                break
            if entry.strip().startswith("- "):
                entries.append(entry.strip()[2:].strip().strip("\"'"))
    return entries


def _binds_only_to_localhost(compose: str) -> bool:
    ports = _published_ports(compose)
    return (
        bool(ports)
        and "network_mode" not in compose
        and all(_PUBLISHED.match(port) for port in ports)
    )


def test_compose_binds_only_to_localhost() -> None:
    compose = _text("compose.yaml")

    assert _published_ports(compose) == ["127.0.0.1:6006:6006"]
    assert _binds_only_to_localhost(compose)
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


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("- 127.0.0.1:6006:6006", "- 127.0.0.1:6006:6006\n      - 0.0.0.0:4317:4317"),
        ("- 127.0.0.1:6006:6006", "- 127.0.0.1:6006:6006\n      - 4317:4317"),
        ("    ports:", "    network_mode: host\n    ports:"),
        ("127.0.0.1:6006:6006", "6006:6006"),
    ],
    ids=["second-all-interfaces-port", "second-bare-port", "host-network", "bare-port"],
)
def test_localhost_check_catches_a_widened_compose_file(old: str, new: str) -> None:
    compose = _text("compose.yaml")
    widened = compose.replace(old, new)

    assert widened != compose
    assert not _binds_only_to_localhost(widened)


def _tune_commands() -> list[str]:
    """Return the commands `ijob tune --help` lists."""
    group = typer.main.get_command(app)
    assert isinstance(group, typer.core.TyperGroup)
    tune = group.commands["tune"]
    assert isinstance(tune, typer.core.TyperGroup)
    return list(tune.commands)


@pytest.mark.parametrize("name", ["screen", "run", "report", "list", "accept", "clear"])
def test_tuning_doc_names_every_tune_command(name: str) -> None:
    assert name in _tune_commands()
    assert f"ijob tune {name}" in _text("docs/tuning.md")


def test_evals_doc_names_add() -> None:
    assert "eval label --add" in _text("docs/evals.md")


def test_readme_links_the_tuning_doc() -> None:
    match = re.search(r"\]\(([^)#]*tuning\.md)(#[^)]*)?\)", _text("README.md"))

    assert match is not None
    assert (_ROOT / match.group(1)).is_file()


def test_readme_links_the_observability_doc() -> None:
    match = re.search(r"\]\(([^)#]*observability\.md)(#[^)]*)?\)", _text("README.md"))

    assert match is not None
    assert (_ROOT / match.group(1)).is_file()


@pytest.mark.parametrize("name", ["show", "prune", "export"])
def test_observability_doc_names_every_traces_command(name: str) -> None:
    group = typer.main.get_command(app)
    assert isinstance(group, typer.core.TyperGroup)
    traces = group.commands["traces"]
    assert isinstance(traces, typer.core.TyperGroup)

    assert name in traces.commands
    assert f"ijob traces {name}" in _text("docs/observability.md")
