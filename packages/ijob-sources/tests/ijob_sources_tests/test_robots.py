import asyncio
from collections.abc import Callable, Iterable
from urllib.robotparser import RobotFileParser

import httpx
import pytest
from ijob_sources.errors import FetchError
from ijob_sources.robots import RobotsPolicy

_ORIGIN = "https://boards.example.test"
_HOUR = 3600.0


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeSend:
    """A `send` that records the URLs asked for and answers from a canned reply."""

    def __init__(
        self, reply: Callable[[str], httpx.Response | FetchError] | None = None
    ) -> None:
        self.urls: list[str] = []
        self._reply = reply or (lambda _: httpx.Response(200, text=""))

    async def __call__(self, url: str) -> httpx.Response:
        self.urls.append(url)
        await asyncio.sleep(0)
        reply = self._reply(url)
        if isinstance(reply, FetchError):
            raise reply
        return reply


def _serving(body: str | bytes, status: int = 200) -> _FakeSend:
    content = body.encode() if isinstance(body, str) else body
    return _FakeSend(lambda _: httpx.Response(status, content=content))


def _policy(send: _FakeSend, clock: _FakeClock | None = None) -> RobotsPolicy:
    return RobotsPolicy(send, clock or _FakeClock())


def _allowed(policy: RobotsPolicy, url: str) -> bool:
    return asyncio.run(policy.allowed(url))


@pytest.mark.parametrize(
    ("body", "path", "expected"),
    [
        ("User-agent: *\nDisallow: /jobs\nAllow: /jobs/public", "/jobs/public", True),
        ("User-agent: *\nAllow: /\nDisallow: /jobs", "/jobs/1", False),
        ("User-agent: *\nAllow: /jobs$\nDisallow: /jobs", "/jobs/1", False),
        ("User-agent: *\nDisallow: /*?", "/search?q=x", False),
    ],
)
def test_rfc9309_longest_match_cases(body: str, path: str, *, expected: bool) -> None:
    assert _allowed(_policy(_serving(body)), _ORIGIN + path) is expected


@pytest.mark.parametrize(
    ("body", "path", "expected"),
    [
        (
            "User-agent: ijob\nDisallow: /foo\n\nUser-agent: ijob\nDisallow: /baz",
            "/foo",
            False,
        ),
        (
            "User-agent: ijob\nDisallow: /foo\n\nUser-agent: ijob\nDisallow: /baz",
            "/baz",
            False,
        ),
        (
            "User-agent: ijob\nDisallow: /foo\n\nUser-agent: ijob\nDisallow: /baz",
            "/ok",
            True,
        ),
        ("User-agent: *\nDisallow: /\n\nUser-agent: IJOB\nAllow: /", "/jobs", True),
        ("User-agent: other\nDisallow: /", "/jobs", True),
        ("", "/jobs", True),
    ],
)
def test_parser_group_cases(body: str, path: str, *, expected: bool) -> None:
    assert _allowed(_policy(_serving(body)), _ORIGIN + path) is expected


def test_robots_txt_path_always_allowed() -> None:
    send = _serving("User-agent: *\nDisallow: /")
    assert _allowed(_policy(send), _ORIGIN + "/robots.txt") is True
    assert send.urls == []


def test_fetches_robots_txt_at_the_origin() -> None:
    send = _serving("")
    _allowed(_policy(send), _ORIGIN + "/v1/boards/x/jobs?content=true")
    assert send.urls == [_ORIGIN + "/robots.txt"]


def test_404_allows_all() -> None:
    policy = _policy(_serving("", status=404))
    assert _allowed(policy, _ORIGIN + "/anything") is True
    assert _allowed(policy, _ORIGIN + "/else") is True


def test_429_disallows_all_for_policy_life() -> None:
    send = _serving("", status=429)
    clock = _FakeClock()
    policy = _policy(send, clock)
    assert _allowed(policy, _ORIGIN + "/jobs") is False
    clock.now = 25 * _HOUR
    assert _allowed(policy, _ORIGIN + "/jobs") is False
    assert len(send.urls) == 1


