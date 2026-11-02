import asyncio
import io
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest
from ijob_app.cli import _stream_search
from ijob_app.cli_progress import ProgressLine
from ijob_app.config import FilterLayers
from ijob_app.llm_ops import JudgeStatus, SearchItem
from ijob_app.service import Service
from ijob_core.model import Posting, Verdict


def _item(title: str, *, keep: bool) -> SearchItem:
    posting = SimpleNamespace(
        id=1,
        title=title,
        company="Acme",
        location=None,
        url="https://example.test/job",
        low_confidence=False,
    )
    verdict = SimpleNamespace(keep=keep, reason="no")
    return SearchItem(
        cast("Posting", posting),
        cast("Verdict", verdict),
        JudgeStatus.JUDGED,
        no_description=False,
    )


class _Service:
    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys
        self.printed: list[str] = []

    async def search(self, _layers: object, _refs: object) -> AsyncIterator[SearchItem]:
        await asyncio.sleep(0)
        yield _item("first", keep=True)
        self.printed.append(self._capsys.readouterr().out)
        yield _item("second", keep=False)
        self.printed.append(self._capsys.readouterr().out)

    def posting_sources(self, _posting_id: object) -> list[str]:
        return []


def test_kept_job_prints_before_the_next_job_is_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _Service(capsys)

    asyncio.run(
        _stream_search(
            cast("Service", service),
            cast("FilterLayers", None),
            [],
            ProgressLine(io.StringIO()),
            show_dropped=False,
        )
    )

    assert "first" in service.printed[0]
    assert service.printed[1] == ""
