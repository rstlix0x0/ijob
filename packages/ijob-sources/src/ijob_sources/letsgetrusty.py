"""Let's Get Rusty's job board as a source of sighting drafts.

`LetsGetRusty` reads the board's listing pages through the `PoliteClient`. Page
1 states the total number of jobs ("Showing 1-10 of 194 jobs"), which sets how
many pages follow, up to a cap. Each page carries its jobs as the items of a
schema.org `ItemList` in a JSON-LD script; `parse_lgr_page` turns them into a
`SourceBatch`. A job that cannot be read is logged at `WARNING` and counted in
`SourceBatch.rejected`. No request carries a `job` parameter: the site's
robots.txt disallows a listing URL that combines `page` and `job`.
"""

import json
import logging
import re
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

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
from ijob_sources.jsonld import JobPostingFields, ld_json_blocks, read_job_posting
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_PAGE_URL: Final = "https://jobs.letsgetrusty.com/?page={number}"
_PAGE_SIZE: Final = 10
_MAX_PAGES: Final = 20
_TOTAL: Final = re.compile(r"Showing\s+\d+\s*-\s*\d+\s+of\s+(\d+)\s+jobs")


class LetsGetRusty:
    """The Let's Get Rusty job board."""

    def __init__(self, key: SourceKey) -> None:
        """Name the source.

        Args:
            key: The token that names this source within its kind.
        """
        self._key = key

    @property
    def ref(self) -> SourceRef:
        """The named source this is."""
        return SourceRef(SourceKind.LETSGETRUSTY, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the listing pages and merge their jobs.

        Args:
            client: The client the requests go through; the first page is
                conditional, the others are plain.

        Returns:
            The drafts of the readable jobs, one per job id, and the count of
            the jobs dropped.

        Raises:
            SourceNotFoundError: A page does not exist.
            ParseError: The first page states no total, or a page holds no
                job list.
            SourcesError: The client could not complete a request.
        """
        first = await client.get(_PAGE_URL.format(number=1), conditional=True)
        first_text = first.content.decode(errors="replace")
        pages = _page_count(_total(first_text))
        batches = [parse_lgr_page(first_text, self._key)]
        for number in range(2, pages + 1):
            response = await client.get(_PAGE_URL.format(number=number))
            batches.append(
                parse_lgr_page(response.content.decode(errors="replace"), self._key)
            )
        return _merge(batches)


def _total(html_text: str) -> int:
    """Read the number of jobs a first page states.

    Raises:
        ParseError: The page has no "Showing … of … jobs" text.
    """
    found = _TOTAL.search(html_text)
    if found is None:
        reason = "page states no total number of jobs"
        raise ParseError(reason)
    return int(found.group(1))


def _page_count(total: int) -> int:
    """Return how many listing pages to read for a total, at least one."""
    return max(1, min(-(-total // _PAGE_SIZE), _MAX_PAGES))


def _merge(batches: list[SourceBatch]) -> SourceBatch:
    """Join page batches, keeping the first draft of each external id."""
    drafts: list[SightingDraft] = []
    seen: set[ExternalId] = set()
    for batch in batches:
        for draft in batch.drafts:
            if draft.external_id not in seen:
                seen.add(draft.external_id)
                drafts.append(draft)
    return SourceBatch(drafts=drafts, rejected=sum(b.rejected for b in batches))


def parse_lgr_page(html_text: str, key: SourceKey) -> SourceBatch:
    """Read the jobs out of one listing page.

    Args:
        html_text: The page's HTML.
        key: The source the page belongs to.

    Returns:
        The drafts of the readable jobs and the count of the jobs dropped.

    Raises:
        ParseError: The page has no `ItemList` JSON-LD block with a list of
            elements.
    """
    elements = _item_list(html_text)
    drafts: list[SightingDraft] = []
    rejected = 0
    for index, element in enumerate(elements):
        try:
            drafts.append(_to_draft(element, key))
        except InvalidValueError as error:
            rejected += 1
            _LOGGER.warning(
                "rejected letsgetrusty job %s: %s", _job_label(element, index), error
            )
    return SourceBatch(drafts=drafts, rejected=rejected)


def _item_list(html_text: str) -> list[object]:
    """Return the `itemListElement` list of the page's first `ItemList` block.

    Raises:
        ParseError: The page has no such block.
    """
    for block in ld_json_blocks(html_text):
        if isinstance(block, dict) and block.get("@type") == "ItemList":
            elements = block.get("itemListElement")
            if isinstance(elements, list):
                return elements
    reason = "page has no 'ItemList' JSON-LD block"
    raise ParseError(reason)


def _job_label(element: object, index: int) -> str:
    """Name a job for the log by its id, or by its position when it has none.

    The id is written with `repr`, so a page cannot put line breaks or terminal
    escapes into the log.
    """
    if isinstance(element, dict):
        item = element.get("item")
        url = item.get("url") if isinstance(item, dict) else None
        if isinstance(url, str) and (job_id := _job_id(url)) is not None:
            return f"id {job_id!r}"
    return f"index {index}"


def _job_id(url: str) -> str | None:
    """Return the `job` query value of a job URL, or `None` when it has none."""
    try:
        values = parse_qs(urlsplit(url).query).get("job")
    except ValueError:
        return None
    return values[0] if values else None


def _to_draft(element: object, key: SourceKey) -> SightingDraft:
    """Map one `ListItem` to a draft.

    Raises:
        InvalidValueError: The element is not a `ListItem` holding a
            `JobPosting` object, a field it needs is missing or unusable, or
            the draft fails validation.
    """
    item = element.get("item") if isinstance(element, dict) else None
    if not isinstance(item, dict):
        raise InvalidValueError("item", "is missing or not an object")
    if item.get("@type") != "JobPosting":
        raise InvalidValueError("item", "is not a JobPosting")
    fields = read_job_posting(item)
    return SightingDraft(
        source=SourceRef(SourceKind.LETSGETRUSTY, key),
        external_id=ExternalId(_external_id(fields)),
        title=_required(fields.title, "title"),
        company=_required(fields.company, "hiringOrganization.name"),
        location=fields.location,
        url=_required(fields.url, "url"),
        posted_at=fields.posted_at,
        raw=_raw(item),
        salaries=fields.salaries,
        country=fields.country,
    )


def _external_id(fields: JobPostingFields) -> str:
    """Return the `job` value of the posting's URL.

    Raises:
        InvalidValueError: The posting has no URL, or its URL has no `job`
            value.
    """
    url = _required(fields.url, "url")
    try:
        job_id = _job_id(url)
    except ValueError as error:
        raise InvalidValueError("url", f"{url!r} cannot be parsed") from error
    if job_id is None:
        raise InvalidValueError("url", f"{url!r} has no 'job' parameter")
    return job_id


def _required(value: str | None, name: str) -> str:
    """Return a field that must be present and not blank."""
    if value is None:
        raise InvalidValueError(name, "is missing or blank")
    return value


def _raw(item: dict[str, Any]) -> str:
    """Serialise the item back to JSON text.

    Raises:
        InvalidValueError: The item is nested too deeply to encode.
    """
    try:
        return json.dumps(item)
    except RecursionError as error:
        raise InvalidValueError("item", "is nested too deeply") from error
