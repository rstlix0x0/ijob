"""RSS 2.0 job feeds as a source of sighting drafts.

`parse_rss` reads any RSS 2.0 body into `RssItem` values and applies no feed's
rules. `FEEDS` holds the rules of each known feed: where the company sits in
the title, where the location sits, and whether the item only links to a page
that carries the job. `RssFeed` fetches one of them through the `PoliteClient`.
`item_link` and `item_raw` are the link check and the `raw` text of an item, for
every source that drafts feed items.

A feed whose items name no company is a detail feed. `RssFeed` fetches the
detail page of each item it has not been told is known, reads the page's
schema.org `JobPosting`, and reports the known items in
`SourceBatch.still_listed`. An item that cannot be read is logged at `WARNING`
and counted in `SourceBatch.rejected`.
"""

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlsplit
from xml.etree.ElementTree import (
    Element,
    fromstring,
)
from xml.etree.ElementTree import ParseError as XmlParseError

from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    SightingDraft,
    SourceKey,
    SourceKind,
    SourceRef,
)

from ijob_sources.collect import SourceBatch
from ijob_sources.errors import ParseError, SourcesError
from ijob_sources.jsonld import job_postings, read_job_posting
from ijob_sources.polite_http import PoliteClient

_LOGGER = logging.getLogger(__name__)
_DETAIL_CAP: Final = 100
_HTTP_PREFIXES: Final = ("http://", "https://")
_ITEM_FIELDS: Final = frozenset({"title", "link", "pubDate", "description"})

type TitleRule = Callable[[str], tuple[str, str]]
type LocationRule = Callable[[RssItem], str | None]


@dataclass(frozen=True, slots=True)
class RssItem:
    """One `<item>` of an RSS 2.0 channel.

    Attributes:
        title: The item's title, or `None` when it has none.
        link: The item's link, or `None` when it has none.
        pub_date: When the item was published, as an aware datetime, or `None`
            when the item has no `pubDate` or an unreadable one. A date with
            no stated zone is read as UTC.
        description: The description with HTML entities unescaped, empty when
            the item has none.
        extras: The text of every other child element, by tag. A namespaced
            tag keeps its namespace (`{uri}name`); when a tag repeats, the
            first occurrence is kept.
    """

    title: str | None
    link: str | None
    pub_date: datetime | None
    description: str
    extras: Mapping[str, str]


def parse_rss(body: bytes) -> list[RssItem]:
    """Read the items of an RSS 2.0 feed.

    Args:
        body: The feed's bytes.

    Returns:
        The items of the feed's `channel`, in document order.

    Raises:
        ParseError: The body is not XML (this includes a declared encoding
            Python does not know), declares XML entities, or is not an RSS
            document with a `channel`.
    """
    _check_body(body)
    try:
        root = fromstring(body)  # noqa: S314 - _check_body refuses entity declarations
    except (XmlParseError, ValueError, LookupError) as error:
        reason = f"body is not XML: {error}"
        raise ParseError(reason) from error
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        reason = "body has no RSS 'channel'"
        raise ParseError(reason)
    return [_read_item(element) for element in channel.findall("item")]


def _check_body(body: bytes) -> None:
    """Refuse a body that could expand entities.

    A feed has no use for entity declarations, and the parser's own limit on
    expansion is the second line of defence, not the first. A NUL byte marks a
    wide encoding in which a declaration would not match the search, and no
    feed this module reads uses one.

    Raises:
        ParseError: The body declares an entity or holds a NUL byte.
    """
    if b"\x00" in body:
        reason = "body is not in an ASCII-compatible encoding"
        raise ParseError(reason)
    if b"<!ENTITY" in body:
        reason = "body declares XML entities"
        raise ParseError(reason)


def _read_item(element: Element) -> RssItem:
    """Read one `<item>` element."""
    texts: dict[str, str] = {}
    for child in element:
        texts.setdefault(child.tag, (child.text or "").strip())
    return RssItem(
        title=texts.get("title") or None,
        link=texts.get("link") or None,
        pub_date=_read_date(texts.get("pubDate")),
        description=unescape(texts.get("description", "")).strip(),
        extras=MappingProxyType(
            {tag: text for tag, text in texts.items() if tag not in _ITEM_FIELDS}
        ),
    )


def _read_date(text: str | None) -> datetime | None:
    """Read an RFC 822 date, or return `None` when there is none or it is unreadable."""
    if not text:
        return None
    try:
        when = parsedate_to_datetime(text)
    except (ValueError, OverflowError):
        _LOGGER.warning("unreadable rss pubDate %r", text)
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _split_first(separator: str) -> TitleRule:
    """Build a rule for `Company<separator>Role`, split at the first separator."""

    def rule(title: str) -> tuple[str, str]:
        company, found, role = title.partition(separator)
        return _both(role, company, found, separator)

    return rule


def _split_last(separator: str, *, no_break_space: bool = False) -> TitleRule:
    """Build a rule for `Role<separator>Company`, split at the last separator.

    With `no_break_space`, U+00A0 is read as a plain space first.
    """

    def rule(title: str) -> tuple[str, str]:
        text = title.replace("\xa0", " ") if no_break_space else title
        role, found, company = text.rpartition(separator)
        return _both(role, company, found, separator)

    return rule


