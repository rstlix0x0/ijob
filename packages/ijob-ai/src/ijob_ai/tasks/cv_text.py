"""Read the text of a stored CV PDF with pypdf and map its failures to `CvTextError`."""

from pathlib import Path
from typing import Final

from pypdf import PdfReader
from pypdf.errors import (
    DependencyError,
    FileNotDecryptedError,
    PdfStreamError,
    PyPdfError,
)

from ijob_ai.errors import CvTextError
from ijob_ai.tasks.text import truncate

CV_TEXT_LIMIT: Final = 30_000
"""The most characters of CV text a call accepts."""

_ENCRYPTED = "CV is password-protected"
_UNREADABLE = "CV is not a readable PDF"
_NO_TEXT_LAYER = "CV has no text layer; OCR is not supported"


def read_cv_text(path: Path) -> str:
    """Extract the CV text from a PDF, page by page in layout mode.

    Layout mode keeps each piece of text at its horizontal position, so
    side-by-side columns stay on the same line. The joined text is cut to
    30,000 characters.

    Args:
        path: The CV PDF.

    Returns:
        The page texts joined by newlines, truncated to `CV_TEXT_LIMIT`.

    Raises:
        CvTextError: If the PDF is encrypted, unreadable, has no text layer,
            or pypdf fails for any other reason.
    """
    # The broad catch is the one boundary around untrusted PDF bytes: pypdf
    # raises non-pypdf errors on some corrupt files, and each must surface as a
    # CvTextError.
    try:
        reader = PdfReader(path)
        pages = [page.extract_text(extraction_mode="layout") for page in reader.pages]
    except FileNotDecryptedError as exc:
        raise CvTextError(_ENCRYPTED) from exc
    except PdfStreamError as exc:
        raise CvTextError(_UNREADABLE) from exc
    except (PyPdfError, DependencyError) as exc:
        reason = f"could not read the CV: {exc}"
        raise CvTextError(reason) from exc
    except Exception as exc:
        reason = f"could not read the CV: {exc}"
        raise CvTextError(reason) from exc
    text = "\n".join(pages)
    if not text.strip():
        raise CvTextError(_NO_TEXT_LAYER)
    return truncate(text, CV_TEXT_LIMIT)
