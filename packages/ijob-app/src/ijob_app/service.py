"""The operations the command line and the terminal UI run on the job pipeline.

`Service` owns what a front end does with the stored data: choosing sources,
fetching the ones that are due, recording what they show, listing the open,
filtered postings, reading a posting's detail and description, voting on
postings, keeping the CV and managing the source table. It returns values and
raises `InvalidValueError` subclasses, `SourcesError` or `OSError`; it neither
prints nor starts an event loop, so each front end keeps its own output, exit
codes and loop.

`open_service` builds a `Service` over the seeded database and the HTTP cache of
an ijob home. The store and the cache are bound to the thread that opens them,
so every `Service` method must run on that thread. The methods that reach the
network are `async` and run on the caller's event loop.
"""

import hashlib
import sqlite3
import tempfile
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from ijob_core.dedupe import normalise_company
from ijob_core.errors import InvalidValueError
from ijob_core.filters import FilterSpec, keep
from ijob_core.model import (
    ExternalId,
    Label,
    Posting,
    PostingId,
    SourceRef,
    StoredCv,
    Vote,
)
from ijob_core.store import SourceOrigin, SourceRow, Store
from ijob_sources.collect import CollectResult, Source, SourceFailure, collect
from ijob_sources.details import describe
from ijob_sources.detect import resolve_url, verify_new_source
from ijob_sources.errors import SourcesError
from ijob_sources.http_cache import SqliteHttpCache
from ijob_sources.polite_http import ConditionalCache, PoliteClient
from ijob_sources.registry import build_source, refresh_interval
from ijob_sources.seeds import load_seeds

from ijob_app.home import IjobHome

type ClientFactory = Callable[[ConditionalCache | None], PoliteClient]

MAX_CV_BYTES: Final = 20_000_000
_PDF_MAGIC: Final = b"%PDF-"
_CV_FILE_NAME: Final = "cv.pdf"


class UnknownSourceError(InvalidValueError):
    """A named source has no row in the table, or was removed."""

    def __init__(self, reason: str) -> None:
        """Build the error from the reason, which names the source.

        Args:
            reason: Why the source cannot be used, naming it.
        """
        super().__init__("source", reason)


class CacheUnavailableError(InvalidValueError):
    """The HTTP cache file cannot be opened."""

    def __init__(self, path: Path, reason: str) -> None:
        """Build the error from the cache path and the underlying reason.

        Args:
            path: The cache file that could not be opened.
            reason: What the filesystem or SQLite reported.
        """
        super().__init__("cache", f"cannot open the HTTP cache {path}: {reason}")


class SourceCompanyError(InvalidValueError):
    """A source reference is already in the table for a different company."""

    def __init__(self, ref: SourceRef, company: str) -> None:
        """Build the error from the reference and the company that holds it.

        Args:
            ref: The reference that was to be added.
            company: The company the table already has it for.
        """
        super().__init__("source", f"{ref} is already a source for {company}")


class InvalidCvError(InvalidValueError):
    """A CV file cannot be stored."""

    def __init__(self, reason: str) -> None:
        """Build the error from the reason, which names the file.

        Args:
            reason: Why the file cannot be stored.
        """
        super().__init__("cv", reason)


class UnknownPostingError(InvalidValueError):
    """A posting id names no posting in the store."""

    def __init__(self, posting_id: PostingId) -> None:
        """Build the error from the unknown id.

        Args:
            posting_id: The id that names no posting.
        """
        super().__init__("posting", f"no posting {posting_id}")


