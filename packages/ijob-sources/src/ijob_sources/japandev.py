"""Japan Dev's jobs, read through its sitemap and the JSON-LD of each job page.

`JapanDev` makes one conditional request for the sitemap, and `parse_sitemap`
lists the job pages in it. The last path segment of a job page's URL is the
job's id. A job already in the `known` set is reported in
`SourceBatch.still_listed` and not fetched. Each other job, up to a cap per
fetch, costs one plain request for its page, whose `JobPosting` JSON-LD block
`read_job_posting` turns into a draft. A job that cannot be fetched or read is
logged at `WARNING` and counted in `SourceBatch.rejected`; the rest of the
batch survives. Nothing but the sitemap and the job pages is requested.
"""

import json
import logging
import xml.etree.ElementTree as ET
from typing import Final
from urllib.parse import urlsplit

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
_HOST: Final = "japan-dev.com"
_SITEMAP_URL: Final = f"https://{_HOST}/cdn/sitemaps/sitemap.xml"
_JOBS_PREFIX: Final = "/jobs/"
_MAX_DETAILS: Final = 100


class JapanDev:
    """The Japan Dev job board."""

    def __init__(
        self, key: SourceKey, *, known: frozenset[ExternalId] = frozenset()
    ) -> None:
        """Name the source and the jobs it need not fetch again.

        Args:
            key: The token that names this source within its kind.
            known: The ids of the jobs already stored. Such a job is reported
                as still listed while the sitemap names it, and its page is not
                requested.
        """
        self._key = key
        self._known = known

    @property
    def ref(self) -> SourceRef:
        """The named source this is."""
        return SourceRef(SourceKind.JAPANDEV, self._key)

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Read the sitemap, then the page of each new job.

        Args:
            client: The client the requests go through; the sitemap request is
                conditional and the job page requests are plain.

        Returns:
            The drafts of the new jobs that could be read, the count of the new
            jobs dropped, and the known jobs the sitemap still lists. At most
            100 new jobs are fetched; the rest wait for a later fetch.

        Raises:
            SourceNotFoundError: The sitemap does not exist.
            ParseError: The sitemap cannot be read.
            SourcesError: The client could not fetch the sitemap.
        """
        response = await client.get(_SITEMAP_URL, conditional=True)
        listed: dict[str, str] = {}
        for url in parse_sitemap(response.content):
            listed.setdefault(_slug(url), url)

        still_listed = tuple(
            ExternalId(slug) for slug in listed if ExternalId(slug) in self._known
        )
        new = [
            (slug, url)
            for slug, url in listed.items()
            if ExternalId(slug) not in self._known
        ]

        drafts: list[SightingDraft] = []
        rejected = 0
        for slug, url in new[:_MAX_DETAILS]:
            try:
                page = await client.get(url)
                html_text = page.content.decode(errors="replace")
                drafts.append(_to_draft(html_text, url, slug, self._key))
            except (SourcesError, InvalidValueError) as error:
                rejected += 1
                _LOGGER.warning("rejected japandev job %r: %s", slug, error)
        return SourceBatch(drafts=drafts, rejected=rejected, still_listed=still_listed)


def parse_sitemap(body: bytes) -> list[str]:
    """List the job page URLs in a sitemap.

    The XML parser refuses a document that expands entities past its
    amplification limit, and a body that declares an encoding Python does not
    know cannot be decoded, so a hostile body ends as a `ParseError`.

    Args:
        body: The sitemap's XML.

    Returns:
        The `<loc>` URLs, in document order and without repeats, that are
        `https` addresses on Japan Dev whose path starts with `/jobs/`, ends in
        a non-empty segment and carries no query or fragment.

    Raises:
        ParseError: The body is not XML, or its root is not a `urlset`.
    """
    try:
        root = ET.fromstring(body)  # noqa: S314 - expat caps entity expansion
    except (ET.ParseError, RecursionError, LookupError) as error:
        reason = f"sitemap is not XML: {error}"
        raise ParseError(reason) from error
    if _local_name(root.tag) != "urlset":
        reason = "sitemap root is not 'urlset'"
        raise ParseError(reason)
    urls: dict[str, None] = {}
    for element in root.iter():
        if _local_name(element.tag) != "loc" or not element.text:
            continue
        url = element.text.strip()
        if _is_job_url(url):
            urls[url] = None
    return list(urls)


def _local_name(tag: str) -> str:
    """Return an element's tag without its namespace."""
    return tag.rpartition("}")[2]


def _is_job_url(url: str) -> bool:
    """Say whether a sitemap URL is a Japan Dev job page."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == _HOST
        and parts.path.startswith(_JOBS_PREFIX)
        and not parts.path.endswith("/")
        and not parts.query
        and not parts.fragment
    )


def _slug(url: str) -> str:
    """Return the last path segment of a job page URL."""
    return urlsplit(url).path.rpartition("/")[2]


def _to_draft(html_text: str, url: str, slug: str, key: SourceKey) -> SightingDraft:
    """Map the first `JobPosting` of a job page to a draft.

    The draft's id is the URL slug, the same id `fetch` matches against `known`;
    the posting's own `identifier` is ignored.

    Raises:
        InvalidValueError: The page has no `JobPosting`, a field it needs is
            missing or unusable, or the draft fails validation.
    """
    postings = job_postings(html_text)
    if not postings:
        raise InvalidValueError("page", "has no JobPosting block")
    posting = postings[0]
    fields = read_job_posting(posting)
    if fields.title is None:
        raise InvalidValueError("title", "is missing or blank")
    if fields.company is None:
        raise InvalidValueError("hiringOrganization.name", "is missing or blank")
    return SightingDraft(
        source=SourceRef(SourceKind.JAPANDEV, key),
        external_id=ExternalId(slug),
        title=fields.title,
        company=fields.company,
        location=fields.location,
        url=fields.url or url,
        posted_at=fields.posted_at,
        raw=_raw(url, posting),
        salaries=fields.salaries,
        country=fields.country,
        remote=fields.remote,
    )


def _raw(url: str, posting: object) -> str:
    """Serialise the sitemap URL and the posting to JSON text.

    Raises:
        InvalidValueError: The posting is nested too deeply to encode.
    """
    try:
        return json.dumps({"item": url, "jobposting": posting})
    except RecursionError as error:
        raise InvalidValueError("jobposting", "is nested too deeply") from error
