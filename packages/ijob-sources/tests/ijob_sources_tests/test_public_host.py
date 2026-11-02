import asyncio
import socket
from collections.abc import Mapping, Sequence

import httpx
import pytest
from ijob_sources.errors import (
    FetchError,
    NotPublicAddressError,
    ReservedHostError,
)
from ijob_sources.polite_http import PoliteClient, RequestGuard
from ijob_sources.public_host import (
    HostResolver,
    public_host_guard,
    refuse_reserved_host,
)


class _FakeResolver:
    """A resolver that answers from a fixed table and records each lookup."""

    def __init__(self, table: Mapping[str, Sequence[str]]) -> None:
        self._table = table
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        return self._table[host]


async def _failing_resolver(_host: str, _port: int) -> Sequence[str]:
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _check(guard: RequestGuard, url: str) -> None:
    asyncio.run(guard(httpx.Request("GET", url)))


def _guard_for(resolver: HostResolver) -> RequestGuard:
    return public_host_guard(resolver)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.5",
        "172.16.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "100.64.0.1",
    ],
)
def test_private_and_special_addresses_refused(address: str) -> None:
    resolver = _FakeResolver({"example.com": [address]})
    url = "http://example.com/jobs"
    with pytest.raises(NotPublicAddressError) as caught:
        _check(_guard_for(resolver), url)
    assert str(caught.value) == f"{url} is not a public address"
    assert caught.value.url == url


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700::1111"])
def test_global_addresses_pass(address: str) -> None:
    resolver = _FakeResolver({"example.com": [address]})
    _check(_guard_for(resolver), "https://example.com/jobs")
    assert resolver.calls == [("example.com", 443)]


def test_resolver_gets_the_port_of_the_request() -> None:
    resolver = _FakeResolver({"example.com": ["8.8.8.8"]})
    _check(_guard_for(resolver), "http://example.com/a")
    _check(_guard_for(resolver), "http://example.com:8080/a")
    assert resolver.calls == [("example.com", 80), ("example.com", 8080)]


def test_one_private_address_among_many_refused() -> None:
    resolver = _FakeResolver({"example.com": ["8.8.8.8", "10.0.0.1"]})
    with pytest.raises(NotPublicAddressError):
        _check(_guard_for(resolver), "http://example.com/x")


def test_no_addresses_refused() -> None:
    resolver = _FakeResolver({"example.com": []})
    with pytest.raises(NotPublicAddressError):
        _check(_guard_for(resolver), "http://example.com/x")


def test_unparseable_address_refused() -> None:
    resolver = _FakeResolver({"example.com": ["not-an-address"]})
    with pytest.raises(NotPublicAddressError):
        _check(_guard_for(resolver), "http://example.com/x")


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::a00:1",
        "64:ff9b::7f00:1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b:1::a00:1",
        "::ffff:10.0.0.1",
        "2002:a00:1::",
    ],
)
def test_ipv6_forms_of_private_ipv4_refused(address: str) -> None:
    resolver = _FakeResolver({"example.com": [address]})
    with pytest.raises(NotPublicAddressError):
        _check(_guard_for(resolver), "http://example.com/x")


def test_nat64_form_of_public_ipv4_passes() -> None:
    resolver = _FakeResolver({"example.com": ["64:ff9b::808:808"]})
    _check(_guard_for(resolver), "http://example.com/x")


def test_nat64_literal_of_private_ipv4_refused() -> None:
    resolver = _FakeResolver({})
    with pytest.raises(NotPublicAddressError):
        _check(_guard_for(resolver), "http://[64:ff9b::a00:1]/x")
    assert resolver.calls == []


def test_undecodable_host_is_not_reserved() -> None:
    refuse_reserved_host("http://xn--japan-dev.com/x")


def test_localhost_refused() -> None:
    resolver = _FakeResolver({"localhost": ["127.0.0.1", "::1"]})
    with pytest.raises(NotPublicAddressError):
        _check(_guard_for(resolver), "http://localhost/x")


