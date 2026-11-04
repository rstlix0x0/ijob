"""The links of an HTML page, resolved and cleaned.

`extract_links` reads the `<a href>` elements of a page body and returns each
distinct `http` or `https` address once, with the text of the link cleaned. A
body that is valid UTF-8 is read as UTF-8; any other follows its declared
charset. It fetches nothing, and a body that is not HTML yields no links rather than an
error.
"""

import unicodedata
from dataclasses import dataclass
from typing import Final
from urllib.parse import urldefrag, urljoin, urlsplit

import lxml.html
from lxml import etree

_WEB_SCHEMES: Final = frozenset({"http", "https"})
_TEXT_LIMIT: Final = 100
_CONTROL: Final = "Cc"


@dataclass(frozen=True, slots=True)
class Link:
    """One link of a page.

    Attributes:
        url: The absolute `http` or `https` address, without a fragment.
        text: The link text with control characters removed and whitespace
            collapsed, cut to 100 characters.
    """

    url: str
    text: str


def extract_links(body: bytes, base_url: str) -> list[Link]:
    """Read the links of an HTML page.

    Each `href` is resolved against `base_url`. A link whose address is not
    `http` or `https`, or cannot be parsed, is dropped, as is an anchor with
    no `href`. The fragment is removed, and a repeated address keeps the text
    of its first link. Links come in document order.

    Args:
        body: The response body.
        base_url: The address the body was read from, which relative links
            resolve against.

    Returns:
        The links, or an empty list when the body is not HTML that can be
        parsed.
    """
    try:
        tree = lxml.html.document_fromstring(body, parser=_parser_for(body))
    except (etree.LxmlError, ValueError):
        return []
    links: dict[str, str] = {}
    for anchor in tree.iter("a"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        url = _web_address(href, base_url)
        if url is not None and url not in links:
            links[url] = _clean(anchor.text_content())
    return [Link(url=url, text=text) for url, text in links.items()]


def _parser_for(body: bytes) -> lxml.html.HTMLParser | None:
    """Read a body that is valid UTF-8 as UTF-8, whatever it declares.

    A body that does not decode as UTF-8 gets the default parser, which follows
    the charset the page declares and falls back to Latin-1 without one.
    """
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return lxml.html.HTMLParser(encoding="utf-8")


def _web_address(href: str, base_url: str) -> str | None:
    """Resolve `href` against the base, or return `None` for a non-web address."""
    try:
        url = urldefrag(urljoin(base_url, href)).url
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in _WEB_SCHEMES or not parts.netloc:
        return None
    return url


def _clean(text: str) -> str:
    """Drop control characters, collapse whitespace and cut to the text limit."""
    kept = "".join(
        char
        for char in text
        if char.isspace() or unicodedata.category(char) != _CONTROL
    )
    return " ".join(kept.split())[:_TEXT_LIMIT].rstrip()
