"""A sighting's posting text, read from its stored payload or fetched on demand.

`describe` answers for every `SourceKind`. Sixteen kinds keep the posting's text
in the `raw` payload of their sightings, so the owning adapter module reads it
back with no request. Three kinds keep only a reference: Workable (a v2 job
route), Y Combinator (the job page) and Let's Get Rusty (the job page). Those
cost one request through the `PoliteClient`, so robots.txt, spacing and the
User-Agent apply as for any other fetch.
"""

import json
from typing import Final, assert_never
from urllib.parse import quote, urlsplit

from ijob_core.model import Sighting, SourceKind

from ijob_sources import (
    arbeitnow,
    ashby,
    fourdayweek,
    gem,
    greenhouse,
    himalayas,
    hn,
    japandev,
    jobicy,
    kalibrr,
    lever,
    manual,
    mycareersfuture,
    remoteok,
    rss,
)
from ijob_sources.description import TextForm, normalize
from ijob_sources.errors import ParseError
from ijob_sources.jsonld import job_postings
from ijob_sources.payload import raw_object
from ijob_sources.polite_http import PoliteClient

_WORKABLE_JOB_URL: Final = (
    "https://apply.workable.com/api/v2/accounts/{key}/jobs/{code}"
)
_YC_ORIGIN: Final = "https://www.ycombinator.com"
_LETSGETRUSTY_HOST: Final = "jobs.letsgetrusty.com"


async def describe(sighting: Sighting, client: PoliteClient) -> str | None:  # noqa: PLR0911, PLR0912 - one arm per source kind, closed by assert_never
    """Return a sighting's description as Markdown.

    Args:
        sighting: The sighting to describe; its source kind picks the route.
        client: The client any request goes through. A kind that keeps its text
            in `raw` makes no request.

    Returns:
        The posting's description, or `None` when the source states none.

    Raises:
        ParseError: The `raw` payload, the fetched response or the fetched
            page does not hold the description field, or a reference in `raw`
            is missing or does not lead to the kind's own site.
        SourcesError: The client could not complete a request (for example
            `SourceNotFoundError` for a missing posting or
            `RobotsDisallowedError` for a disallowed route).
    """
    raw = sighting.raw
    kind = sighting.source.kind
    match kind:
        case SourceKind.GREENHOUSE:
            return greenhouse.description_from_raw(raw)
        case SourceKind.GEM:
            return gem.description_from_raw(raw)
        case SourceKind.ASHBY:
            return ashby.description_from_raw(raw)
        case SourceKind.LEVER:
            return lever.description_from_raw(raw)
        case SourceKind.HIMALAYAS:
            return himalayas.description_from_raw(raw)
        case SourceKind.JOBICY:
            return jobicy.description_from_raw(raw)
        case SourceKind.REMOTEOK:
            return remoteok.description_from_raw(raw)
        case SourceKind.ARBEITNOW:
            return arbeitnow.description_from_raw(raw)
        case SourceKind.MYCAREERSFUTURE:
            return mycareersfuture.description_from_raw(raw)
        case SourceKind.KALIBRR:
            return kalibrr.description_from_raw(raw)
        case SourceKind.FOURDAYWEEK:
            return fourdayweek.description_from_raw(raw)
        case SourceKind.HN:
            return hn.description_from_raw(raw)
        case SourceKind.JAPANDEV:
            return japandev.description_from_raw(raw)
        case SourceKind.JOBPOSTING:
            return manual.description_from_raw(raw)
        case SourceKind.RSS | SourceKind.FEED:
            return rss.description_from_raw(raw)
        case SourceKind.WORKABLE:
            return await _workable_description(sighting, client)
        case SourceKind.YC:
            return await _yc_description(sighting, client)
        case SourceKind.LETSGETRUSTY:
            return await _letsgetrusty_description(sighting, client)
        case _ as unreachable:
            assert_never(unreachable)


