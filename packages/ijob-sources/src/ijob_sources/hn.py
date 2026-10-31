"""The monthly Hacker News "Who is hiring?" thread as a source of sighting drafts.

`HnWhoIsHiring` asks the Algolia HN search API for the newest hiring thread,
then for its comments. `parse_thread` picks the thread; `parse_comments` turns
each top-level comment into a draft. Companies write the first line of a
comment by convention as `Company | Title | Location | ...`, so every draft is
marked `low_confidence`. A comment whose first line has fewer than two fields,
or whose draft is invalid, is logged at `WARNING` and counted in
`SourceBatch.rejected`.
"""

import html
import json
import logging
import re
from datetime import datetime
from typing import Any, Final

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
_API: Final = "https://hn.algolia.com/api/v1"
_THREADS_URL: Final = f"{_API}/search_by_date?tags=story,author_whoishiring"
_COMMENTS_URL: Final = f"{_API}/search?tags=comment,story_{{story_id}}&hitsPerPage=1000"
_ITEM_URL: Final = "https://news.ycombinator.com/item?id={object_id}"
_THREAD_TITLE_PREFIX: Final = "Ask HN: Who is hiring?"
_PARAGRAPH: Final = re.compile(r"<p\s*/?>", re.IGNORECASE)
_TAG: Final = re.compile(r"<[^<>]*>")
_MAX_ID_DIGITS: Final = 18


class HnWhoIsHiring:
    """The newest "Ask HN: Who is hiring?" thread."""

    def __init__(self, key: SourceKey) -> None:
        """Name the source.

        Args:
            key: The token that names this source within its kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this is."""
        return SourceRef(SourceKind.HN, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Find the newest hiring thread and read its top-level comments.

        Args:
            client: The client the two requests go through.

        Returns:
            The drafts of the readable comments and the count of the comments
            dropped.

        Raises:
            SourceNotFoundError: A request answered 404.
            ParseError: There is no hiring thread, or a response is not a
                search result.
            SourcesError: The client could not complete a request.
        """
        threads = await client.get(_THREADS_URL)
        story_id = parse_thread(threads.content)
        comments = await client.get(_COMMENTS_URL.format(story_id=story_id))
        return parse_comments(comments.content, story_id, self._key)


def parse_thread(body: bytes) -> int:
    """Pick the newest hiring thread out of a story search result.

    A thread's `objectID` is a JSON string; the result is its integer value.

    Args:
        body: The response body, a JSON object with a `hits` list.

    Returns:
        The id of the newest hit whose title starts with `Ask HN: Who is
        hiring?`.

    Raises:
        ParseError: The body is not a search result, or no hit is a hiring
            thread.
    """
    newest: tuple[int, str] | None = None
    for hit in _hits(body):
        if not isinstance(hit, dict):
            continue
        title = hit.get("title")
        object_id = _numeric_id(hit.get("objectID"))
        if (
            not isinstance(title, str)
            or not title.startswith(_THREAD_TITLE_PREFIX)
            or object_id is None
        ):
            continue
        created = hit.get("created_at_i")
        age = (
            created if isinstance(created, int) and not isinstance(created, bool) else 0
        )
        if newest is None or age > newest[0]:
            newest = (age, object_id)
    if newest is None:
        reason = "no 'Who is hiring?' thread among the hits"
        raise ParseError(reason)
    return int(newest[1])


def parse_comments(body: bytes, story_id: int, key: SourceKey) -> SourceBatch:
    """Read the top-level comments of a thread as drafts.

    A comment is top-level when its integer `parent_id` equals `story_id`.

    Args:
        body: The response body, a JSON object with a `hits` list.
        story_id: The thread's id.
        key: The source the response came from.

    Returns:
        The drafts of the readable comments and the count of the comments
        dropped.

    Raises:
        ParseError: The body is not a search result.
    """
    drafts: list[SightingDraft] = []
    rejected = 0
    for index, hit in enumerate(_hits(body)):
        if not _is_reply_to(hit, story_id):
            continue
        try:
            drafts.append(_to_draft(hit, key))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning("rejected hn comment %s: %s", _hit_label(hit, index), error)
    return SourceBatch(drafts=drafts, rejected=rejected)


def _hits(body: bytes) -> list[object]:
    """Return the `hits` list of a search response.

    Raises:
        ParseError: The body is not JSON or has no `hits` list.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        reason = f"body is not JSON: {error}"
        raise ParseError(reason) from error
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        reason = "body has no 'hits' list"
        raise ParseError(reason)
    return hits


def _numeric_id(value: object) -> str | None:
    """Return `value` when it is text of 1 to 18 ASCII digits, else `None`.

    The cap keeps every accepted id convertible with `int` and storable as a
    64-bit integer.
    """
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and len(value) <= _MAX_ID_DIGITS
    ):
        return value
    return None


