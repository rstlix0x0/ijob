"""Text that goes into, and comes out of, a task call.

Model-derived text is cleaned before ijob stores it, long text is cut to a
fixed size, and a posting is written as one fixed block whose hash keys the
cache.
"""

import hashlib
import re
from typing import Final

from ijob_core.model import Posting
from ijob_core.money import money_text

POSTING_LIMIT: Final = 12_000
"""The most description characters a posting block carries."""

_CONTROLS: Final = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
_TRUNCATED: Final = "\n[truncated]"


def sanitize(text: str) -> str:
    r"""Strip control characters from text and trim it.

    Removes every C0 control except `\n`, plus `\x7f` and the C1 controls
    (`\x80` to `\x9f`), then trims surrounding whitespace. Other characters,
    including markup, are kept as they are.

    Args:
        text: Text from a model or a third party.

    Returns:
        The cleaned text.
    """
    return _CONTROLS.sub("", text).strip()


def truncate(text: str, limit: int) -> str:
    r"""Cut text to its first `limit` characters and mark the cut.

    Args:
        text: The text to cut.
        limit: The most characters of `text` to keep.

    Returns:
        `text` itself when it fits; otherwise its first `limit` characters
        followed by `\n[truncated]`, which does not count towards `limit`.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATED


def posting_block(posting: Posting) -> str:
    """Write a posting as the labelled lines a task reads.

    Args:
        posting: The posting to write.

    Returns:
        One `label: value` line each for the title, company, location,
        country, remote scope, salary and description. `-` stands for an
        absent value, the salaries are joined with `; `, and the description
        is cut to `POSTING_LIMIT` characters.
    """
    description = (
        truncate(posting.description, POSTING_LIMIT)
        if posting.description
        else "no description available"
    )
    salary = "; ".join(money_text(money) for money in posting.salaries) or "-"
    lines = [
        ("title", posting.title),
        ("company", posting.company),
        ("location", posting.location or "-"),
        ("country", str(posting.country or "-")),
        ("remote", str(posting.remote or "-")),
        ("salary", salary),
        ("description", description),
    ]
    return "\n".join(f"{label}: {value}" for label, value in lines)


def input_sha256(block: str) -> str:
    """Hash a posting block for a cache key.

    Args:
        block: The text from `posting_block`.

    Returns:
        The hex SHA-256 of the block's UTF-8 bytes.
    """
    return hashlib.sha256(block.encode()).hexdigest()
