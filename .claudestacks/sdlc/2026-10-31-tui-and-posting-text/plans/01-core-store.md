---
status: approved
created: 2026-11-01
built-on: a9902751b191
---

# Core Store Migration 3 Implementation Plan

**Goal:** Store migration 3 lands in `ijob_core`, with the model types and `Store` methods that read and write its new column and tables.

**Architecture:**
- `ijob_core.model` gains the `description` field on `SightingDraft` and `Posting`, and the new types `Vote`, `Label` and `StoredCv` (spec §1.1, §4).
- `ijob_core.store` appends migration 3 to `_MIGRATIONS` (`store.py:41`), extends the posting insert, update and select paths with `description`, and adds the new methods. No network or LLM import (CLAUDE.md boundary).
- Every new datetime parameter rejects a naive value, as `record` does (`store.py:314-315`).

**Tech Stack:** Python 3.14, stdlib `sqlite3` (STRICT tables, `PRAGMA user_version`), pytest.

---

## Files that change

```
packages/ijob-core/src/ijob_core/model.py        — [modify] description fields; Vote, Label, StoredCv
packages/ijob-core/src/ijob_core/store.py        — [modify] migration 3; description in insert/update/select; label, cv and lookup methods
packages/ijob-core/tests/ijob_core_tests/test_model.py — [modify] tests for the new fields and types
packages/ijob-core/tests/ijob_core_tests/test_store.py — [modify] tests for migration 3 and the new methods
```

## Order of work

### Task 1 — Model fields and types

**Files:**
- Modify `packages/ijob-core/src/ijob_core/model.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_model.py`

**Change:**
- `SightingDraft` (`model.py:158-193`) and `Posting` (`model.py:224-259`) each gain `description: str | None = None` as their last field, documented in the class docstring's Attributes.
- New `Vote(StrEnum)` with `UP = "up"` and `DOWN = "down"`.
- New frozen, slotted dataclass `Label(posting_id: PostingId, vote: Vote, note: str | None, labeled_at: datetime)`. `__post_init__` raises `InvalidValueError` when `labeled_at` has no timezone, and turns an empty or whitespace-only `note` into an error too (a note is either absent or has text).
- New frozen, slotted dataclass `StoredCv(name: str, sha256: str, size: int, stored_at: datetime)`. `__post_init__` raises `InvalidValueError` for an empty `name`, a `sha256` that is not 64 lowercase hex characters, a negative `size`, or a naive `stored_at`.

**Proof:** written first, seen failing, then passing —
- `test_sighting_draft_description_defaults_to_none` — a draft built without `description` has `None`.
- `test_posting_description_defaults_to_none` — the same for `Posting`.
- `test_vote_values` — `Vote("up") is Vote.UP`, `Vote("down") is Vote.DOWN`, and `Vote("meh")` raises `ValueError`.
- `test_label_rejects_naive_time` — `Label` with a naive `labeled_at` raises `InvalidValueError` naming `labeled_at`.
- `test_label_rejects_blank_note` — a note of `"  "` raises `InvalidValueError` naming `note`; `None` is accepted.
- `test_stored_cv_rejects_bad_fields` — parametrized over empty name, a 63-character hash, an uppercase hash, `size=-1`, and a naive `stored_at`; each raises `InvalidValueError`.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_model.py` passes and the gate in CLAUDE.md is green. Commit `feat(core): add posting description, label and stored CV types`.

### Task 2 — Migration 3 and description persistence

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`

**Change:**
- Append to `_MIGRATIONS` a third tuple holding, in order: `ALTER TABLE postings ADD COLUMN description TEXT`, the `labels` table and the `cv` table, exactly as spec §4 gives them.
- `_insert_posting` (`store.py:393`) writes `draft.description`.
- `_update_posting` (`store.py:421-430`) sets `description = COALESCE(?, description)`, the rule already used for `country` and `remote`.
- `_SELECT_POSTINGS` (`store.py:175`) and `_posting` (`store.py:186-202`) read `description` into `Posting.description`.

