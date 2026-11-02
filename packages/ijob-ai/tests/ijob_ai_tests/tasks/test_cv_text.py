"""Tests for CV text extraction and its error mapping."""

from pathlib import Path

import pytest
from ijob_ai.errors import CvTextError
from ijob_ai.tasks import cv_text
from ijob_ai.tasks.cv_text import read_cv_text
from pypdf.errors import LimitReachedError

from .pdfs import (
    LEFT_FIRST,
    RIGHT_FIRST,
    write_blank_pdf,
    write_encrypted_pdf,
    write_text_pdf,
    write_truncated_pdf,
    write_two_column_pdf,
)


def test_two_column_layout_keeps_columns(tmp_path: Path) -> None:
    text = read_cv_text(write_two_column_pdf(tmp_path / "cv.pdf"))
    assert LEFT_FIRST in text
    assert RIGHT_FIRST in text
    first_line = next(line for line in text.splitlines() if LEFT_FIRST in line)
    assert RIGHT_FIRST in first_line


def test_blank_pdf_message(tmp_path: Path) -> None:
    with pytest.raises(CvTextError) as caught:
        read_cv_text(write_blank_pdf(tmp_path / "cv.pdf"))
    assert caught.value.reason == "CV has no text layer; OCR is not supported"


def test_encrypted_pdf_message(tmp_path: Path) -> None:
    with pytest.raises(CvTextError) as caught:
        read_cv_text(write_encrypted_pdf(tmp_path / "cv.pdf"))
    assert caught.value.reason == "CV is password-protected"


def test_truncated_pdf_message(tmp_path: Path) -> None:
    with pytest.raises(CvTextError) as caught:
        read_cv_text(write_truncated_pdf(tmp_path / "cv.pdf"))
    assert caught.value.reason == "CV is not a readable PDF"


def test_other_pypdf_error_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        message = "too big"
        raise LimitReachedError(message)

    monkeypatch.setattr(cv_text, "PdfReader", fail)
    with pytest.raises(CvTextError) as caught:
        read_cv_text(write_blank_pdf(tmp_path / "cv.pdf"))
    assert caught.value.reason == "could not read the CV: too big"


def test_unexpected_error_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        message = "'NoneType' object has no attribute 'get_object'"
        raise AttributeError(message)

    monkeypatch.setattr(cv_text, "PdfReader", fail)
    with pytest.raises(CvTextError) as caught:
        read_cv_text(write_blank_pdf(tmp_path / "cv.pdf"))
    assert caught.value.reason == (
        "could not read the CV: 'NoneType' object has no attribute 'get_object'"
    )
    assert isinstance(caught.value.__cause__, AttributeError)


def test_long_cv_truncated(tmp_path: Path) -> None:
    path = write_text_pdf(tmp_path / "cv.pdf", "x" * 100, pages=400)
    text = read_cv_text(path)
    assert text.endswith("[truncated]")
    assert len(text) < 30_100
