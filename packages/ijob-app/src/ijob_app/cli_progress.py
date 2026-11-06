"""A line of progress on the terminal, so a long command does not look frozen."""

import os
import shutil
from collections.abc import Callable
from typing import Final, TextIO

from ijob_app.service import FetchProgress

_CLEAR_TO_END: Final = "\x1b[K"
_CUT: Final = "\N{HORIZONTAL ELLIPSIS}"


def _columns_of(stream: TextIO) -> Callable[[], int]:
    """Build a function giving the width of the terminal `stream` writes to.

    It asks the stream's own device, so it is right when only standard error is
    a terminal, and otherwise falls back to `COLUMNS` or standard output.
    """

    def columns() -> int:
        try:
            return os.get_terminal_size(stream.fileno()).columns
        except (OSError, ValueError):
            return shutil.get_terminal_size().columns

    return columns


def _fit(text: str, columns: int) -> str:
    """Cut `text` to fit one row of `columns`, keeping its end.

    The last column stays free, since some terminals wrap on writing to it. The
    end is kept because that is where an update says how far a run has got.
    """
    room = columns - 1
    if len(text) <= room:
        return text
    if room < len(_CUT) + 1:
        return ""
    return _CUT + text[len(text) - room + len(_CUT) :]


def fetch_progress_text(progress: FetchProgress) -> str:
    """Write how far fetching the sources has got.

    Args:
        progress: How far it has got.

    Returns:
        The text, which says the results are being saved once every source ended.
    """
    text = (
        f"fetching sources: {progress.done} of {progress.total} done, "
        f"{progress.failed} failed"
    )
    return f"{text}, saving" if progress.done == progress.total else text


class ProgressLine:
    """Shows how far a run has got.

    On a terminal one line is rewritten in place, cut to the terminal's width so
    it never wraps: a wrapped line cannot be rewritten and leaves a row behind on
    each update. Anywhere else, such as a pipe or a log file, `show` writes each
    update as a line of its own, uncut, and `show_live` writes nothing.
    """

    def __init__(
        self, stream: TextIO, *, columns: Callable[[], int] | None = None
    ) -> None:
        """Build a line that writes to `stream`.

        Args:
            stream: Where the progress goes; it is never standard output, which
                holds the run's results.
            columns: Gives the terminal's width, read again for each update so a
                resized window is followed; by default the width of the terminal
                `stream` writes to.
        """
        self._stream = stream
        self._live = stream.isatty()
        self._open = False
        self._columns = columns if columns is not None else _columns_of(stream)

    def show(self, text: str) -> None:
        """Write an update wherever the output goes.

        Args:
            text: The update.
        """
        if self._live:
            self.show_live(text)
        else:
            self._stream.write(f"{text}\n")
            self._stream.flush()

    def show_live(self, text: str) -> None:
        """Write an update only on a terminal, for runs with many updates.

        Args:
            text: The update, cut to the terminal's width if it is wider.
        """
        if self._live:
            self._stream.write(f"\r{_fit(text, self._columns())}{_CLEAR_TO_END}")
            self._stream.flush()
            self._open = True

    def clear(self) -> None:
        """Wipe a line left open, so a result can print in its place."""
        if self._open:
            self._stream.write(f"\r{_CLEAR_TO_END}")
            self._stream.flush()
            self._open = False

    def close(self) -> None:
        """End a line left open, so what is printed next starts on its own."""
        if self._open:
            self._stream.write("\n")
            self._stream.flush()
            self._open = False
