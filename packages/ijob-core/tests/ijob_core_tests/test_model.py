import dataclasses
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any, override

import pytest
from ijob_core.countries import CountryCode
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    Posting,
    PostingId,
    RemoteScope,
    Sighting,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
    parse_source_ref,
)
from ijob_core.money import Money, Period

_STRIPE = SourceRef(SourceKind.GREENHOUSE, parse_source_key("stripe"))
_AWARE = datetime(2026, 8, 4, 14, 21, 41, tzinfo=timezone(timedelta(hours=-4)))


@pytest.mark.parametrize("text", ["stripe", "mistral.ai", "doordash-usa", "a_b"])
def test_parse_source_key_accepts_token_with_dot_dash_underscore(text: str) -> None:
    assert parse_source_key(text) == text


@pytest.mark.parametrize("text", ["", "a b", "a/b", "x?y", "x#y", "x%2F", ".", ".."])
def test_parse_source_key_rejects_unsafe_text(text: str) -> None:
    with pytest.raises(InvalidValueError):
        parse_source_key(text)


def test_source_ref_str_is_kind_colon_key() -> None:
    ref = SourceRef(SourceKind.GREENHOUSE, parse_source_key("stripe"))
    assert str(ref) == "greenhouse:stripe"


def test_source_ref_is_frozen_and_hashable() -> None:
    first = SourceRef(SourceKind.GREENHOUSE, parse_source_key("stripe"))
    second = SourceRef(SourceKind.GREENHOUSE, parse_source_key("stripe"))
    assert first == second
    assert hash(first) == hash(second)
    assert {first: 1}[second] == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.key = parse_source_key("other")  # type: ignore[misc]  # frozen field


@pytest.mark.parametrize("kind", list(SourceKind))
def test_source_ref_round_trip(kind: SourceKind) -> None:
    ref = SourceRef(kind, parse_source_key("x"))
    assert parse_source_ref(str(ref)) == ref


def test_parse_source_ref_fourdayweek() -> None:
    ref = parse_source_ref("4dayweek:engineering")
    assert ref.kind is SourceKind.FOURDAYWEEK
    assert ref.key == "engineering"


def test_parse_source_ref_rejects_a_second_colon() -> None:
    with pytest.raises(InvalidValueError):
        parse_source_ref("greenhouse:a:b")


@pytest.mark.parametrize("text", ["greenhouse", "nosuch:x", "greenhouse:a/b", ":x"])
def test_parse_source_ref_rejects(text: str) -> None:
    with pytest.raises(InvalidValueError):
        parse_source_ref(text)


def test_remote_scope_values() -> None:
    assert [scope.value for scope in RemoteScope] == [
        "none",
        "country",
        "region",
        "worldwide",
    ]


def _draft(**overrides: Any) -> SightingDraft:  # noqa: ANN401  # forwards arbitrary field overrides
    values: dict[str, Any] = {
        "source": _STRIPE,
        "external_id": ExternalId("4721252005"),
        "title": "Analytics Engineer, Data",
        "company": "Stripe",
        "location": "Remote (Canada)",
        "url": "https://job-boards.greenhouse.io/stripe/jobs/4721252005",
        "posted_at": _AWARE,
        "raw": "{}",
    }
    values.update(overrides)
    return SightingDraft(**values)


def test_sighting_draft_accepts_valid_values() -> None:
    draft = _draft()
    assert draft.source == _STRIPE
    assert draft.external_id == "4721252005"
    assert draft.title == "Analytics Engineer, Data"
    assert draft.company == "Stripe"
    assert draft.location == "Remote (Canada)"
    assert draft.url == "https://job-boards.greenhouse.io/stripe/jobs/4721252005"
    assert draft.posted_at == _AWARE
    assert draft.raw == "{}"

    bare = _draft(location=None, posted_at=None)
    assert bare.location is None
    assert bare.posted_at is None


