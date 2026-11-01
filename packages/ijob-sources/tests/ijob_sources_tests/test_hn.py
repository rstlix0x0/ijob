import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_core.model import ExternalId, SourceKind, SourceRef, parse_source_key
from ijob_core.store import RecordResult, Store
from ijob_sources.collect import CollectResult, SourceBatch, collect
from ijob_sources.errors import ParseError, SourceNotFoundError
from ijob_sources.hn import (
    HnWhoIsHiring,
    description_from_raw,
    parse_comments,
    parse_thread,
)
from ijob_sources.polite_http import PoliteClient

_FIXTURES = Path(__file__).parent / "fixtures" / "hn"
_THREADS = (_FIXTURES / "threads.json").read_bytes()
_COMMENTS = (_FIXTURES / "comments.json").read_bytes()
_STORY = 900000002
_KEY = parse_source_key("whoishiring")
_THREADS_URL = (
    "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring"
)
_COMMENTS_URL = (
    f"https://hn.algolia.com/api/v1/search?tags=comment,story_{_STORY}&hitsPerPage=1000"
)


def _comment(text: object, **overrides: object) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "author": "poster9",
        "comment_text": text,
        "created_at": "2026-10-02T09:15:00Z",
        "objectID": "900000900",
        "parent_id": _STORY,
        "story_id": _STORY,
    }
    hit.update(overrides)
    return hit


def _body(*hits: object) -> bytes:
    return json.dumps({"hits": list(hits)}).encode()


def _thread(title: str, object_id: str, created_at_i: int) -> dict[str, Any]:
    return {"title": title, "objectID": object_id, "created_at_i": created_at_i}


def test_thread_selected_by_title() -> None:
    assert parse_thread(_THREADS) == _STORY


def test_newest_hiring_thread_wins_whatever_the_order() -> None:
    older = _thread("Ask HN: Who is hiring? (September 2026)", "11", 100)
    newer = _thread("Ask HN: Who is hiring? (October 2026)", "22", 200)

    assert parse_thread(_body(older, newer)) == 22
    assert parse_thread(_body(newer, older)) == 22


def test_overlong_thread_id_is_skipped_for_a_valid_one() -> None:
    bad = _thread("Ask HN: Who is hiring? (Future)", "9" * 5000, 300)
    good = _thread("Ask HN: Who is hiring? (October 2026)", "22", 200)

    assert parse_thread(_body(bad, good)) == 22


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        b"[" * 100_000,
        b"[]",
        b"null",
        b'{"hits": {}}',
        b'{"hits": []}',
        _body(_thread("Ask HN: Who wants to be hired?", "5", 1)),
        _body("not a hit", {"title": 3}),
        _body(_thread("Ask HN: Who is hiring? (X)", "not-a-number", 1)),
        _body(_thread("Ask HN: Who is hiring? (X)", "-5", 1)),
        _body(_thread("Ask HN: Who is hiring? (X)", "9" * 5000, 1)),
    ],
    ids=[
        "not-json",
        "too-deep",
        "list",
        "null",
        "hits-not-list",
        "no-hits",
        "no-hiring-thread",
        "junk-hits",
        "non-numeric-id",
        "negative-id",
        "overlong-id",
    ],
)
def test_thread_not_found_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_thread(body)


def test_top_level_only() -> None:
    batch = parse_comments(_COMMENTS, _STORY, _KEY)

    assert [draft.external_id for draft in batch.drafts] == [
        "900000101",
        "900000103",
        "900000104",
    ]


def test_fixture_keeps_recorded_types() -> None:
    thread = json.loads(_THREADS)["hits"][1]
    comments = json.loads(_COMMENTS)["hits"]

    assert isinstance(thread["objectID"], str)
    assert all(isinstance(hit["parent_id"], int) for hit in comments)
    assert all(isinstance(hit["story_id"], int) for hit in comments)
    top_level = [hit for hit in comments if hit["parent_id"] == parse_thread(_THREADS)]
    assert top_level
    assert all(hit["parent_id"] != thread["objectID"] for hit in top_level)
    assert len(parse_comments(_COMMENTS, parse_thread(_THREADS), _KEY).drafts) == 3


def test_string_parent_id_is_not_top_level() -> None:
    body = _body(_comment("A | B", parent_id=str(_STORY)))

    assert parse_comments(body, _STORY, _KEY).drafts == []


def test_draft_fields() -> None:
    draft = parse_comments(_COMMENTS, _STORY, _KEY).drafts[0]

    assert draft.source == SourceRef(SourceKind.HN, _KEY)
    assert draft.external_id == ExternalId("900000101")
    assert draft.company == "NeuralFrame"
    assert (
        draft.title == "Senior Backend Engineer, Medical Data Pipeline (Python + Rust)"
    )
    assert draft.location == "REMOTE (US only, US time zones; no visa sponsorship)"
    assert draft.url == "https://news.ycombinator.com/item?id=900000101"
    assert draft.posted_at == datetime(2026, 10, 2, 9, 15, tzinfo=UTC)
    assert json.loads(draft.raw) == json.loads(_COMMENTS)["hits"][0]


