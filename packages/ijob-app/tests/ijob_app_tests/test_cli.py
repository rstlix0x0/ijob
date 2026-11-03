import itertools
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn, override

import httpx
import pytest
import typer
from ijob_app import cli
from ijob_app.cli import app, make_app
from ijob_app.cli_progress import ProgressLine
from ijob_app.llm_ops import LlmOptions
from ijob_app.service import Service
from ijob_core.countries import parse_country
from ijob_core.model import (
    ExternalId,
    Instructions,
    RemoteScope,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
    parse_source_ref,
)
from ijob_core.money import Money, Period
from ijob_core.store import Store
from ijob_core.traces import TraceStore
from ijob_sources.greenhouse import description_from_raw
from ijob_sources.manual import manual_key
from ijob_sources.polite_http import ConditionalCache, PoliteClient
from ijob_sources.registry import HOST_INTERVALS
from ijob_sources.seeds import load_seeds
from typer.testing import CliRunner, Result

from .llm_fakes import (
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    profile_answer,
    verdict_answer,
    write_cv_pdf,
    write_fake_cli,
    write_llm_config,
)

_FIXTURE = (
    Path(__file__).parents[3]
    / "ijob-sources"
    / "tests"
    / "ijob_sources_tests"
    / "fixtures"
    / "greenhouse"
    / "tailscale_jobs.json"
)
_FIXTURES = _FIXTURE.parents[1]
_BODY = _FIXTURE.read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_BODY)["jobs"]
_ALLOWING_ROBOTS = "User-agent: *\nDisallow: /embed/\n"
_BOARD_PATH = re.compile(r"/v1/boards/(?P<key>[^/]+)/jobs")
_GREENHOUSE_HOST = "boards-api.greenhouse.io"
_T0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
_TAILSCALE = ("--source", "greenhouse:tailscale")

type _Handler = Callable[[httpx.Request], httpx.Response]


class _FakeTime:
    """A monotonic clock and a sleep that advances it, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _serving(
    boards: dict[str, httpx.Response | Exception] | None = None,
    *,
    robots: str = _ALLOWING_ROBOTS,
    requests: list[httpx.Request] | None = None,
) -> _Handler:
    """Serve robots.txt and each Greenhouse board by key.

    A board not listed gets the fixture; any other host gets a 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if requests is not None:
            requests.append(request)
        if request.url.host != _GREENHOUSE_HOST:
            return httpx.Response(404)
        match = _BOARD_PATH.fullmatch(request.url.path)
        assert match is not None, request.url
        answer = (boards or {}).get(match["key"])
        if isinstance(answer, Exception):
            raise answer
        return answer or httpx.Response(200, content=_BODY)

    return handler


def _factory(
    handler: _Handler,
) -> Callable[[ConditionalCache | None], PoliteClient]:
    fake_time = _FakeTime()

    def client_factory(cache: ConditionalCache | None) -> PoliteClient:
        return PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
            cache=cache,
        )

    return client_factory


async def _public(host: str, port: int) -> Sequence[str]:
    """Resolve every host to one public address, so no test asks the network."""
    del host, port
    return ["93.184.216.34"]


def _home(tmp_path: Path) -> Path:
    """The ijob home the tests use, under `tmp_path`."""
    return tmp_path / "home"


def _env(tmp_path: Path) -> dict[str, str]:
    """Create an ijob home under `tmp_path` and point `IJOB_HOME` at it.

    No real user data leaks in, and a command finds the home it requires.
    """
    _home(tmp_path).mkdir(exist_ok=True)
    return {"IJOB_HOME": str(_home(tmp_path))}


def _ticking(start: datetime) -> Callable[[], datetime]:
    """A clock that returns a later time on every call, from `start` on."""
    ticks = itertools.count()
    return lambda: start + timedelta(microseconds=next(ticks))


def _search(
    handler: _Handler,
    tmp_path: Path,
    *args: str,
    now: datetime = _T0,
    db: Path | None = None,
) -> Result:
    """Run `ijob search <args>` against a `tmp_path` database from the time `now`.

    The clock ticks on every call, so two reads of it never agree.
    """
    app: typer.Typer = make_app(_factory(handler), now=_ticking(now))
    argv = ["search", *args, "--db", str(db or tmp_path / "ijob.db")]
    return CliRunner().invoke(app, argv, env=_env(tmp_path))


def _stored(db: Path, key: str = "tailscale") -> int:
    ref = SourceRef(SourceKind.GREENHOUSE, parse_source_key(key))
    with Store.open(db) as store:
        return len(store.postings(sources=[ref]))


def _add_source(tmp_path: Path, ref: str, company: str) -> None:
    """Put a user source row into the `tmp_path` database."""
    with Store.open(tmp_path / "ijob.db") as store:
        store.add_source(parse_source_ref(ref), company)


def _ids(tmp_path: Path) -> dict[str, int]:
    """Map the URL of every stored posting in the `tmp_path` database to its id."""
    with Store.open(tmp_path / "ijob.db") as store:
        refs = [row.ref for row in store.sources()]
        return {posting.url: posting.id for posting in store.postings(sources=refs)}


def _line(
    job: dict[str, Any], ids: dict[str, int], sources: str = "greenhouse:tailscale"
) -> str:
    """The `search` line for a job of the Tailscale fixture, given the stored ids."""
    return (
        f"{ids[job['absolute_url']]}\t{job['title']}\t{job['company_name']}"
        f"\t{job['location']['name']}\t{sources}\t{job['absolute_url']}"
    )


def _titles(result: Result) -> list[str]:
    return sorted(line.split("\t")[1] for line in result.stdout.splitlines())


def test_search_prints_postings(tmp_path: Path) -> None:
    result = _search(_serving(), tmp_path, *_TAILSCALE)

    assert result.exit_code == 0
    assert result.stderr == ""
    ids = _ids(tmp_path)
    assert sorted(result.stdout.splitlines()) == sorted(
        _line(job, ids) for job in _JOBS
    )


def test_search_lines_start_with_posting_id(tmp_path: Path) -> None:
    result = _search(_serving(), tmp_path, *_TAILSCALE)

    stored = set(_ids(tmp_path).values())
    firsts = [line.split("\t")[0] for line in result.stdout.splitlines()]
    assert result.exit_code == 0
    assert len(firsts) == len(_JOBS)
    assert all(first.isdecimal() for first in firsts)
    assert {int(first) for first in firsts} == stored


def test_one_stamp_marks_the_fetch_and_the_sightings(tmp_path: Path) -> None:
    db = tmp_path / "ijob.db"
    ref = parse_source_ref("greenhouse:tailscale")

    result = _search(_serving(), tmp_path, *_TAILSCALE)

    assert result.exit_code == 0
    assert len(result.stdout.splitlines()) == len(_JOBS)
    with Store.open(db) as store:
        row = store.source(ref)
        assert row is not None
        seen = {
            sighting.last_seen
            for job in _JOBS
            if (sighting := store.sighting(ref, ExternalId(str(job["id"]))))
        }
    assert len(seen) == 1
    assert row.last_fetched_at in seen


def test_absent_location_prints_a_dash(tmp_path: Path) -> None:
    job = {k: v for k, v in _JOBS[0].items() if k != "location"}
    body = json.dumps({"jobs": [job]}).encode()
    handler = _serving({"tailscale": httpx.Response(200, content=body)})

    result = _search(handler, tmp_path, *_TAILSCALE)

    [posting_id] = _ids(tmp_path).values()
    assert result.stdout.splitlines() == [
        (
            f"{posting_id}\t{job['title']}\t{job['company_name']}\t-"
            f"\tgreenhouse:tailscale\t{job['absolute_url']}"
        )
    ]


def test_second_search_adds_no_postings(tmp_path: Path) -> None:
    db = tmp_path / "ijob.db"

    first = _search(_serving(), tmp_path, *_TAILSCALE)
    after_first = _stored(db)
    second = _search(_serving(), tmp_path, *_TAILSCALE)

    assert first.exit_code == second.exit_code == 0
    assert after_first == len(_JOBS)
    assert _stored(db) == after_first
    assert sorted(second.stdout.splitlines()) == sorted(first.stdout.splitlines())
    assert len(second.stdout.splitlines()) == len(_JOBS)


def test_robots_blocked_board_reported(tmp_path: Path) -> None:
    handler = _serving(robots="User-agent: *\nDisallow: /v1/\n")

    result = _search(handler, tmp_path, *_TAILSCALE)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "skipped greenhouse:tailscale: disallowed by robots.txt"
    ]


