import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_sources.collect import Source, SourceBatch
from ijob_sources.errors import (
    HttpStatusError,
    RecipeFailedError,
    RobotsDisallowedError,
    UnsupportedUrlError,
)
from ijob_sources.polite_http import PoliteClient
from ijob_sources.recipe_source import RecipeSource

_FIXTURES = Path(__file__).parent / "fixtures" / "recipe"
_KEY = parse_source_key("example")
_REF = SourceRef(SourceKind.RECIPE, _KEY)
_ORIGIN = "https://example.org"
_REANALYZE = "run: ijob sources reanalyze recipe:example"
_ALLOW_ALL = "User-agent: *\nDisallow:\n"
_OVER_LIMIT = b"<html>" + b"x" * (5 * 1024 * 1024)

_PAGED: dict[str, Any] = {
    "version": 1,
    "format": "html",
    "url": f"{_ORIGIN}/jobs/",
    "items": "//li[@class='job']",
    "fields": {"title": ".//a", "url": ".//a/@href"},
    "next_page": "//a[@rel='next']/@href",
}


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Site:
    """Serves canned responses by path and records every page request."""

    def __init__(
        self,
        pages: Mapping[str, httpx.Response],
        robots: str = _ALLOW_ALL,
    ) -> None:
        self._pages = pages
        self._robots = robots
        self.fetched: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self._robots)
        self.fetched.append(request.url.path)
        return self._pages.get(request.url.path, httpx.Response(404))


def _html(body: bytes | str, content_type: str = "text/html") -> httpx.Response:
    content = body.encode() if isinstance(body, str) else body
    return httpx.Response(200, content=content, headers={"Content-Type": content_type})


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _source(recipe: dict[str, Any] | str) -> RecipeSource:
    text = recipe if isinstance(recipe, str) else json.dumps(recipe)
    return RecipeSource(_KEY, company="Example Co", recipe=text)


def _fetch(source: Source, site: _Site) -> SourceBatch:
    async def run() -> SourceBatch:
        fake_time = _FakeTime()
        async with PoliteClient(
            transport=httpx.MockTransport(site),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
            min_interval=0.0,
        ) as client:
            return await source.fetch(client)

    return asyncio.run(run())


def _paged_site(
    overrides: Mapping[str, httpx.Response] | None = None,
    robots: str = _ALLOW_ALL,
) -> _Site:
    pages = {
        "/jobs/": _html(_fixture("paged_1.html")),
        "/jobs/page-2/": _html(_fixture("paged_2.html")),
    }
    pages.update(overrides or {})
    return _Site(pages, robots)


def test_ref_is_the_recipe_kind() -> None:
    assert _source(_PAGED).ref == _REF


def test_reads_one_page() -> None:
    recipe = {
        "version": 1,
        "format": "html",
        "url": f"{_ORIGIN}/jobs/",
        "items": "//li",
        "fields": {
            "title": ".//a/text()",
            "url": ".//a/@href",
        },
    }
    site = _Site({"/jobs/": _html(_fixture("list_bare_company.html"))})

    batch = _fetch(_source(recipe), site)

    assert [draft.title for draft in batch.drafts] == [
        "Senior Python Engineer",
        "Data Platform Developer",
        "Backend Engineer, Django",
    ]
    assert batch.rejected == 0
    assert {draft.company for draft in batch.drafts} == {"Example Co"}
    assert {draft.source for draft in batch.drafts} == {_REF}
    assert site.fetched == ["/jobs/"]


def test_rejected_counts_items_that_are_not_jobs() -> None:
    page = (
        '<ul><li class="job"><a href="/jobs/1/">One</a></li>'
        '<li class="job"><a href="/jobs/2/"></a></li>'
        '<li class="job"><span>no link</span></li></ul>'
    )

    batch = _fetch(_source(_PAGED), _Site({"/jobs/": _html(page)}))

    assert [draft.title for draft in batch.drafts] == ["One"]
    assert batch.rejected == 2


