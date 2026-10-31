import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest
from ijob_core.countries import CountryCode
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    SourceKey,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError
from ijob_sources.polite_http import CachedResponse, ConditionalCache, PoliteClient
from ijob_sources.rss import FEEDS, RssFeed, RssItem, item_link, item_raw, parse_rss

_FIXTURES = Path(__file__).parent / "fixtures" / "rss"


def _load(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


_WWR = _load("wwr_programming.xml")
_PYTHON = _load("python.xml")
_GOLANG = _load("golang.xml")
_ELIXIR = _load("elixir.xml")
_DJINNI = _load("djinni_python.xml")
_BSJ = _load("bsj.xml")
_ELIXIR_OFFER = _load("elixir_offer.html")
_DJINNI_JOB = _load("djinni_job.html")

_TREAT = "https://elixirjobs.net/offers/treat-health-full-stack-engineer-elixir-liveview-react-35604f82"
_DRIFTROCK = "https://elixirjobs.net/offers/driftrock-elixir-software-engineer-289bae36"
_ACTIVE_PRIME = (
    "SENIOR Back-End Python Engineer - FULLY REMOTE-WORLDWIDE, ActivePrime, Inc."
)
_DJINNI_FIRST = "https://djinni.co/jobs/856062-senior-ai-platform-engineer/"


def _key(text: str) -> SourceKey:
    return parse_source_key(text)


def _item_xml(
    title: str | None = "Acme: Engineer",
    link: str | None = "https://example.test/jobs/1",
    description: str = "",
    extra: str = "",
) -> str:
    parts = []
    if title is not None:
        parts.append(f"<title>{escape(title)}</title>")
    if link is not None:
        parts.append(f"<link>{escape(link)}</link>")
    parts.append(f"<description>{escape(description)}</description>")
    parts.append(extra)
    return f"<item>{''.join(parts)}</item>"


def _feed_xml(*items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>t</title>{''.join(items)}</channel></rss>"
    ).encode()


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _DictCache:
    """A `ConditionalCache` that keeps responses in a dict and logs lookups."""

    def __init__(self, entries: dict[str, CachedResponse] | None = None) -> None:
        self.entries = entries or {}
        self.looked_up: list[str] = []

    def lookup(self, url: str) -> CachedResponse | None:
        self.looked_up.append(url)
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


def _serving(
    pages: Mapping[str, bytes | int],
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each URL with its body, or with its status when given an int."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if requests is not None:
            requests.append(request)
        page = pages.get(str(request.url))
        if page is None:
            return httpx.Response(404)
        if isinstance(page, int):
            return httpx.Response(page)
        return httpx.Response(200, content=page)

    return handler


def _fetch(
    feed: RssFeed,
    handler: Callable[[httpx.Request], httpx.Response],
    cache: ConditionalCache | None = None,
) -> SourceBatch:
    fake_time = _FakeTime()

    async def run() -> SourceBatch:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
            cache=cache,
        )
        async with client:
            return await feed.fetch(client)

    return asyncio.run(run())


def _fetch_one_feed(feed_key: str, body: bytes) -> SourceBatch:
    key = _key(feed_key)
    return _fetch(RssFeed(key), _serving({FEEDS[key].url: body}))


# --- parse_rss -------------------------------------------------------------


def test_parse_rss_reads_item_fields() -> None:
    first = parse_rss(_WWR)[0]

    assert first.title == "Toptal: Apigee Engineer for Global Consumer Food Company"
    assert first.link == (
        "https://weworkremotely.com/remote-jobs/"
        "toptal-apigee-engineer-for-global-consumer-food-company"
    )
    assert first.pub_date == datetime(2026, 10, 12, 16, 28, 34, tzinfo=UTC)
    assert first.description.startswith("<p>")
    assert first.extras["region"] == "Anywhere in the World"
    assert "title" not in first.extras
    assert "link" not in first.extras
    assert "pubDate" not in first.extras
    assert "description" not in first.extras


def test_parse_rss_returns_every_item() -> None:
    assert len(parse_rss(_WWR)) == 3
    assert len(parse_rss(_BSJ)) == 3
    assert len(parse_rss(_DJINNI)) == 3


def test_parse_rss_trims_cdata_text() -> None:
    item = parse_rss(_ELIXIR)[1]

    assert item.title == "Elixir Software Engineer"
    assert item.link == _DRIFTROCK


def test_namespaced_children_keep_their_namespace() -> None:
    body = _feed_xml(
        '<item xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<title>t</title><link>https://example.test/1</link>"
        "<dc:creator>someone</dc:creator></item>"
    )

    assert parse_rss(body)[0].extras == {
        "{http://purl.org/dc/elements/1.1/}creator": "someone"
    }


def test_first_of_a_repeated_child_wins() -> None:
    item = parse_rss(_BSJ)[0]

    assert item.extras["category"] == "IT / Software Development"


def test_description_entities_are_unescaped() -> None:
    body = _feed_xml(
        "<item><title>t</title><link>https://example.test/1</link>"
        "<description>Caf&amp;eacute; &amp;amp; &amp;#x2F; bar&amp;nbsp;"
        "</description></item>"
    )

    assert parse_rss(body)[0].description == "Café & / bar"


def test_missing_title_link_and_description() -> None:
    item = parse_rss(_feed_xml("<item><guid>x</guid></item>"))[0]

    assert item == RssItem(None, None, None, "", {"guid": "x"})


@pytest.mark.parametrize("body", [_PYTHON, _GOLANG])
def test_missing_pubdate_is_none(body: bytes) -> None:
    items = parse_rss(body)

    assert items
    assert all(item.pub_date is None for item in items)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15 Oct 2026 14:21:22 +0000", datetime(2026, 10, 15, 14, 21, 22, tzinfo=UTC)),
        (
            "Fri, 30 Oct 2026 18:37:12 +0200",
            datetime(2026, 10, 30, 18, 37, 12, tzinfo=timezone(timedelta(hours=2))),
        ),
        (
            "Fri, 30 Oct 2026 18:37:12 -0000",
            datetime(2026, 10, 30, 18, 37, 12, tzinfo=UTC),
        ),
    ],
    ids=["no-weekday", "weekday-offset", "unknown-zone-is-utc"],
)
def test_pubdate_shapes(text: str, expected: datetime) -> None:
    body = _feed_xml(
        f"<item><title>t</title><link>https://example.test/1</link>"
        f"<pubDate>{text}</pubDate></item>"
    )

    pub_date = parse_rss(body)[0].pub_date

    assert pub_date == expected
    assert pub_date is not None
    assert pub_date.utcoffset() == expected.utcoffset()


