import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, override

import httpx
import pytest
from ijob_sources.errors import UnsupportedUrlError
from ijob_sources.polite_http import MAX_BODY_BYTES, PoliteClient
from ijob_sources.recipe_document import (
    JSON_SAMPLE_CHARS,
    JSON_SAMPLE_ITEMS,
    MAX_JSON_DEPTH,
    SAMPLE_LIMIT,
    HtmlDocument,
    JsonDocument,
    fetch_document,
    load_document,
    sample_text,
)

_URL = "https://example.org/jobs/"
_PAGE = b"<html><body><p>hi</p></body></html>"


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("application/json; charset=utf-8", b'{"a": 1}', JsonDocument),
        ("application/ld+json", b"[1]", JsonDocument),
        ("application/vnd.api+json", b"[1]", JsonDocument),
        ("Application/JSON", b"[1]", JsonDocument),
        ("text/html; charset=utf-8", _PAGE, HtmlDocument),
        ("application/xhtml+xml", _PAGE, HtmlDocument),
        ("TEXT/HTML", _PAGE, HtmlDocument),
    ],
)
def test_format_from_content_type(
    content_type: str, body: bytes, expected: type[object]
) -> None:
    assert isinstance(load_document(body, content_type, _URL), expected)


def test_content_type_wins_over_the_body() -> None:
    assert isinstance(load_document(b"[1]", "text/html", _URL), HtmlDocument)
    with pytest.raises(UnsupportedUrlError):
        load_document(b"<p>x</p>", "application/json", _URL)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"  [1]", JsonDocument),
        (b'{"a":1}', JsonDocument),
        (b"\n\t <html>", HtmlDocument),
        (b"<p>x</p>", HtmlDocument),
    ],
)
def test_format_sniffed_without_header(body: bytes, expected: type[object]) -> None:
    assert isinstance(load_document(body, None, _URL), expected)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", b"hello"),
        ("application/xml", b"<a/>"),
        ("image/png", b"\x89PNG"),
        (None, b""),
        (None, b"   \n"),
        (None, b"hello"),
    ],
)
def test_other_types_refused(content_type: str | None, body: bytes) -> None:
    with pytest.raises(UnsupportedUrlError) as caught:
        load_document(body, content_type, _URL)
    assert caught.value.url == _URL


def test_body_over_cap_refused() -> None:
    padding = b" " * (MAX_BODY_BYTES - len(_PAGE))
    assert isinstance(load_document(_PAGE + padding, "text/html", _URL), HtmlDocument)
    with pytest.raises(UnsupportedUrlError, match="5 MiB"):
        load_document(_PAGE + padding + b" ", "text/html", _URL)


@pytest.mark.parametrize("body", [b'{"a": "\xff"}', b'{"a":', b"", b"[1,]"])
def test_bad_json_refused(body: bytes) -> None:
    with pytest.raises(UnsupportedUrlError):
        load_document(body, "application/json", _URL)


def test_deeply_nested_json_refused() -> None:
    body = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(UnsupportedUrlError):
        load_document(body, "application/json", _URL)


def _nested(opening: bytes, closing: bytes, innermost: bytes, depth: int) -> bytes:
    return opening * depth + innermost + closing * depth


@pytest.mark.parametrize(
    ("opening", "closing", "innermost"),
    [(b"[", b"]", b"1"), (b'{"a":', b"}", b"1")],
    ids=["arrays", "objects"],
)
def test_json_nesting_bound(opening: bytes, closing: bytes, innermost: bytes) -> None:
    at_bound = _nested(opening, closing, innermost, MAX_JSON_DEPTH)
    assert isinstance(load_document(at_bound, "application/json", _URL), JsonDocument)
    over_bound = _nested(opening, closing, innermost, MAX_JSON_DEPTH + 1)
    with pytest.raises(UnsupportedUrlError, match="nested"):
        load_document(over_bound, "application/json", _URL)


