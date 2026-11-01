---
status: done
created: 2026-11-01
built-on: cae3bcdf3b23
---

# Assessment Store Implementation Plan

**Goal:** Store the CV profile, the instructions, the judge verdicts and the scores through store
migration 4.

**Architecture:**
- `ijob_core.model` (spec §5) gains four frozen, slotted, validated dataclasses: `ResumeProfile`,
  `Instructions`, `Verdict` and `Score` (spec §5).
- `ijob_core.store` gains migration 4 and typed read/write methods for each.
- Only the standard library is imported. No DSPy, pydantic or OTel (spec §5).

**Tech Stack:** Python 3.14, sqlite3, pytest.

---

## Files that change

```
packages/ijob-core/src/ijob_core/model.py                       — [modify] add ResumeProfile, Instructions, Verdict, Score, Seniority
packages/ijob-core/src/ijob_core/store.py                       — [modify] migration 4; profile/instructions/verdict/score methods
packages/ijob-core/tests/ijob_core_tests/test_model.py          — [modify] assessment type validation tests
packages/ijob-core/tests/ijob_core_tests/test_store.py          — [modify] migration 4 and method tests
```

## Order of work

### Task 1 — Assessment types

**Files:** Modify `packages/ijob-core/src/ijob_core/model.py`; Test `test_model.py`.

**Change:** add these types.

`Seniority` is a StrEnum: `junior`, `mid`, `senior`, `staff`, `principal`, `unknown`.

`ResumeProfile` fields:

| Field | Limit |
|---|---|
| `summary` | ≤ 600 characters |
| `years_experience` | int 0–60, or None |
| `seniority` | `Seniority` |
| `roles`, `spoken_languages`, `locations`, `work_authorization` | tuple[str, ...] of ≤ 20 items |
| `skills` | tuple[str, ...] of ≤ 100 items |

Every list item is non-blank and ≤ 100 characters.

`ResumeProfile` methods:
- `to_json() -> str`: canonical JSON, keys sorted, no whitespace variance;
- `from_json(text)`;
- `sha256()`: hex digest of `to_json()`.

`Instructions` fields:
- `text`: non-blank, ≤ 4,000 characters;
- `filters_json: str`: the instruction layer only, not the merged spec. It is a JSON object in the
  TOML `[filters]` shape, with the keys of spec §4.3 and role words already escaped. It is produced
  with sorted keys, and must parse as a JSON object;
- `rules: tuple[str, ...]`: 0–10 rules, each 1–200 characters;
- `model: str`, `task_version: str`, `set_at: datetime` (UTC).

`Instructions` derives `text_sha256` and `rules_sha256`. The rules hash is taken over the rules
encoded as a JSON array, so `["a\nb"]` and `["a", "b"]` hash differently.

`Verdict` fields:
- `keep: bool`;
- `reason`: 1–200 characters;
- `rule: int | None`. It is None when `keep` is true, and ≥ 1 when `keep` is false. The upper bound
  depends on the stored rules, so it is checked in ijob-ai (plan 06).

`Score` fields:
- `score`: int 0–100;
- `reasons`: 1–5 items of 1–200 characters each.

Every type validates in `__post_init__` and raises `InvalidValueError` (`ijob_core/errors.py:8`)
naming the field.

**Proof:** written first, seen failing, then passing:
- `test_resume_profile_accepts_bounds`: 600-character summary, years 0 and 60, 20 roles and
  100 skills build.
- `test_resume_profile_rejects_over_bounds`: 601 characters, years 61, 21 roles, a blank item, and a
  101-character item each raise `InvalidValueError` naming the field.
- `test_resume_profile_json_roundtrip_and_hash_stable`: `from_json(p.to_json()) == p`, and two equal
  profiles built in different list-insertion order of keys give the same `sha256()`.
- `test_instructions_hashes`: `rules_sha256` changes when one rule changes and not when `set_at`
  changes.
- `test_instructions_rejects_blank_text_and_too_many_rules`: whitespace text, 4,001 characters,
  11 rules, and a 201-character rule each raise.
- `test_verdict_consistency`: `keep=True, rule=1` and `keep=False, rule=None` and `rule=0` each raise;
  `keep=False, rule=2` builds.