@pytest.mark.parametrize(
    "text",
    ["", "garbage", "Mon, 32 Oct 2026 10:00:00 +0000", "2026-10-15T14:21:22Z"],
)
def test_unreadable_pubdate_is_none(text: str) -> None:
    body = _feed_xml(
        f"<item><title>t</title><link>https://example.test/1</link>"
        f"<pubDate>{text}</pubDate></item>"
    )

    assert parse_rss(body)[0].pub_date is None


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not xml at all",
        b"<rss><channel><item></rss>",
        b"[1, 2]",
    ],
    ids=["empty", "text", "unclosed", "json"],
)
def test_body_that_is_not_xml_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_rss(body)


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><rss version="2.0"></rss>',
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
        b"<html><body><channel/></body></html>",
    ],
    ids=["no-channel", "atom", "wrong-root"],
)
def test_body_without_a_channel_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError, match="channel"):
        parse_rss(body)


def test_channel_with_no_items_is_empty() -> None:
    assert parse_rss(_feed_xml()) == []


def _bomb() -> bytes:
    """Seven entities: a 10-character base expanded tenfold six times (10**7)."""
    declarations = ['<!ENTITY a "0123456789">']
    for name, previous in zip("bcdefg", "abcdef", strict=True):
        declarations.append(f'<!ENTITY {name} "{f"&{previous};" * 10}">')
    return (
        f'<?xml version="1.0"?><!DOCTYPE rss [{"".join(declarations)}]>'
        "<rss><channel><item><title>&g;</title></item></channel></rss>"
    ).encode()


def test_entity_bomb_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_rss(_bomb())


