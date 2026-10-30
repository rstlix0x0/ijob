import importlib
import importlib.metadata
import importlib.resources


def test_package_imports() -> None:
    module = importlib.import_module("ijob_app")
    assert module.__doc__
    assert module.__doc__.strip()
    assert importlib.metadata.version("ijob-app") == "0.1.0"


def test_package_ships_typing_marker() -> None:
    assert (importlib.resources.files("ijob_app") / "py.typed").is_file()
