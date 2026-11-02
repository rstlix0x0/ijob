import asyncio

import pytest
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_sources.collect import (
    CollectResult,
    SourceBatch,
    SourceFailure,
    collect,
    skipped_item,
)
from ijob_sources.errors import SourceNotFoundError
from ijob_sources.polite_http import PoliteClient


def _ref(key: str) -> SourceRef:
    return SourceRef(SourceKind.GREENHOUSE, parse_source_key(key))


class _FakeSource:
    """A source that returns a canned batch or raises a canned error."""

    def __init__(self, ref: SourceRef, outcome: SourceBatch | Exception) -> None:
        self.ref = ref
        self._outcome = outcome

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        del client
        await asyncio.sleep(0)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


async def _collect(sources: list[_FakeSource]) -> CollectResult:
    async with PoliteClient() as client:
        return await collect(sources, client)


def test_collect_returns_each_source_batch() -> None:
    first = SourceBatch(drafts=[], rejected=1)
    second = SourceBatch(drafts=[], rejected=2)
    sources = [_FakeSource(_ref("a"), first), _FakeSource(_ref("b"), second)]

    result = asyncio.run(_collect(sources))

    assert result.batches == {_ref("a"): first, _ref("b"): second}
    assert result.failures == []


def test_batch_still_listed_defaults_empty() -> None:
    assert SourceBatch(drafts=[], rejected=0).still_listed == ()


def test_failing_source_does_not_cancel_others() -> None:
    error = SourceNotFoundError("https://boards.example.test/x")
    good = SourceBatch(drafts=[], rejected=0)
    sources = [_FakeSource(_ref("bad"), error), _FakeSource(_ref("good"), good)]

    result = asyncio.run(_collect(sources))

    assert result.batches == {_ref("good"): good}
    assert result.failures == [SourceFailure(_ref("bad"), error)]


def test_non_sources_error_propagates() -> None:
    bug = RuntimeError("adapter bug")
    sources = [_FakeSource(_ref("bug"), bug)]

    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(_collect(sources))

    assert caught.value.exceptions == (bug,)


def test_skipped_item_cuts_long_text() -> None:
    item = skipped_item("x" * 50_000, "https://example.test/a", ValueError("bad"))

    assert item.text == "x" * 20_000
    assert item.url == "https://example.test/a"
    assert item.reason == "bad"


def test_skipped_item_turns_a_dict_into_json() -> None:
    item = skipped_item({"title": "A"}, None, ValueError("bad"))

    assert item.text == '{"title": "A"}'
    assert item.url is None


def test_skipped_item_survives_an_item_too_deep_to_encode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def too_deep(*_args: object, **_kwargs: object) -> str:
        raise RecursionError

    monkeypatch.setattr("ijob_sources.collect.json.dumps", too_deep)

    item = skipped_item([], None, ValueError("bad"))

    assert item.text == "<unreadable list>"
    assert item.reason == "bad"


def test_collect_tells_when_each_source_ends() -> None:
    error = SourceNotFoundError("https://boards.example.test/x")
    good = SourceBatch(drafts=[], rejected=0)
    sources = [_FakeSource(_ref("bad"), error), _FakeSource(_ref("good"), good)]
    failed: list[bool] = []

    async def run() -> None:
        async with PoliteClient() as client:
            await collect(sources, client, on_done=failed.append)

    asyncio.run(run())

    assert sorted(failed) == [False, True]