**Proof:** written first, seen failing, then passing —
- `test_migration_3_on_version_2_database` — a database built by applying only the first two migrations, holding one posting, opens with `Store.open`; `PRAGMA user_version` is `3`, the posting reads back with `description is None`, and the `labels` and `cv` tables exist.
- `test_record_stores_description` — recording a draft with `description="## About"` yields a posting whose `description` is `"## About"`.
- `test_record_keeps_description_when_draft_has_none` — a second sighting of the same job with `description=None` leaves the stored description unchanged.
- `test_record_replaces_description_when_draft_has_one` — a later draft with a different description replaces it.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_store.py` passes and the gate is green. Commit `feat(core): add store migration 3 with posting descriptions`.

### Task 3 — Lookup, description, label and CV methods

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`

**Change:** new public `Store` methods, each with a Google-style docstring:
- `posting(posting_id: PostingId) -> Posting | None` — one posting by id.
- `sightings_of(posting_id: PostingId) -> list[Sighting]` — the posting's sightings in sighting-id order, built like `sighting()` (`store.py:567`).
- `set_description(posting_id: PostingId, text: str) -> None` — writes the column; raises `InvalidValueError` for an unknown posting.
- `set_label(posting_id: PostingId, vote: Vote, note: str | None, *, at: datetime) -> Label` — insert or replace; rejects a naive `at` and an unknown posting with `InvalidValueError`.
- `clear_label(posting_id: PostingId) -> None` — deletes the row; no error when none exists.
- `label(posting_id: PostingId) -> Label | None`.
- `labels(ids: Collection[PostingId]) -> dict[PostingId, Label]` — only ids that have a label appear; empty input gives `{}`.
- `set_cv(cv: StoredCv) -> None` — insert or replace the single row (`id = 1`).
- `cv() -> StoredCv | None`.

**Proof:** written first, seen failing, then passing —
- `test_posting_by_id` — a recorded posting reads back by id; an unknown id gives `None`.
- `test_sightings_of_in_id_order` — a posting seen on two sources returns both sightings, the first-recorded first.
- `test_set_description_writes_text` — after `set_description`, `posting(id).description` is the text; an unknown id raises `InvalidValueError`.
- `test_set_label_then_replace` — `set_label(UP, "fit")` then `set_label(DOWN, None)` leaves one label, `DOWN` with no note.
- `test_set_label_rejects_naive_time_and_unknown_posting` — both raise `InvalidValueError`.
- `test_clear_label` — after `clear_label`, `label(id)` is `None`; clearing again does not raise.
- `test_labels_for_ids` — with labels on two of three postings, `labels([a, b, c])` has exactly the two; `labels([])` is `{}`.
- `test_label_survives_new_sighting` — a label set on a posting stays on it after a second source's draft attaches a sighting to the same posting.
- `test_cv_round_trip_and_replace` — `cv()` is `None` on a new database; after `set_cv(a)` it is `a`; after `set_cv(b)` it is `b`.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_store.py` passes and the gate is green. Commit `feat(core): add label, CV and posting lookup store methods`.

---

## Risks

- **Migration on real user databases** is the riskiest step: a mistake corrupts `ijob.db`. Migration 3 only adds a nullable column and two new tables, runs inside `_migrate`'s locked transaction (`store.py:727-737`), and is tested on a version-2 database.
- `labels.posting_id` references `postings(id)` with `PRAGMA foreign_keys = ON` (`store.py:271`); the store never deletes postings (spec §4), so no cascade is needed. Not done: deleting labels when a posting goes stale.

## Proof

- `uv run --locked pytest packages/ijob-core` passes.
- The five gate commands in CLAUDE.md exit `0`.
- `grep -rn "httpx\|dspy\|anthropic" packages/ijob-core/src` finds nothing (boundary held).
