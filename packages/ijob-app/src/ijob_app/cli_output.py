"""How a command reports back: drawn on a terminal, plain in a pipe.

Everything a command says outside its main result goes through here, so each kind
of message looks the same in every command: a finished action, an error with its
fix, a note, what a model cost, a question, and a wait that is not silent.
"""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, override

import typer
from rich.text import Text

from ijob_app.llm_ops import OperationReport, cost_line
from ijob_app.view import (
    AMBER,
    GROUND,
    MUTED,
    failed_line,
    fix_line,
    is_terminal,
    make_console,
    notice_line,
    ok_line,
)

_FIX_MARKERS: Final = ("; run: ", ", run: ")
_NOTE_PREFIXES: Final = ("note: ", "warning: ")
_SPINNER: Final = "dots"
_SOURCES_LOGGER: Final = "ijob_sources"


def _stderr_is_terminal() -> bool:
    """Say whether standard error is a terminal."""
    return sys.stderr is not None and sys.stderr.isatty()


def _split_fix(text: str) -> tuple[str, str | None]:
    """Part a message from the command at its end that fixes it, if it has one."""
    for marker in _FIX_MARKERS:
        message, found, fix = text.partition(marker)
        if found:
            return message, fix
    return text, None


def say_done(plain: str, text: str, detail: str = "") -> None:
    """Report a finished action on standard output.

    Args:
        plain: The line a pipe gets.
        text: What a terminal shows beside a tick.
        detail: What a terminal shows after it, muted.
    """
    if is_terminal():
        make_console().print(ok_line(text, detail))
        return
    typer.echo(plain)


def say_error(message: str, fix: str | None = None) -> None:
    """Report an error on standard error.

    Args:
        message: What went wrong; a trailing `; run: <command>` becomes its fix
            on a terminal.
        fix: The command that fixes it, drawn beneath it on a terminal and
            added as `run: <fix>` in a pipe.
    """
    if _stderr_is_terminal():
        bare, found = _split_fix(message)
        console = make_console(stderr=True)
        console.print(failed_line(bare))
        if (command := fix or found) is not None:
            console.print(fix_line(command))
        return
    tail = f"; run: {fix}" if fix else ""
    typer.echo(f"error: {message}{tail}", err=True)


def say_note(text: str) -> None:
    """Report something worth knowing on standard error.

    Args:
        text: The note, which may start with `note: ` or `warning: `, and may
            end in `; run: <command>`, which becomes its fix on a terminal.
    """
    if _stderr_is_terminal():
        bare = text
        for prefix in _NOTE_PREFIXES:
            bare = bare.removeprefix(prefix)
        message, fix = _split_fix(bare)
        console = make_console(stderr=True)
        console.print(notice_line(message))
        if fix is not None:
            console.print(fix_line(fix))
        return
    typer.echo(text, err=True)


def say_cost(report: OperationReport) -> None:
    """Report what an operation spent against its cap, on standard error."""
    if _stderr_is_terminal():
        make_console(stderr=True).print(Text(f"   {cost_line(report)}", style=MUTED))
        return
    typer.echo(cost_line(report), err=True)


def ask(question: str) -> str:
    """Ask the user something on the terminal or in a pipe, and return the answer.

    Args:
        question: The question, such as `Store this source?`, which a pipe sees
            followed by `[y/N]`.

    Returns:
        What the user typed.
    """
    if is_terminal():
        prompt = Text(f" {question} ", style=f"bold {GROUND} on {AMBER}")
        prompt.append("  y yes   n no  ", style=f"not bold {MUTED} on {GROUND}")
        try:
            return make_console().input(prompt)
        except EOFError:
            return ""
    return str(
        typer.prompt(
            f"{question} [y/N]", default="", show_default=False, prompt_suffix=" "
        )
    )


@contextmanager
def working(text: str) -> Iterator[None]:
    """Show that something is running, while it runs, on a terminal.

    Args:
        text: What is running, such as `Reading your CV`.
    """
    if not is_terminal():
        yield
        return
    with make_console().status(
        Text(text, style=AMBER), spinner=_SPINNER, spinner_style=AMBER
    ):
        yield


class _NoteHandler(logging.Handler):
    """Hands each log record to `say_note`, so it looks like every other note."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        """Report one record."""
        say_note(record.getMessage())


@contextmanager
def rejections_shown(*, enabled: bool) -> Iterator[None]:
    """Show each warning of the sources while this runs: each a note on a terminal.

    Args:
        enabled: Whether to show them at all.
    """
    if not enabled:
        yield
        return
    handler: logging.Handler = (
        _NoteHandler() if _stderr_is_terminal() else logging.StreamHandler(sys.stderr)
    )
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(_SOURCES_LOGGER)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