def test_entity_declaration_is_parse_error() -> None:
    body = (
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "b">]>'
        b"<rss><channel><item><title>&a;</title></item></channel></rss>"
    )

    with pytest.raises(ParseError, match="entit"):
        parse_rss(body)


def test_unknown_declared_encoding_is_parse_error() -> None:
    body = (
        b'<?xml version="1.0" encoding="x-nope"?><rss version="2.0"><channel>'
        b"</channel></rss>"
    )

    with pytest.raises(ParseError):
        parse_rss(body)


def test_body_in_a_wide_encoding_is_parse_error() -> None:
    body = _feed_xml(_item_xml()).decode().encode("utf-16")

    with pytest.raises(ParseError):
        parse_rss(body)


# --- feed table ------------------------------------------------------------


def test_feed_urls_are_those_of_the_spec() -> None:
    assert {str(key): spec.url for key, spec in FEEDS.items()} == {
        "wwr-programming": "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "wwr-backend": "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        "python": "https://www.python.org/jobs/feed/rss/",
        "golang": "https://www.golangprojects.com/rss.xml",
        "elixir": "https://elixirjobs.net/rss",
        "djinni-python": "https://djinni.co/jobs/rss/?primary_keyword=Python",
        "bsj": "https://berlinstartupjobs.com/engineering/feed/",
    }


def test_only_elixir_and_djinni_need_a_detail_page() -> None:
    assert {str(key) for key, spec in FEEDS.items() if spec.detail} == {
        "elixir",
        "djinni-python",
    }


def test_unknown_feed_key_is_invalid() -> None:
    with pytest.raises(InvalidValueError):
        RssFeed(_key("no-such-feed"))


def test_ref_names_the_feed() -> None:
    assert RssFeed(_key("golang")).ref == SourceRef(SourceKind.RSS, _key("golang"))
    assert str(RssFeed(_key("djinni-python")).ref) == "rss:djinni-python"


# --- plain feeds -----------------------------------------------------------


def test_wwr_fixture_maps_first_item() -> None:
    batch = _fetch_one_feed("wwr-programming", _WWR)

    draft = batch.drafts[0]
    assert (batch.rejected, len(batch.drafts), batch.still_listed) == (0, 3, ())
    assert draft.source == SourceRef(SourceKind.RSS, _key("wwr-programming"))
    assert draft.title == "Apigee Engineer for Global Consumer Food Company"
    assert draft.company == "Toptal"
    assert draft.location == "Anywhere in the World"
    assert draft.external_id == ExternalId(draft.url)
    assert draft.url.startswith("https://weworkremotely.com/remote-jobs/toptal-apigee")
    assert draft.posted_at == datetime(2026, 10, 12, 16, 28, 34, tzinfo=UTC)
    assert (draft.country, draft.remote, draft.salaries) == (None, None, ())


def test_raw_is_the_items_fields_as_json() -> None:
    draft = _fetch_one_feed("wwr-programming", _WWR).drafts[0]

    raw = json.loads(draft.raw)
    assert raw["title"] == "Toptal: Apigee Engineer for Global Consumer Food Company"
    assert raw["link"] == draft.url
    assert raw["pub_date"] == "2026-10-12T16:28:34+00:00"
    assert raw["extras"]["region"] == "Anywhere in the World"
    assert "Trimmed description." in raw["description"]


@pytest.mark.parametrize(
    ("feed_key", "body"),
    [
        ("wwr-programming", _WWR),
        ("wwr-backend", _load("wwr_backend.xml")),
        ("python", _PYTHON),
        ("golang", _GOLANG),
        ("bsj", _BSJ),
    ],
)
def test_every_plain_fixture_is_fully_drafted(feed_key: str, body: bytes) -> None:
    batch = _fetch_one_feed(feed_key, body)

    assert batch.rejected == 0
    assert len(batch.drafts) == len(parse_rss(body))


