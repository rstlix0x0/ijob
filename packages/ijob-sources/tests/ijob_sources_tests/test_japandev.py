import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from ijob_core.countries import CountryCode
from ijob_core.model import ExternalId, SourceKind, SourceRef, parse_source_key
from ijob_core.money import Money, Period
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError, SourceNotFoundError
from ijob_sources.japandev import JapanDev, parse_sitemap
from ijob_sources.polite_http import CachedResponse, PoliteClient

_FIXTURES = Path(__file__).parent / "fixtures" / "japandev"
_SITEMAP = (_FIXTURES / "sitemap.xml").read_bytes()
_PLAIN = (_FIXTURES / "job_plain.html").read_text(encoding="utf-8")
_REMOTE = (_FIXTURES / "job_remote.html").read_text(encoding="utf-8")
_SALARY = (_FIXTURES / "job_salary.html").read_text(encoding="utf-8")
_KEY = parse_source_key("all")
_SITEMAP_URL = "https://japan-dev.com/cdn/sitemaps/sitemap.xml"
_ORIGIN = "https://japan-dev.com"

_PLAIN_ID = "abbeal-ai-engineer-maj4uw"
_REMOTE_ID = "alpaca-senior-software-engineer---new-markets-japan-jlpt-n1-fluent-swq1og"
_SALARY_ID = (
    "abbeal-avionics-engineer--specification--vv-flight-controls-do-178c--aichi-r4n1gq"
)
_PLAIN_PATH = f"/jobs/abbeal/{_PLAIN_ID}"
_REMOTE_PATH = f"/jobs/alpaca/{_REMOTE_ID}"
_SALARY_PATH = f"/jobs/abbeal/{_SALARY_ID}"
_PAGES = {_PLAIN_PATH: _PLAIN, _REMOTE_PATH: _REMOTE, _SALARY_PATH: _SALARY}


def _sitemap(*paths: str) -> bytes:
    urls = "".join(f"<url><loc>{_ORIGIN}{path}</loc></url>" for path in paths)
    return (
        '<?xml version="1.0"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    ).encode()


def _page_for(slug: str, **overrides: object) -> str:
    posting: dict[str, object] = {
        "@type": "JobPosting",
        "title": f"Engineer {slug}",
        "datePosted": "2026-10-01",
        "url": f"{_ORIGIN}/jobs/acme/{slug}",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressCountry": "JP"},
        },
        "identifier": {"@type": "PropertyValue", "value": slug},
    }
    posting.update(overrides)
    block = json.dumps(posting)
    return f'<script type="application/ld+json">{block}</script><body></body>'


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _DictCache:
    def __init__(self, entries: dict[str, CachedResponse]) -> None:
        self.entries = entries

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


def _site(
    sitemap: bytes,
    pages: dict[str, str] | None = None,
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    served = _PAGES if pages is None else pages

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if requests is not None:
            requests.append(request)
        if request.url.path == "/cdn/sitemaps/sitemap.xml":
            return httpx.Response(200, content=sitemap)
        page = served.get(request.url.path)
        if page is None:
            return httpx.Response(404)
        return httpx.Response(200, content=page.encode())

    return handler


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    cache: _DictCache | None = None,
) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        cache=cache,
    )


def _fetch(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    known: frozenset[ExternalId] = frozenset(),
    cache: _DictCache | None = None,
) -> SourceBatch:
    async def run() -> SourceBatch:
        async with _client(handler, cache) as client:
            return await JapanDev(_KEY, known=known).fetch(client)

    return asyncio.run(run())


def test_ref_names_the_source() -> None:
    assert JapanDev(_KEY).ref == SourceRef(SourceKind.JAPANDEV, _KEY)


def test_sitemap_keeps_job_urls() -> None:
    assert parse_sitemap(_SITEMAP) == [
        f"{_ORIGIN}{_PLAIN_PATH}",
        f"{_ORIGIN}{_REMOTE_PATH}",
        f"{_ORIGIN}{_SALARY_PATH}",
    ]


def test_sitemap_drops_other_hosts_queries_and_empty_ids() -> None:
    body = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://japan-dev.com/jobs/acme/keep-1</loc></url>"
        b"<url><loc>https://evil.test/jobs/acme/other-host</loc></url>"
        b"<url><loc>http://japan-dev.com/jobs/acme/plain-http</loc></url>"
        b"<url><loc>https://japan-dev.com/jobs/acme/with-query?lang=jp</loc></url>"
        b"<url><loc>https://japan-dev.com/jobs/acme/with-fragment#x</loc></url>"
        b"<url><loc>https://japan-dev.com/jobs/</loc></url>"
        b"<url><loc>  https://japan-dev.com/jobs/acme/keep-2  </loc></url>"
        b"<url><priority>0.5</priority></url>"
        b"<url><loc>https://japan-dev.com/jobs/acme/keep-1</loc></url>"
        b"</urlset>"
    )

    assert parse_sitemap(body) == [
        "https://japan-dev.com/jobs/acme/keep-1",
        "https://japan-dev.com/jobs/acme/keep-2",
    ]


