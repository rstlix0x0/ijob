"""The seed list of sources that ships with ijob.

The list is the `seeds.csv` file in the package's `data` directory: a header
`kind,key,company`, then one row per source. A row for a hiring-system board
(Greenhouse, Ashby, Lever, Workable, Gem) names the company the board belongs
to; a row for a job board, a feed or an aggregator has `-` as its company
because that source lists many companies. `load_seeds` reads the file into the
shape `Store.import_seeds` takes.
"""

import csv
import io
from importlib.resources import files
from typing import Final

from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceRef, parse_source_ref

_SEEDS_FILE: Final = "seeds.csv"
_HEADER: Final = ("kind", "key", "company")
_COLUMNS: Final = len(_HEADER)


def _read_text() -> str:
    """Read the seed file out of the package data."""
    return (files("ijob_sources") / "data" / _SEEDS_FILE).read_text(encoding="utf-8")


def load_seeds() -> list[tuple[SourceRef, str]]:
    """Read the bundled seed list.

    Returns:
        Each seed source with the company it belongs to, in file order. The
        company is `-` for a source that lists many companies.

    Raises:
        InvalidValueError: If the file's header is not `kind,key,company`, or a
            row does not have three columns, has a blank company, or names a
            source that `parse_source_ref` rejects. The message names the
            file's line.
    """
    reader = csv.reader(io.StringIO(_read_text(), newline=""))
    header = next(reader, None)
    if header is None or tuple(header) != _HEADER:
        raise InvalidValueError("seeds", f"line 1: header is {header!r}")
    seeds: list[tuple[SourceRef, str]] = []
    for row in reader:
        line = reader.line_num
        if len(row) != _COLUMNS or not row[2].strip():
            raise InvalidValueError(
                "seeds", f"line {line}: expected kind, key and a company, got {row!r}"
            )
        try:
            ref = parse_source_ref(f"{row[0]}:{row[1]}")
        except InvalidValueError as error:
            raise InvalidValueError("seeds", f"line {line}: {error.reason}") from error
        seeds.append((ref, row[2]))
    return seeds
