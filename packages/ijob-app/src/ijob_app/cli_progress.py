"""A line of progress on the terminal, so a long command does not look frozen."""

from typing import Final, TextIO

from ijob_app.service import FetchProgress

_CLEAR_TO_END: Final = "\x1b[K"


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

    On a terminal one line is rewritten in place. Anywhere else, such as a pipe
    or a log file, `show` writes each update as a line of its own and
    `show_live` writes nothing.
    """

    def __init__(self, stream: TextIO) -> None:
        """Build a line that writes to `stream`.

        Args:
            stream: Where the progress goes; it is never standard output, which
                holds the run's results.
        """
        self._stream = stream
        self._live = stream.isatty()
        self._open = False

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
            text: The update.
        """
        if self._live:
            self._stream.write(f"\r{text}{_CLEAR_TO_END}")
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
