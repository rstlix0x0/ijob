import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any, override

import httpx
import pytest
from ijob_core.model import ExternalId, SourceKind, SourceRef, parse_source_key
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError
from ijob_sources.mycareersfuture import (
    MyCareersFutureBoard,
    description_from_raw,
    parse_mycareersfuture,
)
from ijob_sources.polite_http import PoliteClient

_FIXTURES = resources.files("ijob_sources_tests.fixtures.mycareersfuture")
_PAGE0 = _FIXTURES.joinpath("page0.json").read_bytes()
_PAGE1 = _FIXTURES.joinpath("page1.json").read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_PAGE0)["results"]
_NEXT = json.loads(_PAGE0)["_links"]["next"]["href"]
_KEY = parse_source_key("software-engineer")
_FIRST_URL = (
    "https://api.mycareersfuture.gov.sg/v2/jobs"
    "?search=software%20engineer&limit=100&page=0&sortBy=new_posting_date"
)
_API = "https://api.mycareersfuture.gov.sg/v2/jobs?limit=100&page={page}"


def _page(jobs: list[Any], *, next_href: object = None) -> bytes:
    links: dict[str, Any] = {} if next_href is None else {"next": {"href": next_href}}
    return json.dumps({"results": jobs, "_links": links}).encode()


def test_parse_maps_fields() -> None:
    page = parse_mycareersfuture(_PAGE0, _KEY)
    draft = page.batch.drafts[0]

    assert page.batch.rejected == 0
    assert len(page.batch.drafts) == 2
    assert draft.source == SourceRef(SourceKind.MYCAREERSFUTURE, _KEY)
    assert draft.external_id == ExternalId("16a7e73fa1a15e424bdbbbec535bf744")
    assert draft.title == "Junior/Senior Software Engineer"
    assert draft.company == "SCIENTEC CONSULTING PTE. LTD."
    assert draft.country == "SG"
    assert draft.location is None
    assert draft.url == (
        "https://www.mycareersfuture.gov.sg/job/information-technology/"
        "juniorsenior-software-engineer-scientec-consulting-"
        "16a7e73fa1a15e424bdbbbec535bf744"
    )
    assert draft.posted_at == datetime(2026, 10, 31, tzinfo=UTC)
    assert json.loads(draft.raw) == _JOBS[0]


def test_salary_never_mapped() -> None:
    assert _JOBS[0]["salary"]["minimum"] > 0
    assert all(not d.salaries for d in parse_mycareersfuture(_PAGE0, _KEY).batch.drafts)


def test_overseas_has_no_country() -> None:
    job = {**_JOBS[0], "address": {**_JOBS[0]["address"], "isOverseas": True}}

    assert parse_mycareersfuture(_page([job]), _KEY).batch.drafts[0].country is None


@pytest.mark.parametrize(
    "address", [None, {}, {"isOverseas": None}, {"isOverseas": "no"}, "Singapore"]
)
def test_unclear_address_has_no_country(address: object) -> None:
    job = {**_JOBS[0], "address": address}

    assert parse_mycareersfuture(_page([job]), _KEY).batch.drafts[0].country is None


def test_job_without_a_posting_date_has_no_posted_at() -> None:
    job = {**_JOBS[0], "metadata": {**_JOBS[0]["metadata"], "newPostingDate": None}}

    assert parse_mycareersfuture(_page([job]), _KEY).batch.drafts[0].posted_at is None


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        pytest.param({"title": ""}, "id 'x1'", id="empty-title"),
        pytest.param({"title": 5}, "id 'x1'", id="non-text-title"),
        pytest.param({"uuid": None}, "index 1", id="missing-id"),
        pytest.param({"uuid": 7}, "index 1", id="non-text-id"),
        pytest.param({"postedCompany": None}, "id 'x1'", id="no-company"),
        pytest.param({"postedCompany": {"name": 3}}, "id 'x1'", id="non-text-company"),
        pytest.param({"metadata": None}, "id 'x1'", id="no-metadata"),
        pytest.param({"metadata": {}}, "id 'x1'", id="no-url"),
        pytest.param(
            {"metadata": {"jobDetailsUrl": "ftp://example.test/1"}},
            "id 'x1'",
            id="non-http-url",
        ),
        pytest.param(
            {
                "metadata": {
                    "jobDetailsUrl": "https://x.test/1",
                    "newPostingDate": "soon",
                }
            },
            "id 'x1'",
            id="unreadable-date",
        ),
        pytest.param(
            {"metadata": {"jobDetailsUrl": "https://x.test/1", "newPostingDate": 3}},
            "id 'x1'",
            id="non-text-date",
        ),
        pytest.param({"title": "\ud800"}, "id 'x1'", id="lone-surrogate"),
    ],
)
def test_invalid_job_is_rejected_and_others_kept(
    overrides: dict[str, Any], label: str, caplog: pytest.LogCaptureFixture
) -> None:
    bad = {**_JOBS[1], "uuid": "x1", **overrides}
    with caplog.at_level(logging.WARNING, logger="ijob_sources.mycareersfuture"):
        batch = parse_mycareersfuture(_page([_JOBS[0], bad]), _KEY).batch

    assert [draft.external_id for draft in batch.drafts] == [_JOBS[0]["uuid"]]
    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert label in warnings[0].getMessage()