@pytest.mark.parametrize(
    ("opening", "closing", "innermost"),
    [(b"[", b"]", b"1"), (b'{"a":', b"}", b"1")],
    ids=["arrays", "objects"],
)
def test_json_nested_thousands_deep_is_refused_not_a_recursion_error(
    opening: bytes, closing: bytes, innermost: bytes
) -> None:
    body = _nested(opening, closing, innermost, 3000)
    with pytest.raises(UnsupportedUrlError):
        load_document(body, "application/json", _URL)


def test_json_at_the_bound_can_be_sampled() -> None:
    body = _nested(b"[", b"]", b"1", MAX_JSON_DEPTH)
    document = load_document(body, "application/json", _URL)
    assert sample_text(document) == body.decode()


@pytest.mark.parametrize("charset", ["utf-8-sig", "rot13", "base64"])
def test_html_charset_python_knows_but_libxml2_does_not(charset: str) -> None:
    try:
        document = load_document(b"<p>hi</p>", f"text/html; charset={charset}", _URL)
    except UnsupportedUrlError:
        return
    assert isinstance(document, HtmlDocument)
    assert "hi" in sample_text(document)


def test_html_charset_latin_1_is_read() -> None:
    body = "<p>café</p>".encode("latin-1")
    document = load_document(body, "text/html; charset=latin-1", _URL)
    assert isinstance(document, HtmlDocument)
    assert "café" in sample_text(document)


def test_html_that_cannot_be_parsed_refused() -> None:
    with pytest.raises(UnsupportedUrlError):
        load_document(b"<!-- nothing -->", "text/html", _URL)


def test_html_charset_from_header_is_used() -> None:
    body = "<p>café</p>".encode("latin-1")
    document = load_document(body, "text/html; charset=iso-8859-1", _URL)
    assert isinstance(document, HtmlDocument)
    assert "café" in sample_text(document)


_DIRTY = b"""<html><head>
<style>p { color: red }</style>
<script>var x = 1;</script>
<!-- a comment -->
</head><body onload="go()">
<div id="top" class="box" style="color:red" onclick="x()" data-id="7">
  before <script>evil()</script>after-script
  <svg><path d="M0"/></svg><noscript>no js</noscript>
  <iframe src="https://x.example/"></iframe>
  <template><b>tmpl</b></template>
  <time datetime="2026-10-23">kept</time>
  <a href="/jobs/1/" onmouseover="y()">job</a>
</div></body></html>"""


def test_cleaning_drops_scripts_styles_and_handlers() -> None:
    document = load_document(_DIRTY, "text/html", _URL)
    assert isinstance(document, HtmlDocument)
    tree = document.tree
    for tag in ("script", "style", "svg", "noscript", "iframe", "template"):
        assert tree.xpath(f"//{tag}") == []
    assert tree.xpath("//comment()") == []
    assert tree.xpath("//@style") == []
    assert tree.xpath("//@*[starts-with(name(), 'on')]") == []
    assert tree.xpath("//a/@href") == ["/jobs/1/"]
    assert tree.xpath("//div/@class") == ["box"]
    assert tree.xpath("//div/@id") == ["top"]
    assert tree.xpath("//time/@datetime") == ["2026-10-23"]
    assert tree.xpath("//div/@data-id") == ["7"]
    text = sample_text(document)
    assert "after-script" in text
    assert "evil" not in text
    assert "tmpl" not in text
    assert "no js" not in text
    assert "kept" in text


def test_html_sample_collapses_and_truncates() -> None:
    short = load_document(b"<p>a   b\n\n\tc</p>", "text/html", _URL)
    assert sample_text(short) == "<html><body><p>a b c</p></body></html>"
    filler = b"<p>" + b"word " * 20_000 + b"</p>"
    long = load_document(filler, "text/html", _URL)
    sample = sample_text(long)
    assert sample.endswith("\n[truncated]")
    assert len(sample) == SAMPLE_LIMIT + len("\n[truncated]")
    assert sample[:SAMPLE_LIMIT].startswith("<html><body><p>word word")