def test_paginates_and_stops_on_loop() -> None:
    site = _paged_site()

    batch = _fetch(_source(_PAGED), site)

    assert site.fetched == ["/jobs/", "/jobs/page-2/"]
    assert [draft.title for draft in batch.drafts] == [
        "Platform Engineer",
        "Data Engineer",
        "Security Engineer",
        "Site Reliability Engineer",
    ]


def test_stops_at_five_pages() -> None:
    pages = {
        f"/p/{number}": _html(
            f'<ul><li class="job"><a href="/j/{number}">Job {number}</a></li></ul>'
            f'<a rel="next" href="/p/{number + 1}">next</a>'
        )
        for number in range(1, 8)
    }
    recipe = {**_PAGED, "url": f"{_ORIGIN}/p/1"}
    site = _Site(pages)

    batch = _fetch(_source(recipe), site)

    assert site.fetched == [f"/p/{number}" for number in range(1, 6)]
    assert len(batch.drafts) == 5


def test_stops_on_page_without_items() -> None:
    pages = {
        "/jobs/": _html(_fixture("paged_1.html")),
        "/jobs/page-2/": _html(
            '<p>nothing here</p><a rel="next" href="/jobs/p3/">n</a>'
        ),
        "/jobs/p3/": _html(_fixture("paged_2.html")),
    }
    site = _Site(pages)

    batch = _fetch(_source(_PAGED), site)

    assert site.fetched == ["/jobs/", "/jobs/page-2/"]
    assert [draft.title for draft in batch.drafts] == [
        "Platform Engineer",
        "Data Engineer",
    ]


@pytest.mark.parametrize(
    "case",
    ["status", "robots", "content_type", "oversize"],
)
def test_page_two_failure_keeps_page_one(case: str) -> None:
    overrides: dict[str, httpx.Response] = {}
    robots = _ALLOW_ALL
    match case:
        case "status":
            overrides["/jobs/page-2/"] = httpx.Response(500)
        case "robots":
            robots = "User-agent: *\nDisallow: /jobs/page-2/\n"
        case "content_type":
            overrides["/jobs/page-2/"] = _html("plain", "text/plain")
        case _:
            overrides["/jobs/page-2/"] = _html(_OVER_LIMIT)
    site = _paged_site(overrides, robots)

    batch = _fetch(_source(_PAGED), site)

    assert [draft.title for draft in batch.drafts] == [
        "Platform Engineer",
        "Data Engineer",
    ]


@pytest.mark.parametrize(
    ("response", "robots", "error"),
    [
        (httpx.Response(500), _ALLOW_ALL, HttpStatusError),
        (
            _html(_fixture("paged_1.html")),
            "User-agent: *\nDisallow: /jobs/\n",
            RobotsDisallowedError,
        ),
        (_html("plain", "text/plain"), _ALLOW_ALL, UnsupportedUrlError),
        (_html(_OVER_LIMIT), _ALLOW_ALL, UnsupportedUrlError),
    ],
    ids=["status", "robots", "content_type", "oversize"],
)
def test_page_one_failure_propagates(
    response: httpx.Response, robots: str, error: type[Exception]
) -> None:
    site = _Site({"/jobs/": response}, robots)

    with pytest.raises(error):
        _fetch(_source(_PAGED), site)


def test_no_jobs_message() -> None:
    site = _Site({"/jobs/": _html("<ul></ul>")})

    with pytest.raises(RecipeFailedError) as raised:
        _fetch(_source(_PAGED), site)

    assert str(raised.value) == (
        "recipe for recipe:example found no jobs; "
        "if the site changed, run: ijob sources reanalyze recipe:example"
    )


def test_page_one_of_only_invalid_items_is_no_jobs() -> None:
    page = '<ul><li class="job"><span>no link</span></li></ul>'

    with pytest.raises(RecipeFailedError, match="found no jobs"):
        _fetch(_source(_PAGED), _Site({"/jobs/": _html(page)}))


