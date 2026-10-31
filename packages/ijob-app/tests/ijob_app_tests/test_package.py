import importlib
import importlib.metadata
import importlib.resources
import re


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
