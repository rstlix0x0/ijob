import asyncio

import httpx
import pytest
from ijob_app.agent_site import (
    SiteRule,
    guess_refusal,
    is_address,
    request_host,
    site_guard,
)
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_sources.detect import board_host, board_ref, resolve_url
from ijob_sources.errors import OffSiteError
from ijob_sources.polite_http import PoliteClient


@pytest.mark.parametrize(
    "host",
    [
        "www.example.com",
        "example.com",
        "careers.example.com",
        "Careers.Example.COM",
        "jobs.lever.co",
        "boards.greenhouse.io",
    ],
)
def test_allowed_hosts_table(host: str) -> None:
    rule = SiteRule(given_url="https://www.example.com/careers")

    assert rule.allows(host)


@pytest.mark.parametrize(
    "host", ["evil.com", "example.com.evil.com", "notexample.com", "jobs.lever.co.evil"]
)
def test_refused_hosts_table(host: str) -> None:
    rule = SiteRule(given_url="https://www.example.com/careers")

    assert not rule.allows(host)


def test_a_rule_with_no_site_allows_only_board_hosts() -> None:
    rule = SiteRule()

    assert rule.site is None
    assert rule.allows("jobs.lever.co")
    assert not rule.allows("example.com")


def test_given_url_fixes_the_site() -> None:
    rule = SiteRule(given_url="https://www.example.com/careers")

    assert rule.site == "example.com"
    assert rule.admit("https://example.com/jobs") is None
    assert rule.admit("https://other.test/") == "off the site: other.test"
    rule.mark_read()
    assert rule.site == "example.com"


def test_failed_guess_is_replaced() -> None:
    rule = SiteRule()

    assert rule.admit("https://www.acme.test/") is None
    assert rule.site == "acme.test"
    assert rule.admit("https://acme.example/careers") is None
    assert rule.site == "acme.example"


def test_read_fixes_the_site() -> None:
    rule = SiteRule()
    assert rule.admit("https://acme.test/") is None

    rule.mark_read()

    assert rule.admit("https://other.test/") == "off the site: other.test"
    assert rule.site == "acme.test"
    assert rule.admit("https://careers.acme.test/jobs?page=2") is None


def test_read_before_any_guess_fixes_the_site_as_unset() -> None:
    rule = SiteRule()

    rule.mark_read()

    assert rule.site is None
    assert rule.admit("https://evil.example/") is not None
    assert rule.site is None
    assert rule.admit("https://jobs.lever.co/acme") is None
    assert not rule.allows("evil.example")


def test_a_read_of_a_board_page_does_not_let_a_guess_follow() -> None:
    rule = SiteRule()
    url = "https://boards.greenhouse.io/embed/job_board?for=attacker"
    assert rule.admit(url) is None

    rule.mark_read()

    assert rule.admit("https://evil.example/") == "off the site: evil.example"
    assert rule.site is None


def test_a_read_of_a_board_page_does_not_fix_a_failed_guess() -> None:
    rule = SiteRule()
    assert rule.admit("https://failed.test/") is None
    assert rule.admit("https://apply.workable.com/j/abc") is None

    rule.mark_read()

    assert rule.site is None
    assert not rule.allows("failed.test")
    assert rule.admit("https://real.test/") is not None


def test_a_read_of_the_guessed_page_fixes_the_guess_after_a_board_link() -> None:
    rule = SiteRule()
    assert rule.admit("https://apply.workable.com/j/abc") is None
    assert rule.admit("https://acme.test/") is None

    rule.mark_read()

    assert rule.site == "acme.test"


@pytest.mark.parametrize("host", ["com", "localhost"])
def test_a_host_without_a_dot_is_never_a_given_site(host: str) -> None:
    rule = SiteRule(given_url=f"https://{host}/")

    assert rule.site is None
    assert not rule.allows("evil.com")
    assert not rule.allows(host)


def test_www_is_kept_when_removing_it_would_leave_one_label() -> None:
    assert SiteRule(given_url="https://www.com/").site == "www.com"
    assert SiteRule(given_url="https://com/").site is None
    assert SiteRule(given_url="https://www.example.com/").site == "example.com"


@pytest.mark.parametrize("url", ["https://com/", "https://localhost/", "https://./"])
def test_a_guess_without_a_dotted_host_is_refused(url: str) -> None:
    assert guess_refusal(url) is not None
    rule = SiteRule()
    assert rule.admit(url) is not None
    assert rule.site is None