def test_draft_new_fields_default() -> None:
    draft = _draft()
    assert draft.salaries == ()
    assert draft.country is None
    assert draft.remote is None
    assert draft.role_hint is None
    assert not draft.low_confidence


def test_draft_carries_new_fields() -> None:
    pay = Money(Decimal(100), Decimal(200), "USD", Period.YEAR)
    draft = _draft(
        salaries=(pay,),
        country=CountryCode("CA"),
        remote=RemoteScope.COUNTRY,
        role_hint="data engineer",
        low_confidence=True,
    )
    assert draft.salaries == (pay,)
    assert draft.country == "CA"
    assert draft.remote is RemoteScope.COUNTRY
    assert draft.role_hint == "data engineer"
    assert draft.low_confidence


@pytest.mark.parametrize("field", ["external_id", "title", "company", "url"])
def test_sighting_draft_rejects_empty_required_text(field: str) -> None:
    with pytest.raises(InvalidValueError) as exc_info:
        _draft(**{field: ""})
    assert exc_info.value.field == field


@pytest.mark.parametrize("url", ["ftp://x", "job-boards.greenhouse.io/x"])
def test_sighting_draft_rejects_non_http_url(url: str) -> None:
    with pytest.raises(InvalidValueError):
        _draft(url=url)


@pytest.mark.parametrize("url", ["https://[bad/1", "https://example\uff0f.com/x"])
def test_sighting_draft_rejects_unparseable_url(url: str) -> None:
    with pytest.raises(InvalidValueError) as exc_info:
        _draft(url=url)
    assert exc_info.value.field == "url"
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_sighting_draft_rejects_naive_posted_at() -> None:
    with pytest.raises(InvalidValueError):
        _draft(posted_at=datetime(2026, 8, 4, 14, 21, 41))  # noqa: DTZ001  # naive on purpose


class _NoOffset(tzinfo):
    @override
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    @override
    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    @override
    def tzname(self, dt: datetime | None) -> str | None:
        return None


def test_sighting_draft_rejects_posted_at_whose_zone_has_no_offset() -> None:
    with pytest.raises(InvalidValueError):
        _draft(posted_at=datetime(2026, 8, 4, 14, 21, 41, tzinfo=_NoOffset()))


def test_posting_and_sighting_hold_their_fields() -> None:
    first = datetime(2026, 10, 1, tzinfo=UTC)
    last = datetime(2026, 10, 2, tzinfo=UTC)
    posting = Posting(
        id=PostingId(1),
        title="T",
        company="C",
        location=None,
        url="https://x",
        posted_at=None,
        first_seen=first,
        last_seen=last,
    )
    sighting = Sighting(
        posting_id=posting.id,
        source=_STRIPE,
        external_id=ExternalId("1"),
        first_seen=first,
        last_seen=last,
        raw="{}",
    )
    assert sighting.posting_id == posting.id
    assert sighting.last_seen == posting.last_seen


def test_posting_new_fields_default_and_carry() -> None:
    when = datetime(2026, 10, 1, tzinfo=UTC)
    plain = Posting(
        id=PostingId(1),
        title="T",
        company="C",
        location=None,
        url="https://x",
        posted_at=None,
        first_seen=when,
        last_seen=when,
    )
    assert plain.salaries == ()
    assert plain.country is None
    assert plain.remote is None
    assert plain.role_hint is None
    assert not plain.low_confidence

    pay = Money(Decimal(5), None, "EUR", Period.HOUR)
    full = dataclasses.replace(
        plain,
        salaries=(pay,),
        country=CountryCode("DE"),
        remote=RemoteScope.WORLDWIDE,
        role_hint="backend",
        low_confidence=True,
    )
    assert full.salaries == (pay,)
    assert full.country == "DE"
    assert full.remote is RemoteScope.WORLDWIDE
    assert full.role_hint == "backend"
    assert full.low_confidence