def test_sitemap_without_namespace_is_read() -> None:
    body = b"<urlset><url><loc>https://japan-dev.com/jobs/a/b-1</loc></url></urlset>"

    assert parse_sitemap(body) == ["https://japan-dev.com/jobs/a/b-1"]


_ENTITY_BOMB = (
    b'<?xml version="1.0"?><!DOCTYPE urlset ['
    b'<!ENTITY a0 "lol">'
    + b"".join(
        b'<!ENTITY a%d "%s">' % (n, b"&a%d;" % (n - 1) * 10) for n in range(1, 10)
    )
    + b"]><urlset><url><loc>&a9;</loc></url></urlset>"
)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not xml",
        b"<urlset><url></urlset>",
        b"<sitemapindex><sitemap><loc>https://japan-dev.com/x</loc></sitemap></sitemapindex>",
        _ENTITY_BOMB,
    ],
    ids=["empty", "not-xml", "mismatched", "sitemap-index", "entity-bomb"],
)
def test_unreadable_sitemap_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_sitemap(body)


def test_unknown_declared_encoding_is_parse_error() -> None:
    body = (
        b'<?xml version="1.0" encoding="x-nope"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>'
    )

    with pytest.raises(ParseError):
        parse_sitemap(body)


def test_deeply_nested_sitemap_is_not_a_crash() -> None:
    body = b"<urlset>" + b"<a>" * 100_000 + b"</a>" * 100_000 + b"</urlset>"

    assert parse_sitemap(body) == []


def test_known_jobs_still_listed_without_request() -> None:
    requests: list[httpx.Request] = []

    batch = _fetch(
        _site(_SITEMAP, requests=requests), known=frozenset({ExternalId(_PLAIN_ID)})
    )

    assert batch.still_listed == (ExternalId(_PLAIN_ID),)
    assert [draft.external_id for draft in batch.drafts] == [_REMOTE_ID, _SALARY_ID]
    assert [request.url.path for request in requests] == [
        "/cdn/sitemaps/sitemap.xml",
        _REMOTE_PATH,
        _SALARY_PATH,
    ]


def test_known_job_gone_from_sitemap_is_not_listed() -> None:
    batch = _fetch(_site(_SITEMAP), known=frozenset({ExternalId("gone-1")}))

    assert batch.still_listed == ()
    assert len(batch.drafts) == 3


def test_everything_known_makes_no_detail_request() -> None:
    requests: list[httpx.Request] = []
    known = frozenset({ExternalId(_PLAIN_ID), ExternalId(_REMOTE_ID)})

    batch = _fetch(
        _site(_sitemap(_PLAIN_PATH, _REMOTE_PATH), requests=requests), known=known
    )

    assert batch.drafts == []
    assert batch.still_listed == (ExternalId(_PLAIN_ID), ExternalId(_REMOTE_ID))
    assert [request.url.path for request in requests] == ["/cdn/sitemaps/sitemap.xml"]


def test_new_job_mapped() -> None:
    drafts = {draft.external_id: draft for draft in _fetch(_site(_SITEMAP)).drafts}

    remote = drafts[ExternalId(_REMOTE_ID)]
    assert remote.source == SourceRef(SourceKind.JAPANDEV, _KEY)
    assert (
        remote.title == "Senior Software Engineer - New Markets Japan (JLPT N1 Fluent)"
    )
    assert remote.company == "Alpaca"
    assert remote.location == "Remote"
    assert remote.country == CountryCode("JP")
    assert remote.remote is None
    assert remote.url == f"{_ORIGIN}{_REMOTE_PATH}"
    assert remote.posted_at == datetime(2026, 10, 9, tzinfo=UTC)
    assert remote.salaries == ()
    raw = json.loads(remote.raw)
    assert raw["item"] == f"{_ORIGIN}{_REMOTE_PATH}"
    assert raw["jobposting"]["jobLocationType"] == "TELECOMMUTE"

    salary = drafts[ExternalId(_SALARY_ID)]
    assert salary.location == "Nagoya"
    assert salary.country == CountryCode("JP")
    assert salary.salaries == (
        Money(Decimal(8000000), Decimal(16000000), "JPY", Period.YEAR),
    )
    assert drafts[ExternalId(_PLAIN_ID)].salaries == ()


def test_url_falls_back_to_sitemap_and_id_is_the_slug() -> None:
    page = _page_for("x-1", identifier=None, url=None)
    path = "/jobs/acme/x-1"

    draft = _fetch(_site(_sitemap(path), {path: page})).drafts[0]

    assert draft.external_id == "x-1"
    assert draft.url == f"{_ORIGIN}{path}"


