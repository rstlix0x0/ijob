"""The one place a sources-table row becomes a source adapter.

`build_source` maps each `SourceKind` to the adapter class that reads it, and
`refresh_interval` says how long a kind's fetched data stays fresh. The match in
`build_source` covers every `SourceKind` and ends in `assert_never`, so a kind
added without a branch here fails type checking.
"""

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Final, assert_never

from ijob_core.errors import InvalidValueError
from ijob_core.model import ExternalId, SourceKind
from ijob_core.store import SourceRow

from ijob_sources.arbeitnow import ArbeitnowBoard
from ijob_sources.ashby import AshbyBoard
from ijob_sources.collect import Source
from ijob_sources.fourdayweek import FourDayWeekBoard
from ijob_sources.gem import GemBoard
from ijob_sources.greenhouse import GreenhouseBoard
from ijob_sources.himalayas import HimalayasBoard
from ijob_sources.hn import HnWhoIsHiring
from ijob_sources.japandev import JapanDev
from ijob_sources.jobicy import JobicyBoard
from ijob_sources.kalibrr import KalibrrBoard
from ijob_sources.letsgetrusty import LetsGetRusty
from ijob_sources.lever import LeverSite
from ijob_sources.manual import FeedUrl, JobPostingPage
from ijob_sources.mycareersfuture import MyCareersFutureBoard
from ijob_sources.remoteok import RemoteOkBoard
from ijob_sources.rss import RssFeed
from ijob_sources.workable import WorkableAccount
from ijob_sources.yc import YcJobs

HOST_INTERVALS: Final[Mapping[str, float]] = MappingProxyType(
    {"apply.workable.com": 3.0}
)
"""Minimum seconds between requests to a host, as `PoliteClient` takes them."""

_JOBICY_INTERVAL: Final = timedelta(hours=1)
_NO_INTERVAL: Final = timedelta(0)


def build_source(  # noqa: PLR0911, PLR0912 - one branch per SourceKind, so the match stays exhaustive
    row: SourceRow, *, known: frozenset[ExternalId] = frozenset()
) -> Source:
    """Build the adapter that reads the source a row describes.

    A hiring-system source (Greenhouse, Gem, Ashby, Lever, Workable) is given
    the row's company so it can check the board is that company's. A board, a
    feed or an aggregator is not: it lists many companies.

    Args:
        row: The sources-table row to build from.
        known: The ids of the jobs already stored. Only the sources that fetch
            a page per job (`RSS` detail feeds and `JAPANDEV`) use it, to skip
            pages they already hold.

    Returns:
        The adapter for the row; its `ref` equals `row.ref`.

    Raises:
        InvalidValueError: The row is an `RSS` source whose key is not a known
            feed, or a `JOBPOSTING` or `FEED` source without a `target_url`.
    """
    key = row.ref.key
    kind = row.ref.kind
    match kind:
        case SourceKind.GREENHOUSE:
            return GreenhouseBoard(key, company=row.company)
        case SourceKind.GEM:
            return GemBoard(key, company=row.company)
        case SourceKind.ASHBY:
            return AshbyBoard(key, company=row.company)
        case SourceKind.LEVER:
            return LeverSite(key, company=row.company)
        case SourceKind.WORKABLE:
            return WorkableAccount(key, company=row.company)
        case SourceKind.HIMALAYAS:
            return HimalayasBoard(key)
        case SourceKind.JOBICY:
            return JobicyBoard(key)
        case SourceKind.REMOTEOK:
            return RemoteOkBoard(key)
        case SourceKind.ARBEITNOW:
            return ArbeitnowBoard(key)
        case SourceKind.FOURDAYWEEK:
            return FourDayWeekBoard(key)
        case SourceKind.MYCAREERSFUTURE:
            return MyCareersFutureBoard(key)
        case SourceKind.KALIBRR:
            return KalibrrBoard(key)
        case SourceKind.RSS:
            return RssFeed(key, known=known)
        case SourceKind.YC:
            return YcJobs(key)
        case SourceKind.LETSGETRUSTY:
            return LetsGetRusty(key)
        case SourceKind.HN:
            return HnWhoIsHiring(key)
        case SourceKind.JAPANDEV:
            return JapanDev(key, known=known)
        case SourceKind.JOBPOSTING:
            return JobPostingPage(key, target_url=_target_url(row), company=row.company)
        case SourceKind.FEED:
            return FeedUrl(key, target_url=_target_url(row), company=row.company)
        case _ as unreachable:
            assert_never(unreachable)


def refresh_interval(kind: SourceKind) -> timedelta:
    """Say how long a kind's data stays fresh before it is fetched again.

    Args:
        kind: The kind of source.

    Returns:
        One hour for `JOBICY`, whose API asks to be read no more often than
        that, and zero for every other kind.
    """
    if kind is SourceKind.JOBICY:
        return _JOBICY_INTERVAL
    return _NO_INTERVAL


def _target_url(row: SourceRow) -> str:
    """Return the row's `target_url`, which a manual source cannot do without."""
    if row.target_url is None:
        raise InvalidValueError(
            "target_url", f"{row.ref} is a manual source and needs a target_url"
        )
    return row.target_url
