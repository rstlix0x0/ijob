---
status: approved
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