def test_non_object_job_is_rejected() -> None:
    batch = parse_mycareersfuture(_page(["not a job", _JOBS[0]]), _KEY).batch

    assert len(batch.drafts) == 1
    assert batch.rejected == 1


@pytest.mark.parametrize(
    "body",
    [
        b"<html>",
        b"[]",
        b"{}",
        b'{"results": {}}',
        b'{"results": [], "_links": []}',
        b'{"results": [], "_links": {"next": "x"}}',
        b'{"results": [], "_links": {"next": {"href": 5}}}',
        b'{"results": [], "_links": {"next": {}}}',
        b"[" * 100_000,
    ],
    ids=[
        "not-json",
        "list",
        "empty-object",
        "results-not-list",
        "links-not-object",
        "next-not-object",
        "href-not-text",
        "no-href",
        "too-deep",
    ],
)
def test_unreadable_body_raises_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_mycareersfuture(body, _KEY)


@pytest.mark.parametrize(
    "href",
    [
        "http://api.mycareersfuture.gov.sg/v2/jobs?page=1",
        "https://evil.test/v2/jobs?page=1",
        "https://api.mycareersfuture.gov.sg.evil.test/v2/jobs",
        "/v2/jobs?page=1",
    ],
)
def test_next_link_off_the_api_raises_parse_error(href: str) -> None:
    with pytest.raises(ParseError):
        parse_mycareersfuture(_page([], next_href=href), _KEY)


def test_next_href_is_taken_as_given() -> None:
    assert parse_mycareersfuture(_PAGE0, _KEY).next_href == _NEXT
    assert parse_mycareersfuture(_PAGE1, _KEY).next_href is None
    assert parse_mycareersfuture(_page([]), _KEY).next_href is None


