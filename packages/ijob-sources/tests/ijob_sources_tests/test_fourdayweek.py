import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any, override

import httpx
import pytest
from ijob_core.model import (
    ExternalId,
    RemoteScope,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError
from ijob_sources.fourdayweek import (
    FourDayWeekBoard,
    description_from_raw,
    parse_fourdayweek,
)
from ijob_sources.polite_http import PoliteClient

_FIXTURES = resources.files("ijob_sources_tests.fixtures.fourdayweek")
_PAGE1 = _FIXTURES.joinpath("page1.json").read_bytes()
_PAGE2 = _FIXTURES.joinpath("page2.json").read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_PAGE1)["data"]
_KEY = parse_source_key("engineering")
_FIRST_URL = "https://4dayweek.io/api/v2/jobs?category=engineering&limit=100"


def _page(
    jobs: list[Any], *, page: int = 1, has_more: object = False, **extra: object
) -> bytes:
    body = {"data": jobs, "page": page, "has_more": has_more, **extra}
    return json.dumps(body).encode()


def test_parse_maps_fields() -> None:
    page = parse_fourdayweek(_PAGE1, _KEY)
    draft = page.batch.drafts[0]

    assert page.batch.rejected == 0
    assert len(page.batch.drafts) == 3
    assert draft.source == SourceRef(SourceKind.FOURDAYWEEK, _KEY)
    assert draft.external_id == ExternalId("01a1854c-150f-7088-9e45-cb50abfd5080")
    assert draft.title == "Senior Director, AI Software Engineering"
    assert draft.company == "Experian"
    assert draft.location == "Costa Mesa, United States"
    assert draft.country == "US"
    assert draft.remote is RemoteScope.NONE
    assert draft.url == (
        "https://4dayweek.io/job/senior-director-ai-software-engineering-at-experian-deac1eaf"
    )
    assert draft.posted_at == datetime(2026, 10, 29, 1, 35, 18, tzinfo=UTC)
    assert json.loads(draft.raw) == _JOBS[0]


def test_remote_job_has_no_scope_and_a_country_only_location() -> None:
    draft = parse_fourdayweek(_PAGE1, _KEY).batch.drafts[1]

    assert draft.remote is None
    assert draft.location == "United States"
    assert draft.country == "US"


@pytest.mark.parametrize(
    ("arrangement", "expected"),
    [
        ("onsite", RemoteScope.NONE),
        ("hybrid", RemoteScope.NONE),
        ("remote", None),
        ("something-new", None),
        (None, None),
    ],
)
def test_work_arrangement_mapping(
    arrangement: str | None, expected: RemoteScope | None
) -> None:
    job = {**_JOBS[0], "work_arrangement": arrangement}

    assert parse_fourdayweek(_page([job]), _KEY).batch.drafts[0].remote is expected


def test_job_without_locations_has_no_location_or_country() -> None:
    job = {**_JOBS[0], "locations": []}
    draft = parse_fourdayweek(_page([job]), _KEY).batch.drafts[0]

    assert draft.location is None
    assert draft.country is None


def test_salary_never_mapped() -> None:
    assert _JOBS[0]["salary_min"] > 0
    assert all(
        not draft.salaries for draft in parse_fourdayweek(_PAGE1, _KEY).batch.drafts
    )


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        pytest.param({"title": ""}, "id 'x1'", id="empty-title"),
        pytest.param({"url": "ftp://example.test/1"}, "id 'x1'", id="non-http-url"),
        pytest.param({"id": None}, "index 1", id="missing-id"),
        pytest.param({"id": ["x"]}, "index 1", id="list-id"),
        pytest.param({"company": None}, "id 'x1'", id="no-company"),
        pytest.param({"company": {"name": 3}}, "id 'x1'", id="non-text-company"),
        pytest.param({"posted_at": "yesterday"}, "id 'x1'", id="unreadable-date"),
        pytest.param({"posted_at": "2026-10-29T01:35:18"}, "id 'x1'", id="naive-date"),
        pytest.param({"posted_at": 5}, "id 'x1'", id="non-text-date"),
        pytest.param({"locations": "Berlin"}, "id 'x1'", id="locations-not-list"),
        pytest.param({"locations": ["Berlin"]}, "id 'x1'", id="location-not-object"),
        pytest.param(
            {"locations": [{"city": 3, "country": "Germany"}]},
            "id 'x1'",
            id="non-text-city",
        ),
        pytest.param({"title": "\ud800"}, "id 'x1'", id="lone-surrogate"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    overrides: dict[str, Any], label: str, caplog: pytest.LogCaptureFixture
) -> None:
    bad = {**_JOBS[1], "id": "x1", **overrides}
    body = _page([_JOBS[0], bad])
    with caplog.at_level(logging.WARNING, logger="ijob_sources.fourdayweek"):
        batch = parse_fourdayweek(body, _KEY).batch

    assert [draft.external_id for draft in batch.drafts] == [_JOBS[0]["id"]]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()


