import pytest
from ijob_core.countries import CountryCode
from ijob_core.dedupe import canonical_url, fingerprint, normalise_company

SG = CountryCode("SG")
JP = CountryCode("JP")
_FULL_WIDTH_OFFSET = 0xFEE0


def _full_width(text: str) -> str:
    """Write printable ASCII text in its full-width forms."""
    return "".join(chr(ord(char) + _FULL_WIDTH_OFFSET) for char in text)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://remoteOK.com/remote-jobs/x-1137597",
            "https://remoteok.com/remote-jobs/x-1137597",
        ),
        (
            "HTTPS://Example.COM/Path",
            "https://example.com/Path",
        ),
        (
            "https://himalayas.app/companies/a/jobs/b?utm_source=api",
            "https://himalayas.app/companies/a/jobs/b",
        ),
        (
            "https://x.io/j?id=3&utm_medium=m&ref=z&utm_campaign=c",
            "https://x.io/j?id=3&ref=z",
        ),
        (
            "https://jobs.letsgetrusty.com?job=rec1",
            "https://jobs.letsgetrusty.com?job=rec1",
        ),
        ("https://x.io/jobs/1/#apply", "https://x.io/jobs/1"),
        ("https://x.io/", "https://x.io/"),
        ("https://x.io/jobs/1//", "https://x.io/jobs/1/"),
    ],
)
def test_canonical_url_cases(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


def test_fingerprint_suffixes() -> None:
    assert fingerprint("SCIENTEC CONSULTING PTE. LTD.", "Engineer", SG) == fingerprint(
        "Scientec Consulting", "engineer", SG
    )
    assert fingerprint("Dijital Team Pty Ltd", "Engineer", SG) == fingerprint(
        "Dijital Team", "Engineer", SG
    )


def test_fingerprint_joins_parts_with_pipes() -> None:
    assert fingerprint("Acme, Inc.", "Senior  Engineer (Backend)", SG) == (
        "acme|senior engineer backend|SG"
    )


def test_fingerprint_needs_country() -> None:
    assert fingerprint("Acme", "Engineer", None) is None


def test_fingerprint_differs_by_country() -> None:
    assert fingerprint("Acme", "Engineer", SG) != fingerprint("Acme", "Engineer", JP)


def test_fingerprint_unicode() -> None:
    result = fingerprint("Acme", "セキュリティエンジニア", JP)
    assert result == "acme|セキュリティエンジニア|JP"
    full_width_company = _full_width("Acme")
    full_width_title = _full_width("Engineer")
    assert fingerprint(full_width_company, full_width_title, JP) == fingerprint(
        "Acme", "Engineer", JP
    )


def test_normalise_company_removes_at_most_two_suffix_tokens() -> None:
    assert normalise_company("Acme Co Ltd") == "acme"
    assert normalise_company("Acme Inc Corp Ltd") == "acme inc"
    assert normalise_company("Inc Acme") == "inc acme"


def test_normalise_company_keeps_a_name_made_only_of_suffixes() -> None:
    assert normalise_company("Co") == "co"
    assert normalise_company("Inc. Ltd.") == "inc"


def test_normalise_company_collapses_punctuation() -> None:
    assert normalise_company("  Checkout.com  ") == "checkout com"
    assert normalise_company("Anysphere (Cursor)") == "anysphere cursor"