def _is_reply_to(hit: object, story_id: int) -> bool:
    """Whether the hit is an object whose integer `parent_id` is `story_id`."""
    if not isinstance(hit, dict):
        return False
    parent_id = hit.get("parent_id")
    return (
        isinstance(parent_id, int)
        and not isinstance(parent_id, bool)
        and parent_id == story_id
    )


def _hit_label(hit: object, index: int) -> str:
    """Name a hit for the log by its `objectID`, written with `repr`."""
    if isinstance(hit, dict) and isinstance(object_id := hit.get("objectID"), str):
        return f"id {object_id!r}"
    return f"index {index}"


def _to_draft(hit: object, key: SourceKey) -> SightingDraft:
    """Map one comment to a draft.

    Raises:
        InvalidValueError: The comment is not an object, has no readable first
            line of at least two fields, or the draft fails validation.
    """
    if not isinstance(hit, dict):
        raise InvalidValueError("comment", "is not an object")
    object_id = _numeric_id(hit.get("objectID"))
    if object_id is None:
        raise InvalidValueError("objectID", "is missing or not a short numeric string")
    fields = _first_line_fields(hit.get("comment_text"))
    if len(fields) < 2:  # noqa: PLR2004 - company and title are the minimum
        raise InvalidValueError("comment_text", "first line has fewer than two fields")
    return SightingDraft(
        source=SourceRef(SourceKind.HN, key),
        external_id=ExternalId(object_id),
        title=fields[1],
        company=fields[0],
        location=(fields[2] if len(fields) > 2 else "") or None,  # noqa: PLR2004
        url=_ITEM_URL.format(object_id=object_id),
        posted_at=_created_at(hit),
        raw=_raw(hit),
        low_confidence=True,
    )


def _first_line_fields(comment_text: object) -> list[str]:
    """Split a comment's first line on `|`, each field stripped.

    The line ends at the first `<p>`. Tags are removed before entities are
    unescaped, so an escaped `&lt;` stays a literal `<`.

    Raises:
        InvalidValueError: The text is missing, not text, or not valid Unicode.
    """
    if not isinstance(comment_text, str):
        raise InvalidValueError("comment_text", "is missing or not text")
    line = _PARAGRAPH.split(comment_text, maxsplit=1)[0]
    text = html.unescape(_TAG.sub("", line))
    try:
        text.encode()
    except UnicodeEncodeError as error:
        raise InvalidValueError("comment_text", "is not valid Unicode text") from error
    return [field.strip() for field in text.split("|")]


def _created_at(hit: dict[str, Any]) -> datetime:
    """Return `created_at` as a datetime.

    Raises:
        InvalidValueError: The value is missing or not an ISO 8601 date.
    """
    value = hit.get("created_at")
    if not isinstance(value, str):
        raise InvalidValueError("created_at", "is missing or not text")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidValueError("created_at", f"{value!r} is not a date") from error


def _raw(hit: dict[str, Any]) -> str:
    """Serialise the hit back to JSON text.

    Raises:
        InvalidValueError: The hit is nested too deeply to encode.
    """
    try:
        return json.dumps(hit)
    except RecursionError as error:
        raise InvalidValueError("comment", "is nested too deeply") from error
