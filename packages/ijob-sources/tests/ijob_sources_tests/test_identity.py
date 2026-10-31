import pytest
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_sources.errors import IdentityMismatchError
from ijob_sources.identity import check_identity

_REF = SourceRef(SourceKind.GREENHOUSE, parse_source_key("board"))


def test_empty_batch_passes() -> None:
    check_identity(_REF, "Prisma", names=[], texts=[], count=0)


@pytest.mark.parametrize(
    ("company", "name"),
    [
        ("Tailscale", "Tailscale"),
        ("Mercari", "Mercari, inc."),
        ("Mercari, inc.", "Mercari"),
    ],
)
def test_name_match(company: str, name: str) -> None:
    check_identity(_REF, company, names=[name], texts=[], count=1)


def test_name_containing_or_contained_passes() -> None:
    check_identity(_REF, "Hugging", names=["Hugging Face"], texts=[], count=1)
    check_identity(_REF, "Hugging Face Labs", names=["Hugging Face"], texts=[], count=1)


@pytest.mark.parametrize(
    ("company", "name"),
    [
        ("Scale", "Tailscale"),
        ("Ramp", "Trampoline"),
        ("OpenAI", "AI"),
        ("Notion", "Emotional Notions"),
        ("Coda", "Encoda"),
    ],
)
def test_name_match_is_on_token_boundaries(company: str, name: str) -> None:
    with pytest.raises(IdentityMismatchError):
        check_identity(_REF, company, names=[name], texts=[], count=1)


def test_name_that_is_a_whole_token_run_of_the_company_passes() -> None:
    check_identity(_REF, "Scale AI", names=["Scale"], texts=[], count=1)
    check_identity(_REF, "Scale", names=["Scale AI"], texts=[], count=1)


def test_parenthesised_part_matches_a_name() -> None:
    check_identity(_REF, "Anysphere (Cursor)", names=["Cursor"], texts=[], count=1)


def test_text_match() -> None:
    check_identity(
        _REF, "Ramp", names=[], texts=["Ramp is building the future."], count=1
    )


def test_text_match_is_whole_word() -> None:
    with pytest.raises(IdentityMismatchError):
        check_identity(
            _REF, "Ramp", names=[], texts=["We champion rampant growth"], count=1
        )


@pytest.mark.parametrize("company", ["Checkout.com", "Crypto.com"])
def test_dotted_company_matches_text(company: str) -> None:
    check_identity(_REF, company, names=[], texts=[f"Join {company}'s team"], count=1)


@pytest.mark.parametrize(
    "text", ["Cursor builds editors.", "Anysphere builds editors."]
)
def test_parenthesised_company(text: str) -> None:
    check_identity(_REF, "Anysphere (Cursor)", names=[], texts=[text], count=1)


def test_parenthesised_company_matches_full_name_too() -> None:
    check_identity(
        _REF, "Anysphere (Cursor)", names=[], texts=["Anysphere Cursor team"], count=1
    )


def test_mismatch_raises() -> None:
    with pytest.raises(IdentityMismatchError) as caught:
        check_identity(
            _REF,
            "Prisma",
            names=["Tailscale", "Mercari"],
            texts=["Tailscale connects machines."],
            count=2,
        )
    assert caught.value.ref == _REF
    assert caught.value.reason == "company 'Prisma' not found on the board"