def test_first_line_fields() -> None:
    drafts = parse_comments(_COMMENTS, _STORY, _KEY).drafts

    assert [(d.company, d.title, d.location) for d in drafts] == [
        (
            "NeuralFrame",
            "Senior Backend Engineer, Medical Data Pipeline (Python + Rust)",
            "REMOTE (US only, US time zones; no visa sponsorship)",
        ),
        ("Globex Labs", "Platform Engineer", "Tokyo / Osaka"),
        ("Initech", "Staff Engineer", None),
    ]


def test_entities_unescaped_and_escaped_markup_kept() -> None:
    text = "Acme &#x2F; Co | R&amp;D Engineer &lt;3 | Oslo"

    draft = parse_comments(_body(_comment(text)), _STORY, _KEY).drafts[0]

    assert (draft.company, draft.title, draft.location) == (
        "Acme / Co",
        "R&D Engineer <3",
        "Oslo",
    )


def test_fields_beyond_the_third_are_ignored() -> None:
    text = "Acme | Engineer | Rome | REMOTE | VISA"

    assert (
        parse_comments(_body(_comment(text)), _STORY, _KEY).drafts[0].location == "Rome"
    )


def test_empty_location_field_is_none() -> None:
    draft = parse_comments(_body(_comment("Acme | Engineer | ")), _STORY, _KEY).drafts[
        0
    ]

    assert draft.location is None