@dataclass(frozen=True, slots=True)
class PostingDetail:
    """A posting with everything the detail views show about it.

    Attributes:
        posting: The posting, with its description when one is known.
        sources: The sources that have shown it, sorted by their text form.
        label: The user's vote on it, or `None`.
        description: The posting's description as Markdown, or `None` when none
            could be found.
        unavailable_reason: Why `description` is `None`: the failure that kept
            it from being fetched, or that no source publishes one. `None` when
            the description is present.
    """

    posting: Posting
    sources: list[SourceRef]
    label: Label | None
    description: str | None
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What one refresh did, source by source.

    Attributes:
        due: The chosen sources that were due for a fetch, in order.
        kept: The chosen sources left alone because their last fetch is more
            recent than their kind's refresh interval.
        fetched: The sources fetched and recorded, in the order of `due`.
        rejected: How many jobs each fetched source dropped as unusable; only
            sources that dropped at least one appear.
        failures: The sources whose fetch failed, and why.
        refused: The sources the registry could not build, and why.
        stale: The chosen sources whose last fetch is too old for their
            postings to show.
    """

    due: list[SourceRow]
    kept: list[SourceRow]
    fetched: list[SourceRef]
    rejected: dict[SourceRef, int]
    failures: list[SourceFailure]
    refused: dict[SourceRef, InvalidValueError]
    stale: list[SourceRow]


def _read_cv(path: Path) -> bytes:
    """Read a CV file, checking that it is a PDF of an allowed size.

    Args:
        path: The file to read, with `~` already expanded.

    Returns:
        The file's bytes.

    Raises:
        InvalidCvError: The file cannot be read, is larger than `MAX_CV_BYTES`,
            or does not start with the PDF signature.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CV_BYTES + 1)
    except OSError as error:
        reason = f"cannot read {path}: {error}"
        raise InvalidCvError(reason) from error
    if len(data) > MAX_CV_BYTES:
        reason = f"{path} is larger than {MAX_CV_BYTES // 1_000_000} MB"
        raise InvalidCvError(reason)
    if not data.startswith(_PDF_MAGIC):
        reason = f"{path} is not a PDF: it does not start with %PDF-"
        raise InvalidCvError(reason)
    return data


def _write_atomically(target: Path, data: bytes) -> None:
    """Write `data` to `target` through a temporary file beside it, then replace.

    Args:
        target: The file to create or replace.
        data: The bytes to write.

    Raises:
        OSError: The temporary file cannot be written or moved; `target` is left
            as it was and no temporary file remains.
    """
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f"{target.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(target)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _read_existing(target: Path) -> bytes | None:
    """Read `target` if it exists; `None` when there is no such file."""
    try:
        return target.read_bytes()
    except FileNotFoundError:
        return None


def _restore(target: Path, previous: bytes | None) -> None:
    """Put `target` back as it was, as far as the filesystem allows.

    Args:
        target: The file to restore.
        previous: Its earlier bytes, or `None` if it did not exist.
    """
    with suppress(OSError):
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            _write_atomically(target, previous)


def _new_row(ref: SourceRef, company: str) -> SourceRow:
    """Describe a source about to be added, so the registry can build it."""
    return SourceRow(
        ref=ref,
        company=company,
        target_url=None,
        origin=SourceOrigin.USER,
        removed=False,
        last_fetched_at=None,
    )


def _split_by_interval(
    rows: list[SourceRow], at: datetime
) -> tuple[list[SourceRow], list[SourceRow]]:
    """Separate the sources due for a fetch from those fetched recently enough.

    Args:
        rows: The chosen sources.
        at: The time to measure from.

    Returns:
        The sources to fetch and the sources to keep as they are, each in the
        order of `rows`. A source is kept when its last fetch is less than its
        kind's refresh interval before `at`.
    """
    due: list[SourceRow] = []
    kept: list[SourceRow] = []
    for row in rows:
        interval = refresh_interval(row.ref.kind)
        if row.last_fetched_at is not None and at - row.last_fetched_at < interval:
            kept.append(row)
        else:
            due.append(row)
    return due, kept


