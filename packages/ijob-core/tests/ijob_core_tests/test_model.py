import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any, override

import pytest
from ijob_core.countries import CountryCode
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    ExternalId,
    Instructions,
    Label,
    Posting,
    PostingId,
    RemoteScope,
    ResumeProfile,
    Score,
    Seniority,
    Sighting,
    SightingDraft,
    SourceKind,
    SourceRef,
    StoredCv,
    StoredRecipe,
    Verdict,
    Vote,
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


def test_sighting_draft_description_defaults_to_none() -> None:
    assert _draft().description is None
    assert _draft(description="## About").description == "## About"


def test_posting_description_defaults_to_none() -> None:
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
    assert plain.description is None
    assert dataclasses.replace(plain, description="text").description == "text"


def test_vote_values() -> None:
    assert Vote("up") is Vote.UP
    assert Vote("down") is Vote.DOWN
    with pytest.raises(ValueError, match="meh"):
        Vote("meh")


def test_label_holds_its_fields() -> None:
    label = Label(PostingId(3), Vote.UP, "fit", _AWARE)
    assert (label.posting_id, label.vote, label.note) == (3, Vote.UP, "fit")
    assert label.labeled_at == _AWARE
    assert Label(PostingId(3), Vote.DOWN, None, _AWARE).note is None


def test_label_rejects_naive_time() -> None:
    naive = datetime(2026, 10, 1, 8, 0)  # noqa: DTZ001  # naive on purpose
    with pytest.raises(InvalidValueError, match="labeled_at"):
        Label(PostingId(1), Vote.UP, None, naive)


@pytest.mark.parametrize("note", ["", "  ", "\n\t"])
def test_label_rejects_blank_note(note: str) -> None:
    with pytest.raises(InvalidValueError, match="note"):
        Label(PostingId(1), Vote.UP, note, _AWARE)


_HASH = "ab" * 32


def _cv(**overrides: Any) -> StoredCv:  # noqa: ANN401  # forwards arbitrary field overrides
    values: dict[str, Any] = {
        "name": "cv.pdf",
        "sha256": _HASH,
        "size": 1234,
        "stored_at": _AWARE,
    }
    values.update(overrides)
    return StoredCv(**values)


def test_stored_cv_holds_its_fields() -> None:
    cv = _cv()
    assert (cv.name, cv.sha256, cv.size, cv.stored_at) == (
        "cv.pdf",
        _HASH,
        1234,
        _AWARE,
    )
    assert _cv(size=0).size == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"sha256": "a" * 63},
        {"sha256": "A" * 64},
        {"sha256": "g" * 64},
        {"size": -1},
        {"stored_at": datetime(2026, 10, 1, 8, 0)},  # noqa: DTZ001  # naive on purpose
    ],
)
def test_stored_cv_rejects_bad_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(InvalidValueError):
        _cv(**overrides)


def _stored_recipe(**overrides: Any) -> StoredRecipe:  # noqa: ANN401  # forwards arbitrary field overrides
    values: dict[str, Any] = {
        "ref": SourceRef(SourceKind.GREENHOUSE, parse_source_key("acme")),
        "recipe": '{"steps": []}',
        "model": "model-a",
        "created_at": _AWARE,
    }
    values.update(overrides)
    return StoredRecipe(**values)


def test_stored_recipe_holds_its_fields() -> None:
    recipe = _stored_recipe()
    assert recipe.ref == SourceRef(SourceKind.GREENHOUSE, parse_source_key("acme"))
    assert (recipe.recipe, recipe.model, recipe.created_at) == (
        '{"steps": []}',
        "model-a",
        _AWARE,
    )


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"recipe": ""}, "recipe"),
        ({"model": ""}, "model"),
        ({"created_at": datetime(2026, 10, 1, 8, 0)}, "created_at"),  # noqa: DTZ001  # naive on purpose
    ],
)
def test_stored_recipe_validation(overrides: dict[str, Any], field: str) -> None:
    with pytest.raises(InvalidValueError, match=field):
        _stored_recipe(**overrides)


def test_stored_recipe_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        StoredRecipe(  # type: ignore[call-arg]
            SourceRef(SourceKind.GREENHOUSE, parse_source_key("acme")),
            "{}",
            "model-a",
            _AWARE,
        )


def _profile(**overrides: Any) -> ResumeProfile:  # noqa: ANN401  # forwards arbitrary field overrides
    fields: dict[str, Any] = {
        "summary": "Backend engineer.",
        "years_experience": 7,
        "seniority": Seniority.SENIOR,
        "roles": ("backend engineer",),
        "spoken_languages": ("English",),
        "locations": ("Jakarta",),
        "work_authorization": ("Indonesia",),
        "skills": ("python", "sql"),
    }
    fields.update(overrides)
    return ResumeProfile(**fields)


def test_seniority_values() -> None:
    assert [s.value for s in Seniority] == [
        "junior",
        "mid",
        "senior",
        "staff",
        "principal",
        "unknown",
    ]


