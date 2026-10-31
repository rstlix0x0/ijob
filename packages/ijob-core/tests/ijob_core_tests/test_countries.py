import pytest
from ijob_core.countries import CountryCode, country_codes, parse_country


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


@pytest.mark.parametrize("text", ["Remote", "Europe", "", "Atlantis"])
def test_unknown_is_none(text: str) -> None:
    assert parse_country(text) is None
