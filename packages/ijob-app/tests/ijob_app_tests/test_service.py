import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from ijob_app.home import IjobHome
from ijob_app.service import (
    MAX_CV_BYTES,
    CacheUnavailableError,
    ClientFactory,
    InvalidCvError,
    Service,
    SourceCompanyError,
    UnknownPostingError,
    UnknownSourceError,
    open_service,
)
from ijob_core.filters import FilterSpec
from ijob_core.model import (
    ExternalId,
    PostingId,
    SightingDraft,
    SourceRef,
    Vote,
    parse_source_ref,
)
from ijob_core.store import Store
from ijob_sources.errors import SourcesError
from ijob_sources.seeds import load_seeds

_FIXTURES = (
    Path(__file__).parents[3]
    / "ijob-sources"
    / "tests"
    / "ijob_sources_tests"
    / "fixtures"
)
_BODY = (_FIXTURES / "greenhouse" / "tailscale_jobs.json").read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_BODY)["jobs"]
_ALLOWING_ROBOTS = "User-agent: *\nDisallow: /embed/\n"
_BOARD_PATH = re.compile(r"/v1/boards/(?P<key>[^/]+)/jobs")
_TAILSCALE = parse_source_ref("greenhouse:tailscale")
_ACME = parse_source_ref("greenhouse:acme")
_SEEN = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
_WORKABLE_JOB: dict[str, object] = json.loads(
    (_FIXTURES / "workable" / "huggingface_job.json").read_bytes()
)
_SHORTCODE = str(_WORKABLE_JOB["shortcode"])
_HUGGINGFACE = parse_source_ref("workable:huggingface")

type _Handler = Callable[[httpx.Request], httpx.Response]
type _ClientFactoryBuilder = Callable[[_Handler], ClientFactory]
type _Opener = Callable[[_Handler], AbstractContextManager[Service]]


