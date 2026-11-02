"""Rules for which hosts a recipe source may be fetched from.

`public_host_guard` builds a `RequestGuard` that refuses any request that is
not plain HTTP(S) to a public address, and any request to a host that another
source kind reads by its own rules. Attach it to a `PoliteClient` with
`guard_requests`. The address lookup is injected as a `HostResolver`, so tests
need no DNS.
"""

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

import httpx

from ijob_sources.errors import FetchError, NotPublicAddressError, ReservedHostError
from ijob_sources.polite_http import RequestGuard

type HostResolver = Callable[[str, int], Awaitable[Sequence[str]]]
"""Looks up a host and port and returns the address strings it resolves to."""

_WEB_SCHEMES: Final = frozenset({"http", "https"})
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_JOBSTREET_LABEL: Final = "jobstreet"
_JAPANDEV_DOMAIN: Final = "japan-dev.com"
_NAT64_PREFIX: Final = ipaddress.IPv6Network("64:ff9b::/96")


async def system_resolver(host: str, port: int) -> Sequence[str]:
    """Resolve a host with the running loop's `getaddrinfo`.

    Args:
        host: The host name to look up.
        port: The port the connection will use.

    Returns:
        The distinct addresses found, in the order they were answered.

    Raises:
        OSError: The lookup failed, such as `socket.gaierror` for an unknown
            host.
    """
    infos = await asyncio.get_running_loop().getaddrinfo(host, port)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def refuse_reserved_host(url: str) -> None:
    """Refuse a URL whose host belongs to another source kind.

    A host is reserved when any dot-separated label of it is `jobstreet`,
    compared case-insensitively, or when it is `japan-dev.com` or a subdomain
    of it. A trailing dot on the host, the fully qualified form, is ignored.
    The host is compared in its ASCII form, so an internationalized name that
    does not decode is checked as written rather than raising.

    Args:
        url: The absolute URL to check.

    Raises:
        ReservedHostError: The host is reserved.
    """
    host = httpx.URL(url).raw_host.decode("ascii").lower().rstrip(".")
    reserved = _JOBSTREET_LABEL in host.split(".") or (
        host == _JAPANDEV_DOMAIN or host.endswith(f".{_JAPANDEV_DOMAIN}")
    )
    if reserved:
        raise ReservedHostError(host)


def public_host_guard(resolve: HostResolver) -> RequestGuard:
    """Build a request guard that only lets requests to public hosts through.

    The guard refuses a request whose scheme is not `http` or `https`, whose
    host is reserved (see `refuse_reserved_host`), or whose host is or
    resolves to anything but global addresses. An IP-literal host is checked
    without a lookup. A host that resolves to no address, or to one address
    that is not global, is refused.

    Args:
        resolve: Looks up the addresses of a host name.

    Returns:
        An async hook taking the `httpx.Request` about to be sent.
    """

    async def guard(request: httpx.Request) -> None:
        """Raise unless the request goes to a public address.

        Raises:
            NotPublicAddressError: The scheme is not `http` or `https`, or an
                address of the host is not public.
            ReservedHostError: The host is reserved.
            FetchError: The host could not be resolved.
        """
        url = str(request.url)
        if request.url.scheme not in _WEB_SCHEMES:
            raise NotPublicAddressError(url)
        refuse_reserved_host(url)
        host = request.url.host
        try:
            addresses: Sequence[str] = [str(ipaddress.ip_address(host))]
        except ValueError:
            port = request.url.port or _DEFAULT_PORTS[request.url.scheme]
            try:
                addresses = await resolve(host, port)
            except OSError as error:
                raise FetchError(url, str(error) or type(error).__name__) from error
        if not addresses or not all(_is_global(address) for address in addresses):
            raise NotPublicAddressError(url)

    return guard


def _is_global(address: str) -> bool:
    """Return whether the text is an IP address that is globally routable.

    An address in the NAT64 prefix `64:ff9b::/96` reaches the IPv4 address in
    its low 32 bits, so it is global only when that IPv4 address is.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF).is_global
    return ip.is_global