class Service:
    """The pipeline operations over one open store, cache and clock.

    A service is used only on the thread that opened its store, and its `async`
    methods only on one event loop at a time.
    """

    def __init__(
        self,
        store: Store,
        *,
        db_path: Path,
        client_factory: ClientFactory,
        cache: ConditionalCache | None,
        now: Callable[[], datetime],
    ) -> None:
        """Build a service over open resources it does not own.

        Args:
            store: The open store, with the seed sources imported.
            db_path: The database file the store was opened on.
            client_factory: Builds the client a fetch goes through.
            cache: The HTTP cache the fetching clients revalidate against, or
                `None` for none.
            now: The wall clock, returning timezone-aware times.
        """
        self._store = store
        self._db_path = db_path
        self._client_factory = client_factory
        self._cache = cache
        self._now = now

    async def refresh(self, refs: Sequence[SourceRef]) -> RefreshReport:
        """Fetch the chosen sources that are due and record what they show.

        The clock is read twice: once to split the sources by interval before
        fetching, and once after fetching, which stamps both the recorded
        sightings and the fetch times.

        Args:
            refs: The sources to read; empty means every source in the table.

        Returns:
            What was fetched, kept, rejected, refused and found stale.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
            sqlite3.Error: A store read or write failed.
        """
        rows = self._choose_sources(refs)
        due, kept = _split_by_interval(rows, self._now())
        sources, refused = self._build_sources(due)
        result = await self._collect(sources)
        seen_at = self._now()
        fetched = self._record(due, result, seen_at)
        return RefreshReport(
            due=due,
            kept=kept,
            fetched=fetched,
            rejected={
                ref: rejected
                for ref in fetched
                if (rejected := result.batches[ref].rejected) > 0
            },
            failures=list(result.failures),
            refused=refused,
            stale=self._stale(rows, seen_at),
        )

    def open_postings(
        self, spec: FilterSpec, refs: Sequence[SourceRef]
    ) -> list[Posting]:
        """List the open postings that pass the filters.

        Args:
            spec: The filters a posting must pass.
            refs: The sources to read; empty means every source.

        Returns:
            The postings, newest first.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
        """
        self._choose_sources(refs)
        at = self._now()
        postings = self._store.open_postings(now=at, sources=refs or None)
        return [posting for posting in postings if keep(posting, spec, now=at)]

    async def detail(self, posting_id: PostingId) -> PostingDetail:
        """Read a posting with its sources, label and description.

        A posting stored without a description has one looked up from its
        sightings, in the order they were first recorded: the first text found
        is stored and returned. A source that fails is reported in
        `unavailable_reason` and nothing is stored, so the next call tries
        again; the remaining sightings are still tried.

        Args:
            posting_id: The posting to read.

        Returns:
            The posting's detail.

        Raises:
            UnknownPostingError: The id names no posting.
            sqlite3.Error: A store read or write failed.
        """
        posting = self._require_posting(posting_id)
        description = posting.description
        reason: str | None = None
        if description is None:
            description, reason = await self._describe(posting_id)
            posting = self._require_posting(posting_id)
        return PostingDetail(
            posting=posting,
            sources=self._store.posting_sources(posting_id),
            label=self._store.label(posting_id),
            description=description,
            unavailable_reason=reason,
        )

    def labels(self, ids: Collection[PostingId]) -> dict[PostingId, Label]:
        """Read the votes on several postings, touching neither description nor network.

        Args:
            ids: The postings to read.

        Returns:
            The labels by posting id; postings without one are absent.
        """
        return self._store.labels(ids)

    async def label(
        self, posting_id: PostingId, vote: Vote, note: str | None
    ) -> PostingDetail:
        """Record the user's vote on a posting, then read its detail.

        Args:
            posting_id: The posting voted on.
            vote: The verdict.
            note: Why, or `None`.

        Returns:
            The posting's detail, with the new label and the description step
            of `detail` done.

        Raises:
            UnknownPostingError: The id names no posting.
            InvalidValueError: `note` is blank.
            sqlite3.Error: A store read or write failed.
        """
        self._require_posting(posting_id)
        self._store.set_label(posting_id, vote, note, at=self._now())
        return await self.detail(posting_id)

    def clear_label(self, posting_id: PostingId) -> None:
        """Remove the vote on a posting; a posting without one is left as it is.

        Args:
            posting_id: The posting whose vote to remove.

        Raises:
            UnknownPostingError: The id names no posting.
            sqlite3.Error: The write failed.
        """
        self._require_posting(posting_id)
        self._store.clear_label(posting_id)

    def store_cv(self, path: Path) -> StoredCv:
        """Keep a copy of the user's CV beside the database and record it.

        The copy is `cv.pdf` in the database file's directory, written through
        a temporary file and then moved into place, so an earlier CV is replaced
        only by a complete one. A file that is refused, or a record that cannot
        be written, leaves the earlier copy and record as they were.

        Args:
            path: The PDF to store; a leading `~` is expanded.

        Returns:
            The record of the stored CV.

        Raises:
            InvalidCvError: The file cannot be read, is larger than
                `MAX_CV_BYTES`, or does not start with `%PDF-`.
            OSError: The copy beside the database cannot be written. This is
                not a fault of the argument, so it is not an `InvalidCvError`.
            sqlite3.Error: The record could not be written.
        """
        source = path.expanduser()
        data = _read_cv(source)
        target = self._db_path.parent / _CV_FILE_NAME
        previous = _read_existing(target)
        _write_atomically(target, data)
        cv = StoredCv(
            name=source.name,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            stored_at=self._now(),
        )
        try:
            self._store.set_cv(cv)
        except sqlite3.Error:
            _restore(target, previous)
            raise
        return cv

    def cv(self) -> StoredCv | None:
        """Read the record of the stored CV.

        Returns:
            The record, or `None` if no CV was stored.
        """
        return self._store.cv()

    def sources(self) -> list[SourceRow]:
        """List every source that was not removed.

        Returns:
            The rows, ordered by reference.
        """
        return self._store.sources()

    async def add_source(self, ref: SourceRef, company: str) -> SourceRow:
        """Verify a source with one fetch and add it.

        Args:
            ref: The source to add.
            company: The company it belongs to, which the fetch checks.

        Returns:
            The source's row.

        Raises:
            SourceCompanyError: The table already holds `ref` for another
                company, removed or not.
            InvalidValueError: The registry cannot build the source from `ref`
                and `company`.
            SourcesError: The fetch or the identity check failed, or the source
                lists no usable jobs.
            sqlite3.Error: The write failed.
        """
        self._refuse_other_company(ref, company)
        source = build_source(_new_row(ref, company))
        async with self._client_factory(None) as client:
            await verify_new_source(source, client)
        self._store.add_source(ref, company)
        return self._require_source(ref)

    def remove_source(self, ref: SourceRef) -> None:
        """Remove a source; it stays removed when the seeds are imported again.

        Args:
            ref: The source to remove.

        Raises:
            UnknownSourceError: The source has no row or was already removed.
            sqlite3.Error: The write failed.
        """
        row = self._store.source(ref)
        if row is None or row.removed:
            reason = f"unknown source {ref}"
            raise UnknownSourceError(reason)
        self._store.remove_source(ref)

    async def add_url(self, url: str, company: str) -> SourceRow:
        """Work out what a URL is, verify it and add it as a source.

        A URL read from a page or feed is already checked by being read; a
        hiring-system board is fetched once before it is added.

        Args:
            url: A board, job page or feed URL.
            company: The company the source belongs to.

        Returns:
            The source's row.

        Raises:
            SourceCompanyError: The table already holds the resolved reference
                for another company.
            InvalidValueError: The registry cannot build the resolved source.
            SourcesError: The URL is unusable, or the source fails its check.
            sqlite3.Error: The write failed.
        """
        async with self._client_factory(None) as client:
            resolution = await resolve_url(url, company=company, client=client)
            self._refuse_other_company(resolution.ref, company)
            if resolution.batch is None:
                source = build_source(_new_row(resolution.ref, company))
                await verify_new_source(source, client)
        self._store.add_source(resolution.ref, company, resolution.target_url)
        return self._require_source(resolution.ref)

    def posting_sources(self, posting_id: PostingId) -> list[SourceRef]:
        """List the sources that have shown a posting.

        Args:
            posting_id: The posting to read.

        Returns:
            The sources, sorted by their text form; empty if the posting is
            unknown.
        """
        return self._store.posting_sources(posting_id)

    def _refuse_other_company(self, ref: SourceRef, company: str) -> None:
        """Refuse a reference the table already holds for a different company.

        The table keeps the company a reference first had, so adding it again
        under another name would verify against the new name and then store the
        old one. Companies are compared as `normalise_company` writes them.

        Raises:
            SourceCompanyError: `ref` is in the table, removed or not, for a
                different company.
        """
        row = self._store.source(ref)
        if row is not None and normalise_company(row.company) != normalise_company(
            company
        ):
            raise SourceCompanyError(ref, row.company)

    def _require_source(self, ref: SourceRef) -> SourceRow:
        """Read a source's row after it was written.

        Raises:
            UnknownSourceError: The table has no such source.
        """
        row = self._store.source(ref)
        if row is None:
            reason = f"unknown source {ref}"
            raise UnknownSourceError(reason)
        return row

    def _require_posting(self, posting_id: PostingId) -> Posting:
        """Read a posting that must exist.

        Raises:
            UnknownPostingError: The id names no posting.
        """
        posting = self._store.posting(posting_id)
        if posting is None:
            raise UnknownPostingError(posting_id)
        return posting

    async def _describe(self, posting_id: PostingId) -> tuple[str | None, str | None]:
        """Look a posting's description up from its sightings and store it.

        Args:
            posting_id: The posting to describe.

        Returns:
            The description and `None`, or `None` and the reason there is none:
            the message of the first source error, or that no source publishes
            a description.
        """
        failure: str | None = None
        async with self._client_factory(self._cache) as client:
            for sighting in self._store.sightings_of(posting_id):
                try:
                    text = await describe(sighting, client)
                except SourcesError as error:
                    failure = failure or str(error)
                    continue
                if text is not None:
                    self._store.set_description(posting_id, text)
                    return text, None
        return None, failure or "no description published"

    def _choose_sources(self, refs: Sequence[SourceRef]) -> list[SourceRow]:
        """Pick the sources a read covers.

        Args:
            refs: The sources the caller named; empty means every source.

        Returns:
            The rows to read, in the order named, or in `ref` order when none
            was named.

        Raises:
            UnknownSourceError: A named source has no row or was removed.
        """
        rows = self._store.sources()
        if not refs:
            return rows
        by_ref = {row.ref: row for row in rows}
        for ref in refs:
            if ref not in by_ref:
                reason = (
                    f"unknown source {ref}; "
                    f"add it with: ijob sources add {ref} --company NAME"
                )
                raise UnknownSourceError(reason)
        return [by_ref[ref] for ref in refs]

    def _build_sources(
        self, rows: list[SourceRow]
    ) -> tuple[list[Source], dict[SourceRef, InvalidValueError]]:
        """Build the adapter of each row, setting aside the rows the registry refuses.

        Args:
            rows: The sources to build.

        Returns:
            The adapters, and the reason each refused row could not be built.
        """
        sources: list[Source] = []
        refused: dict[SourceRef, InvalidValueError] = {}
        for row in rows:
            try:
                sources.append(
                    build_source(row, known=self._store.external_ids(row.ref))
                )
            except InvalidValueError as error:
                refused[row.ref] = error
        return sources, refused

    async def _collect(self, sources: list[Source]) -> CollectResult:
        """Fetch every source with one client, opened and closed on this loop.

        Args:
            sources: The sources to fetch.

        Returns:
            What `collect` returned for the sources.
        """
        async with self._client_factory(self._cache) as client:
            return await collect(sources, client)

    def _record(
        self, rows: list[SourceRow], result: CollectResult, seen_at: datetime
    ) -> list[SourceRef]:
        """Store what the fetched sources showed and note when each was fetched.

        Both the postings and the fetch times are stamped with the one `seen_at`,
        which is what keeps a source's postings open afterwards.

        Args:
            rows: The sources that were due, in order.
            result: What fetching them returned.
            seen_at: When the fetch happened, timezone-aware.

        Returns:
            The sources that were fetched successfully, in the order of `rows`.
        """
        fetched = [row.ref for row in rows if row.ref in result.batches]
        batches = [result.batches[ref] for ref in fetched]
        still_listed: list[tuple[SourceRef, ExternalId]] = [
            (ref, external_id)
            for ref, batch in zip(fetched, batches, strict=True)
            for external_id in batch.still_listed
        ]
        self._store.record(
            (draft for batch in batches for draft in batch.drafts),
            seen_at=seen_at,
            still_listed=still_listed,
        )
        for ref in fetched:
            self._store.mark_fetched(ref, seen_at)
        return fetched

    def _stale(self, rows: list[SourceRow], at: datetime) -> list[SourceRow]:
        """Pick the chosen sources whose postings are hidden as too old."""
        chosen = {row.ref for row in rows}
        return [
            row
            for row in self._store.stale_sources(now=at)
            if row.ref in chosen and row.last_fetched_at is not None
        ]