@pytest.mark.parametrize(
    ("feed_key", "title", "expected_title", "expected_company"),
    [
        (
            "wwr-programming",
            "Yooli: FULL TIME: Software Engineer Position - React and Rest",
            "FULL TIME: Software Engineer Position - React and Rest",
            "Yooli",
        ),
        (
            "python",
            _ACTIVE_PRIME,
            _ACTIVE_PRIME.removesuffix(", Inc."),
            "Inc.",
        ),
        (
            "golang",
            "Senior Software Engineer\xa0@\xa0Sotheby\u2019s",
            "Senior Software Engineer",
            "Sotheby\u2019s",
        ),
        (
            "bsj",
            "Senior Backend Developer \u2013 Go & Kubernetes // DATATRONiQ",
            "Senior Backend Developer \u2013 Go & Kubernetes",
            "DATATRONiQ",
        ),
    ],
    ids=[
        "wwr-first-colon",
        "python-lossy-last-comma",
        "golang-nbsp",
        "bsj-last-slashes",
    ],
)
def test_title_rules(
    feed_key: str, title: str, expected_title: str, expected_company: str
) -> None:
    batch = _fetch_one_feed(feed_key, _feed_xml(_item_xml(title=title)))

    assert batch.rejected == 0
    assert (batch.drafts[0].title, batch.drafts[0].company) == (
        expected_title,
        expected_company,
    )


def test_title_rules_are_applied_to_the_recorded_titles() -> None:
    assert _fetch_one_feed("wwr-programming", _WWR).drafts[2].company == "Yooli"
    assert _fetch_one_feed("python", _PYTHON).drafts[2].company == "Inc."
    assert _fetch_one_feed("golang", _GOLANG).drafts[0].company == "Sotheby\u2019s"


@pytest.mark.parametrize(
    ("feed_key", "title"),
    [
        ("wwr-programming", "No separator here"),
        ("wwr-backend", "Company:NoSpace"),
        ("python", "NoCommaHere"),
        ("golang", "Plain title with no at sign"),
        ("golang", "Senior Engineer @Acme"),
        ("bsj", "Title with / one slash"),
        ("wwr-programming", ": Role without company"),
        ("wwr-programming", "Company: "),
        ("python", "Role, "),
    ],
)
def test_title_without_its_separator_is_rejected(
    feed_key: str, title: str, caplog: pytest.LogCaptureFixture
) -> None:
    bad = _item_xml(title=title, link="https://example.test/jobs/bad")
    caplog.set_level(logging.WARNING)

    batch = _fetch_one_feed(feed_key, _feed_xml(bad))

    assert batch.rejected == 1
    assert batch.drafts == []
    assert "https://example.test/jobs/bad" in caplog.text


def test_a_rejected_item_does_not_stop_the_others() -> None:
    body = _feed_xml(
        _item_xml(title="No separator", link="https://example.test/jobs/bad"),
        _item_xml(title="Acme: Engineer", link="https://example.test/jobs/good"),
    )

    batch = _fetch_one_feed("wwr-programming", body)

    assert batch.rejected == 1
    assert [draft.url for draft in batch.drafts] == ["https://example.test/jobs/good"]


def test_item_without_title_or_link_is_rejected() -> None:
    body = _feed_xml(
        _item_xml(title=None),
        _item_xml(link=None),
        _item_xml(link="ftp://example.test/jobs/1"),
        _item_xml(link="https://example.test/jobs/ok"),
    )

    batch = _fetch_one_feed("wwr-programming", body)

    assert batch.rejected == 3
    assert [draft.url for draft in batch.drafts] == ["https://example.test/jobs/ok"]


@pytest.mark.parametrize(
    ("feed_key", "item", "expected"),
    [
        (
            "wwr-programming",
            _item_xml(extra="<region>Europe Only</region>"),
            "Europe Only",
        ),
        ("wwr-programming", _item_xml(), None),
        ("wwr-programming", _item_xml(extra="<region> </region>"), None),
        (
            "python",
            _item_xml(title="Role, Co", description="London, UK\n<p>x</p>"),
            "London, UK",
        ),
        ("python", _item_xml(title="Role, Co", description="Paris <p>x</p>"), "Paris"),
        ("python", _item_xml(title="Role, Co", description="<p>x</p>"), None),
        (
            "golang",
            _item_xml(title="Role @ Co", description="Remote - Established in 1744"),
            "Remote",
        ),
        (
            "golang",
            _item_xml(title="Role @ Co", description="Remote Europe - A - B"),
            "Remote Europe",
        ),
        (
            "golang",
            _item_xml(title="Role @ Co", description="No separator at all"),
            None,
        ),
        ("bsj", _item_xml(title="Role // Co", description="Berlin - x"), None),
    ],
    ids=[
        "wwr-region",
        "wwr-none",
        "wwr-blank",
        "python-first-line",
        "python-same-line",
        "python-starts-with-tag",
        "golang-before-dash",
        "golang-first-dash",
        "golang-no-dash",
        "bsj-never",
    ],
)
def test_location_rules(feed_key: str, item: str, expected: str | None) -> None:
    batch = _fetch_one_feed(feed_key, _feed_xml(item))

    assert batch.rejected == 0
    assert batch.drafts[0].location == expected


