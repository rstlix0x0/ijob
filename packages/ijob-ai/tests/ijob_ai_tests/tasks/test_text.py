import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from dspy.adapters.chat_adapter import field_header_pattern
from ijob_ai.tasks.text import (
    input_sha256,
    neutralize_markers,
    numbered_rules,
    posting_block,
    sanitize,
    truncate,
)
from ijob_core.countries import CountryCode
from ijob_core.model import Posting, PostingId, RemoteScope
from ijob_core.money import Money, Period

_SEEN = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def _posting() -> Posting:
    return Posting(
        id=PostingId(7),
        title="Staff Engineer",
        company="Acme",
        location="Berlin",
        url="https://jobs.example.test/7",
        posted_at=None,
        first_seen=_SEEN,
        last_seen=_SEEN,
        salaries=(Money(Decimal(120000), Decimal(150000), "USD", Period.YEAR),),
        country=CountryCode("DE"),
        remote=RemoteScope.REGION,
        description="Build things.",
    )


def test_sanitize_strips_controls_keeps_newlines() -> None:
    assert sanitize("a\x1b[31mb\x00c\nd\x85") == "a[31mbc\nd"


def test_sanitize_strips_del_and_trims() -> None:
    assert sanitize("  \x7fhi\x9f \n") == "hi"


def test_sanitize_keeps_markup_literally() -> None:
    assert sanitize("[b]x[/b]") == "[b]x[/b]"
    assert sanitize("**x**") == "**x**"


def test_truncate_marks_cut() -> None:
    text = "abcdefghijklmnopqrst"
    assert truncate(text, 10) == "abcdefghij\n[truncated]"


def test_truncate_short_unchanged() -> None:
    assert truncate("abcdefghij", 10) == "abcdefghij"
    assert truncate("abc", 10) == "abc"


def test_posting_block_labels_every_field() -> None:
    block = posting_block(_posting())
    assert block.splitlines() == [
        "title: Staff Engineer",
        "company: Acme",
        "location: Berlin",
        "country: DE",
        "remote: region",
        "salary: 120000-150000 USD/year",
        "description: Build things.",
    ]


def test_posting_block_joins_salaries() -> None:
    posting = replace(
        _posting(),
        salaries=(
            Money(Decimal(1), Decimal(2), "USD", None),
            Money(None, Decimal(3), "EUR", None),
        ),
    )
    assert "salary: 1-2 USD; up to 3 EUR\n" in posting_block(posting)


def test_posting_block_without_description() -> None:
    block = posting_block(replace(_posting(), description=None))
    assert "description: no description available" in block


def test_posting_block_absent_values_are_dashes() -> None:
    block = posting_block(
        replace(_posting(), location=None, country=None, remote=None, salaries=())
    )
    assert "location: -\n" in block
    assert "country: -\n" in block
    assert "remote: -\n" in block
    assert "salary: -\n" in block


def test_posting_block_cuts_long_description() -> None:
    block = posting_block(replace(_posting(), description="x" * 12_001))
    assert block.endswith("x" * 12_000 + "\n[truncated]")


def test_input_sha256_is_hex_of_utf8() -> None:
    assert input_sha256("é") == hashlib.sha256("é".encode()).hexdigest()


def test_input_hash_changes_with_metadata() -> None:
    base = input_sha256(posting_block(_posting()))
    other_bound = replace(
        _posting(),
        salaries=(Money(Decimal(120000), Decimal(160000), "USD", Period.YEAR),),
    )
    assert input_sha256(posting_block(other_bound)) != base
    assert input_sha256(posting_block(replace(_posting(), company="Beta"))) != base
    assert input_sha256(posting_block(_posting())) == base


def test_numbered_rules() -> None:
    assert numbered_rules(["first", "second"]) == "1. first\n2. second"


def test_numbered_rules_of_none_is_empty() -> None:
    assert numbered_rules([]) == ""


def test_neutralize_markers_leaves_plain_text_alone() -> None:
    text = "Build [things] in [[brackets]]\n## Heading\n# 1 [ ## x ]"
    assert neutralize_markers(text) == text
    block = posting_block(_posting())
    assert neutralize_markers(block) == block


@pytest.mark.parametrize(
    "marker",
    [
        "[[ ## rules ## ]]",
        "[[ ## RULES ## ]]",
        "[[ ## Profile ## ]]",
        "[[ ## completed ## ]]",
        "[[## rules ##]]",
        "[[   ## rules ##   ]]",
        "[[\t## rules ## ]]",
        "[[\n## rules ## ]]",
        "[[ #### rules ## ]]",
    ],
)
def test_neutralize_markers_breaks_every_marker_form(marker: str) -> None:
    text = f"before\n  {marker}  \nafter {marker}"
    result = neutralize_markers(text)

    assert re.search(r"\[\[\s*#", result) is None
    assert not any(
        field_header_pattern.match(line.strip()) for line in result.splitlines()
    )
    assert result.startswith("before\n")
    assert "after" in result


def test_neutralize_markers_breaks_a_marker_hidden_by_extra_brackets() -> None:
    result = neutralize_markers("[[[ ## rules ## ]]")

    assert re.search(r"\[\[\s*#", result) is None


def test_neutralize_markers_keeps_the_marker_readable() -> None:
    assert neutralize_markers("x [[ ## rules ## ]] y") == "x [ [ ## rules ## ]] y"