- `test_score_bounds`: score −1 and 101, zero reasons and six reasons each raise.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_model.py` passes
and the CLAUDE.md gate is green. Commit `feat(core): add assessment types`.

### Task 2 — Migration 4 and store methods

**Files:** Modify `packages/ijob-core/src/ijob_core/store.py`; Test `test_store.py`.

**Change:** append migration 4 to `_MIGRATIONS` (`store.py:47`). It adds the four tables of spec §5:
- `cv_profile` and `instructions` are single-row (CHECK id=1);
- `verdicts` and `scores` have the five-column primary keys and `posting_id` foreign keys to
  `postings`.

New `Store` methods:
- `set_cv_profile(profile, *, cv_sha256, model, task_version, made_at)`;
- `cv_profile() -> StoredProfile | None`. `StoredProfile` is a dataclass of the profile, `cv_sha256`,
  `model`, `task_version` and `made_at`;
- `set_instructions(instructions)`, `instructions() -> Instructions | None`, `clear_instructions()`;
- `verdict(posting_id, input_sha256, rules_sha256, model, task_version) -> Verdict | None`;
- `put_verdict(posting_id, key fields…, verdict, *, made_at)`, an upsert;
- `score(...)` and `put_score(...)`, with the same shape over `profile_sha256`.

Each write runs in one transaction.

**Proof:** written first, seen failing, then passing:
- `test_migration_4_creates_tables`: a v3 database opened by `Store.open` reaches `user_version` 4
  with the four tables, and existing rows survive.
- `test_cv_profile_roundtrip_and_replace`: a second `set_cv_profile` replaces the first.
- `test_instructions_roundtrip_and_clear`: clearing with none stored is a no-op.
- `test_verdict_cache_hit_and_miss`: the same five key fields return the verdict, and a change to any
  one of them returns None.
- `test_score_cache_hit_and_miss`: as above for scores.
- `test_put_verdict_unknown_posting_fails`: a foreign-key violation raises `sqlite3.IntegrityError`.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_store.py` passes and
the gate is green. Commit `feat(core): add store migration 4 for assessments`.

## Risks

- **Riskiest step:** migration 4 on existing user databases. Task 2 proves a v3 → v4 upgrade keeps
  rows.
- **`Verdict.rule` upper bound.** It cannot be checked here, because it needs the stored rules.
  Plan 06 checks it.
- **Not done:** cache eviction. Rows grow with postings, and pruning is out of scope.

## Proof

- `uv run --locked pytest packages/ijob-core` passes.
- The five gate commands in CLAUDE.md exit 0.

## Review findings

Reviewer verdict: blocking none. DoD re-run green: ruff format, ruff check, mypy --strict, `pytest packages/ijob-core` 291 passed. All findings open (no blocking set, so no fix round):

- open 🟡 `store.py:971` `verdict`/`put_verdict`/`score`/`put_score` take four `str` cache-key args in a row; a swap silently misses the cache. Suggest a `Sha256` NewType or a `VerdictKey`/`ScoreKey` dataclass.
- open 🔵 `model.py:425` `_check_range` docstring says ints only, but the code rejects only `bool`; `Score(score=50.5, …)` builds.
- open 🔵 `model.py:427` no test guards the `bool` rejection.
- open 🔵 `model.py:536` no test for the `ResumeProfile.from_json` type-check branches.
- open 🔵 `model.py:655` `Verdict(keep=False, rule=True)` builds; reject a `bool` rule.
- open 🔵 `store.py:228` `_stamp` duplicates `model._check_aware`.
- open 🔵 `store.py:857` `set_cv_profile` does not validate `cv_sha256` as 64 hex characters.
- open 🔵 `store.py:895,941` `cv_profile()`/`instructions()` docstrings omit `Raises: InvalidValueError`.
- open 🔵 drift: `Instructions.set_at` accepts any aware timezone (plan says UTC); the store returns UTC.
- open 🔵 addition: `model`/`task_version` non-blank and ≤200 characters, and whitespace-only rules/reasons rejected — stricter than the plan.

## Deviations

- 2026-11-01: Tasks end in "Commit …", but the instruction to execute did not ask for commits (CLAUDE.md: commit only when the user asks). The work is left uncommitted.
- 2026-11-01: Plans 01–03 ran concurrently in one worktree. Coders ran scoped checks with `uv run --no-sync` while plan 03 relocked. The orchestrator and the reviewer ran the ijob-core checks; the workspace-wide gate runs once at the end of the run.
