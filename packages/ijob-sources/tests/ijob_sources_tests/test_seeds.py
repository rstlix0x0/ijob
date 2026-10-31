import csv
import importlib.resources
from pathlib import Path

import pytest
from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceRef, parse_source_ref
from ijob_core.store import SourceOrigin, SourceRow
from ijob_sources import seeds
from ijob_sources.registry import build_source
from ijob_sources.seeds import load_seeds

_RESEARCH_CSV = Path(__file__).parents[4] / "docs" / "research" / "companies.csv"
_HIRING_SYSTEMS = frozenset({"greenhouse", "ashby", "lever", "workable", "gem"})
_BOARD_REFS = [
    "himalayas:all",
    "jobicy:engineering",
    "remoteok:all",
    "arbeitnow:all",
    "4dayweek:engineering",
    "mycareersfuture:software-engineer",
    "kalibrr:software-engineer",
    "rss:wwr-programming",
    "rss:wwr-backend",
    "rss:python",
    "rss:golang",
    "rss:elixir",
    "rss:djinni-python",
    "rss:bsj",
    "yc:swe",
    "letsgetrusty:all",
    "japandev:all",
    "hn:whoishiring",
]
_NO_COMPANY = "-"


def _research_rows() -> list[tuple[str, str]]:
    with _RESEARCH_CSV.open(encoding="utf-8", newline="") as handle:
        return [
            (f"{row['ats']}:{row['board_token']}", row["company"])
            for row in csv.DictReader(handle)
            if row["verified_status"] == "200" and row["ats"] in _HIRING_SYSTEMS
        ]


def _hiring_seeds() -> list[tuple[str, str]]:
    return [
        (str(ref), company) for ref, company in load_seeds() if company != _NO_COMPANY
    ]


def test_hiring_rows_equal_research_csv() -> None:
    expected = _research_rows()

    assert len(expected) == 150
    assert _hiring_seeds() == expected


def test_board_rows() -> None:
    boards = [str(ref) for ref, company in load_seeds() if company == _NO_COMPANY]

    assert boards == _BOARD_REFS
    assert len(boards) == 18


def test_every_seed_builds() -> None:
    for ref, company in load_seeds():
        row = SourceRow(
            ref=ref,
            company=company,
            target_url=None,
            origin=SourceOrigin.SEED,
            removed=False,
            last_fetched_at=None,
        )

        assert build_source(row).ref == ref


def test_no_duplicate_refs() -> None:
    refs = [ref for ref, _ in load_seeds()]

    assert len(refs) == len(set(refs)) == 168


def test_load_seeds_returns_source_refs() -> None:
    first_ref, _ = load_seeds()[0]

    assert isinstance(first_ref, SourceRef)
    assert first_ref == parse_source_ref(str(first_ref))


def test_seed_file_ships_as_package_data() -> None:
    path = importlib.resources.files("ijob_sources") / "data" / "seeds.csv"

    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith("kind,key,company\n")


def test_bad_row_raises_naming_its_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "kind,key,company\nashby,notion,Notion\nnotakind,x,Bad Co\n"
    monkeypatch.setattr(seeds, "_read_text", lambda: text)

    with pytest.raises(InvalidValueError, match="line 3"):
        load_seeds()


def test_blank_company_raises_naming_its_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "kind,key,company\nashby,notion,\n"
    monkeypatch.setattr(seeds, "_read_text", lambda: text)

    with pytest.raises(InvalidValueError, match="line 2"):
        load_seeds()
