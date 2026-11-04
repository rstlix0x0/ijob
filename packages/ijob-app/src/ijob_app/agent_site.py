"""The site an agent run may read, and the guard that keeps requests on it.

A `SiteRule` names one site, the bare host of the company being looked up. A
host is allowed when it is that site, a subdomain of it, or a hiring-system
host in `BOARD_HOSTS`. A site always has a dot: a one-label host such as `com`
is never a site. Hosts are compared in their ASCII form, as a request reaches
them. When the run starts from a URL, the site is that URL's host and never
moves. When it starts from a company name, each page the agent opens before one
is read is a guess, and the guess is the site until a page is read; after any
read the site is fixed, even if it is still unset. `guess_refusal` says which
guesses are not worth a request, and `site_guard` builds the request guard that
raises `OffSiteError` for any request, redirect hops included, to a host the
rule does not allow. `is_address` tells a malformed argument, which is no
request at all, from a URL the rule may refuse, and `request_host` names the
host a request for a URL reaches.
"""

from typing import Final

import httpx
from ijob_sources.detect import BOARD_HOSTS, board_host
from ijob_sources.errors import OffSiteError
from ijob_sources.polite_http import RequestGuard

_WEB_SCHEMES: Final = frozenset({"http", "https"})
_SEARCH_ENGINES: Final = frozenset(
    {
        "google.com",
        "bing.com",
        "duckduckgo.com",
        "yahoo.com",
        "yandex.com",
        "baidu.com",
        "search.brave.com",
    }
)
_WWW: Final = "www."
_NOT_WEB_ADDRESS: Final = "not an http or https address"


class SiteRule:
    """The site one agent run may read, which a guess can move until a page is read.

    Attributes:
        site: The bare host requests may go to, in ASCII form with any `www.`
            removed, or `None` while a company-name run has made no guess yet
            or the given URL had no usable host.
    """

    def __init__(self, given_url: str | None = None) -> None:
        """Build the rule for one run.

        Args:
            given_url: The absolute URL the user gave. Its host becomes the
                site and never moves; a URL with no host, or a host without a
                dot, leaves only the board hosts allowed. `None` for a run
                that starts from a company name.
        """
        self.site: str | None = None
        self._fixed = False
        self._pending_is_board = False
        if given_url is not None:
            self.site = _bare_host(given_url)
            self._fixed = True

    def allows(self, host: str) -> bool:
        """Tell whether a request to `host` stays on the site.

        Args:
            host: The host name of the request, in any case and in ASCII
                (punycode) form; a host in Unicode form is not matched.

        Returns:
            `True` for a board host, the site, or a subdomain of the site.
        """
        host = host.lower().rstrip(".")
        if host in BOARD_HOSTS:
            return True
        site = self.site
        return site is not None and (host == site or host.endswith(f".{site}"))

    def admit(self, url: str) -> str | None:
        """Check a URL the agent wants to open, and move the site if it is a guess.

        A URL on a board host, as `board_host` reads it, is no guess: it is
        admitted and the site stays. While the site is not fixed, any other URL
        is a guess; one that passes `guess_refusal` becomes the site, replacing
        an earlier guess that read nothing. Once the site is fixed, only a
        host the rule allows is admitted.

        Args:
            url: The absolute URL about to be opened.

        Returns:
            `None` when the URL may be opened, otherwise the reason it may not.
        """
        if board_host(url) is not None:
            self._pending_is_board = True
            return None
        host = _host(url)
        if self._fixed:
            if host is None:
                return _NOT_WEB_ADDRESS
            reason = None if self.allows(host) else str(OffSiteError(host))
        else:
            reason = guess_refusal(url)
            if reason is None:
                self.site = _bare_host(url)
        if reason is None:
            self._pending_is_board = False
        return reason

    def mark_read(self) -> None:
        """Record that a page was read, which fixes the site as it now is.

        The site is fixed even while it is unset, so no guess is accepted
        afterwards. A guess that was held while the page read was on a board
        host is dropped instead of being fixed: nothing read vouched for it.
        """
        if not self._fixed and self._pending_is_board:
            self.site = None
        self._fixed = True


def guess_refusal(url: str) -> str | None:
    """Say why a URL is not an acceptable guess at a company's site.

    A guess is `http` or `https`, has a host with a dot in it, carries no
    query and no fragment, and is not on a search engine. The host is read as
    a request would reach it, so a user name before it, a trailing dot, or
    ideographic dots in its place change nothing.

    Args:
        url: The absolute URL the agent wants to open.

    Returns:
        `None` when the URL may be tried, otherwise the reason it may not.
    """
    parsed = _web_url(url)
    if parsed is None:
        return _NOT_WEB_ADDRESS
    host = _ascii_host(parsed)
    if parsed.query:
        return "a guess carries no query"
    if parsed.fragment:
        return "a guess carries no fragment"
    if any(host == engine or host.endswith(f".{engine}") for engine in _SEARCH_ENGINES):
        return f"{host} is a search engine"
    if "." not in host:
        return f"{host} is not a site name"
    return None


def is_address(url: str) -> bool:
    """Tell whether text is an absolute URL: it parses and has a scheme and a host.

    Any scheme counts, so `ftp://example.com/` is an address that `admit`
    refuses, while `careers`, `acme.example/jobs`, `//acme.example/jobs` and
    the empty string are not addresses at all.

    Args:
        url: The text the agent gave as a URL.

    Returns:
        `True` when the text has a scheme and a host.
    """
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return False
    return bool(parsed.scheme) and bool(parsed.raw_host)


def request_host(url: str) -> str | None:
    """Return the host a request for an http or https URL would reach.

    The host is read as `httpx` reads it, so ideographic dots, case and a
    trailing dot change nothing.

    Args:
        url: The absolute URL.

    Returns:
        The host in ASCII form, or `None` when the URL is not an http or https
        URL with a host.
    """
    return _host(url)


def site_guard(rule: SiteRule) -> RequestGuard:
    """Build a request guard that only lets requests to the rule's site through.

    The rule is read at each request, so a guess that moves the site before
    the request is sent is honoured.

    Args:
        rule: The rule that decides which hosts are allowed.

    Returns:
        An async hook taking the `httpx.Request` about to be sent, for
        `PoliteClient.guard_requests`. It raises `OffSiteError`, before
        anything is sent, for a host the rule does not allow.
    """

    async def guard(request: httpx.Request) -> None:
        """Raise unless the request goes to a host the rule allows.

        Raises:
            OffSiteError: The request's host is off the site.
        """
        host = _ascii_host(request.url)
        if not rule.allows(host):
            raise OffSiteError(host)

    return guard


def _web_url(url: str) -> httpx.URL | None:
    """Parse an http or https URL that has a host, or return `None`."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return None
    if parsed.scheme not in _WEB_SCHEMES or not _ascii_host(parsed):
        return None
    return parsed


def _ascii_host(url: httpx.URL) -> str:
    """Return a parsed URL's host in ASCII form, lowercase, without a trailing dot."""
    return url.raw_host.decode("ascii").rstrip(".")


def _host(url: str) -> str | None:
    """Return the ASCII host of an http or https URL, or `None`."""
    parsed = _web_url(url)
    return None if parsed is None else _ascii_host(parsed)


def _bare_host(url: str) -> str | None:
    """Return the host a URL names as a site, or `None` when it names none.

    A leading `www.` is removed unless that would leave a host without a dot,
    and a host without a dot is no site.
    """
    host = _host(url)
    if host is None:
        return None
    bare = host.removeprefix(_WWW)
    if "." not in bare:
        bare = host
    return bare if "." in bare else None
