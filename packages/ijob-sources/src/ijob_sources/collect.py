"""The contract every job-source adapter implements, and the runner that uses it.

A source adapter is a class with a `ref` and an async `fetch`; it satisfies the
`Source` protocol structurally and does not import it. `fetch` makes its
requests through the `PoliteClient` it is given and returns a `SourceBatch`.

The adapter contract:

* Keep parsing in a pure function that builds each `SightingDraft` itself.
* Catch `InvalidValueError` per job, log it at `WARNING` with the job's id and
  the reason, count it in `SourceBatch.rejected`, and keep going.
* Raise `ParseError` for a response body that cannot be read at all.
* Let no other exception leave `fetch` except a `SourcesError` from the client.

`collect` is the one place a source failure is isolated: it catches
`SourcesError` and nothing wider, so an adapter bug still crashes the run.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from ijob_core.model import SightingDraft, SourceRef

from ijob_sources.errors import SourcesError
from ijob_sources.polite_http import PoliteClient


@dataclass(frozen=True, slots=True)
class SourceBatch:
    """What one source returned in one fetch.

    Attributes:
        drafts: The valid drafts.
        rejected: How many jobs the adapter dropped because they were unusable.
    """

    drafts: list[SightingDraft]
    rejected: int


class Source(Protocol):
    """A place job postings are collected from."""

    @property
    def ref(self) -> SourceRef:
        """The named source this adapter reads."""
        ...

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Fetch the source and return what it holds.

        Args:
            client: The client every request must go through.

        Returns:
            The valid drafts and the count of rejected jobs.

        Raises:
            SourcesError: The client failed, or the response could not be read.
        """
        ...


@dataclass(frozen=True, slots=True)
class SourceFailure:
    """A source that could not be collected.

    Attributes:
        ref: The source that failed.
        error: Why it failed.
    """

    ref: SourceRef
    error: SourcesError


@dataclass(frozen=True, slots=True)
class CollectResult:
    """The outcome of collecting several sources.

    Attributes:
        batches: The batch of each source that succeeded, by source.
        failures: The sources that failed.
    """

    batches: dict[SourceRef, SourceBatch]
    failures: list[SourceFailure]


async def collect(sources: Iterable[Source], client: PoliteClient) -> CollectResult:
    """Fetch every source concurrently and keep going past source failures.

    Args:
        sources: The sources to fetch.
        client: The client each source makes its requests through.

    Returns:
        The batches of the sources that succeeded and the failures of the rest.

    Raises:
        ExceptionGroup: A source raised something other than a `SourcesError`;
            the group holds that exception.
    """
    batches: dict[SourceRef, SourceBatch] = {}
    failures: list[SourceFailure] = []

    async def run(source: Source) -> None:
        try:
            batches[source.ref] = await source.fetch(client)
        except SourcesError as error:
            failures.append(SourceFailure(source.ref, error))

    async with asyncio.TaskGroup() as group:
        for source in sources:
            group.create_task(run(source))
    return CollectResult(batches=batches, failures=failures)
