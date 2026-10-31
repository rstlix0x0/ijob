import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
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
from ijob_sources.gem import GemBoard, parse_gem
from ijob_sources.polite_http import CachedResponse, ConditionalCache, PoliteClient

_FIXTURE = Path(__file__).parent / "fixtures" / "gem" / "retool_job_posts.json"
_BODY = _FIXTURE.read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_BODY)
_FIRST, _REMOTE, _THIRD = _JOBS
_KEY = parse_source_key("retool")
_COMPANY = "Retool"
_URL = "https://api.gem.com/job_board/v0/retool/job_posts/"
_LABEL = f"id {_THIRD['id']!r}"


def _body(*jobs: object) -> bytes:
    return json.dumps(list(jobs)).encode()


def _without(job: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: value for name, value in job.items() if name not in names}


def _parse(*jobs: object) -> SourceBatch:
    return parse_gem(_body(*jobs), _KEY, _COMPANY)


def test_parse_gem_maps_fields() -> None:
    draft = parse_gem(_BODY, _KEY, _COMPANY).drafts[0]

    assert draft.source == SourceRef(SourceKind.GEM, _KEY)
    assert draft.external_id == ExternalId("4003629005")
    assert draft.title == "Software Engineer"
    assert draft.company == "Retool"
    assert draft.location == "San Francisco, United States"
    assert draft.url == "https://jobs.gem.com/retool/4003629005"
    assert draft.country == CountryCode("US")
    assert draft.remote is RemoteScope.NONE
    assert draft.posted_at == datetime(2022, 3, 25, 18, 46, 39, tzinfo=UTC)
    assert draft.role_hint == "Engineering"
    assert draft.salaries == ()
    assert json.loads(draft.raw) == _FIRST


def test_parse_fixture_returns_three_drafts_none_rejected() -> None:
    batch = parse_gem(_BODY, _KEY, _COMPANY)

    assert len(batch.drafts) == 3
    assert batch.rejected == 0


def test_in_office_location_type_is_not_remote() -> None:
    assert _THIRD["location_type"] == "in_office"
    assert _parse(_THIRD).drafts[0].remote is RemoteScope.NONE


def test_remote_location_type_is_unknown_scope() -> None:
    assert _REMOTE["location_type"] == "remote"
    assert _parse(_REMOTE).drafts[0].remote is None


@pytest.mark.parametrize("location_type", [None, "", "somewhere", 7, ["hybrid"], {}])
def test_other_location_type_is_unknown_scope(location_type: object) -> None:
    assert _parse({**_FIRST, "location_type": location_type}).drafts[0].remote is None


def test_absent_location_type_is_unknown_scope() -> None:
    assert _parse(_without(_FIRST, "location_type")).drafts[0].remote is None


def test_opaque_string_id_kept() -> None:
    job = {**_FIRST, "id": "am9icG9zdDqAUz7G7JlGLHQawWv7Sec2"}

    assert _parse(job).drafts[0].external_id == "am9icG9zdDqAUz7G7JlGLHQawWv7Sec2"


def test_absent_optional_fields_are_none() -> None:
    job = _without(_FIRST, "location", "first_published_at", "departments")

    draft = _parse(job).drafts[0]

    assert draft.location is None
    assert draft.country is None
    assert draft.posted_at is None
    assert draft.role_hint is None


def test_null_optional_fields_are_treated_as_absent() -> None:
    job = {**_FIRST, "location": None, "first_published_at": None, "departments": None}

    draft = _parse(job).drafts[0]

    assert (draft.location, draft.posted_at, draft.role_hint) == (None, None, None)


def test_location_without_a_country_has_none() -> None:
    job = {**_FIRST, "location": {"name": "United States - Remote"}}

    assert _parse(job).drafts[0].country is None


def test_role_hint_takes_first_named_department() -> None:
    departments = [{"name": ""}, {"id": "1"}, "x", {"name": "Platform"}, {"name": "Y"}]

    draft = _parse({**_FIRST, "departments": departments}).drafts[0]

    assert draft.role_hint == "Platform"


def test_departments_that_are_not_a_list_give_no_hint() -> None:
    assert _parse({**_FIRST, "departments": "Engineering"}).drafts[0].role_hint is None


def test_empty_array_is_empty_batch() -> None:
    assert parse_gem(b"[]", _KEY, _COMPANY) == SourceBatch(drafts=[], rejected=0)


@pytest.mark.parametrize(
    "body",
    [b'{"jobs": []}', b"null", b'"text"', b"7"],
    ids=["object", "null", "string", "number"],
)
def test_not_an_array_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_gem(body, _KEY, _COMPANY)


def test_non_json_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_gem(b"<html>not json</html>", _KEY, _COMPANY)

    assert isinstance(caught.value.__cause__, ValueError)


def test_deeply_nested_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_gem(b"[" * 100_000, _KEY, _COMPANY)

    assert isinstance(caught.value.__cause__, RecursionError)


