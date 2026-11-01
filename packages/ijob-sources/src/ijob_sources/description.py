"""Job description text, normalized to a safe Markdown subset.

`normalize` turns the description a source supplies, in HTML, Markdown or
plain text, into one Markdown string. Source text is untrusted. The result
holds no link other than `http(s)`, no image, no raw HTML and no heading,
quote, list, thematic break or indented code block that the source text
formed itself. Fenced code appears only where this module writes it. Pipe
tables and Markdown-form block markup other than the above are not touched.
The module does no I/O.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Final, assert_never, override

MAX_DESCRIPTION_CHARS: Final = 50_000

_CODE_INDENT: Final = 4

_SPECIALS: Final = re.compile(r"[\\*_\[\]`<]")
_INDENT: Final = re.compile(r"[ \t]*")
_LINE_START_MARKER: Final = re.compile(
    r"^(?:[#>+=~-]|(?P<digits>\d+)(?P<delimiter>[.)])(?=[ \t]|$))"
)
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_WHITESPACE: Final = re.compile(r"\s+")
_BACKTICK_RUN: Final = re.compile(r"`+")
_HTTP_URL: Final = re.compile(r"https?://\S", re.IGNORECASE)
_UNSAFE_URL_CHAR: Final = re.compile(r"[\s()<>\\]")
_TAG: Final = re.compile(r"<[a-zA-Z][^>]*>")

_MD_ALT: Final = r"[^\[\]\\<\n]*"
_MD_TARGET: Final = r"(?:[^()\\\n]|\([^()\\\n]*\))*"
_MD_IMAGE: Final = rf"!\[{_MD_ALT}\]\({_MD_TARGET}\)"
_MD_TOKEN: Final = re.compile(
    r"(?P<escape>\\[!-/:-@\[-`{-~])"
    rf"|(?P<image>{_MD_IMAGE}|\[(?:{_MD_IMAGE})+\]\({_MD_TARGET}\))"
    rf"|(?P<link>\[(?P<text>{_MD_ALT})\]\((?P<target>{_MD_TARGET})\))"
    r"|(?P<bracket>[\[\]<])"
)
_MD_HTTP_TARGET: Final = re.compile(r"https?://[^\s()<>\\]+", re.IGNORECASE)

_HEADINGS: Final = {f"h{level}": "#" * level for level in range(1, 7)}
_EMPHASIS: Final = {"strong": "**", "b": "**", "em": "*", "i": "*"}
_BLOCKS: Final = frozenset({"p", "div"})
_LISTS: Final = frozenset({"ul", "ol"})
_DROPPED: Final = frozenset({"script", "style", "iframe"})


class TextForm(StrEnum):
    """The markup a source's description text is written in.

    Attributes:
        HTML: HTML, with entities not yet unescaped.
        MARKDOWN: Markdown.
        PLAIN: Plain text.
        DETECT: Unknown: HTML when the text contains a tag, else plain text.
    """

    HTML = "html"
    MARKDOWN = "markdown"
    PLAIN = "plain"
    DETECT = "detect"


def _sanitize(text: str) -> str:
    """Unify line endings and remove control characters and lone surrogates.

    Tab and newline are kept. A lone surrogate cannot be encoded as UTF-8, so
    it would make the description unstorable.
    """
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def _escape_line_start(line: str) -> str:
    """Neutralize what could make `line` open a Markdown block.

    Indentation of four columns or more, or any tab, is removed. A leading
    `#`, `>`, `+`, `=`, `~`, `-` or ordered-list marker is backslash-escaped.
    """
    indent = _INDENT.match(line)
    width = indent.end() if indent else 0
    kept = line[:width] if width < _CODE_INDENT and "\t" not in line[:width] else ""

    def escape(match: re.Match[str]) -> str:
        if match["digits"] is None:
            return f"\\{match[0]}"
        return f"{match['digits']}\\{match['delimiter']}"

    return kept + _LINE_START_MARKER.sub(escape, line[width:], count=1)


def _escape(text: str, *, at_line_start: bool) -> str:
    """Backslash-escape the characters of `text` that Markdown reads as markup.

    Args:
        text: Text to escape; it may span several lines.
        at_line_start: Whether the first line of `text` begins a line of the
            output, so that block markers and indentation matter on it.

    Returns:
        The escaped text.
    """
    escaped = _SPECIALS.sub(r"\\\g<0>", text)
    lines = escaped.split("\n")
    return "\n".join(
        _escape_line_start(line) if index or at_line_start else line
        for index, line in enumerate(lines)
    )


@dataclass(frozen=True, slots=True)
class _Mark:
    """A position in an `_Emitter`, with the line state at that position."""

    index: int
    newlines: int
    at_line_start: bool
    ends_space: bool
    after_prefix: bool


class _Emitter:
    """Accumulates Markdown and tracks where on a line the output stands."""

    def __init__(self) -> None:
        self._pieces: list[str] = []
        self._newlines = 0
        self._at_line_start = True
        self._ends_space = True
        self._after_prefix = False

    @property
    def after_prefix(self) -> bool:
        """Whether the last thing emitted was a list or heading marker."""
        return self._after_prefix

    def emit(self, text: str, *, prefix: bool = False) -> None:
        """Append `text`; a `prefix` (list or heading marker) keeps the line start."""
        if not text:
            return
        self._pieces.append(text)
        body = text.rstrip("\n")
        self._newlines = len(text) - len(body) if body else self._newlines + len(text)
        self._ends_space = text[-1] in " \n"
        self._after_prefix = prefix
        if not prefix:
            self._at_line_start = self._newlines > 0

    def newlines(self, count: int) -> None:
        """End the output in `count` newlines, unless it is empty or opens an item."""
        if not self._pieces or self._after_prefix:
            return
        if self._newlines < count:
            self.emit("\n" * (count - self._newlines))

    def line_break(self) -> None:
        """Start a new line, unless the output is empty."""
        if self._pieces:
            self.emit("\n")

    def text(self, data: str) -> None:
        """Append a text node, whitespace collapsed and Markdown-escaped."""
        collapsed = _WHITESPACE.sub(" ", data)
        if self._ends_space:
            collapsed = collapsed.lstrip(" ")
        self.emit(_escape(collapsed, at_line_start=self._at_line_start))

    def mark(self) -> _Mark:
        """Record the current position."""
        return _Mark(
            len(self._pieces),
            self._newlines,
            self._at_line_start,
            self._ends_space,
            self._after_prefix,
        )

    def take(self, mark: _Mark) -> str:
        """Remove and return everything emitted since `mark`."""
        taken = "".join(self._pieces[mark.index :])
        del self._pieces[mark.index :]
        self._newlines = mark.newlines
        self._at_line_start = mark.at_line_start
        self._ends_space = mark.ends_space
        self._after_prefix = mark.after_prefix
        return taken

    def last(self) -> str | None:
        """Return the last emitted piece, or `None` when nothing is emitted."""
        return self._pieces[-1] if self._pieces else None

    def trim_last(self, count: int) -> None:
        """Remove the last `count` characters of the last piece."""
        self._pieces[-1] = self._pieces[-1][:-count]

    def result(self) -> str:
        """Return everything emitted."""
        return "".join(self._pieces)


@dataclass(slots=True)
class _Span:
    """An open `strong`, `em` or `a` element whose output is held back.

    A span with an empty `marker` and no `href` adds nothing around its text;
    it keeps an inner element from closing an outer one of the same kind.
    """

    tag: str
    marker: str
    href: str | None
    mark: _Mark


@dataclass(slots=True)
class _ListLevel:
    """An open `ul` or `ol`."""

    ordered: bool
    indent: str
    items: int = 0
    item_width: int = 2
    in_item: bool = False


@dataclass(slots=True)
class _CodeCapture:
    """Raw text of an open `pre` or `code` element."""

    tag: str
    parts: list[str]


def _code_delimiter(code: str, minimum: int) -> str:
    """Return a backtick run of at least `minimum` and longer than any run in `code`."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(code)), default=0)
    return "`" * max(minimum, longest + 1)