def _both(role: str, company: str, found: str, separator: str) -> tuple[str, str]:
    """Return the stripped role and company, which must both be non-empty.

    Raises:
        InvalidValueError: The title lacked the separator, or a side is empty.
    """
    if not found:
        raise InvalidValueError("title", f"has no {separator!r}")
    role, company = role.strip(), company.strip()
    if not role or not company:
        raise InvalidValueError("title", f"has an empty side of {separator!r}")
    return role, company


def _extra(tag: str) -> LocationRule:
    """Build a rule that reads the location from a child element."""

    def rule(item: RssItem) -> str | None:
        return item.extras.get(tag) or None

    return rule


def _python_location(item: RssItem) -> str | None:
    """Read the description's first line, up to the first `<`."""
    first_line = item.description.partition("\n")[0]
    return first_line.partition("<")[0].strip() or None


def _golang_location(item: RssItem) -> str | None:
    """Read the description's text before the first `" - "`, if it has one."""
    before, found, _ = item.description.partition(" - ")
    return (before.strip() or None) if found else None


@dataclass(frozen=True, slots=True)
class FeedSpec:
    """How to read one known feed.

    Attributes:
        url: The feed's address.
        title_rule: Splits an item's title into `(role, company)` and raises
            `InvalidValueError` when it cannot; `None` for a detail feed.
        location_rule: Reads an item's location; `None` when the feed gives none.
        detail: Whether each item only links to a page that states the job.
    """

    url: str
    title_rule: TitleRule | None
    location_rule: LocationRule | None
    detail: bool


FEEDS: Final[Mapping[SourceKey, FeedSpec]] = MappingProxyType(
    {
        SourceKey("wwr-programming"): FeedSpec(
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            _split_first(": "),
            _extra("region"),
            detail=False,
        ),
        SourceKey("wwr-backend"): FeedSpec(
            "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
            _split_first(": "),
            _extra("region"),
            detail=False,
        ),
        SourceKey("python"): FeedSpec(
            "https://www.python.org/jobs/feed/rss/",
            _split_last(", "),
            _python_location,
            detail=False,
        ),
        SourceKey("golang"): FeedSpec(
            "https://www.golangprojects.com/rss.xml",
            _split_last(" @ ", no_break_space=True),
            _golang_location,
            detail=False,
        ),
        SourceKey("bsj"): FeedSpec(
            "https://berlinstartupjobs.com/engineering/feed/",
            _split_last(" // "),
            None,
            detail=False,
        ),
        SourceKey("elixir"): FeedSpec(
            "https://elixirjobs.net/rss", None, None, detail=True
        ),
        SourceKey("djinni-python"): FeedSpec(
            "https://djinni.co/jobs/rss/?primary_keyword=Python",
            None,
            None,
            detail=True,
        ),
    }
)


