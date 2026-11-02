from datetime import UTC, datetime
from decimal import Decimal

from ijob_app.render import plain_cell, posting_fields
from ijob_app.service import PostingDetail
from ijob_core.countries import CountryCode
from ijob_core.model import Posting, PostingId, RemoteScope, parse_source_ref
from ijob_core.money import Money, Period
from rich.text import Text

_SEEN = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _detail() -> PostingDetail:
    posting = Posting(
        id=PostingId(1),
        title="Staff Engineer",
        company="Acme",
        location="Berlin",
        url="https://jobs.example.test/1",
        posted_at=datetime(2026, 10, 20, 8, 30, tzinfo=UTC),
        first_seen=_SEEN,
        last_seen=_SEEN,
        salaries=(
            Money(Decimal(120000), Decimal(150000), "USD", Period.YEAR),
            Money(Decimal(1), None, "USD", None),
        ),
        country=CountryCode("DE"),
        remote=RemoteScope.COUNTRY,
    )
    return PostingDetail(
        posting=posting,
        sources=[parse_source_ref("greenhouse:acme"), parse_source_ref("lever:acme")],
        label=None,
        description=None,
        unavailable_reason=None,
    )


def test_posting_fields_lists_each_field_in_show_order() -> None:
    assert posting_fields(_detail()) == [
        ("location", "Berlin"),
        ("salary", "120000-150000 USD/year"),
        ("salary", "from 1 USD"),
        ("country", "DE"),
        ("remote", "country"),
        ("posted", "2026-10-20"),
        ("sources", "greenhouse:acme,lever:acme"),
    ]


def test_posting_fields_writes_a_dash_for_what_is_absent() -> None:
    bare = _detail()
    posting = Posting(
        id=bare.posting.id,
        title="T",
        company="C",
        location=None,
        url="https://x.test",
        posted_at=None,
        first_seen=_SEEN,
        last_seen=_SEEN,
    )
    detail = PostingDetail(posting, [], None, None, None)

    assert posting_fields(detail) == [
        ("location", "-"),
        ("salary", "-"),
        ("country", "-"),
        ("remote", "-"),
        ("posted", "-"),
        ("sources", ""),
    ]


def test_plain_cell_keeps_markup_as_literal_text() -> None:
    cell = plain_cell(
        "[bold]a[/bold] [/x] [@click=app.quit]b[/] [link=file:///x]c[/link]"
    )

    assert isinstance(cell, Text)
    assert cell.plain == (
        "[bold]a[/bold] [/x] [@click=app.quit]b[/] [link=file:///x]c[/link]"
    )
    assert cell.spans == []
