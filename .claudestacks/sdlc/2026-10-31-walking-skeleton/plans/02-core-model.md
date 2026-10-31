---
status: done
created: 2026-10-31
built-on: aafd5e32e8cd
---

# Core Model Implementation Plan

**Goal:** `ijob-core` defines the validated domain types that every adapter and the store exchange.

**Architecture:** Two new modules in `ijob_core`: `errors` (the package's exception root) and
`model` (identifiers, the closed source-kind set, the draft adapters return and the records the
store returns). Every value is validated at construction, so downstream code trusts the type. No
I/O; standard library only.

**Tech Stack:** Python 3.14 dataclasses (`frozen=True, slots=True`), `enum.StrEnum`,
`typing.NewType`, pytest.

**Content authority:** spec §3, §7 (`ijob_core.errors`).

---

## Files that change

```
packages/ijob-core/src/ijob_core/errors.py                 — [create] CoreError, InvalidValueError
packages/ijob-core/src/ijob_core/model.py                  — [create] domain types and parse_source_key
packages/ijob-core/tests/ijob_core_tests/test_errors.py    — [create] tests for errors
packages/ijob-core/tests/ijob_core_tests/test_model.py     — [create] tests for model
```

## Order of work

### Task 1 — Add the core exception hierarchy

**Files:**
- Create `packages/ijob-core/src/ijob_core/errors.py` — `CoreError(Exception)`, the package root;
  `InvalidValueError(CoreError)`, raised by every failed validation. The exception owns its message
  (ruff `TRY003`): it takes the field name and the reason and builds the text itself.
- Test `packages/ijob-core/tests/ijob_core_tests/test_errors.py`.

**Change:** a validation failure anywhere in `ijob_core` raises one catchable type.

**Proof:** written first, seen failing, then passing —
- `test_invalid_value_error_is_core_error` — an `InvalidValueError` is an instance of `CoreError`.
- `test_invalid_value_error_message_names_field_and_reason` — `str()` of the error contains the field
  name and the reason it was built with.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_errors.py` passes
and the gate is green. Commit `feat(core): add the core exception hierarchy`.

### Task 2 — Add source identity types

**Files:**
- Create `packages/ijob-core/src/ijob_core/model.py` — `SourceKind`, `SourceKey`,
  `parse_source_key`, `SourceRef`, `ExternalId`, `PostingId`.
- Test `packages/ijob-core/tests/ijob_core_tests/test_model.py`.

**Change:**
- `SourceKind` — `StrEnum` with the one member `GREENHOUSE = "greenhouse"`.
- `SourceKey` — `NewType` over `str`, built only through `parse_source_key(text)`, which accepts text
  matching `[A-Za-z0-9._-]+` other than `.` and `..`, and raises `InvalidValueError` otherwise.
- `SourceRef` — frozen slotted dataclass `(kind, key)`; `str(ref)` is `"<kind>:<key>"`, e.g.
  `"greenhouse:stripe"`, the form the store writes to `sightings.source` (plan 03).
- `ExternalId` (`str`) and `PostingId` (`int`) — `NewType`s.

**Proof:** written first, seen failing, then passing —
- `test_parse_source_key_accepts_token_with_dot_dash_underscore` — `stripe`, `mistral.ai`,
  `doordash-usa`, `a_b` parse and come back unchanged.
- `test_parse_source_key_rejects_unsafe_text` (parametrised) — `""`, `"a b"`, `"a/b"`, `"x?y"`,
  `"x#y"`, `"x%2F"`, `"."` and `".."` each raise `InvalidValueError`.
- `test_source_ref_str_is_kind_colon_key` — `str(SourceRef(SourceKind.GREENHOUSE, <key "stripe">))`
  is `"greenhouse:stripe"`.
- `test_source_ref_is_frozen_and_hashable` — two equal refs hash equal and can key a `dict`;
  assigning a field fails.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_model.py` passes
and the gate is green. Commit `feat(core): add source identity types`.

### Task 3 — Add the sighting draft and stored records

**Files:**
- Modify `packages/ijob-core/src/ijob_core/model.py` — add `SightingDraft`, `Posting`, `Sighting`
  (the module created in Task 2).
- Test `packages/ijob-core/tests/ijob_core_tests/test_model.py` (the file created in Task 2).

**Change:**
- `SightingDraft` — frozen slotted dataclass with `source: SourceRef`, `external_id: ExternalId`,
  `title: str`, `company: str`, `location: str | None`, `url: str`, `posted_at: datetime | None`,
  `raw: str`. `__post_init__` raises `InvalidValueError` when `external_id`, `title`, `company` or
  `url` is empty, when `url` does not start with `http://` or `https://`, or when `posted_at` is set
  but has no timezone.
- `Posting` — frozen slotted dataclass with `id: PostingId`, `title`, `company`, `location`, `url`,
  `posted_at`, `first_seen: datetime`, `last_seen: datetime`; built only by the store.
- `Sighting` — frozen slotted dataclass with `posting_id: PostingId`, `source: SourceRef`,
  `external_id: ExternalId`, `first_seen`, `last_seen`, `raw`; built only by the store.
- The module docstring names the display fields of a Posting: `title`, `company`, `location`, `url`,
  `posted_at`.

**Proof:** written first, seen failing, then passing —
- `test_sighting_draft_accepts_valid_values` — a draft with every field set, `posted_at` aware, builds
  and keeps its values; another with `location=None` and `posted_at=None` also builds.
- `test_sighting_draft_rejects_empty_required_text` (parametrised over `external_id`, `title`,
  `company`, `url`) — an empty value raises `InvalidValueError`.
- `test_sighting_draft_rejects_non_http_url` — `ftp://x` and `job-boards.greenhouse.io/x` raise
  `InvalidValueError`.
- `test_sighting_draft_rejects_naive_posted_at` — a `datetime` without `tzinfo` raises
  `InvalidValueError`.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/` passes and the gate
is green. Commit `feat(core): add sighting draft and stored records`.

---

## Risks

- **Most risky step: the `SourceKey` character set.** It is shared by every later adapter (spec §3),
  so a too-loose set lets a key change the URL it is placed in. The parametrised rejection test is
  the guard; widening the set later is an extension, narrowing it is a breaking change.
- `ijob_core/__init__.py` is not changed: callers import `ijob_core.model` and `ijob_core.errors`
  directly, and the export-only `__init__` keeps its docstring alone.
- Not done: no cross-source key fields (spec §1 premise 3), no `Sighting` construction outside the
  store.

## Proof

- `uv run --locked pytest packages/ijob-core` passes; the five-command gate is green.
- `grep -rEn '^\s*(from|import)\s+(httpx|requests|aiohttp|typer|dspy|litellm|anthropic|claude_agent_sdk|urllib\.request|http\.client|socket)\b' packages/ijob-core/src`
  prints nothing (CLAUDE.md boundary: no LLM and no network library in `ijob-core`).

## Review findings

- docstring-discipline — module docstring claimed every value is validated at construction; false for `Posting`, `Sighting`, `SourceRef` — `packages/ijob-core/src/ijob_core/model.py:4` (BLOCKING, fixed and verified: `uv run --locked mypy --strict .` → `Success: no issues found in 16 source files`; `uv run --locked pytest -q` → `42 passed in 0.14s`)
- modularity — `_HTTP_PREFIXES` declared between classes — `model.py:85` (fixed: moved to the module constants)
- modularity — `_STRIPE`/`_AWARE` declared mid-file — `test_model.py:45` (fixed: moved under the imports)
- test-mandate — empty-field test checked only the exception type — `test_model.py:80` (fixed: asserts `exc_info.value.field == field`)
- test-mandate — no test for a tzinfo whose `utcoffset` is `None` — `model.py:132` (fixed: `test_sighting_draft_rejects_posted_at_whose_zone_has_no_offset`)
- strong-types — whitespace-only `external_id`/`title`/`company` accepted; meets spec §3 "non-empty" literally — `model.py:127` (risk, not changed; open for the author)
- errors — `InvalidValueError` does not round-trip through pickle/copy — `errors.py:23` (nit, not fixed)
- docstring-discipline — empty text reports "not made of letters…" though Raises lists "empty" separately — `model.py:60` (nit, not fixed)
- strong-types — URL check is prefix-only (`https://` with no host passes; `HTTPS://x` fails), as the spec states — `model.py:130` (nit, not fixed)
- docstring-discipline — "Built by the store." true only once the store lands (plan 03) — `model.py:140,167` (nit, not fixed)
- test-mandate — `.field`/`.reason` attributes and frozenness of draft/posting/sighting untested — `test_errors.py:8`, `test_model.py:35` (nit, not fixed)
- spec additions (harmless) — `InvalidValueError.field/.reason`; extra test `test_posting_and_sighting_hold_their_fields`.

## Probe results

- No probe beyond the named tests: every asserted fact is created by this plan. Boundary grep over `packages/ijob-core/src` → no output (exit 1).
- Gate before review — `ruff format --check` → `49 files already formatted`; `ruff check --no-fix` → `All checks passed!`; `mypy --strict .` → `no issues found in 16 source files`; `pytest -q` → `41 passed in 0.06s`.

## Deviations

- 2026-10-31 — `InvalidValueError` field names are snake_case (`invalid source_key: …`) so the exception owns its message under ruff TRY003; the plan names no wording.
- 2026-10-31 — the review fix round ran inline on the main thread rather than in a fresh coder spawn (small, mechanical edits).
- 2026-10-31 — per-task commits not made: commits are the user's act (CLAUDE.md).