class RssFeed:
    """One of the known RSS feeds, read through the `PoliteClient`."""

    def __init__(
        self, key: SourceKey, *, known: frozenset[ExternalId] = frozenset()
    ) -> None:
        """Name the feed to read.

        Args:
            key: The feed's key, one of `FEEDS`.
            known: The ids of the jobs already stored. Only a detail feed uses
                it: those jobs are reported as still listed and their pages
                are not fetched.

        Raises:
            InvalidValueError: `key` is not a known feed.
        """
        spec = FEEDS.get(key)
        if spec is None:
            raise InvalidValueError("feed", f"{key!r} is not a known feed")
        self._key = key
        self._spec = spec
        self._known = known

    @property
    def ref(self) -> SourceRef:
        """The named source this feed is."""
        return SourceRef(SourceKind.RSS, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the feed, and the detail page of each new item of a detail feed.

        The feed is one conditional request. A detail feed adds one plain
        request per item that is not known, at most 100 in all; the items
        beyond that are neither drafted nor reported as still listed. An item
        whose link is not an `https` address on the feed's own host is
        rejected, and nothing is requested for it.

        Args:
            client: The client the requests go through.

        Returns:
            The drafts of the readable items and the count of the items dropped.

        Raises:
            ParseError: The feed is not an RSS document.
            SourcesError: The client could not fetch the feed.
        """
        response = await client.get(self._spec.url, conditional=True)
        items = parse_rss(response.content)
        if self._spec.detail:
            return await self._detail_batch(client, items)
        return self._plain_batch(items)

    def _plain_batch(self, items: list[RssItem]) -> SourceBatch:
        """Draft every item from the feed alone."""
        drafts: list[SightingDraft] = []
        rejected = 0
        for index, item in enumerate(items):
            try:
                drafts.append(self._plain_draft(item))
            except InvalidValueError as error:
                rejected += 1
                self._log_rejected(item, index, error)
        return SourceBatch(drafts=drafts, rejected=rejected)

    async def _detail_batch(
        self, client: PoliteClient, items: list[RssItem]
    ) -> SourceBatch:
        """Draft each new item from its detail page."""
        drafts: list[SightingDraft] = []
        still_listed: list[ExternalId] = []
        rejected = 0
        fetched = 0
        for index, item in enumerate(items):
            try:
                link = _detail_link(item, self._spec.url)
                if ExternalId(link) in self._known:
                    still_listed.append(ExternalId(link))
                    continue
                if fetched == _DETAIL_CAP:
                    continue
                fetched += 1
                page = await client.get(link)
                drafts.append(self._detail_draft(item, link, page.content))
            except (InvalidValueError, SourcesError) as error:
                rejected += 1
                self._log_rejected(item, index, error)
        return SourceBatch(
            drafts=drafts, rejected=rejected, still_listed=tuple(still_listed)
        )

    def _plain_draft(self, item: RssItem) -> SightingDraft:
        """Map an item to a draft by the feed's title and location rules.

        Raises:
            InvalidValueError: The item lacks a title or link, or its title
                does not follow the feed's rule.
        """
        link = item_link(item)
        if item.title is None or self._spec.title_rule is None:
            raise InvalidValueError("title", "is missing")
        title, company = self._spec.title_rule(item.title)
        location_rule = self._spec.location_rule
        return SightingDraft(
            source=self.ref,
            external_id=ExternalId(link),
            title=title,
            company=company,
            location=location_rule(item) if location_rule is not None else None,
            url=link,
            posted_at=item.pub_date,
            raw=item_raw(item),
        )

    def _detail_draft(self, item: RssItem, link: str, page: bytes) -> SightingDraft:
        """Map an item and its detail page to a draft.

        Raises:
            InvalidValueError: The page states no `JobPosting`, the posting names
                no company or title, or one of its fields is unusable.
        """
        postings = job_postings(page.decode("utf-8", errors="replace"))
        if not postings:
            raise InvalidValueError("jobposting", "page has none")
        fields = read_job_posting(postings[0])
        company = fields.company
        title = fields.title or item.title
        if company is None:
            raise InvalidValueError("company", "is missing from the posting")
        if title is None:
            raise InvalidValueError("title", "is missing")
        return SightingDraft(
            source=self.ref,
            external_id=ExternalId(link),
            title=title,
            company=company,
            location=fields.location,
            url=link,
            posted_at=item.pub_date,
            raw=_raw({"item": _item_json(item), "jobposting": postings[0]}),
            salaries=fields.salaries,
            country=fields.country,
            remote=fields.remote,
        )

    def _log_rejected(self, item: RssItem, index: int, error: Exception) -> None:
        """Log a dropped item by its link, or by its position when it has none.

        The link is written with `repr`, so a feed cannot put line breaks or
        terminal escapes into the log.
        """
        label = f"link {item.link!r}" if item.link else f"index {index}"
        _LOGGER.warning("rejected rss item %s from %s: %s", label, self._key, error)


def item_link(item: RssItem) -> str:
    """Return the item's link, which must be an `http(s)` address.

    Raises:
        InvalidValueError: The item has no link, or one that is not `http(s)`.
    """
    if item.link is None:
        raise InvalidValueError("link", "is missing")
    if not item.link.startswith(_HTTP_PREFIXES):
        raise InvalidValueError("link", f"{item.link!r} is not an http(s) address")
    return item.link


def _detail_link(item: RssItem, feed_url: str) -> str:
    """Return the item's link, which must be an `https` address on the feed's host.

    A detail feed's items are fetched, so a link that leads elsewhere would turn
    the feed into a way to send requests to any host.

    Raises:
        InvalidValueError: The item has no link, or one that is not an `https`
            address on the host the feed itself is served from.
    """
    link = item_link(item)
    try:
        parts = urlsplit(link)
        hostname = parts.hostname
    except ValueError as error:
        raise InvalidValueError("link", f"{link!r} cannot be parsed") from error
    if parts.scheme != "https" or hostname is None:
        raise InvalidValueError("link", f"{link!r} is not an https address")
    if hostname != urlsplit(feed_url).hostname:
        raise InvalidValueError("link", f"{link!r} is not on the feed's host")
    return link


def item_raw(item: RssItem) -> str:
    """Serialise an item's fields and extras to the JSON text a draft keeps as `raw`.

    Args:
        item: The item to serialise.

    Returns:
        A JSON object with the item's `title`, `link`, `pub_date`,
        `description` and `extras`.

    Raises:
        InvalidValueError: The item is nested too deeply to encode.
    """
    return _raw(_item_json(item))


def _item_json(item: RssItem) -> dict[str, Any]:
    """Return the item's fields as a JSON-ready object."""
    return {
        "title": item.title,
        "link": item.link,
        "pub_date": item.pub_date.isoformat() if item.pub_date else None,
        "description": item.description,
        "extras": dict(item.extras),
    }


def _raw(payload: dict[str, Any]) -> str:
    """Serialise a draft's payload to JSON text.

    Raises:
        InvalidValueError: The payload is nested too deeply to encode.
    """
    try:
        return json.dumps(payload)
    except RecursionError as error:
        raise InvalidValueError("raw", "is nested too deeply") from error
