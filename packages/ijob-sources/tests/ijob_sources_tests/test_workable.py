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
from ijob_sources.polite_http import CachedResponse, PoliteClient
from ijob_sources.workable import WorkableAccount, parse_workable

_KEY = parse_source_key("huggingface")
_COMPANY = "Hugging Face"
_BODY = (
    files("ijob_sources_tests.fixtures.workable") / "huggingface_widget.json"
).read_bytes()
_WIDGET: dict[str, Any] = json.loads(_BODY)
_JOBS: list[dict[str, Any]] = _WIDGET["jobs"]
_FIRST, _SECOND, _THIRD = _JOBS
_URL = "https://apply.workable.com/api/v1/widget/accounts/huggingface"


def _body(*jobs: object, **widget: object) -> bytes:
    return json.dumps({**_WIDGET, "jobs": list(jobs), **widget}).encode()


def _without(job: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: value for name, value in job.items() if name not in names}


def test_parse_workable_maps_fields() -> None:
    batch = parse_workable(_BODY, _KEY, _COMPANY)
    draft = batch.drafts[0]

    assert len(batch.drafts) == 3
    assert batch.rejected == 0
    assert draft.source == SourceRef(SourceKind.WORKABLE, _KEY)
    assert draft.external_id == ExternalId("81B46579FE")
    assert draft.title == "Open-Source Machine Learning Engineer - EMEA Remote"
    assert draft.company == "Hugging Face"
    assert draft.location == "Paris"
    assert draft.url == "https://apply.workable.com/j/81B46579FE"
    assert draft.country == CountryCode("FR")
    assert draft.remote is None
    assert draft.posted_at == datetime(2026, 5, 29, tzinfo=UTC)
    assert draft.role_hint == "Engineering"
    assert json.loads(draft.raw) == _FIRST


def test_country_comes_from_the_first_location() -> None:
    drafts = parse_workable(_BODY, _KEY, _COMPANY).drafts

    assert [draft.country for draft in drafts] == [
        CountryCode("FR"),
        CountryCode("US"),
        CountryCode("FR"),
    ]


def test_first_location_wins_over_later_ones() -> None:
    locations = [
        {"countryCode": "DE", "city": "Berlin"},
        {"countryCode": "FR", "city": "Paris"},
    ]
    job = {**_FIRST, "locations": locations}

    assert parse_workable(_body(job), _KEY, _COMPANY).drafts[0].country == "DE"


@pytest.mark.parametrize(
    "locations",
    [[], [{"city": "Paris"}], [{"countryCode": ""}], [{"countryCode": "ZZ"}]],
    ids=["empty", "no-code", "blank-code", "unknown-code"],
)
def test_no_usable_location_code_is_no_country(locations: list[object]) -> None:
    job = {**_FIRST, "locations": locations}

    assert parse_workable(_body(job), _KEY, _COMPANY).drafts[0].country is None


def test_absent_locations_is_no_country() -> None:
    job = _without(_FIRST, "locations")

    assert parse_workable(_body(job), _KEY, _COMPANY).drafts[0].country is None


def test_telecommuting_false_is_not_remote() -> None:
    job = {**_FIRST, "telecommuting": False}

    draft = parse_workable(_body(job), _KEY, _COMPANY).drafts[0]

    assert draft.remote is RemoteScope.NONE


def test_telecommuting_true_or_absent_is_unknown_scope() -> None:
    absent = _without(_FIRST, "telecommuting")

    drafts = parse_workable(_body(_FIRST, absent), _KEY, _COMPANY).drafts

    assert [draft.remote for draft in drafts] == [None, None]


@pytest.mark.parametrize("function", ["", None])
def test_empty_function_is_no_hint(function: str | None) -> None:
    job = {**_FIRST, "function": function}

    assert parse_workable(_body(job), _KEY, _COMPANY).drafts[0].role_hint is None


def test_absent_function_is_no_hint() -> None:
    job = _without(_FIRST, "function")

    assert parse_workable(_body(job), _KEY, _COMPANY).drafts[0].role_hint is None


