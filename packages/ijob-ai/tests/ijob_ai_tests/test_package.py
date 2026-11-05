import importlib
import importlib.metadata
import importlib.resources
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Modules that must import without loading DSPy, the Claude SDK, pypdf or LiteLLM. Later
# changes extend this list as light modules are added.
LIGHT_MODULES = [
    "ijob_ai",
    "ijob_ai.errors",
    "ijob_ai.llm",
    "ijob_ai.llm.reference",
    "ijob_ai.llm.settings",
    "ijob_ai.llm.meter",
    "ijob_ai.llm.budget",
    "ijob_ai.tasks.versions",
    "ijob_ai.tuning.names",
]

HEAVY_TOP_PACKAGES = ("dspy", "claude_agent_sdk", "pypdf", "litellm", "optuna")

HEAVY_IMPORTS = (
    "import dspy, claude_agent_sdk, pypdf, opentelemetry.sdk.trace, "
    "opentelemetry.exporter.otlp.proto.http.trace_exporter"
)


def test_package_imports() -> None:
    module = importlib.import_module("ijob_ai")
    assert module.__doc__
    assert module.__doc__.strip()
    assert importlib.metadata.version("ijob-ai") == "0.1.0"


def test_package_ships_typing_marker() -> None:
    assert (importlib.resources.files("ijob_ai") / "py.typed").is_file()


def test_package_requires_python_floor() -> None:
    requires = importlib.metadata.metadata("ijob-ai")["Requires-Python"]
    assert requires == ">=3.14.10"


def test_package_import_is_light() -> None:
    code = (
        "import importlib, sys\n"
        f"for name in {LIGHT_MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        f"heavy = {HEAVY_TOP_PACKAGES!r}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_sdk_installed_without_bundled_cli() -> None:
    spec = importlib.util.find_spec("claude_agent_sdk")
    assert spec is not None
    assert spec.origin is not None
    bundled = Path(spec.origin).parent / "_bundled"
    assert sorted(entry.name for entry in bundled.iterdir()) == [".gitignore"]


def test_heavy_imports_emit_no_warnings() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", "-c", HEAVY_IMPORTS],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "module",
    ["ijob_ai.tuning.metrics", "ijob_ai.tuning.optimizers", "ijob_ai.tuning.engine"],
)
def test_tuning_import_does_not_load_optuna(module: str) -> None:
    code = (
        "import sys\n"
        f"import {module}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'optuna'))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_llm_layer_imports_no_tuning_code() -> None:
    code = (
        "import sys\n"
        "import ijob_ai.llm.railed\n"
        "print(sorted(m for m in sys.modules if m.startswith('ijob_ai.tuning')))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"