def test_recorded_locations() -> None:
    assert _fetch_one_feed("python", _PYTHON).drafts[0].location == "London, UK"
    assert _fetch_one_feed("golang", _GOLANG).drafts[0].location == "Remote"


def test_plain_feed_ignores_known() -> None:
    key = _key("wwr-programming")
    known = frozenset(ExternalId(i.link or "") for i in parse_rss(_WWR))

    batch = _fetch(RssFeed(key, known=known), _serving({FEEDS[key].url: _WWR}))

    assert len(batch.drafts) == 3
    assert batch.still_listed == ()


def test_feed_is_one_conditional_get() -> None:
    key = _key("golang")
    url = FEEDS[key].url
    requests: list[httpx.Request] = []
    cache = _DictCache({url: CachedResponse('"v1"', None, _GOLANG)})

    batch = _fetch(RssFeed(key), _serving({url: 304}, requests), cache)

    assert [str(request.url) for request in requests] == [url]
    assert requests[0].headers["if-none-match"] == '"v1"'
    assert len(batch.drafts) == 3


def test_feed_that_is_not_xml_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        _fetch_one_feed("python", b"<html>nope</html>")


def test_feed_satisfies_source_protocol() -> None:
    key = _key("bsj")
    feed = RssFeed(key)
    fake_time = _FakeTime()
    handler = _serving({FEEDS[key].url: _BSJ})

    async def run() -> CollectResult:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
        async with client:
            return await collect([feed], client)

    result = asyncio.run(run())

    assert result.failures == []
    assert len(result.batches[feed.ref].drafts) == 3


# --- detail feeds ----------------------------------------------------------


def test_detail_feed_fetches_new_only() -> None:
    key = _key("elixir")
    requests: list[httpx.Request] = []
    handler = _serving(
        {FEEDS[key].url: _ELIXIR, _TREAT: _ELIXIR_OFFER, _DRIFTROCK: 500}, requests
    )

    batch = _fetch(RssFeed(key, known=frozenset({ExternalId(_DRIFTROCK)})), handler)

    assert [str(request.url) for request in requests] == [FEEDS[key].url, _TREAT]
    assert batch.still_listed == (ExternalId(_DRIFTROCK),)
    assert batch.rejected == 0
    (draft,) = batch.drafts
    assert draft.company == "Treat Health"
    assert draft.title == "Full-stack Engineer (Elixir, LiveView, React)"
    assert draft.location == "Helsinki, Finland"
    assert draft.country == CountryCode("FI")
    assert draft.external_id == ExternalId(_TREAT)
    assert draft.url == _TREAT
    assert draft.posted_at == datetime(2026, 10, 15, 14, 21, 22, tzinfo=UTC)
    assert draft.source == SourceRef(SourceKind.RSS, key)


def test_detail_draft_raw_holds_item_and_jobposting() -> None:
    key = _key("elixir")
    handler = _serving(
        {FEEDS[key].url: _ELIXIR, _TREAT: _ELIXIR_OFFER, _DRIFTROCK: 500}
    )

    batch = _fetch(RssFeed(key, known=frozenset({ExternalId(_DRIFTROCK)})), handler)

    raw = json.loads(batch.drafts[0].raw)
    assert set(raw) == {"item", "jobposting"}
    assert raw["item"]["link"] == _TREAT
    assert raw["jobposting"]["@type"] == "JobPosting"
    assert raw["jobposting"]["hiringOrganization"]["name"] == "Treat Health"


