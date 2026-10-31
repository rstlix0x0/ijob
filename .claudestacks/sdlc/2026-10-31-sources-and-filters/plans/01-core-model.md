---
status: done
created: 2026-10-31
built-on: aeb01064881c
---

# Core Model Implementation Plan

**Goal:** The domain model represents salaries, countries, remote scope and every C2 source kind.

**Architecture:** `ijob-core` stays stdlib-only. Salaries get their own module (`ijob_core.money`) and
countries theirs (`ijob_core.countries`, backed by a bundled copy of tzdata's public-domain
`iso3166.tab`). `ijob_core.model` grows only by extension: new `SourceKind` members,
`parse_source_ref`, `RemoteScope`, and new fields with defaults on `SightingDraft` and `Posting`.
Every C1 caller keeps working unchanged.

**Tech Stack:** stdlib `decimal`, `enum.StrEnum`, `dataclasses`, `importlib.resources`, pytest.

**Content authority:** spec §1 premise 1, §5.1, §5.2, §10.

---

## Files that change

```
packages/ijob-core/src/ijob_core/money.py                     — [create] Period, Money
packages/ijob-core/src/ijob_core/countries.py                 — [create] CountryCode, parse_country
packages/ijob-core/src/ijob_core/data/iso3166.tab             — [create] verbatim copy of /usr/share/zoneinfo/iso3166.tab
packages/ijob-core/src/ijob_core/model.py                     — [modify] SourceKind members, parse_source_ref, RemoteScope, new draft and posting fields
packages/ijob-core/tests/ijob_core_tests/test_money.py        — [create] tests for money
packages/ijob-core/tests/ijob_core_tests/test_countries.py    — [create] tests for countries
packages/ijob-core/tests/ijob_core_tests/test_model.py        — [modify] tests for the model additions
```

The `data/` directory holds no Python, so it needs no `__init__.py`. `importlib.resources.files(
"ijob_core") / "data" / "iso3166.tab"` reads it. Spec §5.2 records the probe showing that `uv_build`
ships a non-Python file under `src/<module>/`.

## Order of work

### Task 1 — Add salaries as Money

**Files:**
- Create `packages/ijob-core/src/ijob_core/money.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_money.py`

**Change:**
- `Period` is a `StrEnum` with `HOUR = "hour"`, `MONTH = "month"`, `YEAR = "year"`.
- `Money` is a frozen slotted dataclass: `min: Decimal | None`, `max: Decimal | None`, `currency:
  str`, `period: Period | None`. `__post_init__` raises `InvalidValueError` when:
  - both bounds are `None`;
  - a bound is negative;
  - `min > max`;
  - `currency` does not match `[A-Z]{3}`.
- `period=None` means "the source states no period" (spec §5.1).
- Module docstring and Google-style docstrings throughout (python guideline, docstring discipline).

**Proof:** written first, seen failing, then passing —
- `test_money_accepts_range_and_single_bound`: `Money(Decimal(1), Decimal(2), "USD", Period.YEAR)`
  and `Money(None, Decimal(5), "EUR", None)` both build.