def test_resume_profile_accepts_bounds() -> None:
    assert len(_profile(summary="x" * 600).summary) == 600
    assert _profile(years_experience=0).years_experience == 0
    assert _profile(years_experience=60).years_experience == 60
    assert _profile(years_experience=None).years_experience is None
    assert len(_profile(roles=("r",) * 20).roles) == 20
    assert len(_profile(skills=("s",) * 100).skills) == 100
    assert len(_profile(skills=("s" * 100,)).skills[0]) == 100


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("summary", {"summary": "x" * 601}),
        ("years_experience", {"years_experience": 61}),
        ("years_experience", {"years_experience": -1}),
        ("roles", {"roles": ("r",) * 21}),
        ("spoken_languages", {"spoken_languages": ("l",) * 21}),
        ("locations", {"locations": ("l",) * 21}),
        ("work_authorization", {"work_authorization": ("w",) * 21}),
        ("skills", {"skills": ("s",) * 101}),
        ("roles", {"roles": ("ok", "  ")}),
        ("skills", {"skills": ("",)}),
        ("skills", {"skills": ("s" * 101,)}),
        ("locations", {"locations": ("l" * 101,)}),
    ],
)
def test_resume_profile_rejects_over_bounds(
    field: str, overrides: dict[str, Any]
) -> None:
    with pytest.raises(InvalidValueError) as caught:
        _profile(**overrides)
    assert caught.value.field == field


def test_resume_profile_json_roundtrip_and_hash_stable() -> None:
    profile = _profile()
    assert ResumeProfile.from_json(profile.to_json()) == profile
    assert ", " not in profile.to_json()
    assert ": " not in profile.to_json()

    shuffled = json.loads(profile.to_json())
    reordered = json.dumps(dict(reversed(list(shuffled.items()))), indent=3)
    rebuilt = ResumeProfile.from_json(reordered)
    assert rebuilt == profile
    assert rebuilt.sha256() == profile.sha256()
    assert profile.sha256() == hashlib.sha256(profile.to_json().encode()).hexdigest()
    assert _profile(skills=("python",)).sha256() != profile.sha256()


def test_resume_profile_from_json_rejects_bad_text() -> None:
    with pytest.raises(InvalidValueError):
        ResumeProfile.from_json("not json")
    with pytest.raises(InvalidValueError):
        ResumeProfile.from_json("[]")
    with pytest.raises(InvalidValueError):
        ResumeProfile.from_json('{"summary": "x"}')
    bad = json.loads(_profile().to_json())
    bad["seniority"] = "wizard"
    with pytest.raises(InvalidValueError):
        ResumeProfile.from_json(json.dumps(bad))


def _instructions(**overrides: Any) -> Instructions:  # noqa: ANN401  # forwards arbitrary field overrides
    fields: dict[str, Any] = {
        "text": "Remote backend roles only.",
        "filters_json": '{"remote":true}',
        "rules": ("no crypto", "no on-site"),
        "model": "m",
        "task_version": "v1",
        "set_at": _AWARE,
    }
    fields.update(overrides)
    return Instructions(**fields)


def test_instructions_hashes() -> None:
    base = _instructions()
    expected_rules = json.dumps(["no crypto", "no on-site"], separators=(",", ":"))
    assert base.rules_sha256 == hashlib.sha256(expected_rules.encode()).hexdigest()
    assert base.text_sha256 == hashlib.sha256(base.text.encode()).hexdigest()
    changed = _instructions(rules=("no crypto", "no travel"))
    assert changed.rules_sha256 != base.rules_sha256
    later = _instructions(set_at=_AWARE + timedelta(days=1))
    assert later.rules_sha256 == base.rules_sha256
    assert later.text_sha256 == base.text_sha256
    joined = _instructions(rules=("a\nb",))
    split = _instructions(rules=("a", "b"))
    assert joined.rules_sha256 != split.rules_sha256


def test_instructions_accepts_bounds() -> None:
    assert len(_instructions(text="x" * 4000).text) == 4000
    assert _instructions(rules=()).rules == ()
    assert len(_instructions(rules=("r",) * 10).rules) == 10
    assert len(_instructions(rules=("r" * 200,)).rules[0]) == 200


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("text", {"text": "   "}),
        ("text", {"text": "x" * 4001}),
        ("rules", {"rules": ("r",) * 11}),
        ("rules", {"rules": ("r" * 201,)}),
        ("rules", {"rules": ("",)}),
        ("filters_json", {"filters_json": "not json"}),
        ("filters_json", {"filters_json": "[1]"}),
        ("model", {"model": " "}),
        ("task_version", {"task_version": ""}),
        ("set_at", {"set_at": datetime(2026, 10, 1, 8, 0)}),  # noqa: DTZ001  # naive
    ],
)
def test_instructions_rejects_blank_text_and_too_many_rules(
    field: str, overrides: dict[str, Any]
) -> None:
    with pytest.raises(InvalidValueError) as caught:
        _instructions(**overrides)
    assert caught.value.field == field


def test_verdict_consistency() -> None:
    assert Verdict(keep=True, reason="fits", rule=None).rule is None
    assert Verdict(keep=False, reason="no", rule=2).rule == 2
    for keep, rule in ((True, 1), (False, None), (False, 0)):
        with pytest.raises(InvalidValueError) as caught:
            Verdict(keep=keep, reason="r", rule=rule)
        assert caught.value.field == "rule"
    for reason in ("", "r" * 201):
        with pytest.raises(InvalidValueError) as caught:
            Verdict(keep=True, reason=reason, rule=None)
        assert caught.value.field == "reason"
    assert len(Verdict(keep=True, reason="r" * 200, rule=None).reason) == 200


def test_score_bounds() -> None:
    assert Score(score=0, reasons=("a",)).score == 0
    assert Score(score=100, reasons=("a",) * 5).score == 100
    assert len(Score(score=50, reasons=("a" * 200,)).reasons[0]) == 200
    for value in (-1, 101):
        with pytest.raises(InvalidValueError) as caught:
            Score(score=value, reasons=("a",))
        assert caught.value.field == "score"
    for reasons in ((), ("a",) * 6, ("",), ("a" * 201,)):
        with pytest.raises(InvalidValueError) as caught:
            Score(score=1, reasons=reasons)
        assert caught.value.field == "reasons"