def _markdown_url(href: str) -> str:
    """Percent-encode the characters that would end a Markdown link target."""
    return _UNSAFE_URL_CHAR.sub(
        lambda match: "".join(f"%{byte:02X}" for byte in match[0].encode()), href
    )


class _HtmlToMarkdown(HTMLParser):
    """Rewrites HTML as Markdown; call `markdown` once after `feed` and `close`."""

    def __init__(self) -> None:
        super().__init__()
        self._out = _Emitter()
        self._spans: list[_Span] = []
        self._wrapped: dict[int, tuple[str, str]] = {}
        self._lists: list[_ListLevel] = []
        self._stray_item = False
        self._heading: _Mark | None = None
        self._code: _CodeCapture | None = None
        self._dropped = 0

    def markdown(self) -> str:
        """Close whatever is still open and return the Markdown."""
        if self._code is not None:
            self._close_code(self._code.tag)
        self._close_spans()
        return self._out.result()

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED:
            self._dropped += 1
        elif self._dropped:
            return
        elif self._code is not None:
            if tag == "br":
                self._code.parts.append("\n")
        elif tag == "br":
            self._out.line_break()
        elif tag in _BLOCKS:
            self._block()
        elif tag in _HEADINGS:
            self._block()
            self._heading = self._out.mark()
            self._out.emit(f"{_HEADINGS[tag]} ", prefix=True)
        elif tag in _LISTS:
            self._open_list(ordered=tag == "ol")
        elif tag == "li":
            self._open_item()
        else:
            self._open_inline(tag, attrs)

    def _open_inline(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _EMPHASIS:
            marker = _EMPHASIS[tag]
            nested = any(span.marker == marker for span in self._spans)
            self._spans.append(
                _Span(tag, "" if nested else marker, None, self._out.mark())
            )
        elif tag == "a":
            href = _sanitize(dict(attrs).get("href") or "").strip()
            nested = any(span.href is not None for span in self._spans)
            usable = _HTTP_URL.match(href) and not nested
            self._spans.append(
                _Span(tag, "", href if usable else None, self._out.mark())
            )
        elif tag in ("pre", "code"):
            self._code = _CodeCapture(tag, [])

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED:
            self._dropped = max(0, self._dropped - 1)
        elif self._dropped:
            return
        elif self._code is not None:
            if tag == self._code.tag:
                self._close_code(tag)
        elif tag in _BLOCKS or tag in _HEADINGS:
            self._block()
        elif tag in _LISTS:
            if self._lists:
                self._lists.pop()
            self._block()
        elif tag == "li":
            self._close_item()
        elif tag in _EMPHASIS or tag == "a":
            self._close_matching_span(tag)

    @override
    def handle_data(self, data: str) -> None:
        if self._dropped:
            return
        clean = _sanitize(data)
        if self._code is not None:
            self._code.parts.append(clean)
        else:
            self._out.text(clean)

    def _block(self) -> None:
        self._heading = None
        self._out.newlines(1 if self._lists else 2)

    def _open_list(self, *, ordered: bool) -> None:
        self._heading = None
        if self._lists:
            parent = self._lists[-1]
            indent = parent.indent + " " * parent.item_width
            self._out.newlines(1)
        else:
            indent = ""
            self._out.newlines(2)
        self._lists.append(_ListLevel(ordered, indent))

    def _open_item(self) -> None:
        self._heading = None
        if self._lists:
            level = self._lists[-1]
            level.in_item = True
        else:
            level = _ListLevel(ordered=False, indent="")
            self._stray_item = True
        level.items += 1
        marker = f"{level.items}. " if level.ordered else "- "
        level.item_width = len(marker)
        self._out.newlines(1)
        self._out.emit(level.indent + marker, prefix=True)

    def _close_item(self) -> None:
        if self._lists:
            self._lists[-1].in_item = False
        else:
            self._stray_item = False
        self._out.newlines(1)

    def _in_item(self) -> bool:
        """Whether a fenced block here would belong to the open list item."""
        return self._lists[-1].in_item if self._lists else True

    def _content_indent(self) -> str:
        """Return the indentation of the open list item's content."""
        if not self._lists:
            return "  " if self._stray_item else ""
        level = self._lists[-1]
        return level.indent + " " * level.item_width

    def _close_spans(self) -> None:
        while self._spans:
            self._close_span(self._spans.pop())

    def _close_matching_span(self, tag: str) -> None:
        for position in range(len(self._spans) - 1, -1, -1):
            if self._spans[position].tag == tag:
                while len(self._spans) > position:
                    self._close_span(self._spans.pop())
                return

    def _close_span(self, span: _Span) -> None:
        held = self._out.take(span.mark)
        core = held.strip()
        if not core:
            self._out.emit(held)
            return
        start = len(held) - len(held.lstrip())
        end = len(held.rstrip())
        self._out.emit(held[:start])
        if span.href is not None and "\n" not in core:
            self._out.emit(f"[{core}]({_markdown_url(span.href)})")
        elif span.marker:
            self._emit_emphasis(span.marker, core, joins_previous=start == 0)
        else:
            self._out.emit(core)
        self._out.emit(held[end:])

    def _emit_emphasis(self, marker: str, core: str, *, joins_previous: bool) -> None:
        """Emit `core` in `marker`, merged into an emphasis just before it."""
        previous = self._out.last()
        known = None if previous is None else self._wrapped.get(id(previous))
        if joins_previous and known is not None and known == (previous, marker):
            self._out.trim_last(len(marker))
            piece = f"{core}{marker}"
        else:
            piece = f"{marker}{core}{marker}"
        self._out.emit(piece)
        self._wrapped[id(piece)] = (piece, marker)

    def _close_code(self, tag: str) -> None:
        capture = self._code
        self._code = None
        if capture is None:
            return
        code = "".join(capture.parts)
        if tag == "pre":
            self._close_pre(code.strip("\n").rstrip())
            return
        inline = _WHITESPACE.sub(" ", code).strip()
        if inline:
            delimiter = _code_delimiter(inline, 1)
            pad = " " if inline.startswith("`") or inline.endswith("`") else ""
            self._out.emit(f"{delimiter}{pad}{inline}{pad}{delimiter}")

    def _close_pre(self, body: str) -> None:
        """Emit `body` as a fenced block starting its own line, or as text.

        Open emphasis and links are closed and an open heading is ended first.
        Inside a list item the fence and its lines sit at the item's content
        column; where no open item holds the block, the text is escaped instead.
        """
        if not body:
            self._block()
            return
        self._close_spans()
        if self._heading is not None and self._out.after_prefix:
            self._out.take(self._heading)
        self._block()
        if not self._in_item():
            self._out.text(body)
            self._block()
            return
        fence = _code_delimiter(body, 3)
        indent = self._content_indent()
        lines = [fence, *body.split("\n"), fence]
        block = "\n".join(f"{indent}{line}" if line else line for line in lines)
        self._out.emit(block.removeprefix(indent) if self._out.after_prefix else block)
        self._block()


def _html_to_markdown(text: str) -> str:
    """Rewrite HTML as Markdown; malformed input is closed at its end."""
    parser = _HtmlToMarkdown()
    parser.feed(text)
    parser.close()
    return parser.markdown()


def _filter_markdown(text: str) -> str:
    """Keep `http(s)` links, drop images, and escape every other bracket.

    One pass reads `text` left to right and never re-reads what it wrote. A
    backslash escape is copied as a unit; an inline image is dropped; an inline
    link with a plain `http(s)` target and a title-free, bracket-free text is
    kept; any other inline link becomes its text; every remaining `[`, `]` and
    `<` is backslash-escaped, so no autolink, reference link, raw HTML or
    mismatched bracket survives.
    """
    parts: list[str] = []
    bare_bang = False
    end = 0
    for match in _MD_TOKEN.finditer(text):
        gap = text[end : match.start()]
        end = match.end()
        if gap:
            parts.append(gap)
            bare_bang = gap.endswith("!")
        if match["escape"] is not None:
            parts.append(match[0])
            bare_bang = False
        elif match["image"] is not None:
            continue
        elif match["link"] is not None:
            if _MD_HTTP_TARGET.fullmatch(match["target"]):
                if bare_bang:
                    parts[-1] = f"{parts[-1][:-1]}\\!"
                parts.append(match[0])
                bare_bang = False
            elif match["text"]:
                parts.append(match["text"])
                bare_bang = match["text"].endswith("!")
        else:
            parts.append(f"\\{match[0]}")
            bare_bang = False
    parts.append(text[end:])
    return "".join(parts)


def _finish(markdown: str) -> str | None:
    """Apply the whitespace, length and emptiness rules every form shares.

    Args:
        markdown: Markdown produced for one description.

    Returns:
        The text with trailing spaces stripped, blank lines collapsed to at
        most one in a row, edge blank lines removed and the length cut to
        `MAX_DESCRIPTION_CHARS`; `None` when nothing is left.
    """
    lines = [line.rstrip() for line in markdown.split("\n")]
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    result = "\n".join(kept).rstrip()[:MAX_DESCRIPTION_CHARS].rstrip()
    return result or None


def normalize(text: str, form: TextForm) -> str | None:
    """Normalize a description to Markdown.

    Line endings are unified and control characters and lone surrogates
    removed from `text` before any form is read, so removing them cannot
    assemble markup. The result always encodes as UTF-8.

    Args:
        text: The description exactly as the source supplies it.
        form: The markup `text` is written in.

    Returns:
        Markdown of at most `MAX_DESCRIPTION_CHARS` characters, or `None` when
        the description is empty.
    """
    clean = _sanitize(text)
    match form:
        case TextForm.PLAIN:
            return _finish(_escape(clean, at_line_start=True))
        case TextForm.HTML:
            return _finish(_html_to_markdown(clean))
        case TextForm.MARKDOWN:
            return _finish(_filter_markdown(clean))
        case TextForm.DETECT:
            detected = TextForm.HTML if _TAG.search(clean) else TextForm.PLAIN
            return normalize(clean, detected)
        case _:
            assert_never(form)
