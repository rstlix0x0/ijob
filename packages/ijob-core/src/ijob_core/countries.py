"""Country codes read from the text of a job's location.

The table of ISO 3166 alpha-2 codes and English names is the `iso3166.tab` file
shipped in the package's `data` directory, a copy of the one in tzdata.
`parse_country` reads a free-text location against it. `countries_in` also knows
the other names and the cities in `places.tab`, and finds every country a location
names, so a job in `Tokyo` counts as being in Japan.
"""

import re
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
_PLACES_FILE: Final = "places.tab"
_FIRST_CJK: Final = 0x3000
_US: Final = CountryCode("US")
_CANADA: Final = CountryCode("CA")
_US_STATE_CODES: Final = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    }
)
_CANADIAN_CODES: Final = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
)
_REGION_CODE: Final = re.compile(r"(?:,|•|-|\u2013|\()\s*([A-Z]{2})(?![A-Za-z])")
_GEORGIA: Final = CountryCode("GE")
_TYPED_ONLY: Final = {"america": _US}


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
    """Map each lowercased country name and alias to its country code."""
    lookup = dict(_ALIASES)
    lookup.update((name.lower(), code) for code, name in _table().items())
    return lookup


@cache
def _code_lookup() -> dict[str, CountryCode]:
    """Map each lowercased alpha-2 code to its country code."""
    return {code.lower(): code for code in _table()}


@cache
def _places() -> dict[str, CountryCode]:
    """Map every name a location may use for a place to its country's code."""
    text = (files("ijob_core") / "data" / _PLACES_FILE).read_text(encoding="utf-8")
    places = dict(_lookup())
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, code = line.split("\t")
            places[name.casefold()] = CountryCode(code)
    return places


@cache
def _place_pattern() -> re.Pattern[str]:
    """Build the pattern that finds a place's name, whole, longest names first."""
    names = sorted(_places(), key=len, reverse=True)
    body = "|".join(map(re.escape, names))
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)|{_cjk_names(names)}")


def _cjk_names(names: list[str]) -> str:
    """Give the pattern for names in a script that puts no space between words."""
    written = [name for name in names if all(ord(char) >= _FIRST_CJK for char in name)]
    return "|".join(map(re.escape, written)) or "(?!)"


@cache
def countries_in(text: str) -> frozenset[CountryCode]:
    """Find every country a location text names.

    Country names, their other names and well-known cities match whole and in any
    case. A two-letter code matches after a comma or a bullet when it is a US
    state or a Canadian province, as in `Austin, TX`.

    Args:
        text: A location as a source reports it.

    Returns:
        The countries it names; empty when it names none.

    Examples:
        >>> sorted(countries_in("Tokyo or Bangkok"))
        ['JP', 'TH']
        >>> sorted(countries_in("Austin, TX"))
        ['US']
        >>> sorted(countries_in("Remote"))
        []
    """
    places = _places()
    found = {
        places[match.group(0)] for match in _place_pattern().finditer(text.casefold())
    }
    for match in _REGION_CODE.finditer(text):
        code = match.group(1)
        if code in _US_STATE_CODES:
            found.add(_US)
        elif code in _CANADIAN_CODES:
            found.add(_CANADA)
    # `Atlanta, Georgia` is the US state, and not the country.
    if _US in found:
        found.discard(_GEORGIA)
    return frozenset(found)


def country_named(text: str) -> CountryCode | None:
    """Read a country the way a person types one when asked which they want.

    As `parse_country`, and it also knows other names and well-known cities, so
    `Holland`, `Korea` and `Tokyo` all work.

    Args:
        text: What was typed.

    Returns:
        The country code, or `None` when the text names no country.

    Examples:
        >>> country_named("Deutschland")
        'DE'
        >>> country_named("Tokyo")
        'JP'
        >>> country_named("Atlantis") is None
        True
    """
    typed = text.strip().casefold()
    return parse_country(text) or _TYPED_ONLY.get(typed) or _places().get(typed)


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
    a country name from the table or one of a few aliases (`USA`, `UK`,
    `United Kingdom`, `South Korea`, `Czechia`). An alpha-2 code matches only as
    the whole text, because a code after a comma is usually a region, as in
    `"San Francisco, CA"`.

    Args:
        text: A location as a source reports it, e.g. `"Toronto, Ontario, Canada"`.

    Returns:
        The country code, or `None` when the text names no country.

    Examples:
        >>> parse_country("San Francisco, United States")
        'US'
        >>> parse_country("Remote") is None
        True
        >>> parse_country("San Francisco, CA") is None
        True
    """
    whole = text.strip().lower()
    code = _lookup().get(whole) or _code_lookup().get(whole)
    if code is not None:
        return code
    last = text.rpartition(_PART_SEPARATOR)[2]
    return _lookup().get(last.strip().lower())