@pytest.mark.parametrize("city", ["", None])
def test_empty_city_is_no_location(city: str | None) -> None:
    job = {**_FIRST, "city": city}

    assert parse_workable(_body(job), _KEY, _COMPANY).drafts[0].location is None


def test_absent_published_on_is_none() -> None:
    job = _without(_FIRST, "published_on")

    assert parse_workable(_body(job), _KEY, _COMPANY).drafts[0].posted_at is None


def test_widget_without_jobs_is_empty_batch() -> None:
    batch = parse_workable(_body(), _KEY, _COMPANY)

    assert batch.drafts == []
    assert batch.rejected == 0


_LABEL = f"id {_SECOND['shortcode']!r}"


@pytest.mark.parametrize(
    ("bad_job", "label"),
    [
        pytest.param({**_SECOND, "title": ""}, _LABEL, id="empty-title"),
        pytest.param(_without(_SECOND, "title"), _LABEL, id="no-title"),
        pytest.param(_without(_SECOND, "shortcode"), "index 1", id="missing-id"),
        pytest.param({**_SECOND, "shortcode": 7}, "index 1", id="non-text-id"),
        pytest.param(_without(_SECOND, "url"), _LABEL, id="no-url"),
        pytest.param({**_SECOND, "url": "ftp://example.test/1"}, _LABEL, id="ftp-url"),
        pytest.param("not a job", "index 1", id="string-job"),
        pytest.param({**_SECOND, "title": "\ud800"}, _LABEL, id="surrogate"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    bad_job: object, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ijob_sources.workable"):
        batch = parse_workable(_body(_FIRST, bad_job), _KEY, _COMPANY)

    assert [draft.external_id for draft in batch.drafts] == ["81B46579FE"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()
    assert "invalid" in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"city": 5}, "location", id="non-text-city"),
        pytest.param({"city": "\ud800"}, "location", id="surrogate-city"),
        pytest.param({"published_on": "May"}, "posted_at", id="bad-date"),
        pytest.param({"published_on": 20260529}, "posted_at", id="int-date"),
        pytest.param({"telecommuting": "yes"}, "remote", id="telecommuting"),
        pytest.param({"function": 3}, "role_hint", id="function"),
        pytest.param({"function": "\ud800"}, "role_hint", id="surrogate-function"),
        pytest.param({"locations": "FR"}, "country", id="locations"),
        pytest.param({"locations": ["FR"]}, "country", id="location-entry"),
        pytest.param({"locations": [{"countryCode": 5}]}, "country", id="code-type"),
        pytest.param(
            {"locations": [{"countryCode": "\ud800"}]}, "country", id="code-surrogate"
        ),
    ],
)
def test_unreadable_optional_field_is_dropped_and_job_kept(
    overrides: dict[str, Any], field: str
) -> None:
    batch = parse_workable(_body({**_SECOND, **overrides}), _KEY, _COMPANY)

    assert batch.rejected == 0
    assert getattr(batch.drafts[0], field) is None


def test_job_that_cannot_be_re_encoded_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def overflow(_: object) -> str:
        raise RecursionError

    monkeypatch.setattr(json, "dumps", overflow)
    batch = parse_workable(_BODY, _KEY, _COMPANY)

    assert batch.drafts == []
    assert batch.rejected == 3


def test_job_id_is_logged_without_control_characters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = {**_FIRST, "shortcode": "a\nFAKE LOG", "title": ""}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.workable"):
        parse_workable(_body(job), _KEY, _COMPANY)

    assert "\n" not in caplog.records[0].getMessage()


def test_deeply_nested_body_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_workable(b"[" * 100_000, _KEY, _COMPANY)


def test_non_json_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_workable(b"Not Found", _KEY, _COMPANY)

    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "body",
    [b'{"name": "x"}', b'{"jobs": {"a": 1}}', b"[]", b"null"],
    ids=["no-jobs", "jobs-not-list", "top-level-list", "null"],
)
def test_missing_jobs_list_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_workable(body, _KEY, _COMPANY)


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
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        requests.append(request)
        return response

    return handler


def _fetch(
    account: WorkableAccount, handler: _Handler, cache: _DictCache | None = None
) -> SourceBatch:
    async def run() -> SourceBatch:
        async with _client(handler, cache) as client:
            return await account.fetch(client)

    return asyncio.run(run())