def test_html_sample_at_the_limit_is_not_marked() -> None:
    probe = sample_text(load_document(b"<p></p>", "text/html", _URL))
    fill = SAMPLE_LIMIT - len(probe)
    body = b"<p>" + b"x" * fill + b"</p>"
    sample = sample_text(load_document(body, "text/html", _URL))
    assert len(sample) == SAMPLE_LIMIT
    assert "[truncated]" not in sample


def test_json_sample_cuts_arrays_and_strings() -> None:
    items: list[dict[str, Any]] = [
        {
            "title": "t" * 1_000,
            "tags": [f"tag{n}" for n in range(10)],
            "nested": {"deep": [{"x": "y" * 1_000} for _ in range(9)]},
            "n": 7,
            "flag": True,
            "none": None,
        }
        for _ in range(35)
    ]
    document = load_document(json.dumps(items).encode(), "application/json", _URL)
    assert isinstance(document, JsonDocument)
    assert len(document.value) == 35  # type: ignore[arg-type]
    sample = json.loads(sample_text(document))
    assert len(sample) == JSON_SAMPLE_ITEMS == 3
    first = sample[0]
    assert first["title"] == "t" * JSON_SAMPLE_CHARS
    assert JSON_SAMPLE_CHARS == 300
    assert first["tags"] == ["tag0", "tag1", "tag2"]
    deep = first["nested"]["deep"]
    assert len(deep) == 3
    assert deep[0]["x"] == "y" * 300
    assert (first["n"], first["flag"], first["none"]) == (7, True, None)


def test_json_sample_keeps_non_ascii_text() -> None:
    document = load_document('{"t": "café"}'.encode(), "application/json", _URL)
    assert sample_text(document) == '{"t": "café"}'


def _site(handler: Callable[[httpx.Request], httpx.Response]) -> PoliteClient:
    async def no_sleep(_: float) -> None:
        return None

    return PoliteClient(
        transport=httpx.MockTransport(handler), sleep=no_sleep, min_interval=0.0
    )


def test_fetch_document_uses_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/robots.txt":
                return httpx.Response(404)
            case "/jobs":
                return httpx.Response(301, headers={"Location": "/jobs/"})
            case _:
                return httpx.Response(
                    200, headers={"Content-Type": "text/html"}, content=_PAGE
                )

    async def run() -> tuple[object, str]:
        async with _site(handler) as client:
            return await fetch_document(client, "https://example.org/jobs")

    document, final = asyncio.run(run())
    assert isinstance(document, HtmlDocument)
    assert final == "https://example.org/jobs/"


def test_fetch_document_reads_json_and_refuses_other_types() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/api":
            return httpx.Response(200, json=[{"a": 1}])
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="x")

    async def run() -> object:
        async with _site(handler) as client:
            document, _ = await fetch_document(client, "https://example.org/api")
            with pytest.raises(UnsupportedUrlError):
                await fetch_document(client, "https://example.org/other")
            return document

    document = asyncio.run(run())
    assert isinstance(document, JsonDocument)
    assert document.value == [{"a": 1}]


class _EndlessPage(httpx.AsyncByteStream):
    """An HTML body of 100 chunks of 1 MiB that counts the chunks read."""

    def __init__(self) -> None:
        self.yielded = 0

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(100):
            self.yielded += 1
            yield b" " * 1_048_576


def test_fetch_document_stops_reading_a_page_over_5_mib() -> None:
    page = _EndlessPage()

    class _Transport(httpx.AsyncBaseTransport):
        @override
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200, headers={"Content-Type": "text/html"}, stream=page
            )

    async def no_sleep(_: float) -> None:
        return None

    async def run() -> None:
        async with PoliteClient(transport=_Transport(), sleep=no_sleep) as client:
            await fetch_document(client, "https://example.org/jobs")

    with pytest.raises(UnsupportedUrlError) as caught:
        asyncio.run(run())

    assert caught.value.url == "https://example.org/jobs"
    assert caught.value.reason == "the page is larger than 5 MiB"
    assert page.yielded == 6
