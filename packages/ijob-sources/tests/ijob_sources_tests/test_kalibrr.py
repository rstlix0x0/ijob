import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from typing import Any, override

import httpx
import pytest
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.money import Money, Period
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError
from ijob_sources.kalibrr import KalibrrBoard, parse_kalibrr
from ijob_sources.polite_http import PoliteClient

_FIXTURES = resources.files("ijob_sources_tests.fixtures.kalibrr")
_PAGE0 = _FIXTURES.joinpath("page0.json").read_bytes()
_PAGE1 = _FIXTURES.joinpath("page1.json").read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_PAGE0)["jobs"]
_VLINK, _HIDDEN, _EMPTY = _JOBS
_KEY = parse_source_key("software-engineer")
_URL = "https://www.kalibrr.com/kjs/job_board/search?limit=15&offset={offset}&text=software%20engineer"


def _page(jobs: list[Any], *, count: object = 1) -> bytes:
    return json.dumps({"count": count, "jobs": jobs}).encode()


def _draft_of(job: dict[str, Any]) -> SightingDraft:
    return parse_kalibrr(_page([job]), _KEY).batch.drafts[0]


def test_parse_maps_fields() -> None:
    page = parse_kalibrr(_PAGE0, _KEY)
    draft = page.batch.drafts[0]

    assert page.count == 20
    assert page.batch.rejected == 0
    assert len(page.batch.drafts) == 3
    assert draft.source == SourceRef(SourceKind.KALIBRR, _KEY)
    assert draft.external_id == ExternalId("274683")
    assert draft.title == "Software Engineer"
    assert draft.company == "VLink Inc"
    assert draft.location == "Central Jakarta"
    assert draft.country == "ID"
    assert (
        draft.url
        == "https://www.kalibrr.com/c/vlink-inc/jobs/274683/software-engineer-2"
    )
    assert draft.posted_at == datetime(2026, 10, 22, 2, 39, 31, 176944, tzinfo=UTC)
    assert json.loads(draft.raw) == _VLINK


def test_salary_maps_to_a_monthly_range() -> None:
    assert _draft_of(_VLINK).salaries == (
        Money(Decimal(10000000), Decimal(11000000), "IDR", Period.MONTH),
    )


def test_salary_shown_respected() -> None:
    assert _HIDDEN["salary_shown"] is False
    assert _HIDDEN["maximum_salary"] > 0

    assert _draft_of(_HIDDEN).salaries == ()


def test_shown_but_empty_has_no_salary() -> None:
    assert _EMPTY["salary_shown"] is True

    assert _draft_of(_EMPTY).salaries == ()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param(
            {"base_salary": None},
            Money(None, Decimal(11000000), "IDR", Period.MONTH),
            id="only-max",
        ),
        pytest.param(
            {"maximum_salary": None},
            Money(Decimal(10000000), None, "IDR", Period.MONTH),
            id="only-min",
        ),
        pytest.param(
            {"salary_interval": None},
            Money(Decimal(10000000), Decimal(11000000), "IDR", None),
            id="no-interval",
        ),
        pytest.param(
            {"salary_interval": "year"},
            Money(Decimal(10000000), Decimal(11000000), "IDR", None),
            id="unmapped-interval",
        ),
        pytest.param(
            {"base_salary": 0},
            Money(None, Decimal(11000000), "IDR", Period.MONTH),
            id="zero-min",
        ),
        pytest.param(
            {"base_salary": 1.5, "maximum_salary": 2.5},
            Money(Decimal("1.5"), Decimal("2.5"), "IDR", Period.MONTH),
            id="fractions",
        ),
    ],
)
def test_salary_bounds_and_period(changes: dict[str, Any], expected: Money) -> None:
    assert _draft_of({**_VLINK, **changes}).salaries == (expected,)


@pytest.mark.parametrize(
    "changes",
    [
        {"salary_currency": None},
        {"base_salary": None, "maximum_salary": None},
        {"base_salary": 0, "maximum_salary": 0},
        {"salary_shown": None},
        {"salary_shown": "yes"},
    ],
    ids=["no-currency", "no-bounds", "zero-bounds", "shown-null", "shown-text"],
)
def test_no_salary_without_currency_and_a_positive_bound(
    changes: dict[str, Any],
) -> None:
    assert _draft_of({**_VLINK, **changes}).salaries == ()


