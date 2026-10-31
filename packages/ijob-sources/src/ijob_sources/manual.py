"""Job sources a user adds by pasting a URL: one job page, or a whole feed.

`JobPostingPage` fetches one page that states a single schema.org `JobPosting`
and hands the body to `parse_jobposting_page`, which also checks that the page
names the company the source was registered for. `FeedUrl` fetches any RSS 2.0
feed and hands the body to `parse_feed`, which drafts every item under the
registered company; a generic feed names no company per item, so it has no
identity check. `manual_key` derives the source key both are filed under from
the URL.

A job or item that cannot be read is logged at `WARNING` and counted in
`SourceBatch.rejected`.
"""

import hashlib
import json
import logging
from typing import Any

from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKey,
    SourceKind,
    SourceRef,
    parse_source_key,
)

from ijob_sources.collect import SourceBatch
from ijob_sources.errors import ParseError
from ijob_sources.identity import check_identity
from ijob_sources.jsonld import job_postings, read_job_posting
from ijob_sources.polite_http import PoliteClient
from ijob_sources.rss import RssItem, item_link, item_raw, parse_rss

_LOGGER = logging.getLogger(__name__)
_KEY_LENGTH = 16


def manual_key(url: str) -> SourceKey:
    """Derive the source key of a manually added URL.

    Args:
        url: The URL the user added.

    Returns:
        The first 16 hex characters of the SHA-256 of the URL's UTF-8 bytes.
    """
    digest = hashlib.sha256(url.encode()).hexdigest()
    return parse_source_key(digest[:_KEY_LENGTH])


