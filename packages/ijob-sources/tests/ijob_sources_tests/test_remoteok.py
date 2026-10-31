import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

import httpx
import pytest
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError
from ijob_sources.polite_http import CachedResponse, PoliteClient
from ijob_sources.remoteok import RemoteOkBoard, parse_remoteok

_KEY = parse_source_key("all")
_URL = "https://remoteok.com/api"
_BODY = (files("ijob_sources_tests.fixtures.remoteok") / "api.json").read_bytes()
_ITEMS: list[dict[str, Any]] = json.loads(_BODY)
_NOTICE, _FRONT_END, _CLOUD = _ITEMS


def _body(*items: object) -> bytes:
    return json.dumps([_NOTICE, *items]).encode()


def _only(job: dict[str, Any]) -> SightingDraft:
    return parse_remoteok(_body(job), _KEY).drafts[0]


def test_notice_element_skipped() -> None:
    batch = parse_remoteok(_BODY, _KEY)

    assert len(batch.drafts) == 2
    assert batch.rejected == 0


def test_first_element_that_is_a_job_is_not_skipped() -> None:
    body = json.dumps([_FRONT_END, _CLOUD]).encode()

    batch = parse_remoteok(body, _KEY)

    assert [draft.external_id for draft in batch.drafts] == ["1137597", "1137600"]
    assert batch.rejected == 0


def test_first_element_that_is_unreadable_and_no_notice_is_rejected() -> None:
    batch = parse_remoteok(json.dumps(["a job", _CLOUD]).encode(), _KEY)

    assert len(batch.drafts) == 1
    assert batch.rejected == 1


def test_parse_maps_fields() -> None:
    draft = parse_remoteok(_BODY, _KEY).drafts[0]

    assert draft.source == SourceRef(SourceKind.REMOTEOK, _KEY)
    assert draft.external_id == ExternalId("1137597")
    assert draft.title == "Front End Developer"
    assert draft.company == "Edconic"
    assert draft.url == (
        "https://remoteOK.com/remote-jobs/remote-front-end-developer-edconic-1137597"
    )
    assert draft.posted_at == datetime.fromtimestamp(1793224395, tz=UTC)
    assert draft.country is None
    assert draft.remote is None
    assert json.loads(draft.raw) == _FRONT_END


def test_empty_location_is_none() -> None:
    assert _only(_FRONT_END).location is None
    assert _only(_CLOUD).location == "Toronto, Ontario"


def test_salary_never_mapped() -> None:
    assert _FRONT_END["salary_min"] == 80000
    assert _only(_FRONT_END).salaries == ()


def test_integer_id_becomes_text() -> None:
    assert _only({**_FRONT_END, "id": 1137597}).external_id == "1137597"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"id": None}, id="no-id"),
        pytest.param({"id": True}, id="bool-id"),
        pytest.param({"position": ""}, id="empty-title"),
        pytest.param({"company": None}, id="no-company"),
        pytest.param({"url": "ftp://x.test/1"}, id="non-http-url"),
        pytest.param({"location": 4}, id="non-text-location"),
        pytest.param({"epoch": "yesterday"}, id="non-number-epoch"),
        pytest.param({"epoch": 10**30}, id="out-of-range-epoch"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    overrides: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    bad = {**_FRONT_END, **overrides}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.remoteok"):
        batch = parse_remoteok(_body(_CLOUD, bad), _KEY)

    assert [draft.external_id for draft in batch.drafts] == ["1137600"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "invalid" in warnings[0].getMessage()


def test_job_that_is_not_an_object_is_rejected() -> None:
    batch = parse_remoteok(_body("a job", _CLOUD), _KEY)

    assert len(batch.drafts) == 1
    assert batch.rejected == 1


def test_absent_epoch_gives_no_posted_at() -> None:
    job = {k: v for k, v in _FRONT_END.items() if k != "epoch"}

    assert _only(job).posted_at is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html></html>", id="not-json"),
        pytest.param(b'{"jobs": []}', id="object"),
        pytest.param(b"null", id="null"),
        pytest.param(b"[" * 100_000, id="too-deep"),
    ],
)
def test_unreadable_body_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_remoteok(body, _KEY)


def test_empty_list_has_no_drafts() -> None:
    batch = parse_remoteok(b"[]", _KEY)

    assert batch.drafts == []
    assert batch.rejected == 0


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

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return handler(request)

    return PoliteClient(
        transport=httpx.MockTransport(wrapped),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        cache=cache,
    )


def _fetch(
    handler: _Handler, cache: _DictCache | None = None
) -> tuple[SourceBatch, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async def run() -> SourceBatch:
        async with _client(recording, cache) as client:
            return await RemoteOkBoard(_KEY).fetch(client)

    return asyncio.run(run()), requests


def test_fetch_makes_one_request() -> None:
    batch, requests = _fetch(lambda _: httpx.Response(200, content=_BODY))

    assert [str(request.url) for request in requests] == [_URL]
    assert batch == parse_remoteok(_BODY, _KEY)


def test_fetch_is_conditional() -> None:
    validator = "Sat, 31 Oct 2026 00:00:00 GMT"
    cache = _DictCache({_URL: CachedResponse(None, validator, _BODY)})

    batch, requests = _fetch(lambda _: httpx.Response(304), cache)

    assert requests[0].headers["If-Modified-Since"] == validator
    assert len(batch.drafts) == 2


def test_remoteok_board_satisfies_source_protocol() -> None:
    board = RemoteOkBoard(_KEY)

    async def run() -> CollectResult:
        async with _client(lambda _: httpx.Response(200, content=_BODY)) as client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.REMOTEOK, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 2
