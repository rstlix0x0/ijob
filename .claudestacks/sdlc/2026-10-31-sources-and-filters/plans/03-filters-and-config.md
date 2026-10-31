---
status: approved
created: 2026-10-31
depends-on: [01]
built-on: aeb01064881c
---

# Filters and Configuration Implementation Plan

**Goal:** A `FilterSpec` built from the user's configuration decides which postings a search prints.

**Architecture:**
- The filters are pure functions in `ijob_core.filters`, over the `Posting` fields from plan 01.
- `ijob_app.config` reads `config.toml` with stdlib `tomllib` and builds a `FilterSpec`, falling back
  to a shipped `default_config.toml`.
- It also owns the parsers for the per-run flag values (`--since`, `--min-salary`, `--remote`,
  `--country`). Plan 10 wires them into the CLI.

**Tech Stack:** stdlib `re`, `tomllib`, `importlib.resources`, `datetime`, `decimal`, pytest.

**Content authority:** spec §1 premise 3, §8.

---

## Files that change

```
packages/ijob-core/src/ijob_core/filters.py                 — [create] FilterSpec, keep
packages/ijob-app/src/ijob_app/config.py                    — [create] config path, load_filters, flag-value parsers
packages/ijob-app/src/ijob_app/data/default_config.toml     — [create] the shipped default filters
packages/ijob-core/tests/ijob_core_tests/test_filters.py    — [create] tests for filters
packages/ijob-app/tests/ijob_app_tests/test_config.py       — [create] tests for config
```

## Order of work

### Task 1 — Decide which postings pass the filters

**Files:**
- Create `packages/ijob-core/src/ijob_core/filters.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_filters.py`

**Change:**
- `FilterSpec`, a frozen slotted dataclass whose fields all default to "no filter":
  - `include: tuple[re.Pattern[str], ...]`, `exclude: tuple[re.Pattern[str], ...]`;
  - `countries: frozenset[CountryCode]`, `remote: frozenset[RemoteScope]`;
  - `since: timedelta | None`, `companies: tuple[str, ...]`;
  - `min_salary: Money | None`, `require_salary: bool`.
- `keep(posting, spec, *, now) -> bool` applies the spec §8 table exactly. A posting is dropped only
  when the data decides:
  - **role:** the title matches an `exclude` pattern; or `include` is non-empty, the title matches
    no `include` pattern, `role_hint` is present, and the hint matches none either. An empty
    `include` never drops. Patterns use `re.search`; Task 2 compiles them with `re.IGNORECASE`.
  - **country:** `countries` is non-empty, `country` is known and not in it, and `remote` is `NONE`
    or `COUNTRY`.
  - **remote:** `remote` is non-empty, and the posting's scope is known and not in it.
  - **since:** `posted_at` is known and `< now - since`.
  - **company:** `companies` is non-empty and no entry is a case-insensitive substring of `company`.
  - **salary:**
    - with `min_salary` set, a salary is comparable when it has the same currency and its period
      converts to the threshold's (equal, or `MONTH`↔`YEAR` with ×12); `HOUR` and `None` periods
      never convert;
    - the posting is dropped when it has at least one comparable salary and none reaches the
      threshold, judged on `max`, else `min`;
    - with `require_salary`, a posting with no salaries is dropped.

**Proof:** written first, seen failing, then passing; each rule gets a drop case and a pass-when-unknown
case —
- `test_role_exclude_drops`: title `Account Executive - Commercial` with exclude
  `\baccount executive\b` → dropped.
- `test_role_no_include_match_without_hint_passes`: title `Platform Lead`, `role_hint=None` → kept.
- `test_role_hint_without_match_drops`: title `Platform Lead`, hint `Finance`, include `\bengineer` →
  dropped.
- `test_empty_include_never_drops`: the same posting with `include=()` → kept.
- `test_country_drops_known_onsite`: `country=DE`, `remote=NONE`, `countries={SG}` → dropped.
- `test_country_passes_unknown_or_wide` (parametrised): `country=None`; `country=DE` with remote
  `None`, `REGION` or `WORLDWIDE` → kept.