def test_single_field_rejected(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ijob_sources.hn"):
        batch = parse_comments(_COMMENTS, _STORY, _KEY)

    assert batch.rejected == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "900000105" in warnings[0].getMessage()


@pytest.mark.parametrize(
    "hit",
    [
        _comment(None),
        _comment(42),
        _comment(""),
        _comment("| Engineer | Rome"),
        _comment("Acme | | Rome"),
        _comment("Acme | Engineer", objectID=7),
        _comment("Acme | Engineer", objectID="12 34"),
        _comment("Acme | Engineer", objectID="12\n34"),
        _comment("Acme | Engineer", objectID="9" * 5000),
        _comment("Acme | Engineer", created_at="2026-10-02T09:15:00"),
        _comment("Acme | Engineer", created_at="yesterday"),
        _comment("Acme | Engineer", created_at=None),
        _comment("Acme | Engineer \ud800"),
        {"parent_id": _STORY},
    ],
    ids=[
        "null-text",
        "non-text",
        "empty-text",
        "empty-company",
        "empty-title",
        "non-text-object-id",
        "space-in-object-id",
        "newline-in-object-id",
        "overlong-object-id",
        "naive-created-at",
        "unreadable-created-at",
        "no-created-at",
        "lone-surrogate",
        "bare-hit",
    ],
)
def test_unreadable_comment_is_rejected_and_others_kept(hit: object) -> None:
    good = _comment("Acme | Engineer", objectID="1")

    batch = parse_comments(_body(good, hit), _STORY, _KEY)

    assert [draft.external_id for draft in batch.drafts] == ["1"]
    assert batch.rejected == 1


def test_all_low_confidence() -> None:
    drafts = parse_comments(_COMMENTS, _STORY, _KEY).drafts

    assert drafts
    assert all(draft.low_confidence for draft in drafts)


@pytest.mark.parametrize(
    "body",
    [b"<html></html>", b"[" * 100_000, b"{}", b'{"hits": {}}', b"null"],
    ids=["not-json", "too-deep", "no-hits", "hits-not-list", "null"],
)
def test_bad_comments_body_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_comments(body, _STORY, _KEY)


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _site(
    threads: httpx.Response,
    comments: httpx.Response,
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if requests is not None:
            requests.append(request)
        if request.url.path.endswith("/search_by_date"):
            return threads
        return comments

    return handler


def _fetch(handler: Callable[[httpx.Request], httpx.Response]) -> SourceBatch:
    fake_time = _FakeTime()

    async def run() -> SourceBatch:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
        async with client:
            return await HnWhoIsHiring(_KEY).fetch(client)

    return asyncio.run(run())


def test_fetch_finds_the_thread_then_reads_its_comments() -> None:
    requests: list[httpx.Request] = []
    handler = _site(
        httpx.Response(200, content=_THREADS),
        httpx.Response(200, content=_COMMENTS),
        requests,
    )

    batch = _fetch(handler)

    assert [str(request.url) for request in requests] == [_THREADS_URL, _COMMENTS_URL]
    assert batch == parse_comments(_COMMENTS, _STORY, _KEY)


def test_fetch_without_a_hiring_thread_is_parse_error() -> None:
    handler = _site(httpx.Response(200, content=b'{"hits": []}'), httpx.Response(500))

    with pytest.raises(ParseError):
        _fetch(handler)


def test_fetch_missing_endpoint_is_not_swallowed() -> None:
    handler = _site(httpx.Response(404, text="no"), httpx.Response(200))

    with pytest.raises(SourceNotFoundError):
        _fetch(handler)


def test_hn_source_satisfies_source_protocol() -> None:
    source = HnWhoIsHiring(_KEY)
    fake_time = _FakeTime()
    handler = _site(
        httpx.Response(200, content=_THREADS), httpx.Response(200, content=_COMMENTS)
    )

    async def run() -> CollectResult:
        client = PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
        async with client:
            return await collect([source], client)

    result = asyncio.run(run())

    assert source.ref == SourceRef(SourceKind.HN, _KEY)
    assert result.failures == []
    assert len(result.batches[source.ref].drafts) == 3


_NEURALFRAME_DESCRIPTION = (
    "NeuralFrame | Senior Backend Engineer, Medical Data Pipeline (Python + "
    "Rust) | REMOTE (US only, US time zones; no visa sponsorship) | Full-time "
    "| $170k\N{EN DASH}$210k + 0.1%\N{EN DASH}0.25% equity\n"
    "\n"
    "We build software that hospitals, cancer registries and researchers use. "
    "You'd own the pipeline that pulls data from hospital systems and "
    "transforms it for analytics and research.\n"
    "\n"
    "Where Rust fits: part of our hospital data acquisition layer, the code "
    "that talks to hospital systems, is written in Rust. You'd work on both "
    "sides: Rust where performance and correctness matter most, async Python "
    "for the rest of the pipeline. Rust experience is a strong plus. Whether "
    "you learned it at work or on your own, we want to hear what you built "
    "with it.\n"
    "\n"
    "Stack: Rust, async Python (mypy, pytest, ruff, uv), PostgreSQL and "
    "MongoDB, Dagster, Terraform/OpenTofu, AWS (ECS/Fargate, RDS, Lambda, S3), "
    "GitHub Actions.\n"
    "\n"
    "You'll need 5+ years building backend services or data pipelines in "
    "production (retries, idempotency, schema design and migrations, "
    "well-tested and typed code) and strong Python and SQL. No healthcare "
    "background needed.\n"
    "\n"
    "Apply: "
    "[https://wellfound.com/jobs/4829721-senior-backend-software-e...](https://"
    "wellfound.com/jobs/4829721-senior-backend-software-engineer-medical-data-p"
    "ipeline). In your note, tell us in a few sentences about one pipeline "
    "failure you traced (what broke, how you found it, what you changed) and, "
    "if you've written Rust, what you built with it."
)


def test_draft_carries_description() -> None:
    draft = parse_comments(_COMMENTS, _STORY, _KEY).drafts[0]

    assert draft.description == _NEURALFRAME_DESCRIPTION


def test_description_from_raw_matches_draft() -> None:
    for draft in parse_comments(_COMMENTS, _STORY, _KEY).drafts:
        assert description_from_raw(draft.raw) == draft.description


def test_description_is_read_as_html_with_entities_unescaped_once() -> None:
    text = "Acme | R&amp;D Engineer | Oslo<p>Pay is 5 &lt; 6 &amp;amp; <i>more</i>"

    (draft,) = parse_comments(_body(_comment(text)), _STORY, _KEY).drafts

    assert draft.description == (
        "Acme | R&D Engineer | Oslo\n\nPay is 5 \\< 6 &amp; *more*"
    )


def test_a_lone_surrogate_in_a_comment_does_not_stop_the_batch_being_stored(
    tmp_path: Path,
) -> None:
    good = _comment("Acme | Engineer | Oslo<p>Fine job", objectID="1")
    broken = _comment("Globex | Engineer | Oslo<p>Broken \ud83d emoji", objectID="2")

    batch = parse_comments(_body(good, broken), _STORY, _KEY)
    with Store.open(tmp_path / "ijob.sqlite") as store:
        result = store.record(batch.drafts, seen_at=datetime(2026, 10, 3, tzinfo=UTC))
        postings = store.postings(sources=[SourceRef(SourceKind.HN, _KEY)])

    assert result == RecordResult(new=2, updated=0)
    assert {p.company: p.description for p in postings} == {
        "Acme": "Acme | Engineer | Oslo\n\nFine job",
        "Globex": "Globex | Engineer | Oslo\n\nBroken emoji",
    }


@pytest.mark.parametrize(
    "raw",
    ["{}", '{"comment_text": null}', '{"comment_text": 5}', '{"comment_text": ""}'],
)
def test_description_missing_gives_none(
    raw: str,
) -> None:
    assert description_from_raw(raw) is None


@pytest.mark.parametrize("raw", ["[]", '"text"', "null", "not json"])
def test_description_from_raw_refuses_a_raw_that_is_not_an_object(raw: str) -> None:
    with pytest.raises(ParseError):
        description_from_raw(raw)
