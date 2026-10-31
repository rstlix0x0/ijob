import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_core.model import ExternalId, SourceKind, SourceRef, parse_source_key
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError, SourceNotFoundError
from ijob_sources.greenhouse import GreenhouseBoard, parse_jobs
from ijob_sources.polite_http import PoliteClient

_FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse" / "tailscale_jobs.json"
_BODY = _FIXTURE.read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_BODY)["jobs"]
_FIRST, _SECOND = _JOBS
_KEY = parse_source_key("tailscale")
_LABEL = f"id {_SECOND['id']}"


def _body(*jobs: object) -> bytes:
    return json.dumps({"jobs": list(jobs)}).encode()


def _without(job: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: value for name, value in job.items() if name not in names}


def test_parse_fixture_maps_first_job() -> None:
    draft = parse_jobs(_BODY, _KEY).drafts[0]

    assert draft.source == SourceRef(SourceKind.GREENHOUSE, _KEY)
    assert draft.external_id == ExternalId("4721252005")
    assert draft.title == "Analytics Engineer, Data"
    assert draft.company == "Tailscale"
    assert draft.location == "Remote (Canada)"
    assert draft.url == "https://job-boards.greenhouse.io/tailscale/jobs/4721252005"
    assert draft.posted_at == datetime(
        2026, 8, 4, 14, 21, 41, tzinfo=timezone(timedelta(hours=-4))
    )
    assert json.loads(draft.raw) == _FIRST


def test_parse_fixture_returns_two_drafts_none_rejected() -> None:
    batch = parse_jobs(_BODY, _KEY)

    assert len(batch.drafts) == 2
    assert batch.rejected == 0


@pytest.mark.parametrize("company_name", [None, ""])
def test_company_falls_back_to_board_key(company_name: str | None) -> None:
    job = {**_FIRST, "company_name": company_name}
    if company_name is None:
        job = _without(_FIRST, "company_name")

    assert parse_jobs(_body(job), _KEY).drafts[0].company == "tailscale"


def test_absent_location_and_first_published_are_none() -> None:
    job = _without(_FIRST, "location", "first_published")

    draft = parse_jobs(_body(job), _KEY).drafts[0]

    assert draft.location is None
    assert draft.posted_at is None


@pytest.mark.parametrize(
    ("bad_job", "label"),
    [
        pytest.param({**_SECOND, "title": ""}, _LABEL, id="empty-title"),
        pytest.param(
            {**_SECOND, "absolute_url": "ftp://example.test/1"},
            _LABEL,
            id="non-http-url",
        ),
        pytest.param(_without(_SECOND, "id"), "index 1", id="missing-id"),
        pytest.param(_without(_SECOND, "title"), _LABEL, id="no-title"),
        pytest.param(_without(_SECOND, "absolute_url"), _LABEL, id="no-url"),
        pytest.param("not a job", "index 1", id="string-job"),
        pytest.param({**_SECOND, "location": "Remote"}, _LABEL, id="string-location"),
        pytest.param(
            {**_SECOND, "location": {"name": 7}},
            _LABEL,
            id="non-text-location-name",
        ),
        pytest.param(
            {**_SECOND, "first_published": "yesterday"},
            _LABEL,
            id="unreadable-first-published",
        ),
        pytest.param(
            {**_SECOND, "first_published": 20260804},
            _LABEL,
            id="non-text-first-published",
        ),
        pytest.param(
            {**_SECOND, "first_published": "2026-08-04T14:21:41"},
            _LABEL,
            id="naive-first-published",
        ),
        pytest.param({**_SECOND, "title": 5}, _LABEL, id="non-text-title"),
        pytest.param(
            {**_SECOND, "absolute_url": ["x"]},
            _LABEL,
            id="non-text-url",
        ),
        pytest.param({**_SECOND, "id": ["x"]}, "index 1", id="list-id"),
        pytest.param({**_SECOND, "id": True}, "index 1", id="bool-id"),
        pytest.param({**_SECOND, "company_name": 3}, _LABEL, id="company"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    bad_job: object, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ijob_sources.greenhouse"):
        batch = parse_jobs(_body(_FIRST, bad_job), _KEY)

    assert [draft.external_id for draft in batch.drafts] == ["4721252005"]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()
    assert "invalid" in warnings[0].getMessage()


def test_null_fields_are_treated_as_absent() -> None:
    job = {**_FIRST, "company_name": None, "location": None, "first_published": None}
    draft = parse_jobs(_body(job), _KEY).drafts[0]

    assert draft.company == "tailscale"
    assert draft.location is None
    assert draft.posted_at is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "\ud800"},
        {"title": "\ud800"},
        {"company_name": "\ud800"},
        {"absolute_url": "https://x.test/\ud800"},
        {"location": {"name": "\ud800"}},
    ],
)
def test_text_that_is_not_utf8_encodable_is_rejected(overrides: dict[str, Any]) -> None:
    batch = parse_jobs(_body({**_FIRST, **overrides}, _SECOND), _KEY)

    assert [draft.external_id for draft in batch.drafts] == [str(_SECOND["id"])]
    assert batch.rejected == 1


def test_job_that_cannot_be_re_encoded_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(_FIRST)

    def overflow(_: object) -> str:
        raise RecursionError

    monkeypatch.setattr(json, "dumps", overflow)
    batch = parse_jobs(body, _KEY)

    assert batch.drafts == []
    assert batch.rejected == 1


def test_job_id_is_logged_without_control_characters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = {**_FIRST, "id": "a\nFAKE LOG", "title": ""}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.greenhouse"):
        parse_jobs(_body(job), _KEY)

    assert "\n" not in caplog.records[0].getMessage()


def test_deeply_nested_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_jobs(b"[" * 100_000, _KEY)
    assert isinstance(caught.value.__cause__, RecursionError)


def test_non_json_body_raises_parse_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_jobs(b"<html>not json</html>", _KEY)

    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "body",
    [b'{"meta": {"total": 0}}', b'{"jobs": {"a": 1}}', b"[]", b"null"],
    ids=["no-jobs", "jobs-not-list", "top-level-list", "null"],
)
def test_missing_jobs_list_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_jobs(body, _KEY)


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _fetch(
    board: GreenhouseBoard, handler: Callable[[httpx.Request], httpx.Response]
) -> SourceBatch:
    fake_time = _FakeTime()

    async def run() -> SourceBatch:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
        async with client:
            return await board.fetch(client)

    return asyncio.run(run())


def _serving(
    board_response: httpx.Response, requests: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /embed/\n")
        if requests is not None:
            requests.append(request)
        return board_response

    return handler


def test_fetch_requests_board_url_with_content() -> None:
    requests: list[httpx.Request] = []
    handler = _serving(httpx.Response(200, content=_BODY), requests)

    batch = _fetch(GreenhouseBoard(_KEY), handler)

    assert [str(request.url) for request in requests] == [
        "https://boards-api.greenhouse.io/v1/boards/tailscale/jobs?content=true"
    ]
    assert batch == parse_jobs(_BODY, _KEY)


def test_fetch_unknown_board_raises_source_not_found() -> None:
    not_found = httpx.Response(404, text='{"status":404,"error":"Job not found"}')

    with pytest.raises(SourceNotFoundError):
        _fetch(
            GreenhouseBoard(parse_source_key("zz-no-such-board")), _serving(not_found)
        )


def test_greenhouse_board_satisfies_source_protocol() -> None:
    board = GreenhouseBoard(_KEY)
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

    assert board.ref == SourceRef(SourceKind.GREENHOUSE, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 2
