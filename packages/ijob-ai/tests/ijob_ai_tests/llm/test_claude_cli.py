import asyncio
import time
from pathlib import Path

import pytest
from ijob_ai.errors import ProviderUnavailableError
from ijob_ai.llm.claude_cli import check_version, resolve_cli


def _script(tmp_path: Path, body: str, name: str = "claude", mode: int = 0o755) -> Path:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_resolve_uses_configured_path(tmp_path: Path) -> None:
    script = _script(tmp_path, "exit 0")
    other = tmp_path / "other"
    assert resolve_cli(script, which=lambda _name: str(other)) == script


def test_resolve_falls_back_to_which(tmp_path: Path) -> None:
    script = _script(tmp_path, "exit 0")
    assert resolve_cli(None, which=lambda _name: str(script)) == script


def test_resolve_refuses_when_which_finds_nothing() -> None:
    with pytest.raises(ProviderUnavailableError):
        resolve_cli(None, which=lambda _name: None)


def test_resolve_refuses_non_executable(tmp_path: Path) -> None:
    script = _script(tmp_path, "exit 0", mode=0o644)
    with pytest.raises(ProviderUnavailableError, match=str(script)):
        resolve_cli(script, which=lambda _name: None)


def test_resolve_refuses_missing_configured_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(ProviderUnavailableError, match=str(missing)):
        resolve_cli(missing, which=lambda _name: None)


def test_resolve_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(tmp_path, "exit 0")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_cli(Path("~/claude"), which=lambda _name: None) == script


def test_check_version_parses(tmp_path: Path) -> None:
    script = _script(tmp_path, "echo '2.1.316 (Claude Code)'")
    assert asyncio.run(check_version(script)) == (2, 1, 316)


def test_check_version_too_old(tmp_path: Path) -> None:
    script = _script(tmp_path, "echo '2.1.247 (Claude Code)'")
    with pytest.raises(ProviderUnavailableError, match=r"2\.1\.247"):
        asyncio.run(check_version(script))


def test_check_version_unparsable(tmp_path: Path) -> None:
    script = _script(tmp_path, "echo banana")
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(check_version(script))


def test_check_version_nonzero_exit(tmp_path: Path) -> None:
    script = _script(tmp_path, "echo '2.1.316 (Claude Code)'; exit 3")
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(check_version(script))


def test_check_version_timeout(tmp_path: Path) -> None:
    script = _script(tmp_path, "sleep 60")
    started = time.monotonic()
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(check_version(script, timeout_s=0.2))
    # far below the child's 60 s, with room for a busy machine
    assert time.monotonic() - started < 30
