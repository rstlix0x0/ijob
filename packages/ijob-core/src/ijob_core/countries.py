"""Country codes read from the text of a job's location.

The table of ISO 3166 alpha-2 codes and English names is the `iso3166.tab` file
shipped in the package's `data` directory, a copy of the one in tzdata.
`parse_country` reads a free-text location against it.
"""

from functools import cache
from importlib.resources import files
from typing import Final, NewType

CountryCode = NewType("CountryCode", str)

_TABLE_FILE: Final = "iso3166.tab"
_ALIASES: Final = {
    "usa": CountryCode("US"),
    "uk": CountryCode("GB"),
    "united kingdom": CountryCode("GB"),
    "south korea": CountryCode("KR"),
    "czechia": CountryCode("CZ"),
}
_PART_SEPARATOR: Final = ", "


@cache
def _table() -> dict[CountryCode, str]:
    """Read the code-to-name table from the package data, once."""
    text = (files("ijob_core") / "data" / _TABLE_FILE).read_text(encoding="utf-8")
    table: dict[CountryCode, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        code, name = line.split("\t")
        table[CountryCode(code)] = name
    return table


@cache
def _lookup() -> dict[str, CountryCode]:
    """Map each lowercased code, name and alias to its country code."""
    table = _table()
    lookup = dict(_ALIASES)
    lookup.update((name.lower(), code) for code, name in table.items())
    lookup.update((code.lower(), code) for code in table)
    return lookup


def country_codes() -> frozenset[CountryCode]:
    """Return the set of alpha-2 codes in the table.

    Returns:
        Every country code the table holds.
    """
    return frozenset(_table())


def parse_country(text: str) -> CountryCode | None:
    """Find the country a location text names.

    The whole text is tried first, then the part after its last `", "`, each
    stripped of surrounding whitespace. A part matches, ignoring case, when it is
    an alpha-2 code, a country name from the table, or one of a few aliases
    (`USA`, `UK`, `United Kingdom`, `South Korea`, `Czechia`).

    Args:
        text: A location as a source reports it, e.g. `"Toronto, Ontario, Canada"`.

    Returns:
        The country code, or `None` when the text names no country.

    Examples:
        >>> parse_country("San Francisco, United States")
        'US'
        >>> parse_country("Remote") is None
        True
    """
    lookup = _lookup()
    last = text.rpartition(_PART_SEPARATOR)[2]
    for part in (text, last):
        code = lookup.get(part.strip().lower())
        if code is not None:
            return code
    return None
