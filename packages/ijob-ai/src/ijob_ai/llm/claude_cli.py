"""Locate the `claude` command-line tool and check that it is recent enough."""

import asyncio
import contextlib
import os
import re
import shutil
import signal
from collections.abc import Callable
from pathlib import Path

from ijob_ai.errors import ProviderUnavailableError

MIN_VERSION = (2, 1, 248)

_VERSION = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)")


def resolve_cli(
    configured: Path | None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """Find the `claude` executable.

    Args:
        configured: Path from configuration, with `~` expanded; `None` searches
            `PATH` for `claude`.
        which: Looks a command name up on `PATH`.

    Returns:
        The path of an executable file.

    Raises:
        ProviderUnavailableError: Nothing was found, or what was found is not
            an executable file.
    """
    if configured is not None:
        path = configured.expanduser()
    else:
        found = which("claude")
        if found is None:
            reason = "claude not found on PATH; install it or set its configured path"
            raise ProviderUnavailableError(reason)
        path = Path(found)
    if not path.is_file() or not os.access(path, os.X_OK):
        reason = f"{path} is not an executable file; check the claude CLI path"
        raise ProviderUnavailableError(reason)
    return path


async def check_version(path: Path, *, timeout_s: float = 10) -> tuple[int, int, int]:
    """Run `<path> --version` and require a supported release.

    Args:
        path: The `claude` executable.
        timeout_s: Seconds to wait for the command.

    Returns:
        The `(major, minor, patch)` the command reports.

    Raises:
        ProviderUnavailableError: The command failed, timed out, printed
            something that is not a version, or reported a version older than
            `MIN_VERSION`.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        reason = f"cannot run {path}: {exc}"
        raise ProviderUnavailableError(reason) from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        reason = f"{path} --version did not finish within {timeout_s} s"
        raise ProviderUnavailableError(reason) from exc
    if proc.returncode != 0:
        reason = f"{path} --version exited with status {proc.returncode}"
        raise ProviderUnavailableError(reason)
    text = stdout.decode("utf-8", errors="replace").strip()
    match = _VERSION.match(text)
    if match is None:
        reason = f"{path} --version printed {text!r}, which is not a version"
        raise ProviderUnavailableError(reason)
    version = (int(match[1]), int(match[2]), int(match[3]))
    if version < MIN_VERSION:
        needed = ".".join(map(str, MIN_VERSION))
        found = ".".join(map(str, version))
        reason = f"claude {found} is too old; install {needed} or newer"
        raise ProviderUnavailableError(reason)
    return version