def test_djinni_detail_keeps_the_items_pubdate() -> None:
    key = _key("djinni-python")
    pages: dict[str, bytes | int] = {
        FEEDS[key].url: _DJINNI,
        _DJINNI_FIRST: _DJINNI_JOB,
    }
    known = frozenset(ExternalId(item.link or "") for item in parse_rss(_DJINNI)[1:])

    batch = _fetch(RssFeed(key, known=known), _serving(pages))

    (draft,) = batch.drafts
    assert draft.company == "JustMarkets Tech"
    assert draft.title == "Senior AI Platform Engineer"
    assert draft.posted_at == datetime(
        2026, 10, 30, 18, 37, 12, tzinfo=timezone(timedelta(hours=2))
    )
    assert draft.remote is None
    assert draft.country is None
    assert len(batch.still_listed) == 2


def test_detail_title_falls_back_to_the_item() -> None:
    key = _key("elixir")
    page = (
        b'<script type="application/ld+json">{"@type":"JobPosting",'
        b'"hiringOrganization":{"name":"Acme"}}</script>'
    )
    link = "https://elixirjobs.net/offers/x"
    handler = _serving(
        {FEEDS[key].url: _feed_xml(_item_xml("Item Title", link)), link: page}
    )

    batch = _fetch(RssFeed(key), handler)

    assert batch.drafts[0].title == "Item Title"
    assert batch.drafts[0].company == "Acme"
    assert batch.drafts[0].location is None


def _numbered_feed(count: int, start: int = 0) -> tuple[bytes, list[str]]:
    links = [
        f"https://elixirjobs.net/offers/job-{n}" for n in range(start, start + count)
    ]
    return _feed_xml(
        *(_item_xml(f"Job {n}", link) for n, link in enumerate(links))
    ), links


def test_detail_cap_100() -> None:
    key = _key("elixir")
    body, links = _numbered_feed(120)
    pages: dict[str, bytes | int] = dict.fromkeys(links, _ELIXIR_OFFER)
    pages[FEEDS[key].url] = body
    requests: list[httpx.Request] = []

    batch = _fetch(RssFeed(key), _serving(pages, requests))

    assert len(requests) == 101
    assert [str(request.url) for request in requests[1:]] == links[:100]
    assert len(batch.drafts) == 100
    assert batch.rejected == 0
    assert batch.still_listed == ()


def test_known_items_do_not_count_against_the_cap() -> None:
    key = _key("elixir")
    body, links = _numbered_feed(120)
    pages: dict[str, bytes | int] = dict.fromkeys(links, _ELIXIR_OFFER)
    pages[FEEDS[key].url] = body
    known = frozenset(ExternalId(link) for link in links[:5])
    requests: list[httpx.Request] = []

    batch = _fetch(RssFeed(key, known=known), _serving(pages, requests))

    assert len(requests) == 101
    assert batch.still_listed == tuple(ExternalId(link) for link in links[:5])
    assert len(batch.drafts) == 100


def test_failed_detail_rejects_only_its_item(caplog: pytest.LogCaptureFixture) -> None:
    key = _key("elixir")
    body, links = _numbered_feed(3)
    pages: dict[str, bytes | int] = {
        FEEDS[key].url: body,
        links[0]: _ELIXIR_OFFER,
        links[1]: 410,
        links[2]: _ELIXIR_OFFER,
    }
    caplog.set_level(logging.WARNING)

    batch = _fetch(RssFeed(key), _serving(pages))

    assert batch.rejected == 1
    assert [draft.url for draft in batch.drafts] == [links[0], links[2]]
    assert links[1] in caplog.text
    assert "410" in caplog.text


