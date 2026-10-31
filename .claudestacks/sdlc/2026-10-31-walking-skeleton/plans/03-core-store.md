---
status: done
created: 2026-10-31
depends-on: [02]
built-on: aafd5e32e8cd
---

# Core Store Implementation Plan

**Goal:** Sighting drafts persist in a local SQLite store that never duplicates a posting from the
same source.

**Architecture:** One new module, `ijob_core.store`, owns the database: opening it, migrating it by
`PRAGMA user_version`, recording drafts in one transaction, and reading postings and sightings back as
the frozen records from `ijob_core.model`. The caller passes `seen_at`, so tests control time. It also
owns the default database path.

**Tech Stack:** stdlib `sqlite3` (`STRICT` tables), `pathlib`, pytest with `tmp_path` and
`monkeypatch`.

**Content authority:** spec §1 premises 2-3, §4.

---

## Files that change

```
packages/ijob-core/src/ijob_core/store.py               — [create] Store, RecordResult, default_db_path
packages/ijob-core/tests/ijob_core_tests/test_store.py  — [create] tests for store
```

## Order of work

### Task 1 — Open and migrate the database

**Files:**
- Create `packages/ijob-core/src/ijob_core/store.py` — `Store` with `Store.open(path)` and context
  manager support; the schema-version-1 migration; `default_db_path(environ)`.
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`.

**Change:**
- `Store.open(path)` creates the parent directory if missing, connects, and applies every pending
  migration: when `PRAGMA user_version` is 0 it creates the `postings` and `sightings` tables exactly
  as spec §4 lists them (`STRICT`, `UNIQUE (source, external_id)`) and sets `user_version` to 1. A
  database already at 1 is left as is. `Store` closes its connection on `__exit__`.
- `default_db_path(environ: Mapping[str, str]) -> Path` returns `$XDG_DATA_HOME/ijob/ijob.db`, or
  `~/.local/share/ijob/ijob.db` when `XDG_DATA_HOME` is unset or empty.

**Proof:** written first, seen failing, then passing —
- `test_open_creates_schema_version_1` — opening a new file under `tmp_path` leaves
  `PRAGMA user_version` at 1 and both tables present.
- `test_open_is_idempotent` — opening the same file twice keeps `user_version` at 1 and keeps rows
  written between the two opens.
- `test_open_creates_missing_parent_directory` — a path whose parent does not exist opens.
- `test_default_db_path_uses_xdg_data_home` — with `XDG_DATA_HOME=/x` the result is
  `/x/ijob/ijob.db`.
- `test_default_db_path_falls_back_to_local_share` (parametrised: `XDG_DATA_HOME` absent, and set to
  `""`) — the result is `~/.local/share/ijob/ijob.db` under the home directory.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_store.py` passes
and the gate is green. Commit `feat(core): open and migrate the sqlite store`.

### Task 2 — Record drafts without duplicates

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py` — add `RecordResult` and `Store.record`
  (the module created in Task 1).
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py` (the file created in Task 1).

**Change:** `Store.record(drafts, *, seen_at) -> RecordResult` runs in one transaction. A draft whose
`(str(source), external_id)` is new inserts a Posting and a Sighting with `first_seen = last_seen =
seen_at`. A known one sets the Sighting's `last_seen` and `raw`, and the Posting's `last_seen` and
display fields (`title`, `company`, `location`, `url`, `posted_at`), from the draft. Timestamps are
stored as ISO-8601 text in UTC. `RecordResult` is a frozen slotted dataclass `(new: int, updated: int)`.

**Proof:** written first, seen failing, then passing —
- `test_record_new_draft_creates_posting_and_sighting` — one draft gives `RecordResult(new=1,
  updated=0)`, one `postings` row and one `sightings` row.
- `test_record_same_draft_again_updates_in_place` — recording the same `(source, external_id)` again
  with a later `seen_at`, a changed `title` and a changed `raw` gives `RecordResult(new=0, updated=1)`;
  row counts stay 1 and 1; `last_seen` moved, `first_seen` did not; the title and raw are the new ones.
- `test_record_same_external_id_from_two_sources_is_two_postings` — the same `external_id` under two
  different `SourceRef`s gives two postings.
- `test_update_touches_only_its_own_source` — after both of those are recorded, recording source A's
  draft again with a changed title changes A's posting title and leaves source B's posting title and
  `last_seen` unchanged.