def test_job_without_location_or_date_keeps_the_rest() -> None:
    job = {**_VLINK, "google_location": None, "activation_date": None}
    draft = _draft_of(job)

    assert draft.location is None
    assert draft.country is None
    assert draft.posted_at is None


def test_unknown_country_is_none() -> None:
    comps = {"city": "Nowhere", "country": "Atlantis"}
    draft = _draft_of({**_VLINK, "google_location": {"address_components": comps}})

    assert draft.location == "Nowhere"
    assert draft.country is None


def test_url_parts_are_escaped() -> None:
    job = {**_VLINK, "company": {"code": "a/b?c", "name": "X"}, "slug": "s#1"}

    assert _draft_of(job).url == "https://www.kalibrr.com/c/a%2Fb%3Fc/jobs/274683/s%231"


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        pytest.param({"name": ""}, "id 9", id="empty-title"),
        pytest.param({"name": 5}, "id 9", id="non-text-title"),
        pytest.param({"id": None}, "index 1", id="missing-id"),
        pytest.param({"id": ["x"]}, "index 1", id="list-id"),
        pytest.param({"id": True}, "index 1", id="bool-id"),
        pytest.param({"company_name": None}, "id 9", id="no-company-name"),
        pytest.param({"slug": None}, "id 9", id="no-slug"),
        pytest.param({"company": None}, "id 9", id="no-company"),
        pytest.param({"company": {"name": "X"}}, "id 9", id="no-company-code"),
        pytest.param({"activation_date": "yesterday"}, "id 9", id="unreadable-date"),
        pytest.param({"activation_date": 5}, "id 9", id="non-text-date"),
        pytest.param(
            {"activation_date": "2026-10-22T02:39:31"}, "id 9", id="naive-date"
        ),
        pytest.param({"google_location": "Jakarta"}, "id 9", id="location-not-object"),
        pytest.param(
            {"google_location": {"address_components": {"city": 3}}},
            "id 9",
            id="non-text-city",
        ),
        pytest.param({"name": "\ud800"}, "id 9", id="lone-surrogate"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    overrides: dict[str, Any], label: str, caplog: pytest.LogCaptureFixture
) -> None:
    bad = {**_VLINK, "id": 9, **overrides}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.kalibrr"):
        batch = parse_kalibrr(_page([_HIDDEN, bad]), _KEY).batch

    assert [draft.external_id for draft in batch.drafts] == [str(_HIDDEN["id"])]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"salary_currency": "rupiah"}, id="bad-currency"),
        pytest.param({"salary_currency": ""}, id="empty-currency"),
        pytest.param({"base_salary": "lots"}, id="non-numeric-salary"),
        pytest.param({"base_salary": True}, id="bool-salary"),
        pytest.param({"base_salary": float("nan")}, id="nan-salary"),
        pytest.param({"base_salary": 5, "maximum_salary": 4}, id="min-above-max"),
    ],
)
def test_unbuildable_salary_is_dropped_and_the_job_kept(
    changes: dict[str, Any],
) -> None:
    page = parse_kalibrr(_page([{**_VLINK, **changes}]), _KEY)

    assert [draft.external_id for draft in page.batch.drafts] == ["274683"]
    assert page.batch.rejected == 0
    assert page.batch.drafts[0].salaries == ()


def test_rejected_ids_are_listed_for_jobs_that_have_one() -> None:
    bad = {**_VLINK, "id": 9, "name": ""}
    no_id = {**_VLINK, "id": None}

    page = parse_kalibrr(_page([bad, no_id, _HIDDEN]), _KEY)

    assert page.batch.rejected == 2
    assert page.rejected_ids == (ExternalId("9"),)


def test_non_object_job_is_rejected() -> None:
    batch = parse_kalibrr(_page(["not a job", _VLINK]), _KEY).batch

    assert len(batch.drafts) == 1
    assert batch.rejected == 1


@pytest.mark.parametrize(
    "body",
    [
        b"<html>",
        b"[]",
        b"{}",
        b'{"count": 1, "jobs": {}}',
        b'{"jobs": []}',
        b'{"count": "1", "jobs": []}',
        b'{"count": true, "jobs": []}',
        b"[" * 100_000,
    ],
    ids=[
        "not-json",
        "list",
        "empty-object",
        "jobs-not-list",
        "no-count",
        "count-text",
        "count-bool",
        "too-deep",
    ],
)
def test_unreadable_body_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_kalibrr(body, _KEY)


