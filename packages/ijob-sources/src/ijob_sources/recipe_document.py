"""The document a recipe reads.

A recipe reads either a cleaned HTML tree or a parsed JSON value. This module
loads a fetched body into one of the two, cleans an HTML tree of everything a
recipe must not depend on, fetches a page through the `PoliteClient`, and cuts
a document down to the sample shown to a model. A recipe is always evaluated on
the same cleaned document that the sample was cut from.
"""

import codecs
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, assert_never

import lxml.html
from lxml import etree

from ijob_sources.errors import UnsupportedUrlError
from ijob_sources.polite_http import PoliteClient

MAX_BODY_BYTES: Final = 5 * 1024 * 1024
SAMPLE_LIMIT: Final = 50_000
JSON_SAMPLE_ITEMS: Final = 3
JSON_SAMPLE_CHARS: Final = 300
MAX_JSON_DEPTH: Final = 64

_TRUNCATED: Final = "\n[truncated]"
_DROPPED_TAGS: Final = ("script", "style", "svg", "noscript", "iframe", "template")
_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_JSON_FIRST_BYTES: Final = frozenset(b"{[")
_HTML_FIRST_BYTE: Final = ord("<")
_CHARSET: Final = re.compile(
    r"""charset\s*=\s*["']?([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)


class RecipeFormat(StrEnum):
    """The kind of document a recipe reads, and so the expression language."""

    HTML = "html"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class HtmlDocument:
    """A parsed and cleaned HTML page.

    Attributes:
        tree: The root element of the cleaned `lxml` tree. XPath expressions
            run against it.
    """

    tree: Any


@dataclass(frozen=True, slots=True)
class JsonDocument:
    """A parsed JSON value.

    Attributes:
        value: The value `json` parsed. JMESPath expressions run against it.
    """

    value: object


type Document = HtmlDocument | JsonDocument


def load_document(body: bytes, content_type: str | None, url: str) -> Document:
    """Parse a fetched body into the document a recipe reads.

    The format comes from the media type of `content_type`, before any `;` and
    lowercased: `application/json` or any `+json` type is JSON, and
    `text/html` or `application/xhtml+xml` is HTML. Without the header, the
    first non-whitespace byte decides: `{` or `[` is JSON and `<` is HTML.
    JSON must be UTF-8 and nested no more than 64 levels deep. A `charset` of
    the header that lxml does not know is ignored and lxml detects the
    encoding. HTML loses its `script`, `style`, `svg`, `noscript`,
    `iframe` and `template` elements (their trailing text stays), its comments,
    and every `style` and `on*` attribute; every other attribute is kept.

    Args:
        body: The response body.
        content_type: The `Content-Type` header value, or `None` if absent.
        url: The address the body came from, named in the error.

    Returns:
        The document.

    Raises:
        UnsupportedUrlError: The body is over 5 MiB, its media type is neither
            JSON nor HTML, it is empty or of no decidable format, it is JSON
            that is not UTF-8, does not parse or is nested too deep, or it is
            HTML that cannot be parsed.
    """
    if len(body) > MAX_BODY_BYTES:
        raise UnsupportedUrlError(url, "the page is larger than 5 MiB")
    recipe_format = _format_of(body, content_type, url)
    match recipe_format:
        case RecipeFormat.JSON:
            return _load_json(body, url)
        case RecipeFormat.HTML:
            return _load_html(body, content_type, url)
        case _:
            assert_never(recipe_format)


async def fetch_document(client: PoliteClient, url: str) -> tuple[Document, str]:
    """Fetch a page, following redirects, and load it as a document.

    Addresses inside the page resolve against the returned final address, not
    the requested one.

    Args:
        client: The client the request goes through.
        url: The absolute address to fetch.

    Returns:
        The document, and the address of the response that held it.

    Raises:
        UnsupportedUrlError: The response is not a usable document.
        SourcesError: The client could not complete the request.
    """
    response = await client.get(url, follow_redirects=True)
    final_url = str(response.url)
    document = load_document(
        response.content, response.headers.get("Content-Type"), final_url
    )
    return document, final_url


def sample_text(document: Document) -> str:
    """Cut a document down to the sample shown to a model.

    HTML is serialized with every whitespace run collapsed to one space, and
    past 50,000 characters it is cut there and a line break and `[truncated]`
    are appended.
    JSON has every array cut to its first 3 items and every string to 300
    characters, at any depth.

    Args:
        document: The document to sample.

    Returns:
        The sample text.
    """
    match document:
        case HtmlDocument(tree=tree):
            text = " ".join(lxml.html.tostring(tree, encoding="unicode").split())
            if len(text) > SAMPLE_LIMIT:
                return text[:SAMPLE_LIMIT] + _TRUNCATED
            return text
        case JsonDocument(value=value):
            return json.dumps(_cut(value), ensure_ascii=False)
        case _:
            assert_never(document)


def _format_of(body: bytes, content_type: str | None, url: str) -> RecipeFormat:
    """Decide the format from the media type, else from the first byte."""
    media = (content_type or "").split(";")[0].strip().lower()
    if media:
        if media == "application/json" or media.endswith("+json"):
            return RecipeFormat.JSON
        if media in _HTML_TYPES:
            return RecipeFormat.HTML
        reason = f"the media type {media!r} is neither JSON nor HTML"
        raise UnsupportedUrlError(url, reason)
    stripped = body.lstrip()
    if not stripped:
        raise UnsupportedUrlError(url, "the page is empty")
    if stripped[0] in _JSON_FIRST_BYTES:
        return RecipeFormat.JSON
    if stripped[0] == _HTML_FIRST_BYTE:
        return RecipeFormat.HTML
    raise UnsupportedUrlError(url, "the page is neither JSON nor HTML")


def _load_json(body: bytes, url: str) -> JsonDocument:
    """Parse the body as UTF-8 JSON."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError) as error:
        reason = f"the page is not valid UTF-8 JSON: {error}"
        raise UnsupportedUrlError(url, reason) from error
    if _is_nested_deeper_than(value, MAX_JSON_DEPTH):
        reason = f"the JSON is nested more than {MAX_JSON_DEPTH} levels deep"
        raise UnsupportedUrlError(url, reason)
    return JsonDocument(value)


def _is_nested_deeper_than(value: object, limit: int) -> bool:
    """Tell whether arrays and objects nest more than `limit` levels deep.

    Walks with an explicit stack, so the walk itself cannot recurse.
    """
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if isinstance(current, list):
            children: Iterable[object] = current
        elif isinstance(current, dict):
            children = current.values()
        else:
            continue
        if depth + 1 > limit:
            return True
        pending.extend((child, depth + 1) for child in children)
    return False


def _load_html(body: bytes, content_type: str | None, url: str) -> HtmlDocument:
    """Parse the body as HTML and clean the tree."""
    try:
        try:
            tree = _parse_html(body, _charset(content_type))
        except LookupError:
            tree = _parse_html(body, None)
    except (etree.LxmlError, ValueError) as error:
        reason = f"the page is not parseable HTML: {error}"
        raise UnsupportedUrlError(url, reason) from error
    _clean(tree)
    return HtmlDocument(tree)


def _parse_html(body: bytes, encoding: str | None) -> Any:  # noqa: ANN401 - lxml is untyped
    """Parse the body as HTML in the given encoding, or one lxml detects.

    Raises:
        LookupError: lxml does not know `encoding`.
    """
    parser = lxml.html.HTMLParser(encoding=encoding)
    return lxml.html.document_fromstring(body, parser=parser)


def _charset(content_type: str | None) -> str | None:
    """Return the charset a `Content-Type` names, when Python knows it.

    Python knows more names than lxml does, so the caller still handles lxml
    refusing the name returned.
    """
    found = _CHARSET.search(content_type or "")
    if found is None:
        return None
    try:
        codecs.lookup(found.group(1))
    except LookupError:
        return None
    return found.group(1)


def _clean(tree: Any) -> None:  # noqa: ANN401 - lxml is untyped
    """Drop what a recipe must not read: dead elements, comments, handlers."""
    for element in list(tree.iter(*_DROPPED_TAGS, etree.Comment)):
        element.drop_tree()
    for element in tree.iter("*"):
        for name in list(element.attrib):
            if name == "style" or name.startswith("on"):
                del element.attrib[name]


def _cut(value: object) -> object:
    """Copy a JSON value with arrays and strings cut to the sample sizes."""
    if isinstance(value, list):
        return [_cut(item) for item in value[:JSON_SAMPLE_ITEMS]]
    if isinstance(value, dict):
        return {key: _cut(item) for key, item in value.items()}
    if isinstance(value, str):
        return value[:JSON_SAMPLE_CHARS]
    return value