@pytest.mark.parametrize(
    ("bad_job", "label"),
    [
        pytest.param({**_THIRD, "title": ""}, _LABEL, id="empty-title"),
        pytest.param({**_THIRD, "title": 5}, _LABEL, id="non-text-title"),
        pytest.param({**_THIRD, "title": "\ud800"}, _LABEL, id="surrogate-title"),
        pytest.param(_without(_THIRD, "title"), _LABEL, id="no-title"),
        pytest.param(
            {**_THIRD, "absolute_url": "ftp://example.test/1"},
            _LABEL,
            id="non-http-url",
        ),
        pytest.param(_without(_THIRD, "absolute_url"), _LABEL, id="no-url"),
        pytest.param({**_THIRD, "absolute_url": ["x"]}, _LABEL, id="non-text-url"),
        pytest.param(_without(_THIRD, "id"), "index 1", id="missing-id"),
        pytest.param({**_THIRD, "id": 7}, "index 1", id="integer-id"),
        pytest.param({**_THIRD, "id": ""}, "id ''", id="empty-id"),
        pytest.param({**_THIRD, "id": "\ud800"}, "id '\\ud800'", id="surrogate-id"),
        pytest.param("not a job", "index 1", id="string-job"),
    ],
)
def test_invalid_job_rejected_others_kept(
    bad_job: object, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ijob_sources.gem"):
        batch = _parse(_FIRST, bad_job)

    assert [draft.external_id for draft in batch.drafts] == ["4003629005"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()
    assert "invalid" in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"location": "Remote"}, "location", id="string-location"),
        pytest.param({"location": {"name": 7}}, "location", id="non-text-location"),
        pytest.param(
            {"location": {"name": "\ud800"}}, "location", id="surrogate-location"
        ),
        pytest.param(
            {"first_published_at": "yesterday"}, "posted_at", id="unreadable-date"
        ),
        pytest.param({"first_published_at": 20260804}, "posted_at", id="non-text-date"),
        pytest.param(
            {"first_published_at": "2026-08-04T14:21:41"},
            "posted_at",
            id="naive-date",
        ),
        pytest.param(
            {"departments": [{"name": "\ud800"}]}, "role_hint", id="surrogate-hint"
        ),
        pytest.param({"departments": [{"name": 7}]}, "role_hint", id="non-text-hint"),
    ],
)
def test_unreadable_optional_field_is_dropped_and_job_kept(
    overrides: dict[str, Any], field: str
) -> None:
    batch = _parse({**_FIRST, **overrides})

    assert batch.rejected == 0
    assert getattr(batch.drafts[0], field) is None


def test_job_id_is_logged_without_control_characters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = {**_FIRST, "id": "a\nFAKE LOG", "title": ""}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.gem"):
        _parse(job)

    assert "\n" not in caplog.records[0].getMessage()


def test_job_that_cannot_be_re_encoded_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(_FIRST)

    def overflow(_: object) -> str:
        raise RecursionError

    monkeypatch.setattr(json, "dumps", overflow)
    batch = parse_gem(body, _KEY, _COMPANY)

    assert batch.drafts == []
    assert batch.rejected == 1


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _DictCache:
    """A `ConditionalCache` that keeps responses in a dict."""

    def __init__(self, entries: dict[str, CachedResponse] | None = None) -> None:
        self.entries = entries or {}

    def lookup(self, url: str) -> CachedResponse | None:
        return self.entries.get(url)

    def store(self, url: str, response: CachedResponse) -> None:
        self.entries[url] = response


def _fetch(
    board: GemBoard,
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
            return await board.fetch(client)

    return asyncio.run(run())


def _serving(
    board_response: httpx.Response, requests: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, json={"message": "Forbidden"})
        if requests is not None:
            requests.append(request)
        return board_response

    return handler


def test_fetch_requests_job_posts_url_conditionally() -> None:
    requests: list[httpx.Request] = []
    cache = _DictCache({_URL: CachedResponse('"v1"', None, _BODY)})

    batch = _fetch(
        GemBoard(_KEY, company=_COMPANY),
        _serving(httpx.Response(304), requests),
        cache,
    )

    assert [str(request.url) for request in requests] == [_URL]
    assert requests[0].headers["if-none-match"] == '"v1"'
    assert batch == parse_gem(_BODY, _KEY, _COMPANY)


def test_fetch_unknown_board_raises_source_not_found() -> None:
    not_found = httpx.Response(404, json={"message": "not found"})
    board = GemBoard(parse_source_key("zz-none"), company=_COMPANY)

    with pytest.raises(SourceNotFoundError):
        _fetch(board, _serving(not_found))


def test_fetch_runs_identity() -> None:
    handler = _serving(httpx.Response(200, content=_BODY))

    batch = _fetch(GemBoard(_KEY, company="Retool"), handler)

    assert len(batch.drafts) == 3
    with pytest.raises(IdentityMismatchError) as caught:
        _fetch(GemBoard(_KEY, company="Groq"), handler)
    assert caught.value.ref == SourceRef(SourceKind.GEM, _KEY)
    assert "Groq" in caught.value.reason


def test_identity_reads_the_text_of_a_job_rejected_for_another_reason() -> None:
    other = {**_FIRST, "title": "", "content_plain": "About Groq"}
    handler = _serving(httpx.Response(200, content=_body(other)))

    batch = _fetch(GemBoard(_KEY, company="Groq"), handler)

    assert (batch.drafts, batch.rejected) == ([], 1)


def test_identity_fails_when_every_listed_job_is_rejected_and_none_names_it() -> None:
    other = {**_FIRST, "title": "", "content_plain": "About Retool"}
    handler = _serving(httpx.Response(200, content=_body(other)))

    with pytest.raises(IdentityMismatchError):
        _fetch(GemBoard(_KEY, company="Groq"), handler)


def test_gem_board_satisfies_source_protocol() -> None:
    board = GemBoard(_KEY, company=_COMPANY)
    fake_time = _FakeTime()
    handler = _serving(httpx.Response(200, content=_BODY))

    async def run() -> CollectResult:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
        async with client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.GEM, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 3
