---
status: approved
created: 2026-11-02
built-on: 6ee2c2bd4580
---

# Salary Text Implementation Plan

**Goal:** Read one free-text salary string into a `Money` value under the spec's §5 rules.

**Architecture:**
- A new focused module `ijob_sources/salary_text.py` holds the §5 rules and nothing else. It
  depends only on `ijob_core.money`.
- `SalaryDefaults` (frozen, slotted, keyword-only dataclass: `currency: str | None`,
  `period: Period | None`) carries a recipe's `salary_defaults` (spec §4.2). Plan 03's recipe type
  builds it.
- `parse_salary(text: str, defaults: SalaryDefaults) -> Money | None` never raises for bad text.
  No salary means `None` (spec §5 rule 5: "No salary never rejects the job").
- `KNOWN_CURRENCIES: Final[frozenset[str]]` holds the §5 rule 1 code list. Plan 03 validates
  `salary_defaults.currency` against it.

**Tech Stack:** Python 3.14, stdlib `re` and `decimal`, `ijob_core.money`, pytest.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/salary_text.py                 — [create] SalaryDefaults, KNOWN_CURRENCIES, parse_salary
packages/ijob-sources/tests/ijob_sources_tests/test_salary_text.py    — [create]
```

## Order of work

### Task 1 — Parse salary text

**Files:**
- Create `ijob_sources/salary_text.py`.
- Test `test_salary_text.py`.

**Change:**
- Rule 1, currency:
  - of the whole uppercase words in the text that are in `KNOWN_CURRENCIES`, the one earliest in the
    text wins;
  - otherwise `€`→EUR, `£`→GBP, `₹`→INR;
  - otherwise `$` or `¥` takes `defaults.currency`;
  - otherwise the result is `None`.
- Rule 2, amounts:
  - digit runs, where `,`, `.` or a space followed by exactly three digits is a thousands
    separator;
  - a single `.` followed by other than three digits is the decimal point;
  - any other separator pattern gives `None`;
  - a following `k`/`K` multiplies by 1,000.
- Rule 3, bounds:
  - two amounts give `min` and `max`;
  - one amount with `up to` gives `max` only;
  - one amount with `or more`, a trailing `+` or a leading `from` gives `min` only;
  - one amount otherwise gives `min = max`;
  - three or more amounts give `None`.
- Rule 4, period:
  - hour: `/hr`, `/hour`, `hourly`, `per hour`, `an hour`;
  - month: `/mo`, `/month`, `monthly`, `per month`;
  - year: `/yr`, `/year`, `annually`, `per annum`, `per year`, `a year`;
  - otherwise `defaults.period`, otherwise no period.
- Rule 5: a `Money` that the constructor refuses, such as `min > max`, gives `None`. Catch
  `InvalidValueError` only.
- The matching is case-insensitive for the bound and period words, and case-sensitive for the
  currency codes (they are uppercase words).

**Proof:** written first, seen failing, then passing. Each test is one row of the spec's §5 table,
with its defaults and its exact expected result:
- `test_range_with_code_and_default_period`: `$50,000 - $74,999 USD` with (USD, year) gives
  50000–74999 USD year.
- `test_or_more_gives_min_only`: `$100,000 or more USD` with no defaults gives min 100000, USD and
  no period.
- `test_decimal_hourly_range`: `$15.00–$17.00 per hour` with (USD, –) gives 15.00–17.00 USD hour.
- `test_en_dash_hourly_range`: `$16–$24 per hour` with (USD, –) gives 16–24 USD hour.
- `test_trailing_plus_hourly`: `$20/hr+` with (USD, –) gives min 20, USD hour.
- `test_dollar_without_default_is_none`: `$20/hr+` with no defaults gives `None`.
- `test_euro_dot_thousands`: `€60.000 - €80.000` with no defaults gives 60000–80000 EUR.
- `test_k_suffix_single_amount`: `$120k` with (USD, year) gives 120000–120000 USD year.
- `test_up_to_pounds_a_year`: `up to £45,000 a year` with no defaults gives max 45000 GBP year.
- `test_words_only_is_none`: `Competitive` with (USD, year) gives `None`.
- `test_min_above_max_is_none`: `$80,000 - $60,000` with (USD, –) gives `None`.
- `test_ambiguous_separator_is_none`: `$1,5k` with (USD, –) gives `None`.
- `test_three_amounts_is_none`: `$90k - $110k - $130k` with (USD, –) gives `None`.
- `test_unknown_uppercase_words_are_not_codes`: `THE API pays 50000` with no defaults gives `None`.
- `test_empty_and_whitespace_are_none`: `""` and `"   "` give `None`.
- `test_earliest_code_in_text_wins`: `CAD 60,000 or USD equivalent` gives 60000–60000 CAD, with no
  defaults.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_salary_text.py`
passes and the CLAUDE.md gate is green. Commit `feat(sources): read salary text into money`.

## Risks

- **Riskiest step:** rule 2's separator grammar. The spec's table only fixes the listed rows. A string
  outside them, such as `1 000 000`, follows the rule text: every separator is followed by three
  digits, so it is thousands.
- **Not done:** wiring into recipe fields. That is plan 03.

## Proof

- `uv run --locked pytest packages/ijob-sources` passes.
- The gate exits 0.
