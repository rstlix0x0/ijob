import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.resources import files

import httpx
import pytest
from ijob_core.model import (
    ExternalId,
    PostingId,
    Sighting,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_sources.arbeitnow import parse_arbeitnow
from ijob_sources.ashby import parse_ashby
from ijob_sources.description import TextForm, normalize
from ijob_sources.details import describe
from ijob_sources.errors import (
    ParseError,
    RobotsDisallowedError,
    SourceNotFoundError,
)
from ijob_sources.fourdayweek import parse_fourdayweek
from ijob_sources.gem import parse_gem
from ijob_sources.greenhouse import parse_jobs
from ijob_sources.himalayas import parse_himalayas
from ijob_sources.hn import parse_comments
from ijob_sources.japandev import JapanDev
from ijob_sources.jobicy import parse_jobicy
from ijob_sources.jsonld import job_postings
from ijob_sources.kalibrr import parse_kalibrr
from ijob_sources.lever import parse_lever
from ijob_sources.manual import parse_feed, parse_jobposting_page
from ijob_sources.mycareersfuture import parse_mycareersfuture
from ijob_sources.polite_http import PoliteClient
from ijob_sources.remoteok import parse_remoteok
from ijob_sources.rss import item_raw, parse_rss

type _Handler = Callable[[httpx.Request], httpx.Response]

_NOW = datetime(2026, 11, 1, tzinfo=UTC)
_KEY = parse_source_key("acme")
_JAPANDEV_KEY = parse_source_key("all")
_ROBOTS_ALLOW = httpx.Response(200, text="User-agent: *\nDisallow:\n")


def _fixture(package: str, name: str) -> bytes:
    return (files(f"ijob_sources_tests.fixtures.{package}") / name).read_bytes()


def _text(package: str, name: str) -> str:
    return _fixture(package, name).decode()


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _client(handler: _Handler) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )


def _serving(
    response: httpx.Response,
    requests: list[httpx.Request],
    robots: httpx.Response = _ROBOTS_ALLOW,
) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return robots
        requests.append(request)
        return response

    return handler


def _describe(sighting: Sighting, handler: _Handler) -> str | None:
    async def run() -> str | None:
        async with _client(handler) as client:
            return await describe(sighting, client)

    return asyncio.run(run())


def _sighting(
    kind: SourceKind,
    raw: str,
    *,
    key: str = "acme",
    external_id: str = "job-1",
) -> Sighting:
    return Sighting(
        posting_id=PostingId(1),
        source=SourceRef(kind, parse_source_key(key)),
        external_id=ExternalId(external_id),
        first_seen=_NOW,
        last_seen=_NOW,
        raw=raw,
    )


def _described(drafts: Sequence[SightingDraft]) -> SightingDraft:
    return next(draft for draft in drafts if draft.description is not None)


