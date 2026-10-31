import importlib
import importlib.metadata
import importlib.resources


def test_package_imports() -> None:
    module = importlib.import_module("ijob_core")
    assert module.__doc__
    assert module.__doc__.strip()
    assert importlib.metadata.version("ijob-core") == "0.1.0"


def test_package_ships_typing_marker() -> None:
    assert (importlib.resources.files("ijob_core") / "py.typed").is_file()


def test_package_requires_python_floor() -> None:
    requires = importlib.metadata.metadata("ijob-core")["Requires-Python"]
    assert requires == ">=3.14.10"
