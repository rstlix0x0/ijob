import io
from typing import override

from ijob_app.cli_progress import ProgressLine, fetch_progress_text
from ijob_app.service import FetchProgress


class _Terminal(io.StringIO):
    @override
    def isatty(self) -> bool:
        return True


def test_show_rewrites_one_line_on_a_terminal() -> None:
    stream = _Terminal()
    line = ProgressLine(stream)

    line.show("first")
    line.show("second")

    assert "\n" not in stream.getvalue()
    assert stream.getvalue().endswith("\rsecond\x1b[K")
    line.close()
    assert stream.getvalue().endswith("\n")


def test_show_prints_plain_lines_when_not_a_terminal() -> None:
    stream = io.StringIO()
    line = ProgressLine(stream)

    line.show("first")
    line.close()

    assert stream.getvalue() == "first\n"


def test_show_live_prints_only_on_a_terminal() -> None:
    plain = io.StringIO()
    ProgressLine(plain).show_live("first")
    terminal = _Terminal()
    ProgressLine(terminal).show_live("first")

    assert plain.getvalue() == ""
    assert "first" in terminal.getvalue()


def test_clear_wipes_an_open_line_on_a_terminal() -> None:
    stream = _Terminal()
    line = ProgressLine(stream)

    line.show_live("reading")
    line.clear()
    line.close()

    assert stream.getvalue() == "\rreading\x1b[K\r\x1b[K"


def test_closing_a_line_that_showed_nothing_writes_nothing() -> None:
    stream = _Terminal()

    ProgressLine(stream).close()

    assert stream.getvalue() == ""


def test_fetch_text_says_how_far_it_got() -> None:
    progress = FetchProgress(total=168, done=12, failed=2)

    assert fetch_progress_text(progress) == "fetching sources: 12 of 168 done, 2 failed"


def test_fetch_text_says_when_it_is_saving() -> None:
    progress = FetchProgress(total=2, done=2, failed=0)

    assert fetch_progress_text(progress) == (
        "fetching sources: 2 of 2 done, 0 failed, saving"
    )
