import io
from typing import override

import pytest
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


def test_show_live_cuts_a_long_line_to_the_terminal_width_keeping_its_end() -> None:
    stream = _Terminal()
    text = "scorer gepa some/model: 112 calls, $0.10 spent"

    ProgressLine(stream, columns=lambda: 20).show_live(text)

    assert stream.getvalue() == "\r\N{HORIZONTAL ELLIPSIS}" + text[-18:] + "\x1b[K"
    assert len(stream.getvalue().removeprefix("\r").removesuffix("\x1b[K")) == 19


def test_show_live_keeps_a_line_that_fits() -> None:
    stream = _Terminal()

    ProgressLine(stream, columns=lambda: 20).show_live("x" * 19)

    assert stream.getvalue() == "\r" + "x" * 19 + "\x1b[K"


def test_show_live_writes_no_text_on_a_terminal_too_narrow_for_it() -> None:
    stream = _Terminal()

    ProgressLine(stream, columns=lambda: 1).show_live("reading")

    assert stream.getvalue() == "\r\x1b[K"


def test_show_live_reads_the_width_again_for_each_update() -> None:
    stream = _Terminal()
    widths = iter([10, 30])
    line = ProgressLine(stream, columns=lambda: next(widths))

    line.show_live("a" * 20)
    line.show_live("b" * 20)

    assert stream.getvalue().endswith("\r" + "b" * 20 + "\x1b[K")


def test_show_does_not_cut_lines_when_not_a_terminal() -> None:
    stream = io.StringIO()

    ProgressLine(stream, columns=lambda: 5).show("a long update")

    assert stream.getvalue() == "a long update\n"


def test_width_falls_back_to_the_environment_without_a_terminal_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "10")
    stream = _Terminal()

    ProgressLine(stream).show_live("a" * 20)

    assert stream.getvalue() == "\r\N{HORIZONTAL ELLIPSIS}" + "a" * 8 + "\x1b[K"


def test_fetch_text_says_how_far_it_got() -> None:
    progress = FetchProgress(total=168, done=12, failed=2)

    assert fetch_progress_text(progress) == "fetching sources: 12 of 168 done, 2 failed"


def test_fetch_text_says_when_it_is_saving() -> None:
    progress = FetchProgress(total=2, done=2, failed=0)

    assert fetch_progress_text(progress) == (
        "fetching sources: 2 of 2 done, 0 failed, saving"
    )