- `test_record_is_atomic` — with the store's per-draft write helper monkeypatched to raise
  `sqlite3.OperationalError` on its second call, recording two new drafts raises and leaves no row
  from the first draft. (Task 2 gives `Store` one private helper that writes one draft, so the test
  has a seam.)

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_store.py` passes
and the gate is green. Commit `feat(core): record sighting drafts without duplicates`.

### Task 3 — Read postings and sightings back

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py` — add `Store.postings` and `Store.sighting`.
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`.

**Change:**
- `Store.postings(*, sources: Collection[SourceRef]) -> list[Posting]` returns the postings with a
  Sighting from any of those sources, newest `posted_at` first, those without `posted_at` last.
- `Store.sighting(source, external_id) -> Sighting | None` returns the one Sighting, raw payload
  included, or `None`.
- Stored text timestamps come back as timezone-aware `datetime`s in UTC.

**Proof:** written first, seen failing, then passing —
- `test_postings_orders_newest_first_with_undated_last` — three drafts posted on day 1, day 3 and
  undated come back as day 3, day 1, undated.
- `test_postings_filters_by_source` — postings from a source not asked for are not returned.
- `test_sighting_round_trips_raw_and_times` — after `record`, `sighting()` returns the draft's `raw`,
  and `first_seen`/`last_seen` equal `seen_at` as aware UTC datetimes.
- `test_sighting_unknown_is_none` — an unrecorded `(source, external_id)` returns `None`.

**Done when:** `uv run --locked pytest packages/ijob-core` passes and the gate is green. Commit
`feat(core): read postings and sightings from the store`.

---

## Risks

- **Most risky step: Task 2's update path.** Updating the Posting's display fields from the newest
  draft is what keeps the listing current; a wrong join updates another source's posting.
  `test_update_touches_only_its_own_source` guards it.
- `default_db_path` also falls back when `XDG_DATA_HOME` is set but empty, a small addition to spec
  §4's "unset", so an empty value never yields a relative `ijob/ijob.db` path.
- The atomicity test patches a private helper; that seam is the one test-driven structure this plan
  adds, and it keeps the failure injection out of the public API.
- Not done: no cross-source key columns (spec §1 premise 3, migration 2 in the follow-on chain), no
  deletion, no ETag storage.

## Proof

- `uv run --locked pytest packages/ijob-core` passes; the five-command gate is green.
- Opening a database written by this plan with `sqlite3 <file> 'pragma user_version'` prints `1`.

## Review findings

- test-mandate — STRICT and UNIQUE(source, external_id) unguarded — `store.py:30` (fixed and verified: `test_schema_enforces_strict_and_unique`, red with UNIQUE→`CHECK (1)` and with STRICT dropped; `uv run --locked pytest -q` → `61 passed in 0.09s`)
- test-mandate — UTC conversion unguarded; Greenhouse sends offset timestamps — `store.py:81` (fixed and verified: `test_record_stores_non_utc_times_as_utc`, red with `.astimezone(UTC)` removed; same run)
- errors / test-mandate — `open` failure path untested; `except Exception` cleanup missed BaseException — `store.py:122` (fixed and verified: `ExitStack` + `closing`; `test_failed_migration_leaves_version_zero`, red with the close removed; same run)
- test-mandate — migration atomicity (explicit BEGIN) unguarded — `store.py:295` (fixed and verified: same test, red with BEGIN removed; same run)
- concurrency — `record` lookups ran before a deferred BEGIN; two runs could lose a batch — `store.py:165` (fixed: `BEGIN IMMEDIATE`; unverified by test — race-only)
- concurrency — `user_version` read outside the migration transaction — `store.py:291` (fixed: `BEGIN IMMEDIATE` and re-read inside; unverified by test — race-only)
- robustness — relative `XDG_DATA_HOME` accepted — `store.py:62` (nit, not fixed)
- modularity — public `Store.__init__(conn)` skips migration and pragmas — `store.py:96` (nit, not fixed)
- docstring — `open` Raises omits `OSError` from `mkdir` — `store.py:114` (nit, not fixed)
- reversion guard — `PRAGMA foreign_keys = ON` untested — `store.py:120` (nit, not fixed)
- question — raw `sqlite3.Error` leaves the public API rather than a `CoreError`; spec §7 defines no store error — `store.py:115` (open, spec's call)
- question — a database whose `user_version` is above the known migrations opens and later fails with "no such table" — `store.py:293` (open, spec §4 silent)
- spec hygiene — empty `XDG_DATA_HOME` fallback goes beyond spec §4's "unset"; recorded in this plan's Risks only.
- Reviewer gate re-run before fixes: all five exit 0, `58 passed`; `sqlite3 <db> 'pragma user_version'` → `1`.

## Probe results

- `STRICT` tables available — `python3.14 -I -c "import sqlite3,sys; …create table t(a TEXT NOT NULL) STRICT…"` → `3.14.10 3.53.1`, table created.
- Fresh database schema version — `Store.open(<scratch>/uv-check.db)` then `sqlite3 <scratch>/uv-check.db 'pragma user_version'` → `1`.
- Final gate after the fix round — `uv sync --locked …` → `Checked 28 packages`; `ruff format --check` → `51 files already formatted`; `ruff check --no-fix` → `All checks passed!`; `mypy --strict .` → `Success: no issues found in 18 source files`; `pytest -q` → `61 passed in 0.09s`; boundary grep → no output.

## Deviations

- 2026-10-31 — additions beyond the plan: naive `seen_at` rejected with `InvalidValueError` (tested); `PRAGMA foreign_keys = ON`; source filter through `json_each(?)` (empty `sources` → `[]`); fixed-width microsecond UTC text so text order equals time order; `BEGIN IMMEDIATE` for migration and `record`.
- 2026-10-31 — per-task commits not made: commits are the user's act (CLAUDE.md).