async def _workable_description(sighting: Sighting, client: PoliteClient) -> str | None:
    """Fetch a Workable job and join its description, requirements and benefits.

    The account key is the sighting's source key and the job's shortcode is its
    external id. Each part is HTML; a part that is missing or blank is left out,
    and the other two follow `## Requirements` and `## Benefits` headings.

    Raises:
        ParseError: The response is not a JSON object, or it has no
            `description` text.
        SourcesError: The client could not complete the request.
    """
    url = _WORKABLE_JOB_URL.format(
        key=quote(sighting.source.key, safe=""),
        code=quote(sighting.external_id, safe=""),
    )
    response = await client.get(url)
    job = _response_object(response.content)
    description = job.get("description")
    if not isinstance(description, str):
        reason = "job response has no 'description' text"
        raise ParseError(reason)
    parts = [
        normalize(description, TextForm.HTML),
        _titled("Requirements", job.get("requirements")),
        _titled("Benefits", job.get("benefits")),
    ]
    joined = "\n\n".join(part for part in parts if part)
    return joined or None


def _titled(title: str, html_text: object) -> str | None:
    """Return a `## title` section, or `None` when the part has no text."""
    if not isinstance(html_text, str):
        return None
    body = normalize(html_text, TextForm.HTML)
    return None if body is None else f"## {title}\n\n{body}"


async def _yc_description(sighting: Sighting, client: PoliteClient) -> str | None:
    """Fetch a Y Combinator job page and read its JSON-LD description.

    The page is the site path the sighting's `raw` holds under `url`.

    Raises:
        ParseError: `raw` is not a JSON object with a site path under `url`, or
            the page has no `JobPosting` with a `description`.
        SourcesError: The client could not complete the request.
    """
    path = raw_object(sighting.raw).get("url")
    if not isinstance(path, str) or not path.startswith("/"):
        reason = "raw has no site path under 'url'"
        raise ParseError(reason)
    response = await client.get(_YC_ORIGIN + path)
    return _page_description(response.content, TextForm.DETECT)


async def _letsgetrusty_description(
    sighting: Sighting, client: PoliteClient
) -> str | None:
    """Fetch a Let's Get Rusty job page and read its JSON-LD description.

    The page is the `https` address under `url` in the sighting's `raw`; it must
    be on the board's own host, so a payload cannot send the request elsewhere.

    Raises:
        ParseError: `raw` is not a JSON object with a board address under
            `url`, or the page has no `JobPosting` with a `description`.
        SourcesError: The client could not complete the request.
    """
    url = raw_object(sighting.raw).get("url")
    if not isinstance(url, str) or not _is_board_url(url):
        reason = f"raw has no address on {_LETSGETRUSTY_HOST} under 'url'"
        raise ParseError(reason)
    response = await client.get(url)
    return _page_description(response.content, TextForm.MARKDOWN)


def _is_board_url(url: str) -> bool:
    """Say whether `url` is an `https` address on the Let's Get Rusty board."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" and hostname == _LETSGETRUSTY_HOST


def _page_description(content: bytes, form: TextForm) -> str | None:
    """Normalize the description of a page's first `JobPosting`.

    Args:
        content: The page body.
        form: The form the board writes its descriptions in.

    Returns:
        The description as Markdown, or `None` when it is blank.

    Raises:
        ParseError: The page has no `JobPosting`, or its first one has no
            `description` text.
    """
    postings = job_postings(content.decode(errors="replace"))
    if not postings:
        reason = "page has no JobPosting block"
        raise ParseError(reason)
    description = postings[0].get("description")
    if not isinstance(description, str):
        reason = "JobPosting has no 'description' text"
        raise ParseError(reason)
    return normalize(description, form)


def _response_object(data: bytes) -> dict[str, object]:
    """Parse a Workable job response, which must be a JSON object.

    Args:
        data: The response body.

    Raises:
        ParseError: `data` is not JSON, or its top level is not an object.
    """
    try:
        value: object = json.loads(data)
    except (ValueError, RecursionError) as error:
        reason = f"job response is not JSON: {error}"
        raise ParseError(reason) from error
    if not isinstance(value, dict):
        reason = "job response is not a JSON object"
        raise ParseError(reason)
    return value
