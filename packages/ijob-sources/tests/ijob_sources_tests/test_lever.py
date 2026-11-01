import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

import httpx
import pytest
from ijob_core.countries import CountryCode
from ijob_core.model import (
    ExternalId,
    RemoteScope,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import IdentityMismatchError, ParseError, SourceNotFoundError
from ijob_sources.lever import LeverSite, description_from_raw, parse_lever
from ijob_sources.polite_http import CachedResponse, PoliteClient

_KEY = parse_source_key("spotify")
_COMPANY = "Spotify"
_BODY = (
    files("ijob_sources_tests.fixtures.lever") / "spotify_postings.json"
).read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_BODY)
_FIRST, _SECOND, _ONSITE = _JOBS
_URL = "https://api.lever.co/v0/postings/spotify?mode=json"


def _body(*jobs: object) -> bytes:
    return json.dumps(list(jobs)).encode()


def _without(job: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: value for name, value in job.items() if name not in names}


def test_parse_lever_maps_fields() -> None:
    batch = parse_lever(_BODY, _KEY, _COMPANY)
    draft = batch.drafts[0]

    assert len(batch.drafts) == 3
    assert batch.rejected == 0
    assert draft.source == SourceRef(SourceKind.LEVER, _KEY)
    assert draft.external_id == ExternalId("2a55d6c9-c564-443a-be3e-a3851db5a93b")
    assert draft.title == "Account Executive - Commercial, Spotify Technology"
    assert draft.company == "Spotify"
    assert draft.location == "New York, NY"
    assert (
        draft.url
        == "https://jobs.lever.co/spotify/2a55d6c9-c564-443a-be3e-a3851db5a93b"
    )
    assert draft.country == CountryCode("US")
    assert draft.remote is RemoteScope.NONE
    assert draft.posted_at == datetime(2026, 10, 23, 15, 0, 40, 768000, tzinfo=UTC)
    assert draft.role_hint == "Operations and Business Support"
    assert json.loads(draft.raw) == _FIRST


def test_empty_site_is_empty_batch() -> None:
    batch = parse_lever(b"[]", _KEY, _COMPANY)

    assert batch.drafts == []
    assert batch.rejected == 0


def test_remote_workplace_is_unknown_scope() -> None:
    job = {**_FIRST, "workplaceType": "remote"}

    assert parse_lever(_body(job), _KEY, _COMPANY).drafts[0].remote is None


@pytest.mark.parametrize("workplace", ["onsite", "hybrid"])
def test_onsite_and_hybrid_are_not_remote(workplace: str) -> None:
    job = {**_FIRST, "workplaceType": workplace}

    assert parse_lever(_body(job), _KEY, _COMPANY).drafts[0].remote is RemoteScope.NONE


@pytest.mark.parametrize("workplace", [None, "unspecified", 3])
def test_unrecognised_workplace_is_unknown_scope(workplace: object) -> None:
    job = {**_FIRST, "workplaceType": workplace}

    assert parse_lever(_body(job), _KEY, _COMPANY).drafts[0].remote is None


def test_fixture_hybrid_and_onsite_jobs_are_not_remote() -> None:
    drafts = parse_lever(_BODY, _KEY, _COMPANY).drafts

    assert [draft.remote for draft in drafts] == [RemoteScope.NONE] * 3
    assert drafts[2].country == CountryCode("FR")


def test_absent_optional_fields_are_none() -> None:
    job = _without(_ONSITE, "categories", "country", "createdAt")

    draft = parse_lever(_body(job), _KEY, _COMPANY).drafts[0]

    assert draft.location is None
    assert draft.role_hint is None
    assert draft.country is None
    assert draft.posted_at is None


def test_unknown_country_is_none() -> None:
    job = {**_ONSITE, "country": "ZZ"}

    assert parse_lever(_body(job), _KEY, _COMPANY).drafts[0].country is None


@pytest.mark.parametrize("department", [None, ""])
def test_empty_department_is_no_hint(department: str | None) -> None:
    job = {**_ONSITE, "categories": {"location": "Paris", "department": department}}

    assert parse_lever(_body(job), _KEY, _COMPANY).drafts[0].role_hint is None


def test_created_at_is_exact_to_the_millisecond() -> None:
    job = {**_ONSITE, "createdAt": 1}

    draft = parse_lever(_body(job), _KEY, _COMPANY).drafts[0]

    assert draft.posted_at == datetime(1970, 1, 1, 0, 0, 0, 1000, tzinfo=UTC)