def test_500_disallows_all() -> None:
    assert _allowed(_policy(_serving("", status=500)), _ORIGIN + "/jobs") is False


def test_unreadable_robots_txt_raises_fetch_error_naming_the_page() -> None:
    cause = FetchError(_ORIGIN + "/robots.txt", "connection refused")
    send = _FakeSend(lambda _: cause)
    with pytest.raises(FetchError) as caught:
        _allowed(_policy(send), _ORIGIN + "/jobs")
    assert caught.value.url == _ORIGIN + "/jobs"
    assert caught.value.reason == "robots.txt could not be read: connection refused"
    assert caught.value.__cause__ is cause


def test_unreadable_robots_txt_is_not_cached() -> None:
    replies: list[httpx.Response | FetchError] = [
        FetchError(_ORIGIN + "/robots.txt", "timed out"),
        httpx.Response(200, text="User-agent: *\nCrawl-delay: 2"),
    ]
    send = _FakeSend(lambda _: replies.pop(0))
    policy = _policy(send)
    with pytest.raises(FetchError):
        _allowed(policy, _ORIGIN + "/jobs")
    assert policy.crawl_delay(_ORIGIN + "/jobs") is None

    assert _allowed(policy, _ORIGIN + "/jobs") is True
    assert send.urls == [_ORIGIN + "/robots.txt"] * 2
    assert policy.crawl_delay(_ORIGIN + "/jobs") == 2.0


def test_failed_refetch_of_an_expired_entry_raises() -> None:
    replies: list[httpx.Response | FetchError] = [
        httpx.Response(200, text=""),
        FetchError(_ORIGIN + "/robots.txt", "timed out"),
    ]
    clock = _FakeClock()
    policy = _policy(_FakeSend(lambda _: replies.pop(0)), clock)
    assert _allowed(policy, _ORIGIN + "/jobs") is True
    clock.now = 25 * _HOUR
    with pytest.raises(FetchError, match=r"robots\.txt could not be read: timed out"):
        _allowed(policy, _ORIGIN + "/jobs")


def test_parse_value_error_disallows_all(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_self: RobotFileParser, _lines: Iterable[str]) -> None:
        raise ValueError

    monkeypatch.setattr(RobotFileParser, "parse", _raise)
    assert _allowed(_policy(_serving("User-agent: *")), _ORIGIN + "/jobs") is False


def test_body_past_500_kib_is_cut() -> None:
    comments = ("#" * 1023 + "\n") * 500
    assert len(comments) == 500 * 1024
    body = comments + "User-agent: *\nDisallow: /\n"
    assert _allowed(_policy(_serving(body)), _ORIGIN + "/jobs") is True


def test_cache_reused_within_24h_and_refetched_after() -> None:
    send = _serving("")
    clock = _FakeClock()
    policy = _policy(send, clock)
    _allowed(policy, _ORIGIN + "/jobs")
    clock.now = 23 * _HOUR
    _allowed(policy, _ORIGIN + "/jobs")
    assert len(send.urls) == 1
    clock.now = 25 * _HOUR
    _allowed(policy, _ORIGIN + "/jobs")
    assert len(send.urls) == 2


def test_cache_is_per_origin() -> None:
    send = _serving("")
    policy = _policy(send)
    _allowed(policy, _ORIGIN + "/jobs")
    _allowed(policy, "https://other.example.test/jobs")
    assert send.urls == [
        _ORIGIN + "/robots.txt",
        "https://other.example.test/robots.txt",
    ]


def test_concurrent_cold_calls_fetch_once() -> None:
    send = _serving("")
    policy = _policy(send)

    async def both() -> list[bool]:
        return list(
            await asyncio.gather(
                policy.allowed(_ORIGIN + "/a"), policy.allowed(_ORIGIN + "/b")
            )
        )

    assert asyncio.run(both()) == [True, True]
    assert len(send.urls) == 1


