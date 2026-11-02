---
status: approved
created: 2026-11-02
built-on: 6ee2c2bd4580
---

# Recipe Storage Implementation Plan

**Goal:** Store a source together with its recipe in one transaction, under schema migration 5.

**Architecture:**
- `ijob-core` stores the recipe as opaque JSON text. It never parses it, and imports no LLM or
  network library (spec §10).
- Migration 5 appends a fifth tuple to `_MIGRATIONS` (`store.py:53-145`, four tuples today). The
  tuple holds one statement: the `source_recipes` table exactly as spec §10 gives it.
- `StoredRecipe` (frozen, slotted, keyword-only dataclass in `ijob_core/model.py`) has `ref`,
  `recipe` (JSON text), `model` and `created_at` (timezone-aware).
- New `Store` methods:
  - `recipe(ref) -> StoredRecipe | None`;
  - `set_recipe_source(ref, *, company, target_url, recipe, model, at)`.
- A removed source keeps its recipe row. `remove_source` is unchanged (spec §10).

**Tech Stack:** Python 3.14, sqlite3 (STRICT tables), pytest.

---

## Files that change

```
packages/ijob-core/src/ijob_core/model.py                  — [modify] StoredRecipe
packages/ijob-core/src/ijob_core/store.py                  — [modify] migration 5, Store.recipe, Store.set_recipe_source
packages/ijob-core/tests/ijob_core_tests/test_store.py     — [modify] migration and recipe tests
packages/ijob-core/tests/ijob_core_tests/test_model.py     — [modify] StoredRecipe validation
```

## Order of work

### Task 1 — Migration 5 and recipe rows

**Files:**
- Modify `model.py`, `store.py`, `test_store.py` and `test_model.py`.

**Change:**
- `StoredRecipe.__post_init__` refuses an empty `recipe` or `model`, and a naive `created_at`, with
  `InvalidValueError`.
- Migration 5 creates
  `source_recipes(ref TEXT PRIMARY KEY REFERENCES sources(ref), recipe TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL) STRICT`.
- `set_recipe_source(ref, *, company, target_url, recipe, model, at)` runs in one
  `with self._conn:` transaction:
  - it inserts the source row with origin `user`, or, when the ref exists, clears `removed`, as
    `add_source` does (`store.py:1149-1170`);
  - it upserts the `source_recipes` row, replacing `recipe`, `model` and `created_at`.
- `created_at` is written as ISO 8601 UTC.
- `recipe(ref)` reads the row back as `StoredRecipe`, or `None`.

**Proof:** written first, seen failing, then passing:
- `test_migration_5_adds_source_recipes`: a fresh store has `user_version` 5 and the table's columns.
  A v4 database opened again gains the table and keeps its rows.
- `test_set_recipe_source_adds_both_rows`: the source row has origin `user`, the given company and
  `target_url`, and `recipe(ref)` returns the stored text, model and time.
- `test_set_recipe_source_replaces_recipe_and_restores`: after `remove_source`, a second call clears
  `removed` and replaces the recipe, model and time. The company stays the first one, as
  `add_source` keeps it.
- `test_removed_source_keeps_recipe`: `recipe(ref)` still returns the row after `remove_source`.
- `test_set_recipe_source_is_atomic`: a recipe row that fails to write leaves no new source row.
  The test creates a `BEFORE INSERT` trigger on `source_recipes` that runs `RAISE(ABORT, 'x')`.
- The existing `user_version == 4` assertions (`test_store.py:93`, `:122`, `:388`, `:899`,
  `:1120`) become `5`.
- `test_recipe_unknown_ref_is_none`.
- `test_stored_recipe_validation` in `test_model.py`: an empty recipe, an empty model and a naive
  time are each refused.

**Done when:** `uv run --locked pytest packages/ijob-core` passes and the CLAUDE.md gate is green.
Commit `feat(core): store source recipes`.

## Risks

- **Riskiest step:** the atomicity test. The trigger must make the recipe insert fail after the
  source insert, inside the same `with self._conn:` block.
- **Not done:** `SourceKind.RECIPE`. It lands with its registry and description arms in plan 05, so
  the exhaustive `match` statements never see a kind they cannot handle.

## Proof

- `uv run --locked pytest packages/ijob-core` passes.
- The gate exits 0.