@pytest.mark.parametrize(
    ("bad_job", "label"),
    [
        pytest.param(
            {**_SECOND, "text": ""}, f"id {_SECOND['id']!r}", id="empty-title"
        ),
        pytest.param(_without(_SECOND, "text"), f"id {_SECOND['id']!r}", id="no-title"),
        pytest.param(_without(_SECOND, "id"), "index 1", id="missing-id"),
        pytest.param({**_SECOND, "id": 7}, "index 1", id="non-text-id"),
        pytest.param(
            _without(_SECOND, "hostedUrl"), f"id {_SECOND['id']!r}", id="no-url"
        ),
        pytest.param(
            {**_SECOND, "hostedUrl": "ftp://example.test/1"},
            f"id {_SECOND['id']!r}",
            id="non-http-url",
        ),
        pytest.param("not a job", "index 1", id="string-job"),
        pytest.param(
            {**_SECOND, "text": "\ud800"}, f"id {_SECOND['id']!r}", id="surrogate"
        ),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    bad_job: object, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ijob_sources.lever"):
        batch = parse_lever(_body(_FIRST, bad_job), _KEY, _COMPANY)

    assert [draft.external_id for draft in batch.drafts] == [_FIRST["id"]]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()
    assert "invalid" in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"categories": "Paris"}, "location", id="categories"),
        pytest.param({"categories": {"location": 5}}, "location", id="location"),
        pytest.param(
            {"categories": {"location": "\ud800"}}, "location", id="surrogate-location"
        ),
        pytest.param({"categories": {"department": 5}}, "role_hint", id="department"),
        pytest.param(
            {"categories": {"department": "\ud800"}},
            "role_hint",
            id="surrogate-department",
        ),
        pytest.param({"createdAt": "now"}, "posted_at", id="created"),
        pytest.param({"createdAt": True}, "posted_at", id="bool-created"),
        pytest.param({"createdAt": 10**30}, "posted_at", id="huge-created"),
        pytest.param({"createdAt": 1.5}, "posted_at", id="float-created"),
        pytest.param({"country": 5}, "country", id="country"),
        pytest.param({"country": "\ud800"}, "country", id="surrogate-country"),
    ],
)
def test_unreadable_optional_field_is_dropped_and_job_kept(
    overrides: dict[str, Any], field: str
) -> None:
    batch = parse_lever(_body({**_SECOND, **overrides}), _KEY, _COMPANY)

    assert batch.rejected == 0
    assert getattr(batch.drafts[0], field) is None


def test_job_that_cannot_be_re_encoded_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def overflow(_: object) -> str:
        raise RecursionError

    monkeypatch.setattr(json, "dumps", overflow)
    batch = parse_lever(_BODY, _KEY, _COMPANY)

    assert batch.drafts == []
    assert batch.rejected == 3


def test_job_id_is_logged_without_control_characters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = {**_FIRST, "id": "a\nFAKE LOG", "text": ""}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.lever"):
        parse_lever(_body(job), _KEY, _COMPANY)

    assert "\n" not in caplog.records[0].getMessage()


def test_deeply_nested_body_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_lever(b"[" * 100_000, _KEY, _COMPANY)


def test_non_json_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_lever(b"<html>not json</html>", _KEY, _COMPANY)

    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "body",
    [b'{"ok": false, "error": "Document not found"}', b'{"jobs": []}', b"null"],
    ids=["error-object", "object", "null"],
)
def test_body_that_is_not_an_array_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_lever(body, _KEY, _COMPANY)


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _DictCache:
    """A `ConditionalCache` backed by a dict."""

    def __init__(self, entries: dict[str, CachedResponse]) -> None:
        self.entries = dict(entries)

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


type _Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: _Handler, cache: _DictCache | None = None) -> PoliteClient:
    fake_time = _FakeTime()
    return PoliteClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        cache=cache,
    )


def _serving(response: httpx.Response, requests: list[httpx.Request]) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        requests.append(request)
        return response

    return handler


def _fetch(
    site: LeverSite, handler: _Handler, cache: _DictCache | None = None
) -> SourceBatch:
    async def run() -> SourceBatch:
        async with _client(handler, cache) as client:
            return await site.fetch(client)

    return asyncio.run(run())


def test_fetch_requests_postings_url() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, content=_BODY), requests)

    batch = _fetch(LeverSite(_KEY, company=_COMPANY), handler)

    assert [str(request.url) for request in requests] == [_URL]
    assert batch == parse_lever(_BODY, _KEY, _COMPANY)