def test_non_object_job_is_rejected() -> None:
    batch = parse_fourdayweek(_page(["not a job", _JOBS[0]]), _KEY).batch

    assert len(batch.drafts) == 1
    assert batch.rejected == 1


@pytest.mark.parametrize(
    "body",
    [
        b"<html>",
        b"[]",
        b"{}",
        b'{"data": {}, "page": 1, "has_more": false}',
        b'{"data": [], "page": "1", "has_more": false}',
        b'{"data": [], "page": true, "has_more": false}',
        b'{"data": [], "page": 1, "has_more": "yes"}',
        b'{"data": [], "page": 1}',
        b"[" * 100_000,
    ],
    ids=[
        "not-json",
        "list",
        "empty-object",
        "data-not-list",
        "page-text",
        "page-bool",
        "has-more-text",
        "no-has-more",
        "too-deep",
    ],
)
def test_unreadable_body_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_fourdayweek(body, _KEY)


def test_next_page_follows_the_response_page() -> None:
    assert parse_fourdayweek(_PAGE1, _KEY).next_page == 2
    assert parse_fourdayweek(_page([], page=7, has_more=True), _KEY).next_page == 8
    assert parse_fourdayweek(_PAGE2, _KEY).next_page is None


class _RecordingClient(PoliteClient):
    """A client that notes whether each request asked to be conditional."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        super().__init__(transport=transport, min_interval=0.0)
        self.conditionals: list[bool] = []

    @override
    async def get(
        self,
        url: str,
        *,
        conditional: bool = False,
        follow_redirects: bool = False,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        self.conditionals.append(conditional)
        return await super().get(
            url,
            conditional=conditional,
            follow_redirects=follow_redirects,
            max_bytes=max_bytes,
        )


def _fetch(
    handler: Callable[[httpx.Request], httpx.Response],
) -> SourceBatch:
    async def run() -> SourceBatch:
        now = 0.0

        async def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        client = PoliteClient(
            transport=httpx.MockTransport(handler), clock=lambda: now, sleep=sleep
        )
        async with client:
            return await FourDayWeekBoard(_KEY).fetch(client)

    return asyncio.run(run())


def _serving(
    answer: Callable[[httpx.Request], httpx.Response],
    requests: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /nothing/\n")
        requests.append(request)
        return answer(request)

    return handler


def test_ref_is_the_engineering_board() -> None:
    assert str(FourDayWeekBoard(_KEY).ref) == "4dayweek:engineering"


def test_paging_uses_response_page() -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, content=_PAGE2)
        return httpx.Response(200, content=_PAGE1)

    batch = _fetch(_serving(answer, requests))

    assert [str(request.url) for request in requests] == [
        _FIRST_URL,
        f"{_FIRST_URL}&page=2",
    ]
    assert len(batch.drafts) == 5
    assert batch.rejected == 0


def test_paging_capped_at_five() -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(
            200,
            content=_page(
                [{**_JOBS[0], "id": f"job-{page}"}], page=page, has_more=True
            ),
        )

    batch = _fetch(_serving(answer, requests))

    assert len(requests) == 5
    assert requests[-1].url.params["page"] == "5"
    assert len(batch.drafts) == 5


def test_rejections_are_summed_across_pages() -> None:
    requests: list[httpx.Request] = []
    bad = {**_JOBS[0], "title": ""}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, content=_page([bad], page=2))
        return httpx.Response(200, content=_page([bad, _JOBS[0]], has_more=True))

    batch = _fetch(_serving(answer, requests))

    assert batch.rejected == 2
    assert len(batch.drafts) == 1


def test_only_the_first_page_is_conditional() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, content=_PAGE2)
        return httpx.Response(200, content=_PAGE1)

    async def run() -> list[bool]:
        client = _RecordingClient(httpx.MockTransport(_serving(answer, [])))
        async with client:
            await FourDayWeekBoard(_KEY).fetch(client)
        return client.conditionals

    # The robots.txt lookup goes through a separate path, so only pages appear.
    assert asyncio.run(run()) == [True, False]


def test_fourdayweek_board_satisfies_source_protocol() -> None:
    board = FourDayWeekBoard(_KEY)

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, content=_PAGE2)
        return httpx.Response(200, content=_PAGE1)

    async def run() -> CollectResult:
        client = _RecordingClient(httpx.MockTransport(_serving(answer, [])))
        async with client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.FOURDAYWEEK, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 5


_DESCRIPTION = (
    "Banyan Software is the best permanent home for software businesses "
    "that serve specialized industries, their employees, and their "
    "customers. With a buy-grow-and-hold-for-life approach and a permanent "
    "capital base, Banyan acquires and grows companies worldwide, honoring "
    "founder legacies and helping portfolio companies modernize through "
    "shared AI expertise and operational discipline. Founded in 2016, "
    "Banyan operates more than 120 portfolio companies across North and "
    "South America, Europe, and APAC, and has appeared on the Inc. 5000 "
    "list for six consecutive years. The Banyan Software Foundation, "
    "endowed with $100 million in Banyan stock, leverages technology to "
    "build a greener and more equitable world.\n"
    "\n"
    "Point & Click Solutions is seeking a Software Developer to build and "
    "maintain healthcare applications used by clinical and administrative "
    "teams. You will work across modern\u00a0[ASP.NET](http://asp.net/) "
    "Core applications, established .NET systems, SQL Server databases, "
    "and patient-facing web experiences.\n"
    "\n"
    "**Responsibilities**\n"
    "\n"
    "- Develop and maintain applications using C#, "
    "[ASP.NET](http://asp.net/) Core, Razor, JavaScript, and SQL Server.\n"
    "\n"
    "- Design APIs, services, background jobs, database procedures, and "
    "responsive user interfaces.\n"
    "\n"
    "- Troubleshoot issues across application, database, integration, and "
    "deployment layers.\n"
    "\n"
    "- Write automated unit and browser-based integration tests.\n"
    "\n"
    "- Modernize legacy .NET and Windows components while preserving "
    "production reliability.\n"
    "\n"
    "- Build secure integrations using REST, SOAP, SFTP, XML, and "
    "healthcare data formats.\n"
    "\n"
    "- Participate in code reviews, technical design, testing, and release "
    "validation.\n"
    "\n"
    "- Protect sensitive healthcare data through secure coding and "
    "authorization practices.\n"
    "\n"
    "- Collaborate with product, support, QA, and implementation teams to "
    "translate operational needs into practical solutions.\n"
    "\n"
    "**Required Qualifications**\n"
    "\n"
    "- Professional experience with C# and .NET."
)


def test_draft_carries_description() -> None:
    draft = parse_fourdayweek(_PAGE1, _KEY).batch.drafts[0]

    assert draft.description == _DESCRIPTION


def test_description_from_raw_matches_draft() -> None:
    draft = parse_fourdayweek(_PAGE1, _KEY).batch.drafts[0]

    assert draft.description is not None
    assert description_from_raw(draft.raw) == draft.description


def test_description_missing_gives_none() -> None:
    job = {name: value for name, value in _JOBS[0].items() if name != "description"}

    draft = parse_fourdayweek(_page([job]), _KEY).batch.drafts[0]

    assert draft.description is None
    assert description_from_raw(draft.raw) is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="null"),
        pytest.param(7, id="number"),
        pytest.param(["<p>text</p>"], id="list"),
        pytest.param("", id="empty"),
        pytest.param(" \n ", id="blank"),
    ],
)
def test_description_without_usable_text_gives_none(value: object) -> None:
    draft = parse_fourdayweek(
        _page([{**_JOBS[0], "description": value}]), _KEY
    ).batch.drafts[0]

    assert draft.description is None
    assert description_from_raw(draft.raw) is None


def test_description_drops_a_lone_surrogate() -> None:
    draft = parse_fourdayweek(
        _page([{**_JOBS[0], "description": "a\ud800b"}]), _KEY
    ).batch.drafts[0]

    assert draft.description == "ab"
    assert description_from_raw(draft.raw) == "ab"


@pytest.mark.parametrize("raw", ["not json", "[]", '"text"', "null"])
def test_description_from_raw_rejects_a_raw_that_is_no_object(raw: str) -> None:
    with pytest.raises(ParseError):
        description_from_raw(raw)
