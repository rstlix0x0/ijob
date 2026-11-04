from ijob_sources.links import Link, extract_links

_BASE = "https://example.com/company/careers"


def _page(*anchors: str) -> bytes:
    return f"<html><body>{''.join(anchors)}</body></html>".encode()


def test_links_resolve_relative_addresses() -> None:
    body = _page(
        '<a href="/jobs">Jobs</a>',
        '<a href="team">Team</a>',
        '<a href="//boards.example.org/x">Board</a>',
        '<a href="https://other.test/a">Other</a>',
    )

    assert extract_links(body, _BASE) == [
        Link(url="https://example.com/jobs", text="Jobs"),
        Link(url="https://example.com/company/team", text="Team"),
        Link(url="https://boards.example.org/x", text="Board"),
        Link(url="https://other.test/a", text="Other"),
    ]


def test_links_drop_other_schemes() -> None:
    body = _page(
        '<a href="mailto:jobs@example.com">Mail</a>',
        '<a href="javascript:void(0)">Script</a>',
        '<a href="tel:+123">Call</a>',
        '<a href="ftp://example.com/x">Ftp</a>',
        '<a href="http://example.com/plain">Plain</a>',
    )

    assert extract_links(body, _BASE) == [
        Link(url="http://example.com/plain", text="Plain")
    ]


def test_links_drop_fragments_and_duplicates() -> None:
    body = _page(
        '<a href="/jobs#open">First</a>',
        '<a href="/jobs#closed">Second</a>',
        '<a href="/jobs">Third</a>',
    )

    assert extract_links(body, _BASE) == [
        Link(url="https://example.com/jobs", text="First")
    ]


def test_links_clean_text() -> None:
    long_text = "x" * 150
    body = _page(
        '<a href="/a">  Open\n\t roles\x07 here  </a>',
        f'<a href="/b">{long_text}</a>',
        '<a href="/c"><span>Nested</span> text</a>',
    )

    links = extract_links(body, _BASE)

    assert [link.text for link in links] == [
        "Open roles here",
        "x" * 100,
        "Nested text",
    ]


def test_links_skip_anchors_without_an_address() -> None:
    body = _page(
        '<a name="top">Top</a>', '<a href="">Empty</a>', '<a href="/ok">Ok</a>'
    )

    assert extract_links(body, _BASE) == [Link(url="https://example.com/ok", text="Ok")]


def test_links_skip_an_address_that_cannot_be_parsed() -> None:
    body = _page('<a href="http://[bad">Bad</a>', '<a href="/ok">Ok</a>')

    assert extract_links(body, _BASE) == [Link(url="https://example.com/ok", text="Ok")]


def test_links_of_a_non_html_body() -> None:
    assert extract_links(b"", _BASE) == []
    assert extract_links(b'{"jobs": []}', _BASE) == []
    assert extract_links(b"\x00\x01\xff\xfe", _BASE) == []


def test_links_read_utf8_without_a_declared_charset() -> None:
    body = (
        "<html><body><a href='/karri\u00e8re/\u30ab'>"
        "\u63a1\u7528\u60c5\u5831</a></body></html>"
    )

    assert extract_links(body.encode("utf-8"), _BASE) == [
        Link(
            url="https://example.com/karri\u00e8re/\u30ab",
            text="\u63a1\u7528\u60c5\u5831",
        )
    ]


def test_links_read_a_declared_legacy_charset() -> None:
    body = (
        "<html><head><meta charset='iso-8859-1'></head>"
        "<body><a href='/jobs'>Caf\u00e9 jobs</a></body></html>"
    )

    assert extract_links(body.encode("iso-8859-1"), _BASE) == [
        Link(url="https://example.com/jobs", text="Caf\u00e9 jobs")
    ]


def test_links_read_utf8_with_an_xml_declaration() -> None:
    body = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html><body><a href='/jobs'>Caf\u00e9</a></body></html>"
    )

    assert extract_links(body.encode("utf-8"), _BASE) == [
        Link(url="https://example.com/jobs", text="Caf\u00e9")
    ]