def test_draft_id_is_the_slug_when_the_page_identifier_differs() -> None:
    path = "/jobs/acme/x-1"
    pages = {path: _page_for("x-1", identifier={"value": "other-9"})}

    first = _fetch(_site(_sitemap(path), pages)).drafts

    assert [draft.external_id for draft in first] == ["x-1"]

    requests: list[httpx.Request] = []
    again = _fetch(
        _site(_sitemap(path), pages, requests),
        known=frozenset({first[0].external_id}),
    )

    assert again.drafts == []
    assert again.still_listed == (ExternalId("x-1"),)
    assert [request.url.path for request in requests] == ["/cdn/sitemaps/sitemap.xml"]


def test_detail_cap_100_and_failure() -> None:
    slugs = [f"job-{n:03d}" for n in range(120)]
    paths = [f"/jobs/acme/{slug}" for slug in slugs]
    pages = {path: _page_for(slug) for path, slug in zip(paths, slugs, strict=True)}
    del pages[paths[7]]
    requests: list[httpx.Request] = []

    batch = _fetch(_site(_sitemap(*paths), pages, requests))

    details = [r for r in requests if r.url.path != "/cdn/sitemaps/sitemap.xml"]
    assert [r.url.path for r in details] == paths[:100]
    assert len(batch.drafts) == 99
    assert batch.rejected == 1
    assert ExternalId("job-007") not in {draft.external_id for draft in batch.drafts}
    assert batch.still_listed == ()


def test_cap_counts_only_new_jobs() -> None:
    slugs = [f"job-{n:03d}" for n in range(120)]
    paths = [f"/jobs/acme/{slug}" for slug in slugs]
    pages = {path: _page_for(slug) for path, slug in zip(paths, slugs, strict=True)}
    known = frozenset(ExternalId(slug) for slug in slugs[:30])

    batch = _fetch(_site(_sitemap(*paths), pages), known=known)

    assert len(batch.still_listed) == 30
    assert [draft.external_id for draft in batch.drafts] == slugs[30:120]


def test_only_sitemap_and_job_pages_requested() -> None:
    requests: list[httpx.Request] = []

    _fetch(_site(_SITEMAP, requests=requests))

    assert requests
    assert all(request.url.host == "japan-dev.com" for request in requests)
    assert all(
        request.url.path == "/cdn/sitemaps/sitemap.xml"
        or request.url.path.startswith("/jobs/")
        for request in requests
    )
    assert all(not request.url.query for request in requests)


def test_sitemap_is_requested_conditionally() -> None:
    cache = _DictCache({_SITEMAP_URL: CachedResponse('"s"', None, _SITEMAP)})
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        requests.append(request)
        if request.url.path == "/cdn/sitemaps/sitemap.xml":
            return httpx.Response(304)
        return httpx.Response(200, content=_PAGES[request.url.path].encode())

    batch = _fetch(handler, cache=cache)

    assert requests[0].headers["if-none-match"] == '"s"'
    assert len(batch.drafts) == 3


@pytest.mark.parametrize(
    "page",
    [
        "<html><body>no postings</body></html>",
        _page_for("x-1", datePosted="yesterday"),
        _page_for("x-1", title=7),
        _page_for("x-1", title=""),
        _page_for("x-1", hiringOrganization=None),
        _page_for("x-1", url="ftp://example.test/x"),
    ],
    ids=[
        "no-posting",
        "bad-date",
        "non-text-title",
        "blank-title",
        "no-company",
        "non-http-url",
    ],
)
def test_unusable_detail_page_is_rejected_and_others_kept(
    page: str, caplog: pytest.LogCaptureFixture
) -> None:
    bad, good = "/jobs/acme/x-1", "/jobs/acme/y-2"
    pages = {bad: page, good: _page_for("y-2")}

    with caplog.at_level(logging.WARNING, logger="ijob_sources.japandev"):
        batch = _fetch(_site(_sitemap(bad, good), pages))

    assert [draft.external_id for draft in batch.drafts] == ["y-2"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "x-1" in warnings[0].getMessage()


def test_failed_detail_request_is_logged_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/cdn/sitemaps/sitemap.xml":
            return httpx.Response(200, content=_sitemap("/jobs/acme/x-1"))
        raise httpx.ConnectError("refused")

    with caplog.at_level(logging.WARNING, logger="ijob_sources.japandev"):
        batch = _fetch(handler)

    assert batch.drafts == []
    assert batch.rejected == 1
    assert "x-1" in caplog.text


def test_missing_sitemap_is_source_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(404)

    with pytest.raises(SourceNotFoundError):
        _fetch(handler)


def test_unreadable_sitemap_fails_the_fetch() -> None:
    with pytest.raises(ParseError):
        _fetch(_site(b"not xml"))


def test_collect_runs_the_source() -> None:
    async def run() -> CollectResult:
        async with _client(_site(_SITEMAP)) as client:
            return await collect([JapanDev(_KEY)], client)

    result = asyncio.run(run())

    assert result.failures == []
    assert len(result.batches[SourceRef(SourceKind.JAPANDEV, _KEY)].drafts) == 3