def _japandev_draft() -> SightingDraft:
    pages = {
        "/jobs/abbeal/abbeal-ai-engineer-maj4uw": _fixture("japandev", "job_plain.html")
    }
    sitemap = (
        b'<?xml version="1.0"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://japan-dev.com/jobs/abbeal/abbeal-ai-engineer-maj4uw</loc></url>"
        b"</urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return _ROBOTS_ALLOW
        if request.url.path == "/cdn/sitemaps/sitemap.xml":
            return httpx.Response(200, content=sitemap)
        return httpx.Response(200, content=pages[request.url.path])

    async def run() -> SightingDraft:
        async with _client(handler) as client:
            batch = await JapanDev(_JAPANDEV_KEY).fetch(client)
        return _described(batch.drafts)

    return asyncio.run(run())


def _feed_drafts() -> list[SightingDraft]:
    return parse_feed(
        _fixture("rss", "golang.xml"),
        parse_source_key("0123456789abcdef"),
        target_url="https://golangprojects.test/rss",
        company="Golang Projects",
    ).drafts


_HN_STORY = 900000002


def _payload_cases() -> list[tuple[SourceKind, SightingDraft]]:
    jobposting = parse_jobposting_page(
        _fixture("jsonld", "jazzhr.html"),
        parse_source_key("0123456789abcdef"),
        target_url="https://eclipse.test/job",
        company="Eclipse Foundation",
    ).drafts
    feed = _described(_feed_drafts())
    return [
        (
            SourceKind.GREENHOUSE,
            _described(
                parse_jobs(_fixture("greenhouse", "tailscale_jobs.json"), _KEY).drafts
            ),
        ),
        (
            SourceKind.GEM,
            _described(
                parse_gem(
                    _fixture("gem", "retool_job_posts.json"), _KEY, "Retool"
                ).drafts
            ),
        ),
        (
            SourceKind.ASHBY,
            _described(
                parse_ashby(
                    _fixture("ashby", "ramp_job_board.json"), _KEY, "Ramp"
                ).drafts
            ),
        ),
        (
            SourceKind.LEVER,
            _described(
                parse_lever(
                    _fixture("lever", "spotify_postings.json"), _KEY, "Spotify"
                ).drafts
            ),
        ),
        (
            SourceKind.HIMALAYAS,
            _described(
                parse_himalayas(_fixture("himalayas", "page1.json"), _KEY).batch.drafts
            ),
        ),
        (
            SourceKind.JOBICY,
            _described(
                parse_jobicy(_fixture("jobicy", "page1.json"), _KEY).batch.drafts
            ),
        ),
        (
            SourceKind.REMOTEOK,
            _described(parse_remoteok(_fixture("remoteok", "api.json"), _KEY).drafts),
        ),
        (
            SourceKind.ARBEITNOW,
            _described(
                parse_arbeitnow(_fixture("arbeitnow", "page1.json"), _KEY).batch.drafts
            ),
        ),
        (
            SourceKind.MYCAREERSFUTURE,
            _described(
                parse_mycareersfuture(
                    _fixture("mycareersfuture", "page0.json"), _KEY
                ).batch.drafts
            ),
        ),
        (
            SourceKind.KALIBRR,
            _described(
                parse_kalibrr(_fixture("kalibrr", "page0.json"), _KEY).batch.drafts
            ),
        ),
        (
            SourceKind.FOURDAYWEEK,
            _described(
                parse_fourdayweek(
                    _fixture("fourdayweek", "page1.json"), _KEY
                ).batch.drafts
            ),
        ),
        (
            SourceKind.HN,
            _described(
                parse_comments(_fixture("hn", "comments.json"), _HN_STORY, _KEY).drafts
            ),
        ),
        (SourceKind.JAPANDEV, _japandev_draft()),
        (SourceKind.JOBPOSTING, _described(jobposting)),
        (SourceKind.FEED, feed),
        (SourceKind.RSS, feed),
    ]


_PAYLOAD_CASES = _payload_cases()


def _no_request(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"unexpected request to {request.url}")


@pytest.mark.parametrize(
    ("kind", "draft"), _PAYLOAD_CASES, ids=[kind for kind, _ in _PAYLOAD_CASES]
)
def test_describe_payload_kinds_use_raw(kind: SourceKind, draft: SightingDraft) -> None:
    sighting = _sighting(kind, draft.raw)

    result = _describe(sighting, _no_request)

    assert draft.description is not None
    assert result == draft.description


def test_describe_feed_kind_reads_rss_raw() -> None:
    item = parse_rss(_fixture("rss", "golang.xml"))[0]
    sighting = _sighting(SourceKind.FEED, item_raw(item))

    result = _describe(sighting, _no_request)

    assert result is not None
    assert result.startswith("Remote - Established in 1744, Sotheby")


def test_describe_raw_without_field_gives_none() -> None:
    sighting = _sighting(SourceKind.GREENHOUSE, json.dumps({"id": 1, "title": "x"}))

    assert _describe(sighting, _no_request) is None


def test_describe_covers_every_source_kind() -> None:
    fetched = {SourceKind.WORKABLE, SourceKind.YC, SourceKind.LETSGETRUSTY}
    covered = {kind for kind, _ in _PAYLOAD_CASES} | fetched

    assert covered == set(SourceKind)


_WORKABLE_JOB: dict[str, object] = json.loads(
    _fixture("workable", "huggingface_job.json")
)
_SHORTCODE = str(_WORKABLE_JOB["shortcode"])


def _workable_sighting() -> Sighting:
    return _sighting(
        SourceKind.WORKABLE,
        json.dumps({"shortcode": _SHORTCODE}),
        key="huggingface",
        external_id=_SHORTCODE,
    )


def _markdown(value: object) -> str:
    assert isinstance(value, str)
    text = normalize(value, TextForm.HTML)
    assert text is not None
    return text


def test_describe_workable_fetches_v2_job() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, json=_WORKABLE_JOB), requests)

    result = _describe(_workable_sighting(), handler)

    assert [(r.method, r.url.host, r.url.path) for r in requests] == [
        (
            "GET",
            "apply.workable.com",
            f"/api/v2/accounts/huggingface/jobs/{_SHORTCODE}",
        )
    ]
    assert result == (
        f"{_markdown(_WORKABLE_JOB['description'])}\n\n"
        f"## Requirements\n\n{_markdown(_WORKABLE_JOB['requirements'])}\n\n"
        f"## Benefits\n\n{_markdown(_WORKABLE_JOB['benefits'])}"
    )
    assert result.startswith("At Hugging Face, we're on a journey")
    assert result.index("## Requirements") < result.index("## Benefits")


def test_describe_workable_skips_empty_parts() -> None:
    job = {**_WORKABLE_JOB, "benefits": ""}
    handler = _serving(httpx.Response(200, json=job), [])

    result = _describe(_workable_sighting(), handler)

    assert result is not None
    assert "## Requirements" in result
    assert "## Benefits" not in result


def test_describe_workable_description_only() -> None:
    job = {"shortcode": _SHORTCODE, "description": _WORKABLE_JOB["description"]}
    handler = _serving(httpx.Response(200, json=job), [])

    result = _describe(_workable_sighting(), handler)

    assert result == _markdown(_WORKABLE_JOB["description"])