@contextmanager
def open_service(
    home: IjobHome,
    db: Path | None,
    client_factory: ClientFactory,
    *,
    now: Callable[[], datetime],
    use_cache: bool = True,
) -> Iterator[Service]:
    """Open the seeded database and the HTTP cache and build a service on them.

    The cache is opened before the database, so an unusable cache leaves the
    database file uncreated.

    Args:
        home: The ijob home, which holds the HTTP cache and, unless `db` names
            another file, the database.
        db: The database file, or `None` for the home's.
        client_factory: Builds the client a fetch goes through.
        now: The wall clock, returning timezone-aware times; the cache ages its
            entries by it too.
        use_cache: Whether to open the HTTP cache and hand it to the clients.
            A caller that never revalidates responses leaves it off.

    Yields:
        The service, valid until the context exits and bound to this thread.

    Raises:
        CacheUnavailableError: The cache file cannot be opened.
    """
    cache: SqliteHttpCache | None = None
    if use_cache:
        path = home.cache
        try:
            cache = SqliteHttpCache.open(path, now=now)
        except (OSError, sqlite3.Error) as error:
            raise CacheUnavailableError(path, str(error)) from error
    try:
        db_path = db or home.db
        with Store.open(db_path) as store:
            store.import_seeds(load_seeds())
            yield Service(
                store,
                db_path=db_path,
                client_factory=client_factory,
                cache=cache,
                now=now,
            )
    finally:
        if cache is not None:
            cache.close()
