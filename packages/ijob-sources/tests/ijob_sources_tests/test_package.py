import importlib
import importlib.metadata
import importlib.resources


def test_package_imports() -> None:
    module = importlib.import_module("ijob_sources")
    assert module.__doc__
    assert module.__doc__.strip()
    assert importlib.metadata.version("ijob-sources") == "0.1.0"


def test_package_ships_typing_marker() -> None:
    assert (importlib.resources.files("ijob_sources") / "py.typed").is_file()