def _serving(
    board: bytes = _BODY, *, requests: list[httpx.Request] | None = None
) -> _Handler:
    """Serve robots.txt and every Greenhouse board with `board`; other hosts get 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        if requests is not None:
            requests.append(request)
        if request.url.host != "boards-api.greenhouse.io":
            return httpx.Response(404)
        assert _BOARD_PATH.fullmatch(request.url.path), request.url
        return httpx.Response(200, content=board)

    return handler


@pytest.fixture
def opener(
    tmp_path: Path,
    home: IjobHome,
    clock: Callable[[], datetime],
    client_factory_for: _ClientFactoryBuilder,
) -> _Opener:
    """Open a service over a temporary database that a handler answers for."""

    @contextmanager
    def open_with(handler: _Handler) -> Iterator[Service]:
        factory: ClientFactory = client_factory_for(handler)
        with open_service(home, tmp_path / "ijob.db", factory, now=clock) as service:
            yield service

    return open_with


def test_refresh_records_due_sources(opener: _Opener) -> None:
    with opener(_serving()) as service:
        report = asyncio.run(service.refresh([_TAILSCALE]))
        postings = service.open_postings(FilterSpec(), [_TAILSCALE])

    assert [row.ref for row in report.due] == [_TAILSCALE]
    assert report.fetched == [_TAILSCALE]
    assert report.kept == []
    assert report.failures == []
    assert report.refused == {}
    assert sorted(posting.title for posting in postings) == sorted(
        job["title"] for job in _JOBS
    )


def test_refresh_reports_rejected_jobs(opener: _Opener) -> None:
    board = json.dumps({"jobs": [_JOBS[0], {**_JOBS[1], "title": ""}]}).encode()

    with opener(_serving(board)) as service:
        report = asyncio.run(service.refresh([_TAILSCALE]))

    assert report.rejected == {_TAILSCALE: 1}
    assert report.fetched == [_TAILSCALE]


def test_refresh_keeps_recent_sources(opener: _Opener) -> None:
    requests: list[httpx.Request] = []
    pages = {
        False: (_FIXTURES / "jobicy" / "page1.json").read_bytes(),
        True: (_FIXTURES / "jobicy" / "page2.json").read_bytes(),
    }

    def jobicy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        if request.url.host != "jobicy.com":
            return httpx.Response(404)
        requests.append(request)
        return httpx.Response(200, content=pages["cursor" in request.url.params])

    ref = parse_source_ref("jobicy:engineering")
    with opener(jobicy) as service:
        asyncio.run(service.refresh([ref]))
        requests.clear()
        report = asyncio.run(service.refresh([ref]))

    assert [row.ref for row in report.kept] == [ref]
    assert report.due == []
    assert report.fetched == []
    assert requests == []


def test_refresh_reports_failures(opener: _Opener) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        return httpx.Response(500, text="oops")

    with opener(failing) as service:
        report = asyncio.run(service.refresh([_TAILSCALE]))

    assert [failure.ref for failure in report.failures] == [_TAILSCALE]
    assert report.fetched == []


def test_refresh_unknown_source_raises(opener: _Opener) -> None:
    with opener(_serving()) as service, pytest.raises(UnknownSourceError) as raised:
        asyncio.run(service.refresh([parse_source_ref("greenhouse:nope")]))

    assert raised.value.field == "source"
    assert "greenhouse:nope" in raised.value.reason


def test_open_postings_unknown_source_raises(opener: _Opener) -> None:
    with opener(_serving()) as service, pytest.raises(UnknownSourceError) as raised:
        service.open_postings(FilterSpec(), [parse_source_ref("greenhouse:nope")])

    assert raised.value.field == "source"


def test_open_postings_applies_filters(opener: _Opener) -> None:
    with opener(_serving()) as service:
        asyncio.run(service.refresh([_TAILSCALE]))
        everything = service.open_postings(FilterSpec(), [_TAILSCALE])
        none = service.open_postings(FilterSpec(companies=("Nobody",)), [_TAILSCALE])

    assert everything
    assert none == []


def test_open_service_unusable_cache_raises_before_creating_the_database(
    tmp_path: Path, home: IjobHome, client_factory_for: _ClientFactoryBuilder
) -> None:
    home.root.mkdir()
    (home.root / "cache").write_text("not a directory", encoding="utf-8")
    db = tmp_path / "ijob.db"

    with (
        pytest.raises(CacheUnavailableError) as raised,
        open_service(
            home,
            db,
            client_factory_for(_serving()),
            now=lambda: _SEEN,
        ),
    ):
        pass

    assert raised.value.field == "cache"
    assert raised.value.reason.startswith("cannot open the HTTP cache ")
    assert not db.exists()


def test_open_service_without_cache_leaves_the_cache_file_alone(
    tmp_path: Path, home: IjobHome, client_factory_for: _ClientFactoryBuilder
) -> None:
    with open_service(
        home,
        tmp_path / "ijob.db",
        client_factory_for(_serving()),
        now=lambda: _SEEN,
        use_cache=False,
    ) as service:
        assert service.sources()

    assert not home.cache.exists()


def _draft(
    source: SourceRef,
    external_id: str,
    *,
    title: str = "Engineer",
    raw: str = "{}",
    description: str | None = None,
) -> SightingDraft:
    return SightingDraft(
        source=source,
        external_id=ExternalId(external_id),
        title=title,
        company="Acme",
        location=None,
        url=f"https://jobs.example.test/{external_id}",
        posted_at=None,
        raw=raw,
        description=description,
    )


def _seed(db: Path, *drafts: SightingDraft) -> list[PostingId]:
    """Record drafts in the database at `db` and return the posting ids in order."""
    with Store.open(db) as store:
        store.record(drafts, seen_at=_SEEN)
        return [
            posting.id
            for draft in drafts
            for posting in store.postings(sources=[draft.source])
            if posting.url == draft.url
        ]


def _workable_draft(description: str | None = None) -> SightingDraft:
    return _draft(
        _HUGGINGFACE,
        _SHORTCODE,
        raw=json.dumps({"shortcode": _SHORTCODE}),
        description=description,
    )


def _workable(requests: list[httpx.Request], *, status: int = 200) -> _Handler:
    """Serve robots.txt and the Workable v2 job route, recording the job requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        requests.append(request)
        if request.url.host != "apply.workable.com":
            return httpx.Response(404)
        assert request.url.path == (
            f"/api/v2/accounts/huggingface/jobs/{_SHORTCODE}"
        ), request.url
        if status != 200:
            return httpx.Response(status, text="oops")
        return httpx.Response(200, json=_WORKABLE_JOB)

    return handler