class _RecordingClient(PoliteClient):
    """A client that notes whether each request asked to be conditional."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        super().__init__(transport=transport, min_interval=0.0)
        self.conditionals: list[bool] = []

    @override
    async def get(self, url: str, *, conditional: bool = False) -> httpx.Response:
        self.conditionals.append(conditional)
        return await super().get(url, conditional=conditional)


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


def _fetch(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[SourceBatch, list[bool]]:
    async def run() -> tuple[SourceBatch, list[bool]]:
        client = _RecordingClient(httpx.MockTransport(handler))
        async with client:
            batch = await KalibrrBoard(_KEY).fetch(client)
        return batch, client.conditionals

    return asyncio.run(run())


def test_ref_is_the_software_engineer_search() -> None:
    assert str(KalibrrBoard(_KEY).ref) == "kalibrr:software-engineer"


def test_repeated_id_dropped() -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = _PAGE1 if request.url.params["offset"] == "15" else _PAGE0
        return httpx.Response(200, content=body)

    batch, _ = _fetch(_serving(answer, requests))

    ids = [draft.external_id for draft in batch.drafts]
    assert ids == ["274683", "275079", "269177", "273527"]
    assert batch.rejected == 0


def test_repeat_within_one_page_is_dropped() -> None:
    def answer(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_page([_VLINK, _VLINK], count=1))

    batch, _ = _fetch(_serving(answer, []))

    assert len(batch.drafts) == 1
    assert batch.rejected == 0


def test_paging_by_offset_and_cap() -> None:
    requests: list[httpx.Request] = []

    def answer(_: httpx.Request) -> httpx.Response:
        job = {**_VLINK, "id": 1000 + len(requests)}
        return httpx.Response(200, content=_page([job], count=1000))

    batch, conditionals = _fetch(_serving(answer, requests))

    assert [str(request.url) for request in requests] == [
        _URL.format(offset=offset) for offset in (0, 15, 30, 45, 60)
    ]
    assert conditionals == [True, False, False, False, False]
    assert len(batch.drafts) == 5


def test_paging_stops_when_the_offset_reaches_the_count() -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = _PAGE1 if request.url.params["offset"] == "15" else _PAGE0
        return httpx.Response(200, content=body)

    _fetch(_serving(answer, requests))

    assert [request.url.params["offset"] for request in requests] == ["0", "15"]


def test_a_single_page_result_makes_one_request() -> None:
    requests: list[httpx.Request] = []

    def answer(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_page([_VLINK], count=15))

    _fetch(_serving(answer, requests))

    assert len(requests) == 1


def test_rejections_are_summed_across_pages() -> None:
    requests: list[httpx.Request] = []

    def answer(_: httpx.Request) -> httpx.Response:
        bad = {**_VLINK, "id": 2000 + len(requests), "name": ""}
        return httpx.Response(200, content=_page([bad], count=20))

    batch, _ = _fetch(_serving(answer, requests))

    assert batch.rejected == 2
    assert batch.drafts == []


def test_repeated_rejected_id_is_counted_once() -> None:
    def answer(_: httpx.Request) -> httpx.Response:
        bad = {**_VLINK, "name": ""}
        return httpx.Response(200, content=_page([bad, bad], count=20))

    batch, _ = _fetch(_serving(answer, []))

    assert batch.rejected == 1
    assert batch.drafts == []


def test_rejected_job_without_an_id_is_counted_every_time() -> None:
    def answer(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_page([{"name": "x"}], count=20))

    batch, _ = _fetch(_serving(answer, []))

    assert batch.rejected == 2


def test_kalibrr_board_satisfies_source_protocol() -> None:
    board = KalibrrBoard(_KEY)

    def answer(request: httpx.Request) -> httpx.Response:
        body = _PAGE1 if request.url.params["offset"] == "15" else _PAGE0
        return httpx.Response(200, content=body)

    async def run() -> CollectResult:
        client = _RecordingClient(httpx.MockTransport(_serving(answer, [])))
        async with client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.KALIBRR, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 4