def test_unknown_board_reported_not_found(tmp_path: Path) -> None:
    _add_source(tmp_path, "greenhouse:nosuch", "Nosuch")
    handler = _serving({"nosuch": httpx.Response(404, text="{}")})

    result = _search(handler, tmp_path, "--source", "greenhouse:nosuch", *_TAILSCALE)

    assert result.exit_code == 0
    assert result.stderr.splitlines() == ["not found: greenhouse:nosuch"]
    assert len(result.stdout.splitlines()) == len(_JOBS)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            httpx.Response(500, text="oops"),
            "failed greenhouse:tailscale: HTTP 500",
            id="http-500",
        ),
        pytest.param(
            httpx.ConnectError("no route to host"),
            "failed greenhouse:tailscale: no route to host",
            id="connect-error",
        ),
    ],
)
def test_http_and_fetch_failures_reported(
    tmp_path: Path, answer: httpx.Response | Exception, expected: str
) -> None:
    handler = _serving({"tailscale": answer})

    result = _search(handler, tmp_path, *_TAILSCALE)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [expected]


def test_unreachable_host_reported_as_unreadable_robots_txt(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        msg = "no route to host"
        raise httpx.ConnectError(msg)

    result = _search(handler, tmp_path, *_TAILSCALE)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "failed greenhouse:tailscale: robots.txt could not be read: no route to host"
    ]


def test_parse_failure_reported(tmp_path: Path) -> None:
    handler = _serving({"tailscale": httpx.Response(200, text="<html>no</html>")})

    result = _search(handler, tmp_path, *_TAILSCALE)

    assert result.exit_code == 1
    [line] = result.stderr.splitlines()
    assert line.startswith("failed greenhouse:tailscale: body is not JSON: ")


def test_rejected_jobs_reported(tmp_path: Path) -> None:
    body = json.dumps({"jobs": [_JOBS[0], {**_JOBS[1], "title": ""}]}).encode()
    handler = _serving({"tailscale": httpx.Response(200, content=body)})

    result = _search(handler, tmp_path, *_TAILSCALE)

    assert result.exit_code == 0
    assert "greenhouse:tailscale: skipped 1 invalid jobs" in result.stderr.splitlines()
    assert len(result.stdout.splitlines()) == 1


def _rejecting_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str
) -> Result:
    """Search a board holding one invalid job, with logging as a terminal run has it.

    pytest keeps capture handlers on the root logger while a test runs, which
    would stop Python's last-resort stderr handler from ever firing, so they are
    taken off here, inside the test, after pytest has put them on.
    """
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    body = json.dumps({"jobs": [_JOBS[0], {**_JOBS[1], "title": ""}]}).encode()
    handler = _serving({"tailscale": httpx.Response(200, content=body)})
    return _search(handler, tmp_path, *_TAILSCALE, *args)


def test_rejected_item_lines_stay_off_stderr_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _rejecting_run(tmp_path, monkeypatch)

    assert result.exit_code == 0
    assert result.stderr.splitlines() == [
        "greenhouse:tailscale: skipped 1 invalid jobs",
        "note: 1 skipped jobs unread; run: ijob sources extract",
    ]


def test_verbose_prints_each_rejected_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _rejecting_run(tmp_path, monkeypatch, "--verbose")

    assert result.exit_code == 0
    lines = result.stderr.splitlines()
    assert "greenhouse:tailscale: skipped 1 invalid jobs" in lines
    (rejected,) = (line for line in lines if line.startswith("rejected "))
    assert rejected.startswith("rejected greenhouse job ")
    assert rejected.endswith("invalid title: is empty")
    assert len(result.stdout.splitlines()) == 1


def test_verbose_handler_is_removed_after_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources_logger = logging.getLogger("ijob_sources")

    _rejecting_run(tmp_path, monkeypatch, "--verbose")
    handlers_after_verbose = list(sources_logger.handlers)
    (tmp_path / "again").mkdir()
    quiet = _rejecting_run(tmp_path / "again", monkeypatch)

    assert all(
        isinstance(handler, logging.NullHandler) for handler in handlers_after_verbose
    )
    assert "rejected " not in quiet.stderr


def test_any_command_silences_the_source_loggers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources_logger = logging.getLogger("ijob_sources")
    monkeypatch.setattr(sources_logger, "handlers", [])
    app_: typer.Typer = make_app(_factory(_serving()))

    for _ in range(2):
        result = CliRunner().invoke(
            app_,
            ["sources", "list", "--db", str(tmp_path / "ijob.db")],
            env=_env(tmp_path),
        )
        assert result.exit_code == 0

    assert [type(h) for h in sources_logger.handlers] == [logging.NullHandler]


def test_identity_failure_line(tmp_path: Path) -> None:
    _add_source(tmp_path, "greenhouse:tailscale", "Ramp")

    result = _search(_serving(), tmp_path, *_TAILSCALE)

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        (
            "skipped greenhouse:tailscale: identity check failed: "
            "company 'Ramp' not found on the board"
        )
    ]
    assert result.stdout == ""
    assert _stored(tmp_path / "ijob.db") == 0


