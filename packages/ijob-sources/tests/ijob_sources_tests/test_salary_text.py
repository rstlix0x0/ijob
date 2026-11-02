from decimal import Decimal

from ijob_core.money import Money, Period
from ijob_sources.salary_text import KNOWN_CURRENCIES, SalaryDefaults, parse_salary

_EN_DASH = chr(0x2013)
_NO_DEFAULTS = SalaryDefaults(currency=None, period=None)
_USD = SalaryDefaults(currency="USD", period=None)
_USD_YEAR = SalaryDefaults(currency="USD", period=Period.YEAR)


def test_range_with_code_and_default_period() -> None:
    assert parse_salary("$50,000 - $74,999 USD", _USD_YEAR) == Money(
        min=Decimal(50000), max=Decimal(74999), currency="USD", period=Period.YEAR
    )


def test_or_more_gives_min_only() -> None:
    assert parse_salary("$100,000 or more USD", _NO_DEFAULTS) == Money(
        min=Decimal(100000), max=None, currency="USD", period=None
    )


def test_decimal_hourly_range() -> None:
    assert parse_salary(f"$15.00{_EN_DASH}$17.00 per hour", _USD) == Money(
        min=Decimal("15.00"), max=Decimal("17.00"), currency="USD", period=Period.HOUR
    )


def test_en_dash_hourly_range() -> None:
    assert parse_salary(f"$16{_EN_DASH}$24 per hour", _USD) == Money(
        min=Decimal(16), max=Decimal(24), currency="USD", period=Period.HOUR
    )


def test_trailing_plus_hourly() -> None:
    assert parse_salary("$20/hr+", _USD) == Money(
        min=Decimal(20), max=None, currency="USD", period=Period.HOUR
    )


def test_dollar_without_default_is_none() -> None:
    assert parse_salary("$20/hr+", _NO_DEFAULTS) is None


def test_euro_dot_thousands() -> None:
    assert parse_salary("€60.000 - €80.000", _NO_DEFAULTS) == Money(
        min=Decimal(60000), max=Decimal(80000), currency="EUR", period=None
    )


def test_k_suffix_single_amount() -> None:
    assert parse_salary("$120k", _USD_YEAR) == Money(
        min=Decimal(120000), max=Decimal(120000), currency="USD", period=Period.YEAR
    )


def test_up_to_pounds_a_year() -> None:
    assert parse_salary("up to £45,000 a year", _NO_DEFAULTS) == Money(
        min=None, max=Decimal(45000), currency="GBP", period=Period.YEAR
    )


def test_words_only_is_none() -> None:
    assert parse_salary("Competitive", _USD_YEAR) is None


def test_min_above_max_is_none() -> None:
    assert parse_salary("$80,000 - $60,000", _USD) is None


def test_ambiguous_separator_is_none() -> None:
    assert parse_salary("$1,5k", _USD) is None


def test_three_amounts_is_none() -> None:
    assert parse_salary("$90k - $110k - $130k", _USD) is None


def test_unknown_uppercase_words_are_not_codes() -> None:
    assert parse_salary("THE API pays 50000", _NO_DEFAULTS) is None


def test_empty_and_whitespace_are_none() -> None:
    assert parse_salary("", _USD_YEAR) is None
    assert parse_salary("   ", _USD_YEAR) is None


def test_earliest_code_in_text_wins() -> None:
    assert parse_salary("CAD 60,000 or USD equivalent", _NO_DEFAULTS) == Money(
        min=Decimal(60000), max=Decimal(60000), currency="CAD", period=None
    )


def test_space_thousands_separator() -> None:
    assert parse_salary("1 000 000 JPY", _NO_DEFAULTS) == Money(
        min=Decimal(1000000), max=Decimal(1000000), currency="JPY", period=None
    )


def test_from_gives_min_only() -> None:
    assert parse_salary("from $90,000", _USD_YEAR) == Money(
        min=Decimal(90000), max=None, currency="USD", period=Period.YEAR
    )


def test_period_words_override_default_period() -> None:
    assert parse_salary("$5,000 monthly", _USD_YEAR) == Money(
        min=Decimal(5000), max=Decimal(5000), currency="USD", period=Period.MONTH
    )


def test_bound_and_period_words_ignore_case() -> None:
    assert parse_salary("UP TO £45,000 PER ANNUM", _NO_DEFAULTS) == Money(
        min=None, max=Decimal(45000), currency="GBP", period=Period.YEAR
    )


def test_lowercase_code_is_not_a_code() -> None:
    assert parse_salary("usd 50000", _NO_DEFAULTS) is None


def test_known_currencies_hold_the_rule_list() -> None:
    assert {"USD", "EUR", "GBP", "ZAR"} <= KNOWN_CURRENCIES
    assert "THE" not in KNOWN_CURRENCIES


def test_dot_as_thousands_and_decimal_is_none() -> None:
    assert parse_salary("€1.000.5", _NO_DEFAULTS) is None


def test_comma_thousands_with_dot_decimal() -> None:
    assert parse_salary("$1,234.56", _USD) == Money(
        min=Decimal("1234.56"), max=Decimal("1234.56"), currency="USD", period=None
    )


def test_plus_after_k_amount_gives_min_only() -> None:
    assert parse_salary("$100k+ per year", _USD) == Money(
        min=Decimal(100000), max=None, currency="USD", period=Period.YEAR
    )


def test_plus_after_amount_before_code_gives_min_only() -> None:
    assert parse_salary("$100,000+ USD", _NO_DEFAULTS) == Money(
        min=Decimal(100000), max=None, currency="USD", period=None
    )


def test_digits_inside_a_word_are_not_amounts() -> None:
    assert parse_salary("H1B visa, $120k", _USD_YEAR) == Money(
        min=Decimal(120000), max=Decimal(120000), currency="USD", period=Period.YEAR
    )


def test_quarter_label_is_not_an_amount() -> None:
    assert parse_salary("Q4 bonus, $80k", _USD) == Money(
        min=Decimal(80000), max=Decimal(80000), currency="USD", period=None
    )


def test_amount_right_after_a_code_is_read() -> None:
    assert parse_salary("USD50,000", _NO_DEFAULTS) == Money(
        min=Decimal(50000), max=Decimal(50000), currency="USD", period=None
    )


def test_retirement_plan_is_not_an_amount() -> None:
    assert parse_salary("$50k/yr + 401k", _USD) == Money(
        min=Decimal(50000), max=Decimal(50000), currency="USD", period=Period.YEAR
    )


def test_retirement_plan_with_parentheses_is_not_an_amount() -> None:
    assert parse_salary("$90,000 - $110,000 + 401(k) match", _USD) == Money(
        min=Decimal(90000), max=Decimal(110000), currency="USD", period=None
    )


def test_other_retirement_plans_are_not_amounts() -> None:
    assert parse_salary("$70k, 403(b) and 457b", _USD) == Money(
        min=Decimal(70000), max=Decimal(70000), currency="USD", period=None
    )


def test_currency_marked_401k_is_an_amount() -> None:
    assert parse_salary("$401k", _USD) == Money(
        min=Decimal(401000), max=Decimal(401000), currency="USD", period=None
    )
