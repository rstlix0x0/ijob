import asyncio
from collections.abc import Callable, Mapping
from datetime import timedelta
from importlib.resources import files

import httpx
import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.model import ExternalId, SourceKind, SourceRef, parse_source_key
from ijob_core.store import SourceOrigin, SourceRow
from ijob_sources.collect import SourceBatch
from ijob_sources.errors import IdentityMismatchError
from ijob_sources.manual import FeedUrl, JobPostingPage
from ijob_sources.polite_http import PoliteClient
from ijob_sources.registry import HOST_INTERVALS, build_source, refresh_interval
from ijob_sources.rss import FEEDS, parse_rss

_MANUAL_KINDS = frozenset({SourceKind.JOBPOSTING, SourceKind.FEED})
_FEED_URL = "https://feed.example.test/rss.xml"
_PAGE_URL = "https://careers.example.test/jobs/release-engineer"
_MANUAL_KEY = "0123456789abcdef"

type _Handler = Callable[[httpx.Request], httpx.Response]


def _fixture(package: str, name: str) -> bytes:
    return (files(f"ijob_sources_tests.fixtures.{package}") / name).read_bytes()


def _row(
    kind: SourceKind,
    key: str,
    *,
    company: str = "Acme",
    target_url: str | None = None,
) -> SourceRow:
    return SourceRow(
        ref=SourceRef(kind, parse_source_key(key)),
        company=company,
        target_url=target_url,
        origin=SourceOrigin.SEED,
        removed=False,
        last_fetched_at=None,
    )


def _row_of_kind(kind: SourceKind) -> SourceRow:
    if kind is SourceKind.RSS:
        return _row(kind, next(iter(FEEDS)))
    if kind in _MANUAL_KINDS:
        return _row(kind, _MANUAL_KEY, target_url=_FEED_URL)
    return _row(kind, "example")


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _fetch(
    row: SourceRow,
    handler: _Handler,
    known: frozenset[ExternalId] = frozenset(),
) -> SourceBatch:
    fake_time = _FakeTime()

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return handler(request)

    async def run() -> SourceBatch:
        async with PoliteClient(
            transport=httpx.MockTransport(serve),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        ) as client:
            return await build_source(row, known=known).fetch(client)

    return asyncio.run(run())


@pytest.mark.parametrize("kind", list(SourceKind))
def test_every_kind_builds(kind: SourceKind) -> None:
    row = _row_of_kind(kind)

    assert build_source(row).ref == row.ref


def test_manual_kinds_build_the_manual_classes() -> None:
    page = build_source(_row(SourceKind.JOBPOSTING, "aa", target_url=_PAGE_URL))
    feed = build_source(_row(SourceKind.FEED, "bb", target_url=_FEED_URL))

    assert isinstance(page, JobPostingPage)
    assert isinstance(feed, FeedUrl)


_HIRING_SYSTEMS = [
    (
        SourceKind.GREENHOUSE,
        "tailscale",
        "greenhouse",
        "tailscale_jobs_pay.json",
        "Tailscale",
    ),
    (SourceKind.GEM, "retool", "gem", "retool_job_posts.json", "Retool"),
    (SourceKind.ASHBY, "ramp", "ashby", "ramp_job_board.json", "Ramp"),
    (SourceKind.LEVER, "spotify", "lever", "spotify_postings.json", "Spotify"),
    (
        SourceKind.WORKABLE,
        "huggingface",
        "workable",
        "huggingface_widget.json",
        "Hugging Face",
    ),
]


@pytest.mark.parametrize(
    ("kind", "key", "package", "name"),
    [system[:4] for system in _HIRING_SYSTEMS],
    ids=[system[0].value for system in _HIRING_SYSTEMS],
)
def test_hiring_system_gets_company(
    kind: SourceKind, key: str, package: str, name: str
) -> None:
    body = _fixture(package, name)
    row = _row(kind, key, company="Nobody In The Board")

    with pytest.raises(IdentityMismatchError) as caught:
        _fetch(row, lambda _request: httpx.Response(200, content=body))

    assert caught.value.ref == row.ref


@pytest.mark.parametrize(
    ("kind", "key", "package", "name", "own"),
    _HIRING_SYSTEMS,
    ids=[kind.value for kind, *_ in _HIRING_SYSTEMS],
)
def test_hiring_system_accepts_its_own_company(
    kind: SourceKind, key: str, package: str, name: str, own: str
) -> None:
    body = _fixture(package, name)
    row = _row(kind, key, company=own)

    batch = _fetch(row, lambda _request: httpx.Response(200, content=body))

    assert batch.drafts
    assert {draft.company for draft in batch.drafts} <= {own}


def test_unknown_rss_key_rejected() -> None:
    with pytest.raises(InvalidValueError):
        build_source(_row(SourceKind.RSS, "no-such-feed"))


def test_exempt_sources_skip_identity() -> None:
    def himalayas(request: httpx.Request) -> httpx.Response:
        page = "page2.json" if "cursor" in request.url.params else "page1.json"
        return httpx.Response(200, content=_fixture("himalayas", page))

    board = _fetch(_row(SourceKind.HIMALAYAS, "all", company="Nobody"), himalayas)
    feed = _fetch(
        _row(
            SourceKind.FEED,
            _MANUAL_KEY,
            company="Nobody In The Feed",
            target_url=_FEED_URL,
        ),
        lambda _request: httpx.Response(200, content=_fixture("rss", "golang.xml")),
    )

    assert board.drafts
    assert feed.drafts


@pytest.mark.parametrize("kind", sorted(_MANUAL_KINDS))
def test_manual_kind_needs_target_url(kind: SourceKind) -> None:
    with pytest.raises(InvalidValueError):
        build_source(_row(kind, _MANUAL_KEY))


def test_refresh_interval() -> None:
    assert refresh_interval(SourceKind.JOBICY) == timedelta(hours=1)
    assert refresh_interval(SourceKind.GREENHOUSE) == timedelta(0)


@pytest.mark.parametrize("kind", [k for k in SourceKind if k is not SourceKind.JOBICY])
def test_every_other_kind_has_no_refresh_interval(kind: SourceKind) -> None:
    assert refresh_interval(kind) == timedelta(0)


def test_workable_host_is_paced_at_three_seconds() -> None:
    assert HOST_INTERVALS == {"apply.workable.com": 3.0}


def test_known_ids_reach_a_detail_feed() -> None:
    row = _row(SourceKind.RSS, "elixir")
    feed = _fixture("rss", "elixir.xml")
    link = parse_rss(feed)[0].link
    assert link is not None
    requested: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=feed if "rss" in request.url.path else b"")

    batch = _fetch(row, serve, known=frozenset({ExternalId(link)}))

    assert link not in requested
    assert ExternalId(link) in batch.still_listed


def test_known_ids_reach_japandev() -> None:
    sitemap = _fixture("japandev", "sitemap.xml")
    row = _row(SourceKind.JAPANDEV, "all")
    known_id = "abbeal-ai-engineer-maj4uw"
    requested: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("sitemap.xml"):
            return httpx.Response(200, content=sitemap)
        return httpx.Response(404)

    batch = _fetch(row, serve, known=frozenset({ExternalId(known_id)}))

    assert batch.still_listed == (ExternalId(known_id),)
    assert f"/jobs/abbeal/{known_id}" not in requested


def test_host_intervals_cannot_be_changed() -> None:
    assert isinstance(HOST_INTERVALS, Mapping)
    assert not isinstance(HOST_INTERVALS, dict)