def test_bad_source_ref_exits_2(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _search(
        _serving(requests=requests), tmp_path, "--source", "greenhouse:a/b"
    )

    assert result.exit_code == 2
    assert "'a/b'" in result.stderr
    assert result.stdout == ""
    assert requests == []
    assert not (tmp_path / "ijob.db").exists()


def test_bad_source_among_good_ones_exits_2_without_fetching(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _search(
        _serving(requests=requests),
        tmp_path,
        *_TAILSCALE,
        "--source",
        "greenhouse:a/b",
    )

    assert result.exit_code == 2
    assert requests == []
    assert not (tmp_path / "ijob.db").exists()


def test_repeated_source_fetched_once(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _search(_serving(requests=requests), tmp_path, *_TAILSCALE, *_TAILSCALE)

    assert result.exit_code == 0
    assert len(requests) == 1
    assert len(result.stdout.splitlines()) == len(_JOBS)


def test_failures_are_reported_in_flag_order(tmp_path: Path) -> None:
    gone = httpx.Response(404, text="{}")
    handler = _serving({"bbb": gone, "aaa": gone, "ccc": gone})
    for key in ("aaa", "bbb", "ccc"):
        _add_source(tmp_path, f"greenhouse:{key}", key)

    result = _search(
        handler,
        tmp_path,
        "--source",
        "greenhouse:bbb",
        "--source",
        "greenhouse:aaa",
        "--source",
        "greenhouse:ccc",
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "not found: greenhouse:bbb",
        "not found: greenhouse:aaa",
        "not found: greenhouse:ccc",
    ]


def test_failed_source_keeps_last_known_postings(tmp_path: Path) -> None:
    db = tmp_path / "ijob.db"
    first = _search(_serving(), tmp_path, *_TAILSCALE)

    result = _search(
        _serving({"tailscale": httpx.Response(500)}),
        tmp_path,
        *_TAILSCALE,
        now=_T0 + timedelta(minutes=5),
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines() == ["failed greenhouse:tailscale: HTTP 500"]
    assert sorted(result.stdout.splitlines()) == sorted(first.stdout.splitlines())
    assert _stored(db) == len(_JOBS)


def test_unusable_db_fails_before_any_request(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    seen: list[httpx.Request] = []
    serve = _serving()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return serve(request)

    result = _search(handler, tmp_path, *_TAILSCALE, db=blocker / "ijob.db")

    assert result.exit_code != 0
    assert seen == []


_SEARCH_OPTIONS = (
    "--source",
    "--country",
    "--remote",
    "--since",
    "--company",
    "--min-salary",
    "--require-salary",
    "--db",
    "--config",
)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Drop the colour codes help output carries when a CI terminal is detected."""
    return _ANSI_ESCAPE.sub("", text)


def test_production_app_shows_help() -> None:
    result = CliRunner().invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    text = _plain(result.stdout)
    for option in _SEARCH_OPTIONS:
        assert option in text
    assert "--greenhouse" not in text


def test_help_lists_the_options() -> None:
    app = make_app(_factory(_serving()))

    result = CliRunner().invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    text = _plain(result.stdout)
    for option in _SEARCH_OPTIONS:
        assert option in text
    assert "--greenhouse" not in text


_JAZZHR_PAGE = (_FIXTURE.parents[1] / "jsonld" / "jazzhr.html").read_bytes()
_PAGE_URL = "https://careers.example.test/jobs/release-engineer"
_LEVER_PATH = re.compile(r"/v0/postings/(?P<key>[^/]+)")


def _web(
    *,
    robots: str = _ALLOWING_ROBOTS,
    page: bytes = _JAZZHR_PAGE,
    greenhouse: bytes = _BODY,
    lever: bytes = b"[]",
    requests: list[httpx.Request] | None = None,
) -> _Handler:
    """Serve robots.txt, a Greenhouse board, a Lever site and one careers page."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if requests is not None:
            requests.append(request)
        host = request.url.host
        if host == "boards-api.greenhouse.io":
            return httpx.Response(200, content=greenhouse)
        if host == "api.lever.co":
            assert _LEVER_PATH.match(request.url.path), request.url
            return httpx.Response(200, content=lever)
        if host == "careers.example.test":
            return httpx.Response(200, content=page)
        return httpx.Response(404)

    return handler


def _run(handler: _Handler, tmp_path: Path, *args: str, db: bool = True) -> Result:
    """Run `ijob <args>` against a `tmp_path` database with the ijob home isolated."""
    app: typer.Typer = make_app(_factory(handler), resolver=_public)
    argv = [*args, "--db", str(tmp_path / "ijob.db")] if db else list(args)
    return CliRunner().invoke(app, argv, env=_env(tmp_path))


def test_sources_list_shows_seeds(tmp_path: Path) -> None:
    result = _run(_web(), tmp_path, "sources", "list")

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 168
    assert "himalayas:all\t-\tseed\t-" in lines


def test_sources_add_verifies_identity(tmp_path: Path) -> None:
    added = _run(
        _web(),
        tmp_path,
        "sources",
        "add",
        "greenhouse:tailscale2",
        "--company",
        "Tailscale",
    )
    refused = _run(
        _web(), tmp_path, "sources", "add", "greenhouse:tailscale3", "--company", "Ramp"
    )

    assert added.exit_code == 0
    assert added.stdout == "added greenhouse:tailscale2\n"
    assert refused.exit_code == 1
    assert "company 'Ramp' not found on the board" in refused.stderr
    assert "Traceback" not in refused.stderr
    listing = _run(_web(), tmp_path, "sources", "list").stdout
    assert "greenhouse:tailscale2\tTailscale\tuser\t-" in listing.splitlines()
    assert "greenhouse:tailscale3" not in listing


def test_sources_add_refuses_empty_board(tmp_path: Path) -> None:
    result = _run(
        _web(lever=b"[]"),
        tmp_path,
        "sources",
        "add",
        "lever:ghost",
        "--company",
        "Ghost",
    )

    assert result.exit_code == 1
    assert "no jobs; cannot verify the company" in result.stderr
    assert "lever:ghost" not in _run(_web(), tmp_path, "sources", "list").stdout


def test_sources_add_fetch_failure_exits_1(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        return httpx.Response(404)

    result = _run(
        handler, tmp_path, "sources", "add", "lever:nosuch", "--company", "No"
    )

    assert result.exit_code == 1
    assert "404" in result.stderr


def test_sources_remove_then_list(tmp_path: Path) -> None:
    removed = _run(_web(), tmp_path, "sources", "remove", "himalayas:all")
    listing = _run(_web(), tmp_path, "sources", "list")
    again = _run(_web(), tmp_path, "sources", "remove", "himalayas:all")

    assert removed.exit_code == 0
    assert removed.stdout == "removed himalayas:all\n"
    assert len(listing.stdout.splitlines()) == 167
    assert not any(
        line.startswith("himalayas:all\t") for line in listing.stdout.splitlines()
    )
    assert again.exit_code == 2


def test_sources_remove_unknown_exits_2(tmp_path: Path) -> None:
    result = _run(_web(), tmp_path, "sources", "remove", "greenhouse:nosuch")

    assert result.exit_code == 2
    assert "unknown source greenhouse:nosuch" in result.stderr


def test_sources_bad_ref_exits_2(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _run(
        _web(requests=requests),
        tmp_path,
        "sources",
        "add",
        "greenhouse:a/b",
        "--company",
        "A",
    )
    removal = _run(_web(), tmp_path, "sources", "remove", "nonsense")

    assert result.exit_code == 2
    assert "'a/b'" in result.stderr
    assert requests == []
    assert removal.exit_code == 2
    assert not (tmp_path / "ijob.db").exists()


def test_sources_add_manual_kind_points_to_add_url(tmp_path: Path) -> None:
    result = _run(
        _web(), tmp_path, "sources", "add", "jobposting:abc", "--company", "Acme"
    )

    assert result.exit_code == 2
    assert "target_url" in result.stderr


def test_sources_add_blank_company_exits_2(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _run(
        _web(requests=requests),
        tmp_path,
        "sources",
        "add",
        "greenhouse:tailscale",
        "--company",
        "  ",
    )

    assert result.exit_code == 2
    assert requests == []


def test_production_client_paces_workable_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, object]] = []

    def record(**kwargs: object) -> str:
        built.append(kwargs)
        return "client"

    monkeypatch.setattr(cli, "PoliteClient", record)

    cli._production_client(None)

    [kwargs] = built
    intervals = kwargs["host_intervals"]
    assert isinstance(intervals, Mapping)
    assert dict(intervals) == dict(HOST_INTERVALS)
    assert intervals["apply.workable.com"] >= 3.0


def test_unusable_cache_exits_1_without_opening_the_db(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    env = _blocked_cache_env(tmp_path)
    app: typer.Typer = make_app(_factory(_serving(requests=requests)), now=lambda: _T0)

    result = CliRunner().invoke(
        app, ["search", *_TAILSCALE, "--db", str(tmp_path / "ijob.db")], env=env
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    [line] = result.stderr.splitlines()
    cache_path = _home(tmp_path) / "cache" / "http.sqlite"
    assert line.startswith(f"error: cannot open the HTTP cache {cache_path}: ")
    assert "Traceback" not in result.stderr
    assert requests == []
    assert not (tmp_path / "ijob.db").exists()


@pytest.mark.parametrize("removed", [False, True], ids=["active", "removed"])
@pytest.mark.parametrize(
    ("command", "target"),
    [
        pytest.param("add", "greenhouse:stripe", id="add"),
        pytest.param(
            "add-url", "https://job-boards.greenhouse.io/stripe?gh_src=x", id="add-url"
        ),
    ],
)
def test_sources_add_of_another_companys_ref_exits_2(
    tmp_path: Path, command: str, target: str, *, removed: bool
) -> None:
    requests: list[httpx.Request] = []
    if removed:
        _run(_web(), tmp_path, "sources", "remove", "greenhouse:stripe")

    result = _run(
        _web(requests=requests),
        tmp_path,
        "sources",
        command,
        target,
        "--company",
        "Tailscale",
    )

    assert result.exit_code == 2
    assert result.stderr.splitlines() == [
        "error: greenhouse:stripe is already a source for Stripe"
    ]
    assert result.stdout == ""
    assert requests == []
    with Store.open(tmp_path / "ijob.db") as store:
        row = store.source(parse_source_ref("greenhouse:stripe"))
    assert row is not None
    assert row.company == "Stripe"
    assert row.removed is removed


@pytest.mark.parametrize(
    ("command", "target"),
    [
        pytest.param("add", "greenhouse:tailscale", id="add"),
        pytest.param(
            "add-url",
            "https://job-boards.greenhouse.io/tailscale?gh_src=x",
            id="add-url",
        ),
    ],
)
def test_sources_add_of_the_same_company_revives_a_removed_source(
    tmp_path: Path, command: str, target: str
) -> None:
    _run(_web(), tmp_path, "sources", "remove", "greenhouse:tailscale")

    result = _run(
        _web(),
        tmp_path,
        "sources",
        command,
        target,
        "--company",
        "TAILSCALE, Inc.",
    )

    assert result.exit_code == 0
    assert result.stdout == "added greenhouse:tailscale\n"
    listing = _run(_web(), tmp_path, "sources", "list").stdout.splitlines()
    assert "greenhouse:tailscale\tTailscale\tseed\t-" in listing


def test_sources_add_url_jobposting(tmp_path: Path) -> None:
    result = _run(
        _web(),
        tmp_path,
        "sources",
        "add-url",
        _PAGE_URL,
        "--company",
        "Eclipse Foundation",
    )

    ref = f"jobposting:{manual_key(_PAGE_URL)}"
    assert result.exit_code == 0
    assert result.stdout == f"added {ref}\n"
    listing = _run(_web(), tmp_path, "sources", "list").stdout.splitlines()
    assert any(
        line.startswith(f"{ref}\tEclipse Foundation\tuser\t") for line in listing
    )
    with Store.open(tmp_path / "ijob.db") as store:
        [row] = [r for r in store.sources() if str(r.ref) == ref]
    assert row.target_url == _PAGE_URL


def test_sources_add_url_hiring_system_is_verified(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _run(
        _web(requests=requests),
        tmp_path,
        "sources",
        "add-url",
        "https://job-boards.greenhouse.io/tailscale2?gh_src=x",
        "--company",
        "Tailscale",
    )

    assert result.exit_code == 0
    assert result.stdout == "added greenhouse:tailscale2\n"
    assert [r.url.host for r in requests] == ["boards-api.greenhouse.io"]


def test_sources_add_url_hiring_system_identity_refused(tmp_path: Path) -> None:
    result = _run(
        _web(),
        tmp_path,
        "sources",
        "add-url",
        "https://job-boards.greenhouse.io/tailscale2",
        "--company",
        "Ramp",
    )

    assert result.exit_code == 1
    assert "company 'Ramp' not found on the board" in result.stderr


def test_sources_add_url_unsupported(tmp_path: Path) -> None:
    result = _run(
        _web(page=b"<html><body><p>Hello</p></body></html>"),
        tmp_path,
        "sources",
        "add-url",
        _PAGE_URL,
        "--company",
        "Acme",
    )

    assert result.exit_code == 1
    assert "no hiring system, job posting or feed found" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "url", ["foo", "", "https://", "//a.example/x", "ftp://a.example/x"]
)
def test_sources_add_url_of_no_web_address_names_the_url(
    tmp_path: Path, url: str
) -> None:
    requests: list[httpx.Request] = []

    result = _run(
        _web(requests=requests),
        tmp_path,
        "sources",
        "add-url",
        url,
        "--company",
        "Acme",
    )

    assert result.exit_code == 1
    assert result.stderr == f"error: cannot use {url}: not an http or https address\n"
    assert requests == []


def test_sources_add_url_robots_blocked(tmp_path: Path) -> None:
    result = _run(
        _web(robots="User-agent: *\nDisallow: /jobs/\n"),
        tmp_path,
        "sources",
        "add-url",
        _PAGE_URL,
        "--company",
        "Eclipse Foundation",
    )

    assert result.exit_code == 1
    assert "robots.txt disallows" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_sources_help_lists_the_commands() -> None:
    result = CliRunner().invoke(app, ["sources", "--help"])

    assert result.exit_code == 0
    for command in ("list", "add", "remove", "add-url"):
        assert command in result.stdout


def _lever_job(
    title: str,
    *,
    country: str | None = None,
    workplace: str | None = None,
) -> dict[str, Any]:
    slug = title.lower().replace(" ", "-")
    job: dict[str, Any] = {
        "id": slug,
        "text": title,
        "categories": {"location": "Somewhere"},
        "descriptionPlain": "Acme builds things.",
        "hostedUrl": f"https://jobs.lever.co/acme/{slug}",
    }
    if country is not None:
        job["country"] = country
    if workplace is not None:
        job["workplaceType"] = workplace
    return job


_ACME_JOBS = [
    _lever_job("Backend Engineer", country="US", workplace="onsite"),
    _lever_job("Platform Engineer"),
    _lever_job("Staff Engineer", country="DE", workplace="onsite"),
    _lever_job("Data Engineer", country="DE", workplace="onsite"),
]


def _lever_only(jobs: list[dict[str, Any]]) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        if request.url.host == "api.lever.co":
            return httpx.Response(200, content=json.dumps(jobs).encode())
        return httpx.Response(404)

    return handler


@pytest.mark.parametrize(
    ("args", "config"),
    [
        pytest.param(["--since", "7x"], None, id="bad-since"),
        pytest.param(["--country", "Atlantis"], None, id="bad-country"),
        pytest.param(["--remote", "sometimes"], None, id="bad-remote"),
        pytest.param(["--company", "  "], None, id="blank-company"),
        pytest.param(["--min-salary", "5"], None, id="bad-min-salary"),
        pytest.param(["--config", "/nonexistent/ijob.toml"], None, id="missing-config"),
        pytest.param([], '[filters]\nsince = "7x"\n', id="bad-config-since"),
    ],
)
def test_bad_flag_or_config_sends_no_request(
    tmp_path: Path, args: list[str], config: str | None
) -> None:
    requests: list[httpx.Request] = []
    if config is not None:
        path = tmp_path / "bad.toml"
        path.write_text(config, encoding="utf-8")
        args = [*args, "--config", str(path)]

    result = _search(_serving(requests=requests), tmp_path, *_TAILSCALE, *args)

    assert result.exit_code == 2
    assert result.stderr != ""
    assert result.stdout == ""
    assert requests == []
    assert not (tmp_path / "ijob.db").exists()


def test_unknown_source_exits_2_with_hint(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    handler = _serving(requests=requests)

    unknown = _search(handler, tmp_path, "--source", "greenhouse:nosuch")
    _run(handler, tmp_path, "sources", "remove", "greenhouse:tailscale")
    removed = _search(handler, tmp_path, *_TAILSCALE)

    assert unknown.exit_code == 2
    assert unknown.stderr.splitlines() == [
        (
            "error: unknown source greenhouse:nosuch; "
            "add it with: ijob sources add greenhouse:nosuch --company NAME"
        )
    ]
    assert removed.exit_code == 2
    assert "unknown source greenhouse:tailscale" in removed.stderr
    assert requests == []


def test_search_without_source_reads_every_row(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        requests.append(request)
        if request.url.path == "/v1/boards/tailscale/jobs":
            return httpx.Response(200, content=_BODY)
        return httpx.Response(404)

    result = _search(handler, tmp_path)

    assert result.exit_code == 0
    ids = _ids(tmp_path)
    assert sorted(result.stdout.splitlines()) == sorted(
        _line(job, ids) for job in _JOBS
    )
    assert len(requests) >= 100
    assert any(
        line.startswith("not found: greenhouse:") for line in result.stderr.splitlines()
    )


def test_filters_from_config_and_flags(tmp_path: Path) -> None:
    _add_source(tmp_path, "lever:acme", "Acme")
    config = tmp_path / "filters.toml"
    config.write_text(
        "[filters]\ncountries = [\"DE\"]\n\n[filters.role]\nexclude = ['\\bdata\\b']\n",
        encoding="utf-8",
    )
    handler = _lever_only(_ACME_JOBS)
    source = ("--source", "lever:acme")

    by_config = _search(handler, tmp_path, *source, "--config", str(config))
    by_flag = _search(
        handler, tmp_path, *source, "--config", str(config), "--country", "US"
    )

    assert by_config.exit_code == by_flag.exit_code == 0
    assert _titles(by_config) == ["Staff Engineer"]
    assert _titles(by_flag) == ["Backend Engineer"]


def test_filters_from_default_config_location(tmp_path: Path) -> None:
    _add_source(tmp_path, "lever:acme", "Acme")
    path = _home(tmp_path) / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[filters]\ncountries = ["DE"]\n', encoding="utf-8")

    result = _search(_lever_only(_ACME_JOBS), tmp_path, "--source", "lever:acme")

    assert _titles(result) == ["Data Engineer", "Staff Engineer"]


_JOBICY_PAGES = {
    False: (_FIXTURES / "jobicy" / "page1.json").read_bytes(),
    True: (_FIXTURES / "jobicy" / "page2.json").read_bytes(),
}


def _jobicy(requests: list[httpx.Request]) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        if request.url.host != "jobicy.com":
            return httpx.Response(404)
        requests.append(request)
        return httpx.Response(
            200, content=_JOBICY_PAGES["cursor" in request.url.params]
        )

    return handler


def test_jobicy_kept_inside_refresh_interval(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    handler = _jobicy(requests)
    source = ("--source", "jobicy:engineering", "--config", _no_role_filters(tmp_path))

    first = _search(handler, tmp_path, *source)
    sent = len(requests)
    second = _search(handler, tmp_path, *source, now=_T0 + timedelta(minutes=10))

    assert first.exit_code == second.exit_code == 0
    assert first.stdout != ""
    assert sent >= 1
    assert len(requests) == sent
    assert second.stderr.splitlines() == [
        "kept jobicy:engineering: fetched less than 60 min ago"
    ]
    assert second.stdout == first.stdout


def _no_role_filters(tmp_path: Path) -> str:
    """Write a config that keeps every role, and return its path."""
    path = tmp_path / "all-roles.toml"
    path.write_text("[filters.role]\ninclude = []\nexclude = []\n", encoding="utf-8")
    return str(path)


def test_jobicy_fetched_again_after_the_interval(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    handler = _jobicy(requests)
    source = ("--source", "jobicy:engineering")

    _search(handler, tmp_path, *source)
    sent = len(requests)
    second = _search(handler, tmp_path, *source, now=_T0 + timedelta(minutes=61))

    assert len(requests) == 2 * sent
    assert "kept" not in second.stderr


def test_stale_source_hidden(tmp_path: Path) -> None:
    _search(_serving(), tmp_path, *_TAILSCALE)

    result = _search(
        _serving({"tailscale": httpx.Response(500)}),
        tmp_path,
        *_TAILSCALE,
        now=_T0 + timedelta(days=8),
    )

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "failed greenhouse:tailscale: HTTP 500",
        "stale greenhouse:tailscale: last fetched 2026-10-31, postings hidden",
    ]
    assert result.stdout == ""


def test_stale_notice_is_limited_to_the_chosen_sources(tmp_path: Path) -> None:
    _search(_serving(), tmp_path, *_TAILSCALE)
    _add_source(tmp_path, "greenhouse:other", "Tailscale")

    result = _search(
        _serving(),
        tmp_path,
        "--source",
        "greenhouse:other",
        now=_T0 + timedelta(days=8),
    )

    assert result.exit_code == 0
    assert result.stderr == ""


_JAPANDEV_ORIGIN = "https://japan-dev.com"
_JAPANDEV_PATH = "/jobs/abbeal/abbeal-ai-engineer-maj4uw"


def _japandev(requests: list[httpx.Request]) -> _Handler:
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{_JAPANDEV_ORIGIN}{_JAPANDEV_PATH}</loc></url></urlset>"
    ).encode()
    page = (_FIXTURES / "japandev" / "job_plain.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host != "japan-dev.com":
            return httpx.Response(404)
        requests.append(request)
        if request.url.path == "/cdn/sitemaps/sitemap.xml":
            return httpx.Response(200, content=sitemap)
        return httpx.Response(200, content=page)

    return handler


def test_still_listed_keeps_detail_jobs_open(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    handler = _japandev(requests)
    source = ("--source", "japandev:all")

    first = _search(handler, tmp_path, *source)
    sent = [r.url.path for r in requests]
    second = _search(handler, tmp_path, *source, now=_T0 + timedelta(days=6))

    assert first.exit_code == second.exit_code == 0
    assert sent == ["/cdn/sitemaps/sitemap.xml", _JAPANDEV_PATH]
    assert [r.url.path for r in requests[2:]] == ["/cdn/sitemaps/sitemap.xml"]
    assert first.stdout != ""
    assert second.stdout == first.stdout


def test_cross_source_posting_lists_both_refs(tmp_path: Path) -> None:
    _add_source(tmp_path, "greenhouse:tailscale2", "Tailscale")

    result = _search(
        _serving(), tmp_path, *_TAILSCALE, "--source", "greenhouse:tailscale2"
    )

    assert result.exit_code == 0
    ids = _ids(tmp_path)
    assert sorted(result.stdout.splitlines()) == sorted(
        _line(job, ids, "greenhouse:tailscale,greenhouse:tailscale2") for job in _JOBS
    )


def test_low_confidence_prefix(tmp_path: Path) -> None:
    threads = (_FIXTURES / "hn" / "threads.json").read_bytes()
    comments = (_FIXTURES / "hn" / "comments.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        assert request.url.host == "hn.algolia.com", request.url
        if request.url.path.endswith("/search_by_date"):
            return httpx.Response(200, content=threads)
        return httpx.Response(200, content=comments)

    result = _search(handler, tmp_path, "--source", "hn:whoishiring")

    lines = result.stdout.splitlines()
    assert result.exit_code == 0
    assert lines
    assert all(line.split("\t")[1].startswith("? ") for line in lines)
    assert any("Senior Backend Engineer" in line for line in lines)
    assert all("\thn:whoishiring\t" in line for line in lines)


def test_search_low_confidence_flag_follows_id(tmp_path: Path) -> None:
    threads = (_FIXTURES / "hn" / "threads.json").read_bytes()
    comments = (_FIXTURES / "hn" / "comments.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        if request.url.path.endswith("/search_by_date"):
            return httpx.Response(200, content=threads)
        return httpx.Response(200, content=comments)

    result = _search(handler, tmp_path, "--source", "hn:whoishiring")

    ids = set(_ids(tmp_path).values())
    lines = result.stdout.splitlines()
    assert result.exit_code == 0
    assert lines
    for line in lines:
        identifier, flagged = line.split("\t")[:2]
        assert int(identifier) in ids
        assert flagged.startswith("? ")
        assert line.startswith(f"{identifier}\t? ")


def test_conditional_cache_used(tmp_path: Path) -> None:
    validators: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        sent = request.headers.get("If-None-Match")
        validators.append(sent)
        if sent == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=_BODY, headers={"ETag": '"v1"'})

    first = _search(handler, tmp_path, *_TAILSCALE)
    second = _search(handler, tmp_path, *_TAILSCALE, now=_T0 + timedelta(minutes=1))

    assert first.exit_code == second.exit_code == 0
    assert validators == [None, '"v1"']
    assert len(second.stdout.splitlines()) == len(_JOBS)
    assert second.stdout == first.stdout
    assert (_home(tmp_path) / "cache" / "http.sqlite").exists()


def test_search_with_every_source_removed_exits_0(tmp_path: Path) -> None:
    assert _run(_serving(), tmp_path, "sources", "list").exit_code == 0
    with Store.open(tmp_path / "ijob.db") as store:
        for row in store.sources():
            store.remove_source(row.ref)

    result = _search(_serving(), tmp_path)

    assert result.exit_code == 0
    assert result.stdout == ""


def _posting_id(tmp_path: Path, job: dict[str, Any]) -> int:
    """The stored id of a job of the Tailscale fixture."""
    return _ids(tmp_path)[job["absolute_url"]]


def _show(handler: _Handler, tmp_path: Path, posting_id: int) -> Result:
    return _run(handler, tmp_path, "show", str(posting_id))


def test_show_prints_metadata_and_description(tmp_path: Path) -> None:
    job = _JOBS[0]
    assert _search(_serving(), tmp_path, *_TAILSCALE).exit_code == 0
    description = description_from_raw(json.dumps(job))
    assert description

    result = _show(_serving(), tmp_path, _posting_id(tmp_path, job))

    lines = result.stdout.splitlines()
    assert result.exit_code == 0
    assert result.stderr == ""
    assert lines[:2] == [job["title"], job["company_name"]]
    assert f"url: {job['absolute_url']}" in lines
    assert "sources: greenhouse:tailscale" in lines
    assert description in result.stdout
    assert result.stdout.index(job["absolute_url"]) < result.stdout.index(description)
    assert "label:" not in result.stdout
    assert "description unavailable" not in result.stdout


_SEEN = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _seed_workable(tmp_path: Path) -> int:
    """Store a Workable posting that has no description yet; return its id."""
    ref = parse_source_ref("workable:acme")
    draft = SightingDraft(
        source=ref,
        external_id=ExternalId("ABC123"),
        title="Platform Engineer",
        company="Acme",
        location="Remote",
        url="https://apply.workable.com/acme/j/ABC123/",
        posted_at=None,
        raw=json.dumps({"shortcode": "ABC123"}),
    )
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([draft], seen_at=_SEEN)
        [posting] = store.postings(sources=[ref])
        return posting.id


def _workable(status: int) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=_ALLOWING_ROBOTS)
        if request.url.host == "apply.workable.com":
            return httpx.Response(status, text="oops")
        return httpx.Response(404)

    return handler


def test_show_unavailable_description(tmp_path: Path) -> None:
    posting_id = _seed_workable(tmp_path)

    result = _show(_workable(500), tmp_path, posting_id)

    lines = result.stdout.splitlines()
    assert result.exit_code == 0
    assert lines[0] == "Platform Engineer"
    assert lines[-1].startswith("description unavailable: ")
    assert "500" in lines[-1]


def test_show_unknown_posting(tmp_path: Path) -> None:
    result = _show(_serving(), tmp_path, 999)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["error: invalid posting: no posting 999"]


def _searched_id(tmp_path: Path) -> int:
    """Run a Tailscale search and return the id of its first job's posting."""
    assert _search(_serving(), tmp_path, *_TAILSCALE).exit_code == 0
    return _posting_id(tmp_path, _JOBS[0])


def test_label_up_with_note(tmp_path: Path) -> None:
    posting_id = _searched_id(tmp_path)

    result = _run(
        _serving(), tmp_path, "label", str(posting_id), "up", "--note", "Go + payments"
    )
    shown = _show(_serving(), tmp_path, posting_id)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [f"{posting_id}\tup\tGo + payments"]
    assert "label: up" in shown.stdout.splitlines()
    assert "note: Go + payments" in shown.stdout.splitlines()


def test_label_replace_and_clear(tmp_path: Path) -> None:
    posting_id = _searched_id(tmp_path)
    identifier = str(posting_id)
    _run(_serving(), tmp_path, "label", identifier, "up", "--note", "Go")

    replaced = _run(_serving(), tmp_path, "label", identifier, "down")
    after_replace = _show(_serving(), tmp_path, posting_id).stdout.splitlines()
    cleared = _run(_serving(), tmp_path, "label", identifier, "--clear")
    after_clear = _show(_serving(), tmp_path, posting_id).stdout.splitlines()

    assert replaced.exit_code == 0
    assert replaced.stdout.splitlines() == [f"{posting_id}\tdown\t-"]
    assert "label: down" in after_replace
    assert not any(line.startswith("note:") for line in after_replace)
    assert cleared.exit_code == 0
    assert cleared.stdout.splitlines() == [f"{posting_id}\tcleared"]
    assert not any(line.startswith(("label:", "note:")) for line in after_clear)


@pytest.mark.parametrize(
    "args",
    [
        pytest.param([], id="neither"),
        pytest.param(["up", "--clear"], id="vote-and-clear"),
        pytest.param(["--clear", "--note", "why"], id="note-and-clear"),
        pytest.param(["--note", "why"], id="note-alone"),
        pytest.param(["sideways"], id="bad-vote"),
    ],
)
def test_label_usage_errors(tmp_path: Path, args: list[str]) -> None:
    posting_id = _searched_id(tmp_path)

    result = _run(_serving(), tmp_path, "label", str(posting_id), *args)
    shown = _show(_serving(), tmp_path, posting_id)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr != ""
    assert "label:" not in shown.stdout


@pytest.mark.parametrize(
    "args",
    [
        pytest.param([], id="neither"),
        pytest.param(["up", "--clear"], id="vote-and-clear"),
        pytest.param(["--clear", "--note", "why"], id="note-and-clear"),
        pytest.param(["--note", "why"], id="note-alone"),
    ],
)
def test_label_usage_errors_say_what_to_give(tmp_path: Path, args: list[str]) -> None:
    posting_id = _searched_id(tmp_path)

    result = _run(_serving(), tmp_path, "label", str(posting_id), *args)

    assert result.stderr.splitlines() == [
        "error: give a vote (up or down) with an optional --note, or --clear alone"
    ]


def test_label_blank_note_exits_2(tmp_path: Path) -> None:
    posting_id = _searched_id(tmp_path)

    result = _run(_serving(), tmp_path, "label", str(posting_id), "up", "--note", " ")

    assert result.exit_code == 2
    assert result.stderr.splitlines() == ["error: invalid note: is blank"]


@pytest.mark.parametrize("args", [["up"], ["--clear"]])
def test_label_unknown_posting(tmp_path: Path, args: list[str]) -> None:
    result = _run(_serving(), tmp_path, "label", "999", *args)

    assert result.exit_code == 2
    assert result.stderr.splitlines() == ["error: invalid posting: no posting 999"]


def _pdf(tmp_path: Path, name: str = "resume.pdf") -> Path:
    """Write a made-up PDF: the signature is all the check reads."""
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n% made up for the test\n")
    return path


def test_cv_set_then_show(tmp_path: Path) -> None:
    file = _pdf(tmp_path)
    size = file.stat().st_size

    stored = _run(_serving(), tmp_path, "cv", "set", str(file))
    shown = _run(_serving(), tmp_path, "cv", "show")

    assert stored.exit_code == 0
    name, stored_size, sha256 = stored.stdout.strip().split("\t")
    assert (name, int(stored_size)) == ("resume.pdf", size)
    assert re.fullmatch(r"[0-9a-f]{64}", sha256)
    assert shown.exit_code == 0
    name, shown_size, stored_at = shown.stdout.strip().split("\t")
    assert (name, int(shown_size)) == ("resume.pdf", size)
    assert stored_at.startswith("20")


def test_cv_show_none(tmp_path: Path) -> None:
    result = _run(_serving(), tmp_path, "cv", "show")

    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["no CV stored"]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param("not a pdf", "is not a PDF", id="text-file"),
        pytest.param(None, "cannot read", id="missing-file"),
    ],
)
def test_cv_set_rejects_non_pdf(
    tmp_path: Path, content: str | None, reason: str
) -> None:
    file = tmp_path / "notes.txt"
    if content is not None:
        file.write_text(content, encoding="utf-8")

    result = _run(_serving(), tmp_path, "cv", "set", str(file))
    shown = _run(_serving(), tmp_path, "cv", "show")

    assert result.exit_code == 2
    assert result.stdout == ""
    [line] = result.stderr.splitlines()
    assert line.startswith("error: ")
    assert reason in line
    assert shown.stdout.splitlines() == ["no CV stored"]


def test_cv_set_copy_failure_exits_1(tmp_path: Path) -> None:
    file = _pdf(tmp_path)
    (tmp_path / "cv.pdf").mkdir()

    result = _run(_serving(), tmp_path, "cv", "set", str(file))
    shown = _run(_serving(), tmp_path, "cv", "show")

    assert result.exit_code == 1
    assert result.stdout == ""
    [line] = result.stderr.splitlines()
    assert line.startswith("error: cannot store the CV: ")
    assert shown.stdout.splitlines() == ["no CV stored"]


def test_non_pdf_cv_is_reported_as_the_bare_reason(tmp_path: Path) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("not a pdf", encoding="utf-8")

    result = _run(_serving(), tmp_path, "cv", "set", str(file))

    assert result.exit_code == 2
    assert result.stderr.splitlines() == [
        f"error: {file} is not a PDF: it does not start with %PDF-"
    ]


def _model_app(
    tmp_path: Path, engines: FakeEngines, *, model: bool = True
) -> typer.Typer:
    """Build an app whose LLM calls go to `engines`, with a config naming a model.

    Args:
        tmp_path: The test's directory; the config goes into its ijob home.
        engines: The fake engine factory every model call is routed through.
        model: Whether the config sets `[models].cv_profile`.

    Returns:
        The application.
    """
    cli_path = write_fake_cli(tmp_path / "bin").path
    write_llm_config(
        _home(tmp_path) / "config.toml",
        judge=None,
        cv_profile=MADE_UP_CLAUDE if model else None,
        cli_path=cli_path,
    )

    def options(_ijob_home: object, config_path: Path) -> LlmOptions:
        return LlmOptions(config_path=config_path, engines=engines)

    return make_app(_factory(_serving()), llm_options=options)


def _invoke(app_: typer.Typer, tmp_path: Path, *args: str) -> Result:
    """Run `ijob <args>` on `app_` against the `tmp_path` database."""
    argv = [*args, "--db", str(tmp_path / "ijob.db")]
    return CliRunner().invoke(app_, argv, env=_env(tmp_path))


def test_cv_set_without_model_notes_and_exits_0(tmp_path: Path) -> None:
    file = _pdf(tmp_path)
    app_ = _model_app(tmp_path, FakeEngines(), model=False)

    result = _invoke(app_, tmp_path, "cv", "set", str(file))

    assert result.exit_code == 0
    [stored] = result.stdout.splitlines()
    assert stored.startswith("resume.pdf\t")
    assert result.stderr.splitlines() == [
        "note: profile not built; set [models].cv_profile"
    ]


def test_cv_set_builds_profile(tmp_path: Path) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf")
    engines = FakeEngines(FakeAsyncEngine([profile_answer()]))
    app_ = _model_app(tmp_path, engines)

    result = _invoke(app_, tmp_path, "cv", "set", str(file))

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].startswith("resume.pdf\t")
    assert lines[1:] == [
        "summary: Backend engineer",
        "years_experience: 7",
        "seniority: senior",
        "roles: Backend Engineer",
        "skills: Python",
        "spoken_languages: English",
        "locations: Jakarta",
        "work_authorization: Indonesia",
    ]
    assert len(engines.engine.requests) == 1


def _refuse_trace_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the trace store refuse every write, so each span is dropped."""

    def refuse(_store: TraceStore, _rows: object) -> None:
        raise sqlite3.OperationalError("refused")

    monkeypatch.setattr(TraceStore, "insert", refuse)


def test_cv_set_reports_spans_that_were_not_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf")
    app_ = _model_app(tmp_path, FakeEngines(FakeAsyncEngine([profile_answer()])))
    _refuse_trace_writes(monkeypatch)

    result = _invoke(app_, tmp_path, "cv", "set", str(file))

    assert result.exit_code == 0, result.stderr
    [note] = result.stderr.splitlines()
    assert re.fullmatch(r"warning: \d+ spans not stored", note)


def test_cv_profile_reports_spans_that_were_not_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf")
    app_ = _model_app(tmp_path, FakeEngines(FakeAsyncEngine([profile_answer()])))
    _invoke(app_, tmp_path, "cv", "set", str(file))
    _refuse_trace_writes(monkeypatch)

    result = _invoke(app_, tmp_path, "cv", "profile", "--refresh")

    assert result.exit_code == 0, result.stderr
    [note] = result.stderr.splitlines()
    assert re.fullmatch(r"warning: \d+ spans not stored", note)


def test_cv_set_profile_oserror_is_not_a_store_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf")
    app_ = _model_app(tmp_path, FakeEngines())

    async def unreadable(_service: Service, *, refresh: bool) -> NoReturn:
        msg = f"profile io {refresh}"
        raise OSError(msg)

    monkeypatch.setattr(Service, "build_profile", unreadable)

    result = _invoke(app_, tmp_path, "cv", "set", str(file))
    shown = _invoke(app_, tmp_path, "cv", "show")

    assert isinstance(result.exception, OSError)
    assert "cannot store the CV" not in result.stderr
    assert shown.stdout.startswith("resume.pdf\t")


def test_cv_set_profile_error_keeps_pdf_exit_1(tmp_path: Path) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf", text="")
    app_ = _model_app(tmp_path, FakeEngines())

    result = _invoke(app_, tmp_path, "cv", "set", str(file))
    shown = _invoke(app_, tmp_path, "cv", "show")

    assert result.exit_code == 1
    assert "CV has no text layer; OCR is not supported" in result.stderr
    assert result.stderr.startswith("error: ")
    assert shown.stdout.startswith("resume.pdf\t")


def test_cv_set_bad_config_exits_2_with_pdf_kept(tmp_path: Path) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf")
    app_ = _model_app(tmp_path, FakeEngines())
    (_home(tmp_path) / "config.toml").write_text("[llm]\nbogus = 1\n", encoding="utf-8")

    result = _invoke(app_, tmp_path, "cv", "set", str(file))
    shown = _invoke(app_, tmp_path, "cv", "show")

    assert result.exit_code == 2
    assert result.stderr.startswith("error: ")
    assert "llm" in result.stderr
    assert shown.stdout.startswith("resume.pdf\t")


def test_cv_profile_no_cv_exit_1(tmp_path: Path) -> None:
    app_ = _model_app(tmp_path, FakeEngines())

    result = _invoke(app_, tmp_path, "cv", "profile")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["error: no CV is stored; add one first"]


def test_cv_profile_without_model_exit_1(tmp_path: Path) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf")
    app_ = _model_app(tmp_path, FakeEngines(), model=False)
    _invoke(app_, tmp_path, "cv", "set", str(file))

    result = _invoke(app_, tmp_path, "cv", "profile")

    assert result.exit_code == 1
    assert result.stderr.startswith("error: no model for cv_profile; set ")


def test_cv_profile_refresh_calls_again(tmp_path: Path) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf")
    engines = FakeEngines(FakeAsyncEngine([profile_answer()]))
    app_ = _model_app(tmp_path, engines)
    _invoke(app_, tmp_path, "cv", "set", str(file))

    plain = _invoke(app_, tmp_path, "cv", "profile")
    after_plain = len(engines.engine.requests)
    refreshed = _invoke(app_, tmp_path, "cv", "profile", "--refresh")

    assert plain.exit_code == refreshed.exit_code == 0
    assert plain.stdout.splitlines()[0] == "summary: Backend engineer"
    assert refreshed.stdout == plain.stdout
    assert after_plain == 1
    assert len(engines.engine.requests) == after_plain + 1


def test_cv_profile_builds_when_absent(tmp_path: Path) -> None:
    file = write_cv_pdf(tmp_path / "resume.pdf")
    engines = FakeEngines(FakeAsyncEngine([profile_answer()]))
    app_ = _model_app(tmp_path, engines, model=False)
    _invoke(app_, tmp_path, "cv", "set", str(file))

    result = _invoke(_model_app(tmp_path, engines), tmp_path, "cv", "profile")

    assert result.exit_code == 0
    assert "seniority: senior" in result.stdout.splitlines()
    assert len(engines.engine.requests) == 1


def test_setup_and_cv_show_import_no_llm(tmp_path: Path) -> None:
    code = (
        "import atexit, sys\n"
        "from ijob_app.cli import main\n"
        "atexit.register(lambda: print('LOADED', [m for m in "
        "('dspy', 'claude_agent_sdk') if m in sys.modules]))\n"
        "main()\n"
    )
    env = {**os.environ, "IJOB_HOME": str(tmp_path / "fresh-home")}

    for argv in (["setup"], ["cv", "show"]):
        run = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
            [sys.executable, "-I", "-c", code, *argv],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stderr
        assert run.stdout.splitlines()[-1] == "LOADED []"


def _blocked_cache_env(tmp_path: Path) -> dict[str, str]:
    """The test environment with a cache directory that cannot be created."""
    env = _env(tmp_path)
    (_home(tmp_path) / "cache").write_text("not a directory", encoding="utf-8")
    return env


def test_sources_list_does_not_touch_the_http_cache(tmp_path: Path) -> None:
    env = _blocked_cache_env(tmp_path)
    app: typer.Typer = make_app(_factory(_serving()), now=lambda: _T0)

    result = CliRunner().invoke(
        app, ["sources", "list", "--db", str(tmp_path / "ijob.db")], env=env
    )

    assert result.exit_code == 0
    assert result.stdout != ""
    assert result.stderr == ""
    assert (_home(tmp_path) / "cache").is_file()


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["show", "1"], id="show"),
        pytest.param(["label", "1", "up"], id="label"),
    ],
)
def test_unusable_cache_exits_1_for_show_and_label(
    tmp_path: Path, args: list[str]
) -> None:
    env = _blocked_cache_env(tmp_path)
    app: typer.Typer = make_app(_factory(_serving()), now=lambda: _T0)

    result = CliRunner().invoke(
        app, [*args, "--db", str(tmp_path / "ijob.db")], env=env
    )

    cache_path = _home(tmp_path) / "cache" / "http.sqlite"
    assert result.exit_code == 1
    assert result.stdout == ""
    [line] = result.stderr.splitlines()
    assert line.startswith(f"error: cannot open the HTTP cache {cache_path}: ")
    assert not (tmp_path / "ijob.db").exists()


def _seed_priced(tmp_path: Path, *, with_metadata: bool = True) -> int:
    """Store a posting that carries every detail `show` prints; return its id."""
    ref = parse_source_ref("workable:acme")
    draft = SightingDraft(
        source=ref,
        external_id=ExternalId("PRICED"),
        title="Staff Engineer",
        company="Acme",
        location="Berlin, Germany" if with_metadata else None,
        url="https://apply.workable.com/acme/j/PRICED/",
        posted_at=datetime(2026, 10, 20, 8, 30, tzinfo=UTC) if with_metadata else None,
        raw=json.dumps({"shortcode": "PRICED"}),
        salaries=(
            (
                Money(Decimal(120000), Decimal(150000), "USD", Period.YEAR),
                Money(Decimal(120000), None, "USD", None),
                Money(None, Decimal(150000), "EUR", Period.MONTH),
            )
            if with_metadata
            else ()
        ),
        country=parse_country("Germany") if with_metadata else None,
        remote=RemoteScope.COUNTRY if with_metadata else None,
        description="Build the platform.",
    )
    with Store.open(tmp_path / "ijob.db") as store:
        store.record([draft], seen_at=_SEEN)
        [posting] = store.postings(sources=[ref])
        return posting.id


def test_show_prints_location_salary_country_remote_and_posted(
    tmp_path: Path,
) -> None:
    posting_id = _seed_priced(tmp_path)

    result = _show(_workable(500), tmp_path, posting_id)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "Staff Engineer",
        "Acme",
        "location: Berlin, Germany",
        "salary: 120000-150000 USD/year",
        "salary: from 120000 USD",
        "salary: up to 150000 EUR/month",
        "country: DE",
        "remote: country",
        "posted: 2026-10-20",
        "sources: workable:acme",
        "url: https://apply.workable.com/acme/j/PRICED/",
        "",
        "Build the platform.",
    ]


def test_show_prints_a_dash_for_absent_metadata(tmp_path: Path) -> None:
    posting_id = _seed_priced(tmp_path, with_metadata=False)

    result = _show(_workable(500), tmp_path, posting_id)

    assert result.stdout.splitlines()[2:9] == [
        "location: -",
        "salary: -",
        "country: -",
        "remote: -",
        "posted: -",
        "sources: workable:acme",
        "url: https://apply.workable.com/acme/j/PRICED/",
    ]


_JUDGE_RULE = "no crypto, blockchain or Web3 companies"
_JOBICY = parse_source_ref("jobicy:engineering")
_JOBICY_SEARCH = ("--source", "jobicy:engineering")
_LATER = _T0 + timedelta(minutes=10)


def _no_request(request: httpx.Request) -> httpx.Response:
    msg = f"a judged search made a request: {request.url}"
    raise AssertionError(msg)


_NO_CLIENT = _factory(_no_request)


def _seed_judged(
    tmp_path: Path,
    *descriptions: str | None,
    rules: tuple[str, ...] = (_JUDGE_RULE,),
) -> list[str]:
    """Store one open Jobicy posting per description and the instructions' rules.

    The first description is the newest posting. The source counts as fetched
    just now, so a search a few minutes later reads it without a request.

    Returns:
        The postings' titles, newest first.
    """
    drafts = [
        SightingDraft(
            source=_JOBICY,
            external_id=ExternalId(f"J{number}"),
            title=f"Job {number}",
            company="Acme",
            location=None,
            url=f"https://jobicy.com/jobs/{number}",
            posted_at=_T0 - timedelta(days=number + 1),
            raw="{}",
            description=text,
        )
        for number, text in enumerate(descriptions)
    ]
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds(load_seeds())
        store.record(drafts, seen_at=_T0)
        store.mark_fetched(_JOBICY, _T0)
        store.set_instructions(
            Instructions(
                text="no crypto",
                filters_json="{}",
                rules=rules,
                model=MADE_UP_CLAUDE,
                task_version="1",
                set_at=_T0,
            )
        )
    return [draft.title for draft in drafts]


def _judge_app(
    tmp_path: Path, engines: FakeEngines, *, cap: float | None = None
) -> typer.Typer:
    """Build an app whose judge answers from `engines`, keeping every role."""
    cli_path = write_fake_cli(tmp_path / "bin").path
    config = _home(tmp_path) / "config.toml"
    write_llm_config(
        config,
        judge=MADE_UP_CLAUDE,
        concurrency=1,
        max_cost_usd=cap,
        cli_path=cli_path,
    )
    with config.open("a", encoding="utf-8") as handle:
        handle.write("[filters.role]\ninclude = []\nexclude = []\n")

    def options(_ijob_home: object, config_path: Path) -> LlmOptions:
        return LlmOptions(config_path=config_path, engines=engines)

    return make_app(_NO_CLIENT, now=lambda: _LATER, llm_options=options)


def _judged(
    tmp_path: Path, engines: FakeEngines, *args: str, cap: float | None = None
) -> Result:
    """Run `ijob search` on the Jobicy source against the seeded database."""
    _env(tmp_path)
    app_ = _judge_app(tmp_path, engines, cap=cap)
    argv = [
        "search",
        "--judge",
        *_JOBICY_SEARCH,
        *args,
        "--db",
        str(tmp_path / "ijob.db"),
    ]
    return CliRunner().invoke(app_, argv, env=_env(tmp_path))


def _answers(*texts: str) -> FakeEngines:
    return FakeEngines(FakeAsyncEngine(list(texts)))


def _drop(reason: str = "crypto") -> str:
    return verdict_answer(keep=False, reason=reason, rule=1)


def _kept_line() -> str:
    return "kept jobicy:engineering: fetched less than 60 min ago"


def test_search_without_rules_output_unchanged(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one", "two", rules=())
    engines = _answers(verdict_answer())

    result = _judged(tmp_path, engines)

    assert result.exit_code == 0
    assert result.stderr.splitlines() == [_kept_line()]
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert all(len(line.split("\t")) == 6 for line in lines)
    assert engines.calls == []


def test_search_with_rules_marks_and_hides_dropped(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one", "two", "three")

    result = _judged(tmp_path, _answers(verdict_answer(), _drop(), verdict_answer()))

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert [line.split("\t")[1] for line in lines] == ["Job 0", "Job 2"]
    assert all(line.endswith("\t\u2714") for line in lines)
    summary = result.stderr.splitlines()[-1]
    assert summary.startswith("judged 3, cached 0, dropped 1, not judged 0, cost $")
    assert " of $" in summary


def test_search_dropped_flag_lists_both(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one", "two", "three")

    result = _judged(
        tmp_path,
        _answers(verdict_answer(), _drop(), verdict_answer()),
        "--dropped",
    )

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert [line.split("\t")[1] for line in lines] == ["Job 0", "Job 1", "Job 2"]
    assert lines[1].endswith("\t\u2718 crypto")
    assert lines[0].endswith("\t\u2714")
    assert lines[2].endswith("\t\u2714")


def test_search_prints_in_filter_order_after_cached_run(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one", "two", "three")
    _judged(tmp_path, _answers(verdict_answer()), "--dropped")
    _seed_judged(tmp_path, "one", "two", "three changed")
    engines = _answers(verdict_answer())

    result = _judged(tmp_path, engines, "--dropped")

    assert [line.split("\t")[1] for line in result.stdout.splitlines()] == [
        "Job 0",
        "Job 1",
        "Job 2",
    ]
    assert result.stderr.splitlines()[-1].startswith("judged 1, cached 2, ")


def test_search_no_description_marker(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one", None)

    first = _judged(tmp_path, _answers(verdict_answer()))
    second = _judged(tmp_path, _answers(verdict_answer()))

    for result in (first, second):
        lines = result.stdout.splitlines()
        assert lines[0].endswith("\t\u2714")
        assert lines[1].endswith("\t\u2714 (no description)")
    assert second.stderr.splitlines()[-1].startswith("judged 0, cached 2, ")


def test_search_cap_reached_exit_0_with_note(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one", "two", "three")

    result = _judged(
        tmp_path,
        FakeEngines(FakeAsyncEngine([verdict_answer()], cost=0.5)),
        "--dropped",
        cap=0.3,
    )

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].endswith("\t\u2714")
    assert [line.split("\t")[-1] for line in lines[1:]] == ["\u00b7 cap reached"] * 2
    summary = result.stderr.splitlines()[-1]
    assert summary.startswith("judged 1, cached 0, dropped 0, not judged 2, cost $")
    assert summary.endswith("; stopped: cap reached")


def test_search_provider_failure_prints_partial_exit_1(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one", "two")
    _env(tmp_path)
    app_ = _judge_app(tmp_path, FakeEngines())
    write_fake_cli(tmp_path / "bin", version="1.0.0")

    result = CliRunner().invoke(
        app_,
        [
            "search",
            "--judge",
            *_JOBICY_SEARCH,
            "--dropped",
            "--db",
            str(tmp_path / "ijob.db"),
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert [line.split("\t")[-1] for line in lines] == ["\u00b7 provider error"] * 2
    assert result.stderr.splitlines()[-1].endswith("; stopped: provider error")


def test_search_reason_printed_literally(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one")

    result = _judged(
        tmp_path,
        _answers(_drop("[b]x[/b] **x** \x1b[31mred")),
        "--dropped",
    )

    assert result.exit_code == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert result.stdout.splitlines()[0].endswith("\t\u2718 [b]x[/b] **x** [31mred")


def test_search_without_judge_model_exits_1(tmp_path: Path) -> None:
    _seed_judged(tmp_path, "one")
    _env(tmp_path)
    write_llm_config(_home(tmp_path) / "config.toml", judge=None)
    app_ = make_app(_NO_CLIENT, now=lambda: _LATER)

    result = CliRunner().invoke(
        app_,
        ["search", "--judge", *_JOBICY_SEARCH, "--db", str(tmp_path / "ijob.db")],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines()[-1].startswith("error: no model for judge")


def test_search_shows_fetch_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []

    class _Recording(ProgressLine):
        @override
        def show_live(self, text: str) -> None:
            shown.append(text)

    monkeypatch.setattr("ijob_app.cli.ProgressLine", _Recording)

    result = _rejecting_run(tmp_path, monkeypatch)

    assert result.exit_code == 0
    assert shown == [
        "fetching sources: 0 of 1 done, 0 failed",
        "fetching sources: 1 of 1 done, 0 failed, saving",
    ]
    assert "fetching" not in result.stderr