def _stored_description(db: Path, posting_id: PostingId) -> str | None:
    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute(
            "SELECT description FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
    text: str | None = row[0]
    return text


def test_detail_returns_stored_description_without_request(
    opener: _Opener, tmp_path: Path
) -> None:
    [posting_id] = _seed(tmp_path / "ijob.db", _workable_draft("Already stored text."))
    requests: list[httpx.Request] = []

    with opener(_workable(requests)) as service:
        detail = asyncio.run(service.detail(posting_id))

    assert detail.description == "Already stored text."
    assert detail.unavailable_reason is None
    assert detail.sources == [_HUGGINGFACE]
    assert detail.label is None
    assert requests == []


def test_detail_fills_pre_migration_row_from_raw(
    opener: _Opener, tmp_path: Path
) -> None:
    db = tmp_path / "ijob.db"
    requests: list[httpx.Request] = []
    with opener(_serving(requests=requests)) as service:
        asyncio.run(service.refresh([_TAILSCALE]))
        [posting, *_] = service.open_postings(FilterSpec(), [_TAILSCALE])
    expected = posting.description
    assert expected
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE postings SET description = NULL")
    requests.clear()

    with opener(_serving(requests=requests)) as service:
        detail = asyncio.run(service.detail(posting.id))

    assert detail.description == expected
    assert detail.posting.description == expected
    assert _stored_description(db, posting.id) == expected
    assert requests == []


def test_detail_fetches_workable(opener: _Opener, tmp_path: Path) -> None:
    db = tmp_path / "ijob.db"
    [posting_id] = _seed(db, _workable_draft())
    requests: list[httpx.Request] = []

    with opener(_workable(requests)) as service:
        first = asyncio.run(service.detail(posting_id))
        sent = len(requests)
        second = asyncio.run(service.detail(posting_id))

    assert sent == 1
    assert [(r.method, r.url.host) for r in requests] == [("GET", "apply.workable.com")]
    assert first.description is not None
    assert first.description.startswith("At Hugging Face, we're on a journey")
    assert first.unavailable_reason is None
    assert _stored_description(db, posting_id) == first.description
    assert second.description == first.description
    assert len(requests) == sent


def test_detail_failure_is_reported_and_not_stored(
    opener: _Opener, tmp_path: Path
) -> None:
    db = tmp_path / "ijob.db"
    [posting_id] = _seed(db, _workable_draft())
    requests: list[httpx.Request] = []

    with opener(_workable(requests, status=500)) as service:
        first = asyncio.run(service.detail(posting_id))
        sent = len(requests)
        second = asyncio.run(service.detail(posting_id))

    assert first.description is None
    assert first.unavailable_reason is not None
    assert "500" in first.unavailable_reason
    assert _stored_description(db, posting_id) is None
    assert second.unavailable_reason is not None
    assert len(requests) > sent


def test_detail_without_published_description_says_so(
    opener: _Opener, tmp_path: Path
) -> None:
    [posting_id] = _seed(
        tmp_path / "ijob.db",
        _draft(_TAILSCALE, "1", raw=json.dumps({"id": 1, "title": "x"})),
    )

    with opener(_workable([])) as service:
        detail = asyncio.run(service.detail(posting_id))

    assert detail.description is None
    assert detail.unavailable_reason == "no description published"


def test_detail_tries_the_next_sighting_after_a_failure(
    opener: _Opener, tmp_path: Path
) -> None:
    broken = _draft(_TAILSCALE, "1", raw="not json")
    working = replace(
        broken,
        source=parse_source_ref("greenhouse:other"),
        external_id=ExternalId("2"),
        raw=json.dumps({"content": "<p>From the second source.</p>"}),
    )
    [posting_id, again] = _seed(tmp_path / "ijob.db", broken, working)
    assert posting_id == again

    with opener(_workable([])) as service:
        detail = asyncio.run(service.detail(posting_id))

    assert detail.description == "From the second source."
    assert detail.unavailable_reason is None


def test_detail_unknown_posting_raises(opener: _Opener) -> None:
    with opener(_workable([])) as service, pytest.raises(UnknownPostingError) as raised:
        asyncio.run(service.detail(PostingId(404)))

    assert raised.value.field == "posting"
    assert raised.value.reason == "no posting 404"


def test_labels_reads_without_request(opener: _Opener, tmp_path: Path) -> None:
    db = tmp_path / "ijob.db"
    ids = _seed(db, *(_draft(_TAILSCALE, str(n), title=f"Job {n}") for n in range(3)))
    with Store.open(db) as store:
        store.set_label(ids[0], Vote.UP, "fit", at=_SEEN)
        store.set_label(ids[2], Vote.DOWN, None, at=_SEEN)
    requests: list[httpx.Request] = []

    with opener(_workable(requests)) as service:
        labels = service.labels(ids)

    assert set(labels) == {ids[0], ids[2]}
    assert labels[ids[0]].vote is Vote.UP
    assert labels[ids[2]].vote is Vote.DOWN
    assert requests == []


def test_label_sets_label_and_fetches_description(
    opener: _Opener, tmp_path: Path
) -> None:
    db = tmp_path / "ijob.db"
    [posting_id] = _seed(db, _workable_draft())

    with opener(_workable([])) as service:
        detail = asyncio.run(service.label(posting_id, Vote.UP, "fit"))

    assert detail.label is not None
    assert detail.label.vote is Vote.UP
    assert detail.label.note == "fit"
    assert detail.description is not None
    assert _stored_description(db, posting_id) == detail.description


def test_label_unknown_posting_raises(opener: _Opener) -> None:
    with opener(_workable([])) as service, pytest.raises(UnknownPostingError):
        asyncio.run(service.label(PostingId(404), Vote.UP, None))


def test_clear_label(opener: _Opener, tmp_path: Path) -> None:
    [posting_id] = _seed(tmp_path / "ijob.db", _workable_draft("Stored."))

    with opener(_workable([])) as service:
        asyncio.run(service.label(posting_id, Vote.DOWN, None))
        service.clear_label(posting_id)
        detail = asyncio.run(service.detail(posting_id))

    assert detail.label is None


def test_clear_label_unknown_posting_raises(opener: _Opener) -> None:
    with opener(_workable([])) as service, pytest.raises(UnknownPostingError):
        service.clear_label(PostingId(404))


def _pdf(directory: Path, name: str, body: bytes = b"1.4\n%made up\n") -> Path:
    """Write a made-up PDF named `name` under `directory`."""
    path = directory / name
    path.write_bytes(b"%PDF-" + body)
    return path


def test_store_cv_copies_next_to_database(opener: _Opener, tmp_path: Path) -> None:
    source = _pdf(tmp_path, "mine.pdf")

    with opener(_workable([])) as service:
        assert service.cv() is None
        stored = service.store_cv(source)
        read_back = service.cv()

    copy = tmp_path / "cv.pdf"
    assert copy.read_bytes() == source.read_bytes()
    assert read_back == stored
    assert stored.name == "mine.pdf"
    assert stored.size == source.stat().st_size
    assert stored.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert stored.stored_at.utcoffset() is not None
    assert sorted(path.name for path in tmp_path.glob("cv*")) == ["cv.pdf"]


def test_store_cv_replaces_previous(opener: _Opener, tmp_path: Path) -> None:
    first = _pdf(tmp_path, "first.pdf", b"1.4 one")
    second = _pdf(tmp_path, "second.pdf", b"1.7 a longer second body")

    with opener(_workable([])) as service:
        service.store_cv(first)
        stored = service.store_cv(second)
        read_back = service.cv()

    assert (tmp_path / "cv.pdf").read_bytes() == second.read_bytes()
    assert read_back == stored
    assert stored.name == "second.pdf"


def test_store_cv_expands_the_home_directory(
    opener: _Opener, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _pdf(home, "mine.pdf")
    monkeypatch.setenv("HOME", str(home))

    with opener(_workable([])) as service:
        stored = service.store_cv(Path("~/mine.pdf"))

    assert stored.name == "mine.pdf"


def _earlier_cv_survives(
    service: Service, tmp_path: Path, bad: Path, reason: str
) -> None:
    good = _pdf(tmp_path, "good.pdf")
    kept = service.store_cv(good)

    with pytest.raises(InvalidCvError) as raised:
        service.store_cv(bad)

    assert raised.value.field == "cv"
    assert reason in raised.value.reason
    assert service.cv() == kept
    assert (tmp_path / "cv.pdf").read_bytes() == good.read_bytes()


def test_store_cv_rejects_missing_file(opener: _Opener, tmp_path: Path) -> None:
    with opener(_workable([])) as service:
        _earlier_cv_survives(service, tmp_path, tmp_path / "nope.pdf", "cannot read")


def test_store_cv_rejects_non_pdf(opener: _Opener, tmp_path: Path) -> None:
    text = tmp_path / "notes.txt"
    text.write_text('"""Do not store this."""\n', encoding="utf-8")

    with opener(_workable([])) as service:
        _earlier_cv_survives(service, tmp_path, text, "not a PDF")


def test_cv_size_limit_is_twenty_megabytes() -> None:
    assert MAX_CV_BYTES == 20_000_000


def test_store_cv_rejects_oversize(
    opener: _Opener, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ijob_app.service.MAX_CV_BYTES", 64)
    big = _pdf(tmp_path, "big.pdf", b"x" * (64 - len(b"%PDF-") + 1))
    assert big.stat().st_size == 65

    with opener(_workable([])) as service:
        _earlier_cv_survives(service, tmp_path, big, "larger than")


def test_store_cv_accepts_the_size_limit(
    opener: _Opener, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ijob_app.service.MAX_CV_BYTES", 64)
    limit = _pdf(tmp_path, "limit.pdf", b"x" * (64 - len(b"%PDF-")))
    assert limit.stat().st_size == 64

    with opener(_workable([])) as service:
        stored = service.store_cv(limit)

    assert stored.size == 64


def _locked(_store: Store, _cv: object) -> None:
    """Stand in for `Store.set_cv` on a database that refuses the write."""
    reason = "database is locked"
    raise sqlite3.OperationalError(reason)


def test_store_cv_keeps_the_earlier_cv_when_the_record_cannot_be_written(
    opener: _Opener, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _pdf(tmp_path, "first.pdf", b"1.4 one")
    second = _pdf(tmp_path, "second.pdf", b"1.7 a longer second body")

    with opener(_workable([])) as service:
        kept = service.store_cv(first)
        monkeypatch.setattr(Store, "set_cv", _locked)
        with pytest.raises(sqlite3.OperationalError):
            service.store_cv(second)
        monkeypatch.undo()
        assert service.cv() == kept

    assert (tmp_path / "cv.pdf").read_bytes() == first.read_bytes()
    assert sorted(path.name for path in tmp_path.glob("cv*")) == ["cv.pdf"]


def test_store_cv_failed_first_record_leaves_no_file(
    opener: _Opener, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _pdf(tmp_path, "mine.pdf")

    with opener(_workable([])) as service:
        monkeypatch.setattr(Store, "set_cv", _locked)
        with pytest.raises(sqlite3.OperationalError):
            service.store_cv(source)
        monkeypatch.undo()
        assert service.cv() is None

    assert list(tmp_path.glob("cv*")) == []


def test_store_cv_write_failure_is_not_an_invalid_argument(
    opener: _Opener, tmp_path: Path
) -> None:
    source = _pdf(tmp_path, "mine.pdf")
    (tmp_path / "cv.pdf").mkdir()

    with opener(_workable([])) as service:
        with pytest.raises(OSError, match=r"cv\.pdf") as raised:
            service.store_cv(source)
        assert service.cv() is None

    assert not isinstance(raised.value, InvalidCvError)
    assert sorted(path.name for path in tmp_path.glob("cv*")) == ["cv.pdf"]


def test_add_source_verifies_with_one_fetch(opener: _Opener) -> None:
    requests: list[httpx.Request] = []

    with opener(_serving(requests=requests)) as service:
        added = asyncio.run(service.add_source(_ACME, "Tailscale"))
        listed = service.sources()

    assert len(requests) == 1
    assert added.ref == _ACME
    assert added.company == "Tailscale"
    assert _ACME in [row.ref for row in listed]


def test_add_source_refuses_empty_board(opener: _Opener) -> None:
    with opener(_serving(b'{"jobs": []}')) as service:
        with pytest.raises(SourcesError):
            asyncio.run(service.add_source(_ACME, "Tailscale"))
        listed = service.sources()

    assert _ACME not in [row.ref for row in listed]


def test_add_source_refuses_other_company_before_fetching(opener: _Opener) -> None:
    requests: list[httpx.Request] = []

    with (
        opener(_serving(requests=requests)) as service,
        pytest.raises(SourceCompanyError) as raised,
    ):
        asyncio.run(service.add_source(_TAILSCALE, "Ramp"))

    assert raised.value.field == "source"
    assert "greenhouse:tailscale is already a source for" in raised.value.reason
    assert requests == []


def test_remove_source(opener: _Opener) -> None:
    with opener(_serving()) as service:
        service.remove_source(_TAILSCALE)
        listed = service.sources()

    assert _TAILSCALE not in [row.ref for row in listed]


def test_remove_unknown_source_raises(opener: _Opener) -> None:
    with opener(_serving()) as service, pytest.raises(UnknownSourceError) as raised:
        service.remove_source(parse_source_ref("greenhouse:nope"))

    assert raised.value.reason == "unknown source greenhouse:nope"


def test_add_url_resolves_greenhouse(opener: _Opener) -> None:
    requests: list[httpx.Request] = []
    url = "https://job-boards.greenhouse.io/acme?gh_src=x"

    with opener(_serving(requests=requests)) as service:
        added = asyncio.run(service.add_url(url, "Tailscale"))
        listed = service.sources()

    assert added.ref == _ACME
    assert len(requests) == 1
    assert _ACME in [row.ref for row in listed]


def test_refresh_reports_refused_sources(opener: _Opener, tmp_path: Path) -> None:
    refused = parse_source_ref("rss:nosuchfeed")
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds(load_seeds())
        store.add_source(refused, "Nowhere")
    requests: list[httpx.Request] = []

    with opener(_serving(requests=requests)) as service:
        report = asyncio.run(service.refresh([refused]))

    assert [row.ref for row in report.due] == [refused]
    assert list(report.refused) == [refused]
    assert report.fetched == []
    assert report.failures == []
    assert requests == []


def test_refresh_reports_stale_sources(opener: _Opener, tmp_path: Path) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        return httpx.Response(500, text="oops")

    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds(load_seeds())
        store.mark_fetched(_TAILSCALE, _SEEN - timedelta(days=8))
        store.mark_fetched(_ACME, _SEEN - timedelta(days=8))

    with opener(failing) as service:
        report = asyncio.run(service.refresh([_TAILSCALE]))

    assert [row.ref for row in report.stale] == [_TAILSCALE]
    assert [failure.ref for failure in report.failures] == [_TAILSCALE]


def test_refresh_reports_no_stale_source_after_a_good_fetch(opener: _Opener) -> None:
    with opener(_serving()) as service:
        report = asyncio.run(service.refresh([_TAILSCALE]))

    assert report.stale == []


def test_detail_keeps_the_first_error_as_the_reason(
    opener: _Opener, tmp_path: Path
) -> None:
    first = _workable_draft()
    second = replace(
        first,
        source=_TAILSCALE,
        external_id=ExternalId("2"),
        raw="not json",
    )
    [posting_id, again] = _seed(tmp_path / "ijob.db", first, second)
    assert posting_id == again

    with opener(_workable([], status=500)) as service:
        detail = asyncio.run(service.detail(posting_id))

    assert detail.description is None
    assert detail.unavailable_reason is not None
    assert "500" in detail.unavailable_reason
