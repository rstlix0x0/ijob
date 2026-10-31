"""Keys that recognise one job listed by more than one source.

`canonical_url` compares listings by address and `fingerprint` compares them
by company, title and country. `normalise_company` is the company half of the
fingerprint, public so that other checks can compare company names the same way.
"""

import re
import unicodedata
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from ijob_core.countries import CountryCode

_LEGAL_SUFFIXES: Final = frozenset(
    {"inc", "llc", "ltd", "gmbh", "corp", "co", "pte", "pty"}
)
_MAX_SUFFIX_TOKENS: Final = 2
_NON_ALPHANUMERIC: Final = re.compile(r"[\W_]+")
_TRACKING_PREFIX: Final = "utm_"


def _normalise_text(text: str) -> str:
    """Fold case and width, and reduce every run of punctuation to one space."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _NON_ALPHANUMERIC.sub(" ", folded).strip()


def normalise_company(text: str) -> str:
    """Reduce a company name to the form used to compare companies.

    The text is folded as a title is, then up to two trailing legal-suffix
    tokens such as `ltd` or `pty` are removed. A name made only of suffix
    tokens keeps its first token rather than becoming empty.

    Args:
        text: The company name as a source wrote it.

    Returns:
        The lower-case name with single spaces and no trailing legal suffix.
    """
    tokens = _normalise_text(text).split()
    removed = 0
    while (
        len(tokens) > 1
        and removed < _MAX_SUFFIX_TOKENS
        and tokens[-1] in _LEGAL_SUFFIXES
    ):
        tokens.pop()
        removed += 1
    return " ".join(tokens)


def canonical_url(url: str) -> str:
    """Rewrite a listing address so that equivalent addresses compare equal.

    The scheme and host are lower-cased, the fragment and every `utm_` query
    parameter (matched without regard to case) are dropped (the others keep
    their order), and one trailing `/` is dropped from a path longer than `/`.

    Args:
        url: The address as a source gave it. It must be parseable by
            `urllib.parse.urlsplit`, as the address of a `SightingDraft` is.

    Returns:
        The canonical address.

    Raises:
        ValueError: If `url` cannot be parsed. A `SightingDraft` never holds
            such an address.
    """
    parts = urlsplit(url)
    userinfo, at, host = parts.netloc.rpartition("@")
    netloc = f"{userinfo}{at}{host.lower()}"
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and not pair.casefold().startswith(_TRACKING_PREFIX)
    )
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def fingerprint(company: str, title: str, country: CountryCode | None) -> str | None:
    """Build the key that matches one job across sources.

    Args:
        company: The employer's name.
        title: The job title.
        country: The country of the job, or `None` when it is unknown.

    Returns:
        `company|title|CC` with the company and title normalised, or `None`
        when `country` is `None`, so that jobs in unknown places never merge.
    """
    if country is None:
        return None
    return f"{normalise_company(company)}|{_normalise_text(title)}|{country}"