- `test_money_rejects_no_bound`: both bounds `None` raises `InvalidValueError`.
- `test_money_rejects_negative_bound`: `min=-1` raises.
- `test_money_rejects_min_above_max`: `min=3, max=2` raises.
- `test_money_rejects_bad_currency` (parametrised): `"usd"`, `"US"`, `"USDX"`, `""` each raise.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_money.py` passes
and the gate is green. Commit `feat(core): add the money type`.

### Task 2 — Read countries as ISO 3166 alpha-2 codes

**Files:**
- Create `packages/ijob-core/src/ijob_core/data/iso3166.tab`: copy `/usr/share/zoneinfo/iso3166.tab`
  byte for byte, header comments included (the header states its public-domain status).
- Create `packages/ijob-core/src/ijob_core/countries.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_countries.py`

**Change:**
- `CountryCode = NewType("CountryCode", str)`.
- `parse_country(text: str) -> CountryCode | None` follows spec §5.2:
  - It tries the whole text, then the part after its last `", "`, stripping each part of
    surrounding whitespace.
  - A part matches, case-insensitively, when it is an alpha-2 code in the table, a table name, or an
    alias from the spec §5.2 table: `USA` → `US`, `UK` → `GB`, `United Kingdom` → `GB`,
    `South Korea` → `KR`, `Czechia` → `CZ`.
  - It returns `None` otherwise, and never raises for unknown text.
- The table is loaded once, on first use, from the package-data file; comment lines are skipped and
  columns are tab-separated.

**Proof:** written first, seen failing, then passing —
- `test_table_has_249_codes`: the loaded table has 249 entries (spec §5.2).
- `test_codes_and_names` (parametrised): `"US"`, `"us"`, `"United States"`, `"Japan"`,
  `"Britain (UK)"` map to `US`, `US`, `US`, `JP`, `GB`.
- `test_aliases` (parametrised): `"USA"` → `US`, `"UK"` → `GB`, `"United Kingdom"` → `GB`,
  `"South Korea"` → `KR`, `"Czechia"` → `CZ`.
- `test_last_comma_part`: `"San Francisco, United States"` → `US`; `"Toronto, Ontario, Canada"` →
  `CA`; `"London, United Kingdom"` → `GB`; Jobicy's `"Canada,  USA"` (two spaces) → `US`.
- `test_unknown_is_none` (parametrised): `"Remote"`, `"Europe"`, `""`, `"Atlantis"` → `None`.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_countries.py`
passes and the gate is green. Commit `feat(core): read country codes and names`.

### Task 3 — Extend the model for C2 sources and fields

**Files:**
- Modify `packages/ijob-core/src/ijob_core/model.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_model.py` (existing C1 tests stay as they are)

**Change:**
- New `SourceKind` members:
  - lower-case values: `GEM, ASHBY, LEVER, WORKABLE, HIMALAYAS, JOBICY, REMOTEOK, ARBEITNOW,
    MYCAREERSFUTURE, KALIBRR, RSS, YC, LETSGETRUSTY, JAPANDEV, HN, JOBPOSTING, FEED`;
  - `FOURDAYWEEK = "4dayweek"`.
- `parse_source_ref(text: str) -> SourceRef` splits `<kind>:<key>` at the first `:`. The kind must be
  a `SourceKind` value and the key goes through `parse_source_key`; a missing `:`, an unknown kind or
  a bad key raises `InvalidValueError`. It is the inverse of `str(SourceRef)`.
- `RemoteScope` is a `StrEnum`: `NONE = "none"`, `COUNTRY = "country"`, `REGION = "region"`,
  `WORLDWIDE = "worldwide"`.
- `SightingDraft` gains these fields, after the existing ones and with defaults:
  - `salaries: tuple[Money, ...] = ()`;
  - `country: CountryCode | None = None`;
  - `remote: RemoteScope | None = None`;
  - `role_hint: str | None = None`;
  - `low_confidence: bool = False`.

  Its existing validation is unchanged.
- `Posting` gains the same five fields with the same defaults, after the existing ones, so the C1
  store keeps building it until plan 02 reads them.
- Update the module docstring to name the new types.

**Proof:** written first, seen failing, then passing —
- `test_source_ref_round_trip` (parametrised over every `SourceKind`):
  `parse_source_ref(str(SourceRef(kind, parse_source_key("x"))))` equals the ref.
- `test_parse_source_ref_fourdayweek`: `"4dayweek:engineering"` → kind `FOURDAYWEEK`, key
  `engineering`.
- `test_parse_source_ref_rejects` (parametrised): `"greenhouse"`, `"nosuch:x"`, `"greenhouse:a/b"`,
  `":x"` each raise `InvalidValueError`.
- `test_draft_new_fields_default`: a draft built with only the C1 arguments has `salaries == ()`,
  `country is None`, `remote is None`, `role_hint is None`, and `low_confidence` false.