@pytest.mark.parametrize("url", ["http://10.0.0.1/x", "http://[::1]/x"])
def test_ip_literal_checked_without_resolving(url: str) -> None:
    resolver = _FakeResolver({})
    with pytest.raises(NotPublicAddressError):
        _check(_guard_for(resolver), url)
    assert resolver.calls == []


def test_global_ip_literal_passes_without_resolving() -> None:
    resolver = _FakeResolver({})
    _check(_guard_for(resolver), "https://8.8.8.8/x")
    assert resolver.calls == []


def test_non_http_scheme_refused() -> None:
    resolver = _FakeResolver({"example.com": ["8.8.8.8"]})
    url = "ftp://example.com/x"
    with pytest.raises(NotPublicAddressError) as caught:
        _check(_guard_for(resolver), url)
    assert str(caught.value) == f"{url} is not a public address"
    assert resolver.calls == []


@pytest.mark.parametrize(
    "host",
    ["www.jobstreet.com", "jobstreet.co.id", "my.JobStreet.com.my", "jobstreet.com."],
)
def test_jobstreet_labels_refused(host: str) -> None:
    with pytest.raises(ReservedHostError) as caught:
        refuse_reserved_host(f"https://{host}/jobs")
    expected = host.lower().rstrip(".")
    assert str(caught.value) == f"{expected} is read only by its own source kind"
    assert caught.value.host == expected


def test_jobstreet_must_be_a_whole_label() -> None:
    refuse_reserved_host("https://notjobstreet.com/jobs")


@pytest.mark.parametrize(
    "host",
    ["japan-dev.com", "www.japan-dev.com", "japan-dev.com.", "www.japan-dev.com."],
)
def test_japandev_hosts_refused(host: str) -> None:
    with pytest.raises(ReservedHostError):
        refuse_reserved_host(f"https://{host}/sitemap.xml")


@pytest.mark.parametrize("host", ["notjapan-dev.com", "japan-dev.com.example.org"])
def test_japandev_must_be_the_whole_domain(host: str) -> None:
    refuse_reserved_host(f"https://{host}/jobs")


def test_guard_refuses_reserved_host_before_resolving() -> None:
    resolver = _FakeResolver({"www.jobstreet.com": ["8.8.8.8"]})
    with pytest.raises(ReservedHostError):
        _check(_guard_for(resolver), "https://www.jobstreet.com/jobs")
    assert resolver.calls == []


def test_unresolvable_host_is_fetch_error() -> None:
    url = "http://nowhere.example/x"
    with pytest.raises(FetchError) as caught:
        _check(_guard_for(_failing_resolver), url)
    assert caught.value.url == url
    assert isinstance(caught.value.__cause__, socket.gaierror)


def test_guard_on_polite_client_names_robots_url() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text="ok")

    async def sleep(_: float) -> None:
        return None

    async def run() -> None:
        async with PoliteClient(
            transport=httpx.MockTransport(handler), sleep=sleep
        ) as client:
            client.guard_requests(
                public_host_guard(_FakeResolver({"example.com": ["10.0.0.1"]}))
            )
            await client.get("http://example.com/jobs")

    with pytest.raises(NotPublicAddressError) as caught:
        asyncio.run(run())
    assert caught.value.url == "http://example.com/robots.txt"
    assert str(caught.value) == "http://example.com/robots.txt is not a public address"
    assert requested == []


def test_guarded_client_with_undecodable_host_sends_nothing() -> None:
    requested: list[str] = []
    resolver = _FakeResolver({})

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text="ok")

    async def sleep(_: float) -> None:
        return None

    async def run() -> None:
        async with PoliteClient(
            transport=httpx.MockTransport(handler), sleep=sleep
        ) as client:
            client.guard_requests(public_host_guard(resolver))
            await client.get("http://xn--japan-dev.com/x")

    with pytest.raises(FetchError) as caught:
        asyncio.run(run())
    assert caught.value.reason.startswith("robots.txt could not be read: ")
    assert requested == []
    assert resolver.calls == []
