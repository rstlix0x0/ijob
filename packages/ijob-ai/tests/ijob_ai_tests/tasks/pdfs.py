"""Made-up PDFs for the CV text tests, built in memory with pypdf."""

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

LEFT_FIRST = "Senior Python Engineer"
RIGHT_FIRST = "Skills and Tools"

_PAGE_SIZE = 612.0, 792.0


def _text_lines(x: int, lines: list[str]) -> str:
    """Return content-stream operators that draw `lines` downward from `x`."""
    ops = ["BT", "/F1 11 Tf", f"1 0 0 1 {x} 700 Tm", "16 TL"]
    ops.extend(f"({line}) Tj T*" for line in lines)
    ops.append("ET")
    return "\n".join(ops)


def _page_with_content(writer: PdfWriter, content: str) -> None:
    """Append a page drawn by `content`, with Helvetica available as `/F1`."""
    page = writer.add_blank_page(*_PAGE_SIZE)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)


def _save(writer: PdfWriter, path: Path) -> Path:
    """Write `writer` to `path` and return `path`."""
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def write_two_column_pdf(path: Path) -> Path:
    """Write a one-page, two-column CV and return `path`."""
    writer = PdfWriter()
    left = [LEFT_FIRST, "Acme Corp 2019 to 2024", "Built data pipelines"]
    right = [RIGHT_FIRST, "Python and SQL", "Docker and Linux"]
    content = _text_lines(60, left) + "\n" + _text_lines(340, right)
    _page_with_content(writer, content)
    return _save(writer, path)


def write_text_pdf(path: Path, text: str, *, pages: int = 1) -> Path:
    """Write a CV of `pages` pages that each hold the one line `text`."""
    writer = PdfWriter()
    for _ in range(pages):
        _page_with_content(writer, _text_lines(40, [text]))
    return _save(writer, path)


def write_blank_pdf(path: Path) -> Path:
    """Write a one-page PDF with no text layer and return `path`."""
    writer = PdfWriter()
    writer.add_blank_page(*_PAGE_SIZE)
    return _save(writer, path)


def write_encrypted_pdf(path: Path) -> Path:
    """Write a password-protected AES PDF that holds some text and return `path`."""
    writer = PdfWriter()
    _page_with_content(writer, _text_lines(40, ["Secret CV text"]))
    writer.encrypt("owner-pass", algorithm="AES-256")
    return _save(writer, path)


def write_truncated_pdf(path: Path) -> Path:
    """Write a file that starts like a PDF and stops mid-object; return `path`."""
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Cat")
    return path
