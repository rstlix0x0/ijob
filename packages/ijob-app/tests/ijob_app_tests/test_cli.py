import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from ijob_app.cli import app, make_app
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_core.store import Store
from ijob_sources.polite_http import PoliteClient
from typer.testing import CliRunner, Result

_FIXTURE = (
    Path(__file__).parents[3]
    / "ijob-sources"
    / "tests"
    / "ijob_sources_tests"
    / "fixtures"
    / "greenhouse"
    / "tailscale_jobs.json"
)
_BODY = _FIXTURE.read_bytes()
_JOBS: list[dict[str, Any]] = json.loads(_BODY)["jobs"]
_ALLOWING_ROBOTS = "User-agent: *\nDisallow: /embed/\n"
_BOARD_PATH = re.compile(r"/v1/boards/(?P<key>[^/]+)/jobs")

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
    """Serve robots.txt and each board by key; a board not listed gets the fixture."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if requests is not None:
            requests.append(request)
        match = _BOARD_PATH.fullmatch(request.url.path)
        assert match is not None, request.url
        answer = (boards or {}).get(match["key"])
        if isinstance(answer, Exception):
            raise answer
        return answer or httpx.Response(200, content=_BODY)

    return handler


def _factory(handler: _Handler) -> Callable[[], PoliteClient]:
    fake_time = _FakeTime()

    def client_factory() -> PoliteClient:
        return PoliteClient(
            transport=httpx.MockTransport(handler),
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )

    return client_factory


def _search(handler: _Handler, db: Path, greenhouse: str = "tailscale") -> Result:
    app: typer.Typer = make_app(_factory(handler))
    return CliRunner().invoke(
        app, ["search", "--greenhouse", greenhouse, "--db", str(db)]
    )


def _stored(db: Path, key: str = "tailscale") -> int:
    ref = SourceRef(SourceKind.GREENHOUSE, parse_source_key(key))
    with Store.open(db) as store:
        return len(store.postings(sources=[ref]))


def test_search_prints_postings(tmp_path: Path) -> None:
    result = _search(_serving(), tmp_path / "ijob.db")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert sorted(result.stdout.splitlines()) == sorted(
        f"{job['title']}\t{job['company_name']}\t{job['location']['name']}"
        f"\t{job['absolute_url']}"
        for job in _JOBS
    )


def test_absent_location_prints_a_dash(tmp_path: Path) -> None:
    job = {k: v for k, v in _JOBS[0].items() if k != "location"}
    body = json.dumps({"jobs": [job]}).encode()
    handler = _serving({"tailscale": httpx.Response(200, content=body)})

    result = _search(handler, tmp_path / "ijob.db")

    assert result.stdout.splitlines() == [
        f"{job['title']}\t{job['company_name']}\t-\t{job['absolute_url']}"
    ]


def test_second_search_adds_no_postings(tmp_path: Path) -> None:
    db = tmp_path / "ijob.db"

    first = _search(_serving(), db)
    after_first = _stored(db)
    second = _search(_serving(), db)

    assert first.exit_code == second.exit_code == 0
    assert after_first == len(_JOBS)
    assert _stored(db) == after_first
    assert sorted(second.stdout.splitlines()) == sorted(first.stdout.splitlines())
    assert len(second.stdout.splitlines()) == len(_JOBS)


def test_robots_blocked_board_reported(tmp_path: Path) -> None:
    handler = _serving(robots="User-agent: *\nDisallow: /v1/\n")

    result = _search(handler, tmp_path / "ijob.db")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "skipped greenhouse:tailscale: disallowed by robots.txt"
    ]


def test_unknown_board_reported_not_found(tmp_path: Path) -> None:
    handler = _serving({"nosuch": httpx.Response(404, text="{}")})

    result = _search(handler, tmp_path / "ijob.db", "nosuch,tailscale")

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

    result = _search(handler, tmp_path / "ijob.db")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [expected]


def test_parse_failure_reported(tmp_path: Path) -> None:
    handler = _serving({"tailscale": httpx.Response(200, text="<html>no</html>")})

    result = _search(handler, tmp_path / "ijob.db")

    assert result.exit_code == 1
    [line] = result.stderr.splitlines()
    assert line.startswith("failed greenhouse:tailscale: body is not JSON: ")


def test_rejected_jobs_reported(tmp_path: Path) -> None:
    body = json.dumps({"jobs": [_JOBS[0], {**_JOBS[1], "title": ""}]}).encode()
    handler = _serving({"tailscale": httpx.Response(200, content=body)})

    result = _search(handler, tmp_path / "ijob.db")

    assert result.exit_code == 0
    assert "greenhouse:tailscale: skipped 1 invalid jobs" in result.stderr.splitlines()
    assert len(result.stdout.splitlines()) == 1


def test_bad_key_exits_2(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _search(_serving(requests=requests), tmp_path / "ijob.db", "a/b")

    assert result.exit_code == 2
    assert "'a/b'" in result.stderr
    assert result.stdout == ""
    assert requests == []
    assert not (tmp_path / "ijob.db").exists()


def test_bad_key_among_good_ones_exits_2_without_fetching(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _search(_serving(requests=requests), tmp_path / "ijob.db", "tailscale,a/b")

    assert result.exit_code == 2
    assert requests == []


def test_missing_greenhouse_option_exits_2(tmp_path: Path) -> None:
    app = make_app(_factory(_serving()))

    result = CliRunner().invoke(app, ["search", "--db", str(tmp_path / "ijob.db")])

    assert result.exit_code == 2


def test_keys_are_trimmed_and_fetched_once(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    result = _search(
        _serving(requests=requests),
        tmp_path / "ijob.db",
        " tailscale , tailscale,tailscale ",
    )

    assert result.exit_code == 0
    assert len(requests) == 1
    assert len(result.stdout.splitlines()) == len(_JOBS)


def test_failures_are_reported_in_key_order(tmp_path: Path) -> None:
    gone = httpx.Response(404, text="{}")
    handler = _serving({"bbb": gone, "aaa": gone, "ccc": gone})

    result = _search(handler, tmp_path / "ijob.db", "bbb,aaa,ccc")

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "not found: greenhouse:bbb",
        "not found: greenhouse:aaa",
        "not found: greenhouse:ccc",
    ]


def test_failed_board_lists_none_of_its_stored_postings(tmp_path: Path) -> None:
    db = tmp_path / "ijob.db"
    _search(_serving(), db)

    result = _search(_serving({"tailscale": httpx.Response(500)}), db)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert _stored(db) == len(_JOBS)


def test_unusable_db_fails_before_any_request(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    seen: list[httpx.Request] = []
    serve = _serving()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return serve(request)

    result = _search(handler, blocker / "ijob.db")

    assert result.exit_code != 0
    assert seen == []


def test_production_app_shows_help() -> None:
    result = CliRunner().invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    assert "--greenhouse" in result.stdout


def test_help_lists_the_options() -> None:
    app = make_app(_factory(_serving()))

    result = CliRunner().invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    assert "--greenhouse" in result.stdout
    assert "--db" in result.stdout