def test_describe_workable_without_description_raises() -> None:
    job = {k: v for k, v in _WORKABLE_JOB.items() if k != "description"}
    handler = _serving(httpx.Response(200, json=job), [])

    with pytest.raises(ParseError):
        _describe(_workable_sighting(), handler)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"text"'])
def test_describe_workable_non_object_body_raises(body: bytes) -> None:
    handler = _serving(httpx.Response(200, content=body), [])

    with pytest.raises(ParseError):
        _describe(_workable_sighting(), handler)


def test_describe_workable_http_error_propagates() -> None:
    handler = _serving(httpx.Response(404), [])

    with pytest.raises(SourceNotFoundError):
        _describe(_workable_sighting(), handler)


_YC_PATH = "/companies/duffel/jobs/Loght1V-software-engineer-backend"
_YC_PAGE = _text("yc", "job_page.html")


def _yc_sighting() -> Sighting:
    raw = json.dumps({"id": 1, "title": "Software Engineer, Backend", "url": _YC_PATH})
    return _sighting(SourceKind.YC, raw, key="all", external_id="1")


def _expected_jsonld_description(page: str, form: TextForm) -> str:
    description = job_postings(page)[0]["description"]
    text = normalize(description, form)
    assert text is not None
    return text


def test_describe_yc_fetches_job_page() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, text=_YC_PAGE), requests)

    result = _describe(_yc_sighting(), handler)

    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", f"https://www.ycombinator.com{_YC_PATH}")
    ]
    assert result == _expected_jsonld_description(_YC_PAGE, TextForm.DETECT)
    assert result.startswith("### **Create the future of travel with us**")


def test_describe_yc_page_without_jobposting_raises() -> None:
    handler = _serving(httpx.Response(200, text="<html><body>none</body></html>"), [])

    with pytest.raises(ParseError):
        _describe(_yc_sighting(), handler)


def test_describe_yc_posting_without_description_raises() -> None:
    page = (
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "x"}</script>'
    )
    handler = _serving(httpx.Response(200, text=page), [])

    with pytest.raises(ParseError):
        _describe(_yc_sighting(), handler)


def test_describe_yc_robots_disallow_propagates() -> None:
    requests: list[httpx.Request] = []
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /companies/\n")
    handler = _serving(httpx.Response(200, text=_YC_PAGE), requests, robots)

    with pytest.raises(RobotsDisallowedError):
        _describe(_yc_sighting(), handler)

    assert requests == []


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "not json",
        json.dumps({"title": "x"}),
        json.dumps({"url": "@evil.test/x"}),
        json.dumps({"url": "https://evil.test/x"}),
    ],
)
def test_describe_yc_raw_without_site_path_raises(raw: str) -> None:
    sighting = _sighting(SourceKind.YC, raw, key="all", external_id="1")

    with pytest.raises(ParseError):
        _describe(sighting, _no_request)


_LGR_URL = "https://jobs.letsgetrusty.com?job=recKZTPUl4654gtLM"
_LGR_PAGE = _text("letsgetrusty", "job_page.html")


def _lgr_sighting() -> Sighting:
    raw = json.dumps(
        {"@type": "JobPosting", "title": "Solana Program Engineer", "url": _LGR_URL}
    )
    return _sighting(
        SourceKind.LETSGETRUSTY, raw, key="all", external_id="recKZTPUl4654gtLM"
    )


def test_describe_letsgetrusty_fetches_job_page() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, text=_LGR_PAGE), requests)

    result = _describe(_lgr_sighting(), handler)

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "jobs.letsgetrusty.com"
    assert dict(requests[0].url.params) == {"job": "recKZTPUl4654gtLM"}
    assert result == _expected_jsonld_description(_LGR_PAGE, TextForm.MARKDOWN)
    assert "Baton Corporation is the development company" in result
    assert "**About Baton Corporation**" in result


def test_describe_letsgetrusty_page_without_jobposting_raises() -> None:
    handler = _serving(httpx.Response(200, text="<html><body>none</body></html>"), [])

    with pytest.raises(ParseError):
        _describe(_lgr_sighting(), handler)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/?job=x",
        "http://jobs.letsgetrusty.com/?job=x",
        "https://jobs.letsgetrusty.com.evil.test/?job=x",
        "//jobs.letsgetrusty.com/?job=x",
        "https://[bad/?job=x",
    ],
)
def test_describe_letsgetrusty_foreign_url_raises(url: str) -> None:
    raw = json.dumps({"url": url})
    sighting = _sighting(SourceKind.LETSGETRUSTY, raw, key="all", external_id="x")

    with pytest.raises(ParseError):
        _describe(sighting, _no_request)


@pytest.mark.parametrize("raw", ["[]", "not json", json.dumps({"title": "x"})])
def test_describe_letsgetrusty_raw_without_url_raises(raw: str) -> None:
    sighting = _sighting(SourceKind.LETSGETRUSTY, raw, key="all", external_id="x")

    with pytest.raises(ParseError):
        _describe(sighting, _no_request)