def test_fetch_requests_widget_url() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, content=_BODY), requests)

    batch = _fetch(WorkableAccount(_KEY, company=_COMPANY), handler)

    assert [str(request.url) for request in requests] == [_URL]
    assert batch == parse_workable(_BODY, _KEY, _COMPANY)


def test_fetch_revalidates_a_cached_widget() -> None:
    requests: list[httpx.Request] = []
    cache = _DictCache({_URL: CachedResponse('"v1"', None, _BODY)})
    handler = _serving(httpx.Response(304), requests)

    batch = _fetch(WorkableAccount(_KEY, company=_COMPANY), handler, cache)

    assert requests[0].headers["If-None-Match"] == '"v1"'
    assert len(batch.drafts) == 3


def test_fetch_runs_identity() -> None:
    handler = _serving(httpx.Response(200, content=_BODY), [])

    batch = _fetch(WorkableAccount(_KEY, company="Hugging Face"), handler)

    assert len(batch.drafts) == 3
    with pytest.raises(IdentityMismatchError) as caught:
        _fetch(WorkableAccount(_KEY, company="Mercari"), handler)
    assert caught.value.ref == SourceRef(SourceKind.WORKABLE, _KEY)


def test_identity_accepts_the_company_in_the_blurb_alone() -> None:
    body = _body(_FIRST, name="")
    handler = _serving(httpx.Response(200, content=body), [])

    batch = _fetch(WorkableAccount(_KEY, company=_COMPANY), handler)

    assert len(batch.drafts) == 1


def test_identity_accepts_a_company_with_a_legal_suffix() -> None:
    body = _body(_FIRST, name="Mercari, inc.", description="<h3>careers</h3>")
    handler = _serving(httpx.Response(200, content=body), [])

    batch = _fetch(WorkableAccount(_KEY, company="Mercari"), handler)

    assert len(batch.drafts) == 1


def test_identity_ignores_a_name_and_blurb_that_are_not_text() -> None:
    body = _body(_FIRST, name=5, description=None)
    handler = _serving(httpx.Response(200, content=body), [])

    with pytest.raises(IdentityMismatchError):
        _fetch(WorkableAccount(_KEY, company=_COMPANY), handler)


def test_identity_counts_a_rejected_job_as_listed() -> None:
    body = _body({**_FIRST, "title": ""})
    handler = _serving(httpx.Response(200, content=body), [])

    batch = _fetch(WorkableAccount(_KEY, company=_COMPANY), handler)

    assert (batch.drafts, batch.rejected) == ([], 1)
    with pytest.raises(IdentityMismatchError):
        _fetch(WorkableAccount(_KEY, company="Mercari"), handler)


def test_identity_runs_after_the_jobs_are_parsed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _body({**_FIRST, "title": ""})
    handler = _serving(httpx.Response(200, content=body), [])

    with (
        caplog.at_level(logging.WARNING, logger="ijob_sources.workable"),
        pytest.raises(IdentityMismatchError),
    ):
        _fetch(WorkableAccount(_KEY, company="Mercari"), handler)

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_empty_widget_passes_identity() -> None:
    handler = _serving(httpx.Response(200, content=_body(name="Other")), [])

    batch = _fetch(WorkableAccount(_KEY, company=_COMPANY), handler)

    assert batch.drafts == []


def test_fetch_unknown_account_raises_source_not_found() -> None:
    not_found = httpx.Response(404, text="Not Found")

    with pytest.raises(SourceNotFoundError):
        _fetch(
            WorkableAccount(parse_source_key("zz-no-such-account"), company=_COMPANY),
            _serving(not_found, []),
        )


def test_workable_account_satisfies_source_protocol() -> None:
    account = WorkableAccount(_KEY, company=_COMPANY)
    handler = _serving(httpx.Response(200, content=_BODY), [])

    async def run() -> CollectResult:
        async with _client(handler) as client:
            return await collect([account], client)

    result = asyncio.run(run())

    assert account.ref == SourceRef(SourceKind.WORKABLE, _KEY)
    assert result.failures == []
    assert len(result.batches[account.ref].drafts) == 3