def test_format_mismatch_message() -> None:
    recipe = {
        "version": 1,
        "format": "json",
        "url": f"{_ORIGIN}/jobs/",
        "items": "[*]",
        "fields": {"title": "title", "url": "url"},
    }
    site = _Site({"/jobs/": _html(_fixture("paged_1.html"))})

    with pytest.raises(RecipeFailedError) as raised:
        _fetch(_source(recipe), site)

    assert str(raised.value) == f"recipe for recipe:example expected json; {_REANALYZE}"


def test_html_recipe_served_json_is_a_format_mismatch() -> None:
    site = _Site({"/jobs/": _html("[]", "application/json")})

    with pytest.raises(RecipeFailedError) as raised:
        _fetch(_source(_PAGED), site)

    assert str(raised.value) == f"recipe for recipe:example expected html; {_REANALYZE}"


def test_page_two_format_mismatch_keeps_page_one() -> None:
    site = _paged_site({"/jobs/page-2/": _html("[]", "application/json")})

    batch = _fetch(_source(_PAGED), site)

    assert len(batch.drafts) == 2


def test_evaluation_error_message() -> None:
    recipe = {**_PAGED, "fields": {"title": "foo()", "url": ".//a/@href"}}
    site = _Site({"/jobs/": _html(_fixture("paged_1.html"))})

    with pytest.raises(RecipeFailedError) as raised:
        _fetch(_source(recipe), site)

    message = str(raised.value)
    assert message.startswith("recipe for recipe:example failed: ")
    assert message.endswith(f"; {_REANALYZE}")
    assert len(message) > len(f"recipe for recipe:example failed: ; {_REANALYZE}")


def test_page_two_evaluation_error_fails_the_source() -> None:
    page_two = '<ul data-bad="1"><li class="job"><a href="/jobs/3/">T</a></li></ul>'
    recipe = {**_PAGED, "items": "//li[@class='job'] | //ul/@data-bad"}
    site = _paged_site({"/jobs/page-2/": _html(page_two)})

    with pytest.raises(RecipeFailedError) as raised:
        _fetch(_source(recipe), site)

    assert str(raised.value).startswith("recipe for recipe:example failed: ")


def test_invalid_stored_recipe_message() -> None:
    with pytest.raises(RecipeFailedError) as raised:
        _fetch(_source("{"), _Site({}))

    message = str(raised.value)
    assert message.startswith("recipe for recipe:example failed: invalid recipe: ")
    assert message.endswith(f"; {_REANALYZE}")


def test_unsupported_version_message() -> None:
    recipe = {**_PAGED, "version": 2}
    site = _Site({})

    with pytest.raises(RecipeFailedError) as raised:
        _fetch(_source(recipe), site)

    assert str(raised.value) == f"recipe version 2 is not supported; {_REANALYZE}"
    assert site.fetched == []


def test_duplicate_ids_across_pages_keep_first() -> None:
    pages = {
        "/jobs/": _html(
            '<ul><li class="job"><a href="/jobs/1/">First</a></li></ul>'
            '<a rel="next" href="/jobs/page-2/">n</a>'
        ),
        "/jobs/page-2/": _html(
            '<ul><li class="job"><a href="/jobs/1/">Repeat</a></li>'
            '<li class="job"><a href="/jobs/2/">Second</a></li></ul>'
        ),
    }

    batch = _fetch(_source(_PAGED), _Site(pages))

    assert [draft.title for draft in batch.drafts] == ["First", "Second"]


def test_rejected_sums_every_page() -> None:
    pages = {
        "/jobs/": _html(
            '<ul><li class="job"><a href="/jobs/1/">One</a></li>'
            '<li class="job"><span>x</span></li></ul>'
            '<a rel="next" href="/jobs/page-2/">n</a>'
        ),
        "/jobs/page-2/": _html(
            '<ul><li class="job"><a href="/jobs/2/">Two</a></li>'
            '<li class="job"><span>y</span></li>'
            '<li class="job"><span>z</span></li></ul>'
        ),
    }

    batch = _fetch(_source(_PAGED), _Site(pages))

    assert len(batch.drafts) == 2
    assert batch.rejected == 3