class JobPostingPage:
    """One job page that states a single schema.org `JobPosting`."""

    def __init__(self, key: SourceKey, *, target_url: str, company: str) -> None:
        """Name the page to read.

        Args:
            key: The source key, normally `manual_key(target_url)`.
            target_url: The page's address.
            company: The company the page was registered for; `fetch` checks
                that the page names it.
        """
        self._key = key
        self._target_url = target_url
        self._company = company

    @property
    def ref(self) -> SourceRef:
        """The named source this page is."""
        return SourceRef(SourceKind.JOBPOSTING, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the page with one plain request.

        Args:
            client: The client the request goes through.

        Returns:
            The draft of the page's job, or a rejection count when the job
            cannot be read.

        Raises:
            ParseError: The page states no `JobPosting`, or several.
            IdentityMismatchError: The page names a different company.
            SourcesError: The client could not complete the request.
        """
        response = await client.get(self._target_url)
        return parse_jobposting_page(
            response.content,
            self._key,
            target_url=self._target_url,
            company=self._company,
        )


def parse_jobposting_page(
    body: bytes, key: SourceKey, *, target_url: str, company: str
) -> SourceBatch:
    """Read the single `JobPosting` of a job page.

    The page must name the company whether or not its posting can be read:
    the check runs on the company name the posting states, before the posting
    is turned into a draft. A posting that cannot be read, or that names no
    title, is then logged and counted in `SourceBatch.rejected`.

    Args:
        body: The page's bytes, read as UTF-8.
        key: The source key of the page.
        target_url: The page's address; it stands in for the posting's own id
            and URL when the posting gives none.
        company: The company the page was registered for.

    Returns:
        The draft of the page's job, or a rejection count of one.

    Raises:
        ParseError: The page states no `JobPosting`, or several.
        IdentityMismatchError: The posting names a different company, or none.
    """
    postings = job_postings(body.decode("utf-8", errors="replace"))
    if len(postings) != 1:
        reason = f"page states {len(postings)} JobPosting objects, expected 1"
        raise ParseError(reason)
    ref = SourceRef(SourceKind.JOBPOSTING, key)
    check_identity(
        ref, company, names=_stated_companies(postings[0]), texts=[], count=1
    )
    try:
        draft = _posting_draft(postings[0], ref, target_url)
    except InvalidValueError as error:
        _LOGGER.warning("rejected job page %r: %s", target_url, error)
        return SourceBatch(drafts=[], rejected=1)
    return SourceBatch(drafts=[draft], rejected=0)


def _stated_companies(posting: dict[str, Any]) -> list[str]:
    """List the company name a posting states, even when other fields are bad."""
    organization = posting.get("hiringOrganization")
    if not isinstance(organization, dict):
        return []
    name = organization.get("name")
    return [name] if isinstance(name, str) else []


def _posting_draft(
    posting: dict[str, Any], ref: SourceRef, target_url: str
) -> SightingDraft:
    """Map a `JobPosting` object to a draft.

    Raises:
        InvalidValueError: A field of the posting is unusable, or it names no
            title or company.
    """
    fields = read_job_posting(posting)
    if fields.title is None:
        raise InvalidValueError("title", "is missing")
    if fields.company is None:
        raise InvalidValueError("company", "is missing")
    return SightingDraft(
        source=ref,
        external_id=ExternalId(fields.identifier or target_url),
        title=fields.title,
        company=fields.company,
        location=fields.location,
        url=fields.url or target_url,
        posted_at=fields.posted_at,
        raw=_raw(posting),
        salaries=fields.salaries,
        country=fields.country,
        remote=fields.remote,
    )


class FeedUrl:
    """Any RSS 2.0 feed, with every item drafted under one registered company."""

    def __init__(self, key: SourceKey, *, target_url: str, company: str) -> None:
        """Name the feed to read.

        Args:
            key: The source key, normally `manual_key(target_url)`.
            target_url: The feed's address.
            company: The company every item is drafted under.
        """
        self._key = key
        self._target_url = target_url
        self._company = company

    @property
    def ref(self) -> SourceRef:
        """The named source this feed is."""
        return SourceRef(SourceKind.FEED, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the feed with one conditional request.

        Args:
            client: The client the request goes through.

        Returns:
            The drafts of the readable items and the count of the items dropped.

        Raises:
            ParseError: The body is not an RSS document.
            SourcesError: The client could not complete the request.
        """
        response = await client.get(self._target_url, conditional=True)
        return parse_feed(
            response.content,
            self._key,
            target_url=self._target_url,
            company=self._company,
        )


def parse_feed(
    body: bytes, key: SourceKey, *, target_url: str, company: str
) -> SourceBatch:
    """Draft every item of an RSS 2.0 feed under one company.

    An item without a title, or without an `http(s)` link, is logged and counted
    in `SourceBatch.rejected`. The feed is not checked for identity.

    Args:
        body: The feed's bytes.
        key: The source key of the feed.
        target_url: The feed's address, named in the log of a dropped item.
        company: The company every item is drafted under.

    Returns:
        The drafts of the readable items and the count of the items dropped.

    Raises:
        ParseError: The body is not an RSS document.
    """
    ref = SourceRef(SourceKind.FEED, key)
    drafts: list[SightingDraft] = []
    rejected = 0
    for index, item in enumerate(parse_rss(body)):
        try:
            drafts.append(_feed_draft(item, ref, company))
        except InvalidValueError as error:
            rejected += 1
            label = f"link {item.link!r}" if item.link else f"index {index}"
            _LOGGER.warning(
                "rejected feed item %s from %r: %s", label, target_url, error
            )
    return SourceBatch(drafts=drafts, rejected=rejected)


def _feed_draft(item: RssItem, ref: SourceRef, company: str) -> SightingDraft:
    """Map an item to a draft.

    Raises:
        InvalidValueError: The item lacks a title or an `http(s)` link.
    """
    link = item_link(item)
    if item.title is None:
        raise InvalidValueError("title", "is missing")
    return SightingDraft(
        source=ref,
        external_id=ExternalId(link),
        title=item.title,
        company=company,
        location=None,
        url=link,
        posted_at=item.pub_date,
        raw=item_raw(item),
    )


def _raw(payload: dict[str, Any]) -> str:
    """Serialise a draft's payload to JSON text.

    Raises:
        InvalidValueError: The payload is nested too deeply to encode.
    """
    try:
        return json.dumps(payload)
    except RecursionError as error:
        raise InvalidValueError("raw", "is nested too deeply") from error