def test_a_guess_of_www_com_does_not_open_the_whole_domain() -> None:
    rule = SiteRule()

    assert rule.admit("https://www.com/") is None

    assert rule.site == "www.com"
    assert not rule.allows("evil.com")
    assert rule.allows("jobs.www.com")


def test_ideographic_dots_do_not_hide_a_search_engine() -> None:
    assert guess_refusal("https://www\u3002google\u3002com/") is not None
    assert guess_refusal("https://google\u3002com/") is not None
    rule = SiteRule()
    assert rule.admit("https://www\u3002google\u3002com/search") is not None
    assert rule.site is None


def test_ideographic_dots_name_the_ascii_site() -> None:
    rule = SiteRule(given_url="https://www\u3002acme\u3002test/")

    assert rule.site == "acme.test"
    assert rule.allows("careers.acme.test")


def test_trailing_dots_are_ignored() -> None:
    rule = SiteRule(given_url="https://www.example.com./careers")

    assert rule.site == "example.com"
    assert rule.allows("example.com.")
    assert rule.allows("Jobs.Example.COM.")
    assert rule.admit("https://example.com./jobs") is None
    assert not rule.allows("evil.com.")
    assert guess_refusal("https://www.google.com./") is not None
    assert guess_refusal("https://example.com./") is None


def test_userinfo_is_not_the_host() -> None:
    rule = SiteRule(given_url="https://example.com@evil.com/")

    assert rule.site == "evil.com"
    assert not rule.allows("example.com")
    assert rule.admit("https://example.com/") == "off the site: example.com"
    assert guess_refusal("https://user:pw@www.google.com/") is not None
    fresh = SiteRule()
    assert fresh.admit("https://google.com@acme.test/") is None
    assert fresh.site == "acme.test"


def test_an_idn_site_is_kept_in_ascii_form() -> None:
    rule = SiteRule(given_url="https://www.m\u00fcnchen.example/karriere")

    assert rule.site == "xn--mnchen-3ya.example"
    assert rule.allows("xn--mnchen-3ya.example")
    assert rule.allows("careers.xn--mnchen-3ya.example")
    assert rule.admit("https://m\u00fcnchen.example/jobs") is None
    assert rule.admit("https://careers.m\u00fcnchen.example/") is None
    assert not rule.allows("xn--exmple-4nf.example")
    assert rule.admit("https://xn--mnchen-3ya.evil.example/") is not None


def test_an_idn_guess_is_accepted_in_ascii_form() -> None:
    rule = SiteRule()

    assert rule.admit("https://www.m\u00fcnchen.example/") is None

    assert rule.site == "xn--mnchen-3ya.example"


def test_a_look_alike_board_host_does_not_move_the_site() -> None:
    url = "https://job\u017f.lever.co/acme"
    assert board_host(url) is None
    assert board_ref(url) is None
    rule = SiteRule()
    assert rule.admit("https://acme.test/") is None

    assert rule.admit(url) is not None
    assert rule.site == "acme.test"

    rule.mark_read()

    assert rule.admit(url) is not None
    assert rule.site == "acme.test"


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/acme",
        "https://JOBS.Lever.CO/acme",
        "https://jobs.lever.co./acme",
        "https://u@jobs.lever.co/acme",
        "https://jobs.lever.co:443/acme",
        "https://jobs.lever.co/",
        "https://boards.greenhouse.io/acme",
        "https://apply.workable.com/acme/j/1",
        "https://jobsxlever.co/acme",
        "https://jobs.lever.co.evil.test/acme",
    ],
)
def test_a_url_is_a_board_for_admit_exactly_when_it_is_for_board_host(
    url: str,
) -> None:
    rule = SiteRule()
    assert rule.admit("https://acme.test/") is None

    reason = rule.admit(url)

    on_board = board_host(url) is not None
    assert (rule.site == "acme.test") == on_board
    if on_board:
        assert reason is None
    if board_ref(url) is not None:
        assert on_board


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/",
        "https://example.com/careers",
        "http://example.com/careers",
    ],
)
def test_guess_form_accepts(url: str) -> None:
    assert guess_refusal(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/search?q=stripe+jobs",
        "https://duckduckgo.com/",
        "https://example.com/jobs?page=1",
        "https://example.com/jobs#open",
        "ftp://example.com/",
        "https://www.bing.com/",
        "https://search.yahoo.com/",
        "https://yandex.com/",
        "https://www.baidu.com/",
        "https://search.brave.com/",
        "not a url",
        "https://",
    ],
)
def test_guess_form_table(url: str) -> None:
    assert guess_refusal(url) is not None