- `test_remote_filter`: scope `NONE` with `remote={WORLDWIDE}` → dropped; scope `None` → kept.
- `test_since`: `posted_at` 10 days old with `since=7 days` → dropped; `posted_at=None` → kept.
- `test_company_substring`: companies `("stripe",)` keeps `Stripe, Inc.` and drops `Ramp`.
- `test_salary_comparable_below_drops`: threshold `6000 SGD MONTH`, salary `max 5000 SGD MONTH` →
  dropped.
- `test_salary_month_year_convert`: threshold `72000 SGD YEAR`, salary `max 6500 SGD MONTH` → kept.
- `test_salary_incomparable_passes` (parametrised): other currency; `HOUR`; `period=None` → kept.
- `test_require_salary`: no salaries with `require_salary` → dropped; without it → kept.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_filters.py`
passes and the gate is green. Commit `feat(core): add deterministic posting filters`.

### Task 2 — Load filters from config and parse flag values

**Files:**
- Create `packages/ijob-app/src/ijob_app/config.py`
- Create `packages/ijob-app/src/ijob_app/data/default_config.toml` with:
  - `[filters.role] include = ['\bengineer', '\bdeveloper', 'エンジニア', '開発']`;
  - `exclude = ['\bsales\b', '\brecruit', '\bmarketing\b', '\baccount executive\b']`;
  - no other filter set.

  Use TOML literal strings (single quotes) so the backslashes stay literal.
- Test `packages/ijob-app/tests/ijob_app_tests/test_config.py`

**Change:**
- `default_config_path(environ) -> Path`: `$XDG_CONFIG_HOME/ijob/config.toml`, else
  `~/.config/ijob/config.toml` when `XDG_CONFIG_HOME` is unset or empty.
- `load_filters(path: Path, *, explicit: bool) -> FilterSpec`:
  - always reads the packaged default with `tomllib`;
  - layers the user file over it key by key when the file exists (spec §8). A key the file does not
    set keeps the default's value;
  - when `explicit` (a `--config` path was given) and the file does not exist, raises
    `InvalidValueError` naming the path;
  - reads the tables `[filters.role] include, exclude` and `[filters] countries, remote, since,
    companies, min_salary = {amount, currency, period}, require_salary`;
  - compiles patterns with `re.IGNORECASE`;
  - countries go through `parse_country`;
  - raises `InvalidValueError` naming the key (`filters.since`, …) on a bad value or a bad pattern.
- Flag-value parsers, each raising `InvalidValueError`:
  - `parse_since("7d"|"24h") -> timedelta`;
  - `parse_min_salary("6000:SGD:month") -> Money`;
  - `parse_remote("worldwide") -> RemoteScope`;
  - `parse_country_flag("SG") -> CountryCode`, which rejects anything `parse_country` cannot read.

**Proof:** written first, seen failing, then passing —
- `test_default_config_when_file_missing`: a missing path yields the four include and four exclude
  patterns.
- `test_default_patterns` (parametrised, the spec §8 probe table): the default spec keeps or drops
  each listed title by role alone exactly as the table's include/exclude columns say. `Sales
  Engineer` is dropped; the `Salesforce Developers` title is kept.
- `test_full_config_round_trip`: a `tmp_path` config setting every key builds the matching
  `FilterSpec`.
- `test_partial_config_keeps_default_roles`: a file setting only `countries = ["SG"]` keeps the four
  default include and four exclude patterns.
- `test_missing_explicit_config_raises`: `explicit=True` with a missing path raises, naming the path.
  `explicit=False` falls back to the default.
- `test_bad_config_names_key` (parametrised): `since = "7x"`, `remote = ["moon"]`, `include = ["("]`
  each raise with the key name in the message.
- `test_flag_parsers` (parametrised): valid and invalid values for each parser.
- `test_config_path_xdg`: `XDG_CONFIG_HOME` set, and unset or empty.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_config.py` passes
and the gate is green. Commit `feat(app): load search filters from config`.

---

## Risks

- **Most risky: the role rule's double negative.** "Drop when no include match and the hint also
  matches none" must not become "drop when no include match". `Platform Lead` without a hint has to
  survive. The pass-when-unknown tests guard it.
- The TOML file holds regexes; a basic-string escape (`"\b"`) would become a backspace character, so
  the default file uses literal strings and a test asserts the compiled patterns.
- Not done here: merging flags over config inside the CLI (plan 10).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_filters.py
  packages/ijob-app/tests/ijob_app_tests/test_config.py` passes.