@pytest.mark.parametrize(
    "page",
    [
        b"<html><body>no structured data</body></html>",
        (
            b'<script type="application/ld+json">'
            b'{"@type":"JobPosting","title":"T"}</script>'
        ),
        (
            b'<script type="application/ld+json">'
            b'{"@type":"JobPosting","datePosted":"soon",'
            b'"hiringOrganization":{"name":"Acme"}}</script>'
        ),
    ],
    ids=["no-jobposting", "no-company", "unreadable-field"],
)
def test_unusable_detail_page_rejects_its_item(page: bytes) -> None:
    key = _key("elixir")
    link = "https://elixirjobs.net/offers/x"
    handler = _serving({FEEDS[key].url: _feed_xml(_item_xml("Job", link)), link: page})

    batch = _fetch(RssFeed(key), handler)

    assert batch.rejected == 1
    assert batch.drafts == []


def test_detail_item_without_a_usable_link_is_rejected_without_a_request() -> None:
    key = _key("elixir")
    requests: list[httpx.Request] = []
    body = _feed_xml(_item_xml(link=None), _item_xml(link="file:///etc/passwd"))

    batch = _fetch(RssFeed(key), _serving({FEEDS[key].url: body}, requests))

    assert batch.rejected == 2
    assert [str(request.url) for request in requests] == [FEEDS[key].url]


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/offers/1",
        "http://elixirjobs.net/offers/1",
        "https://localhost/offers/1",
        "https://169.254.169.254/latest/meta-data/",
        "https://elixirjobs.net.evil.example/offers/1",
        "https://user@evil.example/offers/1",
        "https://www.elixirjobs.net/offers/1",
    ],
    ids=[
        "foreign-host",
        "plain-http",
        "localhost",
        "metadata-address",
        "suffix-host",
        "userinfo-host",
        "subdomain",
    ],
)
def test_detail_link_off_the_feed_host_is_rejected_without_a_request(
    link: str,
) -> None:
    key = _key("elixir")
    requests: list[httpx.Request] = []
    on_host = "https://elixirjobs.net/offers/ok"
    body = _feed_xml(_item_xml("Foreign", link), _item_xml("Local", on_host))
    pages: dict[str, bytes | int] = {FEEDS[key].url: body, on_host: _ELIXIR_OFFER}

    batch = _fetch(RssFeed(key), _serving(pages, requests))

    assert [str(request.url) for request in requests] == [FEEDS[key].url, on_host]
    assert batch.rejected == 1
    assert [draft.url for draft in batch.drafts] == [on_host]


def test_detail_link_off_the_feed_host_is_not_still_listed() -> None:
    key = _key("elixir")
    link = "https://evil.example/offers/1"
    body = _feed_xml(_item_xml("Foreign", link))

    batch = _fetch(
        RssFeed(key, known=frozenset({ExternalId(link)})),
        _serving({FEEDS[key].url: body}),
    )

    assert (batch.rejected, batch.still_listed) == (1, ())


def test_only_the_feed_is_fetched_conditionally() -> None:
    key = _key("elixir")
    pages: dict[str, bytes | int] = {
        FEEDS[key].url: _ELIXIR,
        _TREAT: _ELIXIR_OFFER,
        _DRIFTROCK: _ELIXIR_OFFER,
    }
    cache = _DictCache()

    _fetch(RssFeed(key), _serving(pages), cache)

    assert cache.looked_up == [FEEDS[key].url]


def test_item_raw_holds_the_fields_and_the_extras() -> None:
    body = _feed_xml(
        _item_xml(
            description="About the job",
            extra=(
                "<category>go</category>"
                "<pubDate>Tue, 06 Oct 2026 10:00:00 +0000</pubDate>"
            ),
        )
    )

    raw = json.loads(item_raw(parse_rss(body)[0]))

    assert raw == {
        "title": "Acme: Engineer",
        "link": "https://example.test/jobs/1",
        "pub_date": "2026-10-06T10:00:00+00:00",
        "description": "About the job",
        "extras": {"category": "go"},
    }


def test_item_link_returns_an_http_link() -> None:
    item = parse_rss(_feed_xml(_item_xml(link="http://example.test/a")))[0]

    assert item_link(item) == "http://example.test/a"


@pytest.mark.parametrize("link", [None, "ftp://example.test/a", "/relative"])
def test_item_link_refuses_a_missing_or_non_http_link(link: str | None) -> None:
    item = parse_rss(_feed_xml(_item_xml(link=link)))[0]

    with pytest.raises(InvalidValueError):
        item_link(item)