def test_fetch_revalidates_a_cached_listing() -> None:
    requests: list[httpx.Request] = []
    cache = _DictCache({_URL: CachedResponse('"v1"', None, _BODY)})
    handler = _serving(httpx.Response(304), requests)

    batch = _fetch(LeverSite(_KEY, company=_COMPANY), handler, cache)

    assert requests[0].headers["If-None-Match"] == '"v1"'
    assert len(batch.drafts) == 3


def test_fetch_runs_identity() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, content=_BODY), requests)

    batch = _fetch(LeverSite(_KEY, company="Spotify"), handler)

    assert len(batch.drafts) == 3
    with pytest.raises(IdentityMismatchError) as caught:
        _fetch(LeverSite(_KEY, company="Palantir"), handler)
    assert caught.value.ref == SourceRef(SourceKind.LEVER, _KEY)


def test_identity_needs_one_job_to_name_the_company() -> None:
    nameless = {**_SECOND, "descriptionPlain": "Build things."}
    body = _body(nameless, _ONSITE)
    handler = _serving(httpx.Response(200, content=body), [])

    batch = _fetch(LeverSite(_KEY, company=_COMPANY), handler)

    assert len(batch.drafts) == 2


def test_identity_reads_the_text_of_a_job_rejected_for_another_reason() -> None:
    body = _body({**_FIRST, "text": ""})
    handler = _serving(httpx.Response(200, content=body), [])

    batch = _fetch(LeverSite(_KEY, company=_COMPANY), handler)

    assert batch.drafts == []
    assert batch.rejected == 1


def test_identity_fails_when_every_listed_job_is_rejected_and_none_names_it() -> None:
    body = _body({**_FIRST, "text": ""})
    handler = _serving(httpx.Response(200, content=body), [])

    with pytest.raises(IdentityMismatchError):
        _fetch(LeverSite(_KEY, company="Palantir"), handler)


def test_identity_runs_after_the_postings_are_parsed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _body({**_FIRST, "text": ""})
    handler = _serving(httpx.Response(200, content=body), [])

    with (
        caplog.at_level(logging.WARNING, logger="ijob_sources.lever"),
        pytest.raises(IdentityMismatchError),
    ):
        _fetch(LeverSite(_KEY, company="Palantir"), handler)

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_empty_site_passes_identity() -> None:
    handler = _serving(httpx.Response(200, content=b"[]"), [])

    batch = _fetch(LeverSite(_KEY, company="Anything"), handler)

    assert batch.drafts == []


def test_fetch_unknown_site_raises_source_not_found() -> None:
    not_found = httpx.Response(404, text='{"ok":false,"error":"Document not found"}')

    with pytest.raises(SourceNotFoundError):
        _fetch(
            LeverSite(parse_source_key("zz-no-such-site"), company=_COMPANY),
            _serving(not_found, []),
        )


def test_lever_site_satisfies_source_protocol() -> None:
    site = LeverSite(_KEY, company=_COMPANY)
    handler = _serving(httpx.Response(200, content=_BODY), [])

    async def run() -> CollectResult:
        async with _client(handler) as client:
            return await collect([site], client)

    result = asyncio.run(run())

    assert site.ref == SourceRef(SourceKind.LEVER, _KEY)
    assert result.failures == []
    assert len(result.batches[site.ref].drafts) == 3


_EXPECTED_DESCRIPTION = (
    "Spotify Technology is our business-to-business software team, helping "
    "organizations improve how they build and deliver software. Our "
    "products, including Backstage Portal, Confidence, and Xirp, support "
    "developers and AI-powered systems as they navigate increasingly "
    "complex engineering environments.\n\n"
    "As an Account Executive on our Commercial team, you will drive new "
    "business for Spotify Technology. You'll focus on companies with fewer "
    "than 1,000 employees, where sales cycles typically run from weeks to "
    "a few months and success depends on a repeatable, scalable sales "
    "motion. You'll manage a high volume of opportunities, work with one "
    "or a small group of decision-makers (often a hands-on engineering "
    "leader or CTO), and move deals efficiently from the first "
    "conversation to close. You'll also help shape the playbook that "
    "enables our commercial business to grow.\n\n"
    "You'll collaborate with Sales Engineering, Marketing, Product, and "
    "Customer Success to create a seamless customer experience. Your "
    "ability to quickly understand customers' technical needs and connect "
    "them to clear business value will be key to your success.\n\n"
    "## What You'll Do\n\n"
    "• Identify and pursue new business opportunities with companies of "
    "fewer than 1,000 employees. While significant inbound demand supports "
    "the pipeline, top performers will exceed targets by treating this as "
    "a “Hunter” role in addition to converting inbound demand.\n\n"
    "• Own the full sales cycle, from prospecting and discovery through "
    "demos, proposals, negotiations, and close.\n\n"
    "• Manage a high-velocity pipeline across multiple opportunities, "
    "keeping deals moving with clear next steps and maintaining accurate "
    "pipeline and forecast data in the CRM.\n\n"
    "• Achieve and exceed quarterly and annual revenue targets.\n\n"
    "## Who You Are\n\n"
    "• You have 4+ years of experience in a quota-carrying sales role, "
    "including 2+ years in a revenue-closing role, ideally in SaaS, "
    "developer platforms, or cloud services.\n\n"
    "• You have a proven track record of achieving and exceeding targets "
    "in a fast-paced, high-volume B2B environment, ideally selling to "
    "small and medium-sized businesses (SMB) or mid-market companies.\n\n"
    "• You are skilled at managing fast sales cycles efficiently while "
    "maintaining a thoughtful and effective discovery process.\n\n"
    "The United States base range for this position is $87,069-$124,385 "
    "plus equity. The benefits available for this position include health "
    "insurance, six month paid parental leave, 401(k) retirement plan, "
    "monthly meal allowance, 23 paid days off, 13 paid flexible holidays, "
    "paid sick leave. These ranges may be modified in the future."
)