- `test_draft_carries_new_fields`: a draft built with every new field keeps their values.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests` passes (C1 tests
included) and the gate is green. Commit `feat(core): extend the model for every C2 source`.

---

## Risks

- **Most risky: Task 3's dataclass field order.** New fields must come after every existing field and
  carry defaults, or C1 call sites that pass arguments by position break. The C1 test suite is the
  guard.
- `iso3166.tab` must be copied verbatim. A hand-edited table would drift from tzdata and lose the
  public-domain header that justifies shipping it.
- Not done here: persisting the new fields (plan 02), the filters that read them (plan 03).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-core` passes, the C1 tests included.
- `grep -rnE "^(import|from) (httpx|anthropic|openai|dspy)" packages/ijob-core/src` prints nothing:
  `ijob-core` stays free of network and LLM imports.

## Review findings

- docstring-discipline — `low_confidence` docstrings on SightingDraft and Posting stated the wrong meaning — `model.py:176`, `model.py:237` — fixed and verified: `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_model.py packages/ijob-core/tests/ijob_core_tests/test_countries.py -q` → `72 passed in 0.03s`; both docstrings now read "Whether the fields were parsed heuristically (as from free-text comments) and may be mis-assigned."
- correctness (spec defect, deferred) — spec §5.2 accepts an alpha-2 code as the last comma part, so US "City, ST" locations resolve to other countries ("San Francisco, CA"→CA, "Atlanta, GA"→GA, "Wilmington, DE"→DE) — `countries.py:80` / spec §5.2 — not changed: the code follows the approved spec; to be amended by `design` before plan 06 calls `parse_country` on Greenhouse locations.
- docstring-discipline — `country_codes` summary said "List" for a frozenset — `countries.py:48` — fixed (summary "Return the set of alpha-2 codes in the table.").
- test-mandate — `test_parse_source_ref_splits_at_the_first_colon` claimed more than it tested — `test_model.py:65` — fixed (renamed `test_parse_source_ref_rejects_a_second_colon`).
- strict-quality — two `# type: ignore[arg-type]` in test_model.py — fixed (typed construction plus `dataclasses.replace`).
- strong-types (nit, no action) — Money fields `min`/`max` shadow builtins; currency is a validated `str` — `money.py:38` — kept: spec §5.1 names the fields.
- test-mandate (nit, no action) — nothing in the gate catches `iso3166.tab` missing from the wheel; verified by hand with `uv build --package ijob-core --wheel` (ships `ijob_core/data/iso3166.tab`, 4841 bytes).
- spec addition — public `country_codes()` added for the 249-row test (see Deviations).
- spec addition — Money rejects NaN and ±Infinity bounds (see Deviations).

## Probe results

- iso3166.tab row count and licence — `grep -vc '^#' /usr/share/zoneinfo/iso3166.tab` → `249`; line 3 `# This file is in the public domain, so clarified as of`; spellings `CZ Czech Republic`, `GB Britain (UK)`, `KR Korea (South)`, `US United States` (aliases needed, as planned).
- model symbols Task 3 assumes — `grep -nE "^class |^def " model.py errors.py` → `SourceKind` L25, `parse_source_key` L36, `SourceRef` L70, `SightingDraft` L88, `Posting` L136, `InvalidValueError` errors.py L8 (as planned).
- plan proof — `grep -rnE "^(import|from) (httpx|anthropic|openai|dspy)" packages/ijob-core/src` → no output (exit 1).
- plan proof — `uv run --locked pytest packages/ijob-core -q` → `114 passed` (before the fix round).

## Deviations

- 2026-10-31 — `Money.__post_init__` also rejects non-finite bounds (NaN, ±Infinity) with `InvalidValueError`. `Decimal("NaN") < 0` raises `decimal.InvalidOperation`, which would break spec §10's "raises InvalidValueError". Plan-level addition; spec §5.1 should record it at the next design pass.
- 2026-10-31 — `countries.py` exposes public `country_codes() -> frozenset[CountryCode]` so `test_table_has_249_codes` does not read a private member. Plan-level addition.
- 2026-10-31 — extra tests beyond the named ones (Posting defaults, RemoteScope values, zero/equal bounds, frozen/hashable Money).