class _RecordingClient(PoliteClient):
    """A client that notes whether each request asked to be conditional."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        super().__init__(transport=transport, min_interval=0.0)
        self.conditionals: list[bool] = []

    @override
    async def get(
        self, url: str, *, conditional: bool = False, follow_redirects: bool = False
    ) -> httpx.Response:
        self.conditionals.append(conditional)
        return await super().get(
            url, conditional=conditional, follow_redirects=follow_redirects
        )


def _serving(
    answer: Callable[[httpx.Request], httpx.Response],
    requests: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requests.append(request)
        return answer(request)

    return handler


def _fetch(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[SourceBatch, list[bool]]:
    async def run() -> tuple[SourceBatch, list[bool]]:
        client = _RecordingClient(httpx.MockTransport(handler))
        async with client:
            batch = await MyCareersFutureBoard(_KEY).fetch(client)
        return batch, client.conditionals

    return asyncio.run(run())


def test_ref_is_the_software_engineer_search() -> None:
    assert str(MyCareersFutureBoard(_KEY).ref) == "mycareersfuture:software-engineer"


def test_paging_follows_next_href() -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "1":
            return httpx.Response(200, content=_PAGE1)
        return httpx.Response(200, content=_PAGE0)

    batch, conditionals = _fetch(_serving(answer, requests))

    assert [str(request.url) for request in requests] == [_FIRST_URL, _NEXT]
    assert conditionals == [True, False]
    assert len(batch.drafts) == 4
    assert batch.rejected == 0


def test_paging_capped_at_five() -> None:
    requests: list[httpx.Request] = []

    def answer(_: httpx.Request) -> httpx.Response:
        number = len(requests)
        job = {**_JOBS[0], "uuid": f"job-{number}"}
        return httpx.Response(
            200, content=_page([job], next_href=_API.format(page=number))
        )

    batch, _ = _fetch(_serving(answer, requests))

    assert len(requests) == 5
    assert str(requests[-1].url) == _API.format(page=4)
    assert len(batch.drafts) == 5


def test_rejections_are_summed_across_pages() -> None:
    requests: list[httpx.Request] = []
    bad = {**_JOBS[0], "title": ""}

    def answer(_: httpx.Request) -> httpx.Response:
        if len(requests) == 1:
            return httpx.Response(200, content=_page([bad, _JOBS[0]], next_href=_NEXT))
        return httpx.Response(200, content=_page([bad]))

    batch, _ = _fetch(_serving(answer, requests))

    assert batch.rejected == 2
    assert len(batch.drafts) == 1


def test_mycareersfuture_board_satisfies_source_protocol() -> None:
    board = MyCareersFutureBoard(_KEY)

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "1":
            return httpx.Response(200, content=_PAGE1)
        return httpx.Response(200, content=_PAGE0)

    async def run() -> CollectResult:
        client = _RecordingClient(httpx.MockTransport(_serving(answer, [])))
        async with client:
            return await collect([board], client)

    result = asyncio.run(run())

    assert board.ref == SourceRef(SourceKind.MYCAREERSFUTURE, _KEY)
    assert result.failures == []
    assert len(result.batches[board.ref].drafts) == 4


_EXPECTED_DESCRIPTION = (
    "**Location:** Central (Hybrid)\n"
    "\n"
    "**What You'll Be Doing**\n"
    "\n"
    "- Design, develop, and maintain scalable, high-performance "
    "full-stack applications .\n"
    "- Take ownership of features throughout the entire software "
    "development lifecycle, from requirements gathering and "
    "solution design to implementation, testing, deployment, and "
    "post-production support.\n"
    "- Drive continuous improvements in software engineering "
    "practices, development workflows, CI/CD pipelines, and "
    "automation initiatives.\n"
    "- Conduct code reviews and provide constructive feedback to "
    "ensure high standards of code quality, maintainability, and "
    "performance.\n"
    "- Collaborate with product managers, designers, and "
    "cross-functional teams to deliver innovative and user-centric "
    "solutions.\n"
    "- Support client integrations, troubleshoot technical issues, "
    "and provide timely resolution to production challenges.\n"
    "- Identify opportunities to improve application reliability, "
    "scalability, security, and development efficiency.\n"
    "- Ensure adherence to engineering best practices, coding "
    "standards, Agile methodologies, and established software "
    "delivery processes.\n"
    "\n"
    "**What We're Looking For**\n"
    "\n"
    "- Min. 1-7 years of experience designing, building, and "
    "deploying enterprise-scale full-stack applications.\n"
    "- Hands-on experience with modern front-end frameworks such "
    "as React, Next.js, Angular, or Vue.js.\n"
    "- Strong proficiency in one or more programming languages, "
    "including Golang, Java, Python, or Node.js.\n"
    "- Hands-on experience with cloud platforms such as AWS or "
    "Azure, including deploying and supporting cloud-native "
    "applications.\n"
    "- Familiarity with CI/CD pipelines, DevOps practices, and "
    "modern software deployment methodologies.\n"
    "- Experience working with IAC such as Terraform would be "
    "greatly advantageous"
)


def test_draft_carries_description() -> None:
    draft = parse_mycareersfuture(_PAGE0, _KEY).batch.drafts[0]

    assert draft.description == _EXPECTED_DESCRIPTION


def test_description_from_raw_matches_draft() -> None:
    draft = parse_mycareersfuture(_PAGE0, _KEY).batch.drafts[0]

    assert description_from_raw(draft.raw) == draft.description


@pytest.mark.parametrize(
    "change",
    [{}, {"description": None}, {"description": 5}, {"description": ""}],
    ids=["absent", "null", "non-text", "empty"],
)
def test_description_missing_gives_none(change: dict[str, Any]) -> None:
    job = {k: v for k, v in _JOBS[0].items() if k != "description"} | change
    draft = parse_mycareersfuture(_page([job]), _KEY).batch.drafts[0]

    assert draft.description is None
    assert description_from_raw(draft.raw) is None


@pytest.mark.parametrize("raw", ["<html>", "[]", "null", '"text"'])
def test_description_from_raw_rejects_non_object(raw: str) -> None:
    with pytest.raises(ParseError):
        description_from_raw(raw)
