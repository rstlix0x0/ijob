"""Exceptions raised by ijob_sources."""

from typing import Self

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


class NotPublicAddressError(SourcesError):
    """A request was refused because it does not go to a public address.

    Attributes:
        url: The URL of the refused request.
    """

    def __init__(self, url: str) -> None:
        """Build the error from the refused request's URL.

        Args:
            url: The URL of the refused request.
        """
        super().__init__(f"{url} is not a public address")
        self.url = url


class ReservedHostError(SourcesError):
    """A host is reserved for the source kind that reads it by its own rules.

    Attributes:
        host: The refused host name.
    """

    def __init__(self, host: str) -> None:
        """Build the error from the refused host name.

        Args:
            host: The refused host name.
        """
        super().__init__(f"{host} is read only by its own source kind")
        self.host = host


class InvalidRecipeError(SourcesError):
    """Text is not a valid recipe.

    Attributes:
        reason: What is wrong with the recipe.
    """

    def __init__(self, reason: str) -> None:
        """Build the error from what is wrong with the recipe.

        Args:
            reason: What is wrong with the recipe.
        """
        super().__init__(f"invalid recipe: {reason}")
        self.reason = reason


class UnsupportedRecipeVersionError(SourcesError):
    """A recipe states a version this release cannot read.

    Attributes:
        version: The version the recipe states.
    """

    def __init__(self, version: int) -> None:
        """Build the error from the stated version.

        Args:
            version: The version the recipe states.
        """
        super().__init__(f"recipe version {version} is not supported")
        self.version = version


class RecipeEvaluationError(SourcesError):
    """An expression engine failed while a recipe was being evaluated.

    The message is the engine's own text, so a caller can place it inside a
    longer sentence of its own.

    Attributes:
        reason: The text of the failure the engine raised.
    """

    def __init__(self, reason: str) -> None:
        """Build the error from the engine failure's text.

        Args:
            reason: The text of the failure the engine raised.
        """
        super().__init__(reason)
        self.reason = reason


class RecipeFailedError(SourcesError):
    """A stored recipe could not be used to read its source at search time.

    Every message ends by naming the command that learns the recipe again. Build
    one with the classmethod for the failure.
    """

    @classmethod
    def failed(cls, ref: SourceRef, error: str) -> Self:
        """Build the error for a recipe that is invalid or failed to evaluate.

        Args:
            ref: The recipe source.
            error: The text of the failure.
        """
        return cls(f"recipe for {ref} failed: {error}; {_reanalyze(ref)}")

    @classmethod
    def unsupported_version(cls, ref: SourceRef, version: int) -> Self:
        """Build the error for a recipe whose version this release cannot read.

        Args:
            ref: The recipe source.
            version: The version the recipe states.
        """
        return cls(f"recipe version {version} is not supported; {_reanalyze(ref)}")

    @classmethod
    def expected_format(cls, ref: SourceRef, expected: str) -> Self:
        """Build the error for a page of another format than the recipe reads.

        Args:
            ref: The recipe source.
            expected: The format the recipe reads, `html` or `json`.
        """
        return cls(f"recipe for {ref} expected {expected}; {_reanalyze(ref)}")

    @classmethod
    def no_jobs(cls, ref: SourceRef) -> Self:
        """Build the error for a first page that yielded no valid job.

        Args:
            ref: The recipe source.
        """
        return cls(
            f"recipe for {ref} found no jobs; if the site changed, {_reanalyze(ref)}"
        )


def _reanalyze(ref: SourceRef) -> str:
    """Return the command that learns a source's recipe again."""
    return f"run: ijob sources reanalyze {ref}"