def test_callers_waiting_on_a_dead_origin_share_its_failure() -> None:
    cause = FetchError(_ORIGIN + "/robots.txt", "timed out")
    send = _FakeSend(lambda _: cause)
    policy = _policy(send)
    urls = [f"{_ORIGIN}/jobs/{number}" for number in range(5)]

    async def all_at_once() -> list[object]:
        return list(
            await asyncio.gather(
                *(policy.allowed(url) for url in urls), return_exceptions=True
            )
        )

    results = asyncio.run(all_at_once())

    assert send.urls == [_ORIGIN + "/robots.txt"]
    assert all(isinstance(result, FetchError) for result in results)
    errors = [result for result in results if isinstance(result, FetchError)]
    assert [error.url for error in errors] == urls
    assert {error.reason for error in errors} == {
        "robots.txt could not be read: timed out"
    }
    assert all(error.__cause__ is cause for error in errors)


def test_a_call_after_the_failed_attempt_tries_again_and_can_succeed() -> None:
    replies: list[httpx.Response | FetchError] = [
        FetchError(_ORIGIN + "/robots.txt", "timed out"),
        httpx.Response(200, text="User-agent: *\nCrawl-delay: 2"),
    ]
    send = _FakeSend(lambda _: replies.pop(0))
    policy = _policy(send)

    async def run() -> list[object]:
        first = await asyncio.gather(
            policy.allowed(_ORIGIN + "/a"),
            policy.allowed(_ORIGIN + "/b"),
            return_exceptions=True,
        )
        later = await policy.allowed(_ORIGIN + "/c")
        return [*first, later]

    results = asyncio.run(run())

    assert [type(result) for result in results] == [FetchError, FetchError, bool]
    assert results[2] is True
    assert send.urls == [_ORIGIN + "/robots.txt"] * 2


def test_a_failure_on_one_origin_does_not_fail_callers_of_another() -> None:
    send = _FakeSend(
        lambda url: (
            FetchError(url, "timed out")
            if url.startswith(_ORIGIN)
            else httpx.Response(200)
        )
    )
    policy = _policy(send)

    async def run() -> list[object]:
        return list(
            await asyncio.gather(
                policy.allowed(_ORIGIN + "/a"),
                policy.allowed("https://other.example.test/a"),
                return_exceptions=True,
            )
        )

    results = asyncio.run(run())

    assert isinstance(results[0], FetchError)
    assert results[1] is True


def test_crawl_delay_read() -> None:
    policy = _policy(_serving("User-agent: *\nAllow: /\nCrawl-delay: 1"))
    _allowed(policy, _ORIGIN + "/jobs")
    delay = policy.crawl_delay(_ORIGIN + "/jobs")
    assert delay == 1.0
    assert isinstance(delay, float)


def test_crawl_delay_absent() -> None:
    policy = _policy(_serving("User-agent: *\nAllow: /"))
    _allowed(policy, _ORIGIN + "/jobs")
    assert policy.crawl_delay(_ORIGIN + "/jobs") is None


def test_crawl_delay_unknown_origin() -> None:
    send = _serving("User-agent: *\nCrawl-delay: 1")
    policy = _policy(send)
    assert policy.crawl_delay(_ORIGIN + "/jobs") is None
    assert send.urls == []


def test_crawl_delay_blanket_rules() -> None:
    policy = _policy(_serving("", status=404))
    _allowed(policy, _ORIGIN + "/jobs")
    assert policy.crawl_delay(_ORIGIN + "/jobs") is None


def test_crawl_delay_fraction_counts_as_no_delay() -> None:
    policy = _policy(_serving("User-agent: *\nAllow: /\nCrawl-delay: 1.5"))
    _allowed(policy, _ORIGIN + "/jobs")
    assert policy.crawl_delay(_ORIGIN + "/jobs") is None
