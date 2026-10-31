import importlib
import importlib.metadata
import importlib.resources
import re


def test_package_imports() -> None:
    module = importlib.import_module("ijob_sources")
    assert module.__doc__
    assert module.__doc__.strip()
    assert importlib.metadata.version("ijob-sources") == "0.1.0"


def test_package_ships_typing_marker() -> None:
    assert (importlib.resources.files("ijob_sources") / "py.typed").is_file()


def test_package_requires_python_floor() -> None:
    requires = importlib.metadata.metadata("ijob-sources")["Requires-Python"]
    assert requires == ">=3.14.10"


def test_runtime_dependencies_declared() -> None:
    requirements = importlib.metadata.requires("ijob-sources") or []
    declared = {
        match.group(0)
        for requirement in requirements
        if (match := re.match(r"[A-Za-z0-9_.-]+", requirement))
    }
    assert {"httpx", "ijob-core"} <= declared
