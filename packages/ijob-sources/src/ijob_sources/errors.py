"""Exceptions raised by ijob_sources."""

from ijob_core.model import SourceRef


class SourcesError(Exception):
    """Root of every exception ijob_sources raises."""


class RobotsDisallowedError(SourcesError):
    """The site's robots.txt does not allow the request.

    Attributes:
        url: The URL that was refused.
    """

    def __init__(self, url: str) -> None:
        """Build the error from the refused URL.

        Args:
            url: The URL that was refused.
        """
        super().__init__(f"robots.txt disallows {url}")
        self.url = url


class HttpStatusError(SourcesError):
    """The server answered with a status the caller cannot use.

    Attributes:
        url: The URL that was requested.
        status: The HTTP status code of the answer.
    """

    def __init__(self, url: str, status: int) -> None:
        """Build the error from the requested URL and the status.

        Args:
            url: The URL that was requested.
            status: The HTTP status code of the answer.
        """
        super().__init__(f"{url} answered HTTP {status}")
        self.url = url
        self.status = status


class SourceNotFoundError(HttpStatusError):
    """The server answered 404 for the requested URL."""

    def __init__(self, url: str) -> None:
        """Build the error from the requested URL.

        Args:
            url: The URL that was requested.
        """
        super().__init__(url, 404)


class FetchError(SourcesError):
    """The request failed before a usable response came back.

    Attributes:
        url: The URL that was requested.
        reason: The text of the failure that caused this error.
    """

    def __init__(self, url: str, reason: str) -> None:
        """Build the error from the requested URL and the cause's text.

        Args:
            url: The URL that was requested.
            reason: The text of the failure that caused this error.
        """
        super().__init__(f"could not fetch {url}: {reason}")
        self.url = url
        self.reason = reason


class ParseError(SourcesError):
    """A response body could not be read.

    Attributes:
        reason: What could not be read, and why.
    """

    def __init__(self, reason: str) -> None:
        """Build the error from what could not be read.

        Args:
            reason: What could not be read, and why.
        """
        super().__init__(f"could not parse response: {reason}")
        self.reason = reason


class IdentityMismatchError(SourcesError):
    """A board does not belong to the company it was registered for.

    Attributes:
        ref: The source whose board failed the check.
        reason: Why the board was refused.
    """

    def __init__(self, ref: SourceRef, reason: str) -> None:
        """Build the error from the refused source and the reason.

        Args:
            ref: The source whose board failed the check.
            reason: Why the board was refused.
        """
        super().__init__(f"{ref}: {reason}")
        self.ref = ref
        self.reason = reason


class UnsupportedUrlError(SourcesError):
    """A URL, or a source named by one, cannot be used as a job source.

    Attributes:
        url: The URL, or the text standing in for it, that was refused.
        reason: Why it was refused.
    """

    def __init__(self, url: str, reason: str) -> None:
        """Build the error from the refused URL and the reason.

        Args:
            url: The URL, or the text standing in for it, that was refused.
            reason: Why it was refused.
        """
        super().__init__(f"cannot use {url}: {reason}")
        self.url = url
        self.reason = reason
