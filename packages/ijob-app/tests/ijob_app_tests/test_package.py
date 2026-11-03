import importlib
import importlib.metadata
import importlib.resources
import re
import subprocess
import sys

import pytest


def test_package_imports() -> None:
    module = importlib.import_module("ijob_app")
    assert module.__doc__
    assert module.__doc__.strip()
    assert importlib.metadata.version("ijob-app") == "0.1.0"


def test_package_ships_typing_marker() -> None:
    assert (importlib.resources.files("ijob_app") / "py.typed").is_file()


def test_package_requires_python_floor() -> None:
    requires = importlib.metadata.metadata("ijob-app")["Requires-Python"]
    assert requires == ">=3.14.10"


def test_runtime_dependencies_declared() -> None:
    requirements = importlib.metadata.requires("ijob-app") or []
    declared = {
        match.group(0)
        for requirement in requirements
        if (match := re.match(r"[A-Za-z0-9_.-]+", requirement))
    }
    assert {"typer", "ijob-core", "ijob-sources"} <= declared


def test_cli_import_leaves_deepeval_unloaded() -> None:
    probe = (
        "import importlib.util, sys;"
        "import ijob_app.cli;"
        "assert importlib.util.find_spec('deepeval'), 'deepeval not installed';"
        "assert 'deepeval' not in sys.modules, 'deepeval was imported'"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# Third-party pytest plugins allowed to load in the session. Every other
# `pytest11` entry point (deepeval and its dependencies bring several) must be
# switched off by a `-p no:` flag in the root `pyproject.toml`.
_ALLOWED_THIRD_PARTY_PLUGINS = frozenset({"anyio", "platformdirs"})


def test_only_allowed_third_party_plugins_load(pytestconfig: pytest.Config) -> None:
    installed = {
        entry_point.name
        for entry_point in importlib.metadata.entry_points(group="pytest11")
    }
    loaded = {name for name in installed if pytestconfig.pluginmanager.has_plugin(name)}
    unexpected = loaded - _ALLOWED_THIRD_PARTY_PLUGINS
    assert not unexpected, (
        f"plugins loaded without a `-p no:` flag: {sorted(unexpected)}"
    )
