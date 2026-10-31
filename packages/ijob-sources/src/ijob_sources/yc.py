"""Y Combinator's software-engineer job pages as a source of sighting drafts.

`YcJobs` reads two listing pages through the `PoliteClient`: all software
engineering jobs and the remote ones. Each page carries its postings as JSON in
the `data-page` attribute of one element. `parse_yc_page` reads that JSON,
`YcJobs` merges the postings of both pages by `id` and maps each to a draft. A
posting that cannot be mapped is logged at `WARNING` and counted in
`SourceBatch.rejected`. The pages state how long ago a job was posted ("3
months"), not when, so no draft carries `posted_at`.
"""

import json
import logging
from html.parser import HTMLParser
from typing import Any, Final, override

from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKey,
    SourceKind,
    SourceRef,
)

from ijob_sources.collect import SourceBatch
from ijob_sources.errors import ParseError
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_ORIGIN: Final = "https://www.ycombinator.com"
_PAGE_URLS: Final = (
    f"{_ORIGIN}/jobs/role/software-engineer",
    f"{_ORIGIN}/jobs/role/software-engineer/remote",
)


class YcJobs:
    """The software-engineer job pages of Y Combinator."""

    def __init__(self, key: SourceKey) -> None:
        """Name the source.

        Args:
            key: The token that names this source within its kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this is."""
        return SourceRef(SourceKind.YC, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch both listing pages and merge their postings.

        Args:
            client: The client the requests go through; both are conditional.

        Returns:
            The drafts of the readable postings, one per `id`, and the count of
            the postings dropped.

        Raises:
            SourceNotFoundError: A page does not exist.
            ParseError: A page holds no postings list.
            SourcesError: The client could not complete a request.
        """
        postings: list[object] = []
        for url in _PAGE_URLS:
            response = await client.get(url, conditional=True)
            postings.extend(parse_yc_page(response.content.decode(errors="replace")))
        return _to_batch(postings, self._key)


class _DataPageFinder(HTMLParser):
    """Collects the value of every `data-page` attribute in a document."""

    def __init__(self) -> None:
        super().__init__()
        self.values: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.values.extend(
            value for name, value in attrs if name == "data-page" and value is not None
        )


def parse_yc_page(html_text: str) -> list[object]:
    """Read the postings out of a YC listing page.

    Args:
        html_text: The page's HTML.

    Returns:
        The elements of `props.jobPostings` in the page's `data-page` JSON,
        which are objects unless the page is malformed.

    Raises:
        ParseError: The page has no `data-page` attribute, the attribute is not
            JSON, or it holds no postings list.
    """
    finder = _DataPageFinder()
    finder.feed(html_text)
    finder.close()
    if not finder.values:
        reason = "page has no 'data-page' attribute"
        raise ParseError(reason)
    try:
        data = json.loads(finder.values[0])
    except (ValueError, RecursionError) as error:
        reason = f"'data-page' is not JSON: {error}"
        raise ParseError(reason) from error
    props = data.get("props") if isinstance(data, dict) else None
    postings = props.get("jobPostings") if isinstance(props, dict) else None
    if not isinstance(postings, list):
        reason = "'data-page' has no 'props.jobPostings' list"
        raise ParseError(reason)
    return postings


def _to_batch(postings: list[object], key: SourceKey) -> SourceBatch:
    """Map postings to drafts, keeping the first posting of each `id`."""
    drafts: list[SightingDraft] = []
    rejected = 0
    seen: set[int] = set()
    for index, posting in enumerate(postings):
        job_id = _raw_id(posting)
        if job_id is not None:
            if job_id in seen:
                continue
            seen.add(job_id)
        try:
            drafts.append(_to_draft(posting, key))
        except InvalidValueError as error:
            rejected += 1
            label = f"id {job_id!r}" if job_id is not None else f"index {index}"
            _LOGGER.warning("rejected yc posting %s: %s", label, error)
    return SourceBatch(drafts=drafts, rejected=rejected)


def _raw_id(posting: object) -> int | None:
    """Return the posting's `id` when it is a JSON integer, else `None`."""
    if not isinstance(posting, dict):
        return None
    job_id = posting.get("id")
    if isinstance(job_id, int) and not isinstance(job_id, bool):
        return job_id
    return None


def _to_draft(posting: object, key: SourceKey) -> SightingDraft:
    """Map one posting to a draft.

    Raises:
        InvalidValueError: The posting is not an object, a field it needs is
            missing or of the wrong type, or the draft fails validation.
    """
    if not isinstance(posting, dict):
        raise InvalidValueError("posting", "is not an object")
    job_id = _raw_id(posting)
    if job_id is None:
        raise InvalidValueError("id", "is missing or not an integer")
    path = _required_text(posting, "url")
    if not path.startswith("/"):
        raise InvalidValueError("url", f"{path!r} is not a site path")
    return SightingDraft(
        source=SourceRef(SourceKind.YC, key),
        external_id=ExternalId(str(job_id)),
        title=_required_text(posting, "title"),
        company=_required_text(posting, "companyName"),
        location=_optional_text(posting, "location"),
        url=_ORIGIN + path,
        posted_at=None,
        raw=_raw(posting),
    )


def _raw(posting: dict[str, Any]) -> str:
    """Serialise the posting back to JSON text.

    Raises:
        InvalidValueError: The posting is nested too deeply to encode.
    """
    try:
        return json.dumps(posting)
    except RecursionError as error:
        raise InvalidValueError("posting", "is nested too deeply") from error


def _required_text(posting: dict[str, Any], name: str) -> str:
    """Return a field that must be present and text."""
    value = posting.get(name)
    if not isinstance(value, str):
        raise InvalidValueError(name, "is missing or not text")
    return _encodable(name, value)


def _optional_text(posting: dict[str, Any], name: str) -> str | None:
    """Return a field that may be absent, null or empty, but is text when set."""
    value = posting.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError(name, "is not text")
    return _encodable(name, value) or None


def _encodable(name: str, value: str) -> str:
    """Return text that can be stored as UTF-8, such as one without lone surrogates.

    Raises:
        InvalidValueError: The text cannot be encoded as UTF-8.
    """
    try:
        value.encode()
    except UnicodeEncodeError as error:
        raise InvalidValueError(name, "is not valid Unicode text") from error
    return value