def test_a_refused_guess_leaves_the_site_as_it_was() -> None:
    rule = SiteRule()
    assert rule.admit("https://acme.test/") is None

    reason = rule.admit("https://www.google.com/search?q=acme")

    assert reason is not None
    assert rule.site == "acme.test"


def test_board_url_is_no_guess() -> None:
    rule = SiteRule()
    assert rule.admit("https://acme.test/") is None

    assert rule.admit("https://jobs.lever.co/acme?lever-source=x") is None
    assert rule.site == "acme.test"

    fresh = SiteRule()
    assert fresh.admit("https://boards.greenhouse.io/acme") is None
    assert fresh.site is None


class _Server:
    """Answers robots.txt and the test paths, and records what was sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/off":
            return httpx.Response(302, headers={"Location": "https://evil.com/x"})
        if request.url.path == "/board":
            return httpx.Response(
                302, headers={"Location": "https://jobs.lever.co/example"}
            )
        return httpx.Response(200, content=b"<html></html>")


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _client(server: _Server, rule: SiteRule) -> PoliteClient:
    fake_time = _FakeTime()
    client = PoliteClient(
        transport=httpx.MockTransport(server),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    client.guard_requests(site_guard(rule))
    return client


def test_guard_stops_an_off_site_redirect() -> None:
    server = _Server()
    rule = SiteRule(given_url="https://www.example.com/")

    async def run() -> None:
        async with _client(server, rule) as client:
            await client.get("https://www.example.com/off", follow_redirects=True)

    with pytest.raises(OffSiteError) as caught:
        asyncio.run(run())

    assert str(caught.value) == "off the site: evil.com"
    assert caught.value.host == "evil.com"
    assert not any("evil.com" in url for url in server.sent)


def test_guard_stops_a_request_to_another_site() -> None:
    server = _Server()
    rule = SiteRule(given_url="https://www.example.com/")

    async def run() -> None:
        async with _client(server, rule) as client:
            await client.get("https://evil.com/page")

    with pytest.raises(OffSiteError):
        asyncio.run(run())

    assert server.sent == []


def test_guard_allows_a_board_redirect() -> None:
    server = _Server()
    rule = SiteRule(given_url="https://www.example.com/")

    async def run() -> SourceRef:
        async with _client(server, rule) as client:
            resolution = await resolve_url(
                "https://www.example.com/board", company="Example", client=client
            )
            return resolution.ref

    ref = asyncio.run(run())

    assert ref == SourceRef(SourceKind.LEVER, parse_source_key("example"))
    assert any(url.startswith("https://www.example.com/board") for url in server.sent)


def test_guard_lets_an_idn_site_through_and_stops_a_look_alike() -> None:
    server = _Server()
    rule = SiteRule(given_url="https://www.m\u00fcnchen.example/")

    async def run(url: str) -> None:
        async with _client(server, rule) as client:
            await client.get(url)

    asyncio.run(run("https://m\u00fcnchen.example/ok"))
    asyncio.run(run("https://careers.m\u00fcnchen.example/ok"))
    with pytest.raises(OffSiteError):
        asyncio.run(run("https://m\u00fcnchen.evil.example/ok"))

    assert [url for url in server.sent if "robots" not in url] == [
        "https://xn--mnchen-3ya.example/ok",
        "https://careers.xn--mnchen-3ya.example/ok",
    ]


def test_guard_stops_a_com_wide_site() -> None:
    server = _Server()
    rule = SiteRule()
    assert rule.admit("https://www.com/") is None

    async def run() -> None:
        async with _client(server, rule) as client:
            await client.get("https://evil.com/page")

    with pytest.raises(OffSiteError):
        asyncio.run(run())

    assert server.sent == []


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.example/jobs",
        "http://127.0.0.1/",
        "ftp://example.com/",
        "http://[bad",
        "HTTP://ACME.EXAMPLE",
    ],
)
def test_is_address_accepts_a_url_with_a_scheme_and_a_host(url: str) -> None:
    assert is_address(url)


@pytest.mark.parametrize(
    "url",
    ["", "careers", "acme.example/jobs", "//acme.example/jobs", "mailto:a@acme.test"],
)
def test_is_address_refuses_text_that_is_no_url(url: str) -> None:
    assert not is_address(url)


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://Acme.Example./jobs", "acme.example"),
        ("http://127。0。0。1/", "127.0.0.1"),
        ("https://user@acme.example:8443/x", "acme.example"),
        ("ftp://acme.example/", None),
        ("careers", None),
    ],
)
def test_request_host_is_the_host_a_request_reaches(url: str, host: str | None) -> None:
    assert request_host(url) == host
