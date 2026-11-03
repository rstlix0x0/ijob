import pytest
from ijob_core.countries import (
    CountryCode,
    countries_in,
    country_codes,
    country_named,
    parse_country,
)


def test_table_has_249_codes() -> None:
    assert len(country_codes()) == 249


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("US", "US"),
        ("us", "US"),
        ("United States", "US"),
        ("Japan", "JP"),
        ("Britain (UK)", "GB"),
    ],
)
def test_codes_and_names(text: str, code: str) -> None:
    assert parse_country(text) == CountryCode(code)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("USA", "US"),
        ("UK", "GB"),
        ("United Kingdom", "GB"),
        ("South Korea", "KR"),
        ("Czechia", "CZ"),
    ],
)
def test_aliases(text: str, code: str) -> None:
    assert parse_country(text) == CountryCode(code)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("San Francisco, United States", "US"),
        ("Toronto, Ontario, Canada", "CA"),
        ("London, United Kingdom", "GB"),
        ("Canada,  USA", "US"),
    ],
)
def test_last_comma_part(text: str, code: str) -> None:
    assert parse_country(text) == CountryCode(code)


@pytest.mark.parametrize("text", ["San Francisco, CA", "Atlanta, GA", "Wilmington, DE"])
def test_last_comma_part_is_not_a_code(text: str) -> None:
    assert parse_country(text) is None


@pytest.mark.parametrize("text", ["Remote", "Europe", "", "Atlantis"])
def test_unknown_is_none(text: str) -> None:
    assert parse_country(text) is None


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("Tokyo", {"JP"}),
        ("東京都中央区", {"JP"}),
        ("Tokyo, Japan", {"JP"}),
        ("Japan or Bangkok (One Bangkok)", {"JP", "TH"}),
        ("Austin, TX", {"US"}),
        ("Toronto, ON", {"CA"}),
        ("San Francisco, CA • New York, NY", {"US"}),
        ("Remote - USA", {"US"}),
        ("Remote UK", {"GB"}),
        ("Remote - CA", {"US"}),
        ("Remote - AZ; Remote - TX", {"US"}),
        ("Atlanta, Georgia", {"US"}),
        ("Tbilisi, Georgia", {"GE"}),
        ("Latin America", set()),
        ("New Mexico", {"US"}),
        ("静岡県裾野市", {"JP"}),
        ("US-Remote", {"US"}),
        ("Cluj-Napoca", {"RO"}),
        ("Zürich", {"CH"}),
        ("Bengaluru, India", {"IN"}),
        ("Remote", set()),
        ("Hybrid", set()),
        ("Indiana", {"US"}),
        ("", set()),
    ],
)
def test_countries_in_finds_every_country_named(text: str, codes: set[str]) -> None:
    assert countries_in(text) == codes


@pytest.mark.parametrize(
    ("typed", "code"),
    [
        ("Japan", "JP"),
        ("jp", "JP"),
        ("America", "US"),
        ("Britain", "GB"),
        ("England", "GB"),
        ("Korea", "KR"),
        ("Viet Nam", "VN"),
        ("UAE", "AE"),
        ("The Netherlands", "NL"),
        ("Holland", "NL"),
        ("Deutschland", "DE"),
        ("Türkiye", "TR"),
        ("Ivory Coast", "CI"),
        ("Tokyo", "JP"),
        ("Berlin", "DE"),
    ],
)
def test_a_country_can_be_typed_any_way_a_person_would(typed: str, code: str) -> None:
    assert country_named(typed) == code


def test_a_place_it_does_not_know_is_no_country() -> None:
    assert country_named("Atlantis") is None