def test_draft_carries_description() -> None:
    draft = parse_lever(_BODY, _KEY, _COMPANY).drafts[0]

    assert draft.description == _EXPECTED_DESCRIPTION


def test_description_from_raw_matches_draft() -> None:
    draft = parse_lever(_BODY, _KEY, _COMPANY).drafts[0]

    assert description_from_raw(draft.raw) == draft.description


def test_description_missing_gives_none() -> None:
    batch = parse_lever(
        _body(_without(_FIRST, "description", "lists", "additional")), _KEY, _COMPANY
    )

    assert len(batch.drafts) == 1
    assert batch.drafts[0].description is None
    assert description_from_raw(batch.drafts[0].raw) is None


@pytest.mark.parametrize("value", [None, 7, ["<p>x</p>"], {"html": "<p>x</p>"}])
def test_description_not_text_gives_none(value: object) -> None:
    job = {**_without(_FIRST, "lists", "additional"), "description": value}

    draft = parse_lever(_body(job), _KEY, _COMPANY).drafts[0]

    assert draft.description is None


def test_description_drops_a_lone_surrogate() -> None:
    job = {**_without(_FIRST, "lists", "additional"), "description": "<p>a\ud800b</p>"}

    draft = parse_lever(_body(job), _KEY, _COMPANY).drafts[0]

    assert draft.description == "ab"
    assert description_from_raw(draft.raw) == "ab"


@pytest.mark.parametrize("raw", ["[]", '"text"', "null"])
def test_description_from_raw_non_object_raises_parse_error(raw: str) -> None:
    with pytest.raises(ParseError):
        description_from_raw(raw)


def test_description_from_raw_not_json_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        description_from_raw("{not json")


def test_lever_description_includes_lists_and_additional() -> None:
    description = parse_lever(_BODY, _KEY, _COMPANY).drafts[0].description

    assert description is not None
    markers = [
        "Spotify Technology is our business-to-business software team",
        "## What You'll Do",
        "Own the full sales cycle",
        "## Who You Are",
        "You have 4+ years of experience",
        "The United States base range for this position",
    ]
    positions = [description.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_lever_list_heading_is_escaped() -> None:
    job = {
        **_FIRST,
        "description": "<p>Intro</p>",
        "lists": [{"text": "R&D <b>Team</b>", "content": "<p>Body</p>"}],
        "additional": "",
    }

    draft = parse_lever(_body(job), _KEY, _COMPANY).drafts[0]

    assert draft.description == "Intro\n\n## R&D \\<b>Team\\</b>\n\nBody"


def test_lever_unreadable_list_items_are_skipped() -> None:
    job = {
        **_without(_FIRST, "additional"),
        "description": "<p>Intro</p>",
        "lists": [
            "not an object",
            {"text": 7, "content": "<p>Kept</p>"},
            {"text": "Heading only", "content": None},
            {"text": "Duties"},
        ],
    }

    draft = parse_lever(_body(job), _KEY, _COMPANY).drafts[0]

    assert draft.description == "Intro\n\nKept\n\n## Heading only\n\n## Duties"


@pytest.mark.parametrize("lists", [None, "text", {"text": "Duties"}])
def test_lever_lists_not_a_list_is_ignored(lists: object) -> None:
    job = {
        **_without(_FIRST, "additional"),
        "description": "<p>Intro</p>",
        "lists": lists,
    }

    assert parse_lever(_body(job), _KEY, _COMPANY).drafts[0].description == "Intro"
