---
status: done
created: 2026-10-31
depends-on: [01]
built-on: aeb01064881c
---

# Core Store Implementation Plan

**Goal:** The store keeps one posting per job across sources under schema version 2.

**Architecture:**
- Dedupe keys are pure functions in a new `ijob_core.dedupe`.
- `ijob_core.store` gains migration 2, the new posting columns, cross-source matching inside
  `record`, the `sources` table, and the reads the CLI needs (`open_postings`, `stale_sources`,
  `external_ids`).
- C1 operations keep their meaning: `postings`, `sighting`, and `record` for a known
  `(source, external_id)`.

**Tech Stack:** stdlib `sqlite3` (STRICT tables, `json`), `unicodedata`, `urllib.parse`, pytest with
`tmp_path`.

**Content authority:** spec §1 premises 2 and 4, §4, §7.

---

## Files that change

```
packages/ijob-core/src/ijob_core/dedupe.py                  — [create] normalise_company, canonical_url, fingerprint
packages/ijob-core/src/ijob_core/store.py                   — [modify] migration 2, new fields, matching, sources table, new reads
packages/ijob-core/tests/ijob_core_tests/test_dedupe.py     — [create] tests for dedupe
packages/ijob-core/tests/ijob_core_tests/test_store.py      — [modify] tests for the store additions
```

## Order of work

### Task 1 — Derive cross-source dedupe keys

**Files:**
- Create `packages/ijob-core/src/ijob_core/dedupe.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_dedupe.py`

**Change:** three pure functions, per spec §7:

- `normalise_company(text: str) -> str` does the company half of the fingerprint normalisation
  (NFKC, casefold, every non-alphanumeric run to one space, trim, up to two trailing suffix tokens
  removed). Plan 06's identity check reuses it.

- `canonical_url(url: str) -> str`:
  - lower-case the scheme and host;
  - drop the fragment and every query parameter whose name starts with `utm_`, keeping the order of
    the others;
  - drop one trailing `/` from a path longer than `/`.
- `fingerprint(company: str, title: str, country: CountryCode | None) -> str | None`:
  - return `None` when `country` is `None`;
  - normalise the company with `normalise_company`, and the title the same way without the suffix
    step (NFKC, casefold, every run of non-alphanumeric characters to one space, trim);
  - the suffix set is {`inc`, `llc`, `ltd`, `gmbh`, `corp`, `co`, `pte`, `pty`};
  - the result joins normalised company, normalised title and country code with `|`.

**Proof:** written first, seen failing, then passing —
- `test_canonical_url_cases` (parametrised):
  - `https://remoteOK.com/remote-jobs/x-1137597` → `https://remoteok.com/remote-jobs/x-1137597`;
  - `https://himalayas.app/companies/a/jobs/b?utm_source=api` → `https://himalayas.app/companies/a/jobs/b`;
  - `https://jobs.letsgetrusty.com?job=rec1` keeps `?job=rec1`;
  - `https://x.io/jobs/1/#apply` → `https://x.io/jobs/1`.
- `test_fingerprint_suffixes`: `fingerprint("SCIENTEC CONSULTING PTE. LTD.", "Engineer", "SG")` and
  `fingerprint("Scientec Consulting", "engineer", "SG")` are equal. So are the forms of
  `"Dijital Team Pty Ltd"` and `"Dijital Team"`.
- `test_fingerprint_needs_country`: `country=None` → `None`.
- `test_fingerprint_unicode`: a full-width or Japanese title (`"セキュリティエンジニア"`) is kept,
  not stripped to empty.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_dedupe.py` passes
and the gate is green. Commit `feat(core): derive cross-source dedupe keys`.

### Task 2 — Migrate to schema 2 and persist the new fields

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`

**Change:**
- Append migration 2 to `_MIGRATIONS`: the exact `ALTER TABLE`, `CREATE INDEX` and `CREATE TABLE
  sources` statements of spec §4.
- When a posting is created or updated, `record` writes the draft's new fields:
  - `salaries` as a JSON list of objects `{"min", "max", "currency", "period"}`, with Decimals as
    strings and `None` as `null`;
  - `country`, `remote` (the enum value), `role_hint` and `low_confidence`;
  - `canonical_url` and `fingerprint`, computed from the posting's resulting values: its `url`,
    `company`, `title` and `country` after the update (spec §4). Task 3 adds the rule that an empty
    draft value never overwrites, so a draft without a country cannot erase the fingerprint.
- Reads (`postings`, and every new read below) build `Posting` with those fields restored to their
  types.

**Proof:** written first, seen failing, then passing —
- `test_migration_1_to_2_keeps_rows`:
  - build a version-1 database with C1's schema and one posting (raw `sqlite3`, `user_version = 1`);
  - `Store.open` it;
  - `user_version` is 2, and the posting reads back with `salaries == ()`; `country`, `remote` and
    `role_hint` are `None`, and `low_confidence` is false (spec §4 probe).
- `test_new_fields_round_trip`: a draft carrying two `Money` values (one with `period=None`), a
  country, `RemoteScope.WORLDWIDE`, a role hint and `low_confidence=True` reads back equal.
- `test_fresh_database_is_version_2`: a new file opens at `user_version` 2 with a `sources` table.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_store.py` passes
and the gate is green. Commit `feat(core): migrate the store to schema 2`.

### Task 3 — Merge sightings across sources

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`

**Change:** per spec §4, the `record` row.

- For a new `(source, external_id)`, look for a posting with **no sighting from the same source**
  whose `canonical_url` equals the draft's, and failing that whose non-null `fingerprint` equals the
  draft's. When several qualify, take the lowest `id`.
- On a match:
  - attach the new sighting to that posting;
  - update its display fields and `last_seen` from the draft, as C1 does;
  - overwrite `country`, `remote`, `role_hint` and `salaries` only with non-empty draft values;
  - recompute `canonical_url` and `fingerprint` from the result (Task 2).
- Without a match, create a posting as before.
- A known `(source, external_id)` updates the same way: display fields from the draft, the new
  fields only when non-empty (spec §4, "On every update").
- `RecordResult` gains `merged: int = 0`, which counts drafts that attached a new sighting to an
  existing posting; `new` still counts postings created.
- `record` gains the keyword `still_listed: Collection[tuple[SourceRef, ExternalId]] = ()`. Inside
  the same transaction, each known pair sets its sighting's and its posting's `last_seen` to
  `seen_at`; unknown pairs are ignored.

**Proof:** written first, seen failing, then passing —
- `test_merge_by_canonical_url`: drafts from two sources with URLs differing only in host case and
  `utm_source` give 1 posting and 2 sightings. The first `record` returns
  `RecordResult(new=1, updated=0, merged=0)`, the second `RecordResult(new=0, updated=0, merged=1)`.
- `test_merge_keeps_fingerprint_without_draft_country`: a posting with country `GB` merged by URL
  with a draft whose country is `None` keeps `country == "GB"`. A third source can then still match
  it by fingerprint.
- `test_lowest_id_wins_on_tie`: two candidate postings → the sighting attaches to the lower `id`.
- `test_merge_by_fingerprint`: different URLs, same company/title/country across two sources → 1
  posting.
- `test_no_merge_without_country`: same company and title, `country=None`, different URLs → 2
  postings.
- `test_no_merge_within_one_source`: two drafts from one source with different external ids but the
  same title and country → 2 postings (spec §4: the Palantir case).
- `test_empty_draft_value_does_not_overwrite`: a merged posting keeps its salary when a later draft
  has `salaries=()`.
- `test_still_listed_refreshes_last_seen`: a known pair in `still_listed` moves `last_seen`; an
  unknown pair changes nothing.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_store.py` passes
and the gate is green. Commit `feat(core): merge sightings across sources`.

### Task 4 — Keep the sources table

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`

**Change:**
- Add `SourceOrigin`, a `StrEnum` with `SEED = "seed"` and `USER = "user"` (python guideline, strong
  types: closed sets).
- Add `SourceRow`, a frozen slotted dataclass `(ref: SourceRef, company: str, target_url: str |
  None, origin: SourceOrigin, removed: bool, last_fetched_at: datetime | None)`.
- Add these operations, per spec §4:
  - `import_seeds(rows: Iterable[tuple[SourceRef, str]]) -> int` inserts `origin='seed'` rows whose
    `ref` is absent, leaves present rows untouched (removed ones included), and returns the count
    inserted;
  - `add_source(ref, company, target_url=None)` inserts a `user` row, or clears `removed` on an
    existing row and keeps its `origin`;
  - `remove_source(ref)` sets `removed = 1`;
  - `source(ref) -> SourceRow | None` reads one row, removed or not; its `removed` field tells which;
  - `sources() -> list[SourceRow]` reads every row not removed, ordered by `ref`;
  - `mark_fetched(ref, at)` sets `last_fetched_at`, and raises `InvalidValueError` on a naive `at`.

**Proof:** written first, seen failing, then passing —
- `test_import_seeds_is_idempotent`: importing the same two rows twice returns 2, then 0.
- `test_removed_seed_stays_removed`: import, remove, import again → `sources()` lacks it.
- `test_add_revives_and_keeps_origin`: removing a seed and then `add_source` makes it listed again
  with `origin == "seed"`.
- `test_add_user_source`: a new ref gets `origin == SourceOrigin.USER` and its `target_url`.
- `test_source_reports_removed`: after `remove_source`, `source(ref).removed` is true and
  `sources()` lacks it.
- `test_mark_fetched`: `source(ref).last_fetched_at` equals the aware UTC time written.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_store.py` passes
and the gate is green. Commit `feat(core): keep the sources table`.

### Task 5 — List open postings

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py`
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`

**Change:** per spec §1 premise 4 and §4.

- `open_postings(*, now: datetime, sources: Collection[SourceRef] | None = None) -> list[Posting]`
  returns postings that have a sighting whose `last_seen` equals its source row's `last_fetched_at`,
  where:
  - the row is not removed;
  - `last_fetched_at` is at most 7 days before `now`;
  - the row's ref is in `sources` when `sources` is given.

  The order is C1's (`posted_at` newest first, nulls last, then `id`).
- `stale_sources(*, now) -> list[SourceRow]` returns rows not removed whose `last_fetched_at` is set
  and more than 7 days before `now`.
- `external_ids(ref) -> frozenset[ExternalId]` returns every external id the source has shown.
- `posting_sources(posting_id) -> list[SourceRef]` returns the refs of the posting's sightings,
  sorted. The CLI prints them as the listing's source column (spec §9 step 5).

**Proof:** written first, seen failing, then passing —
- `test_open_postings_follow_latest_fetch`:
  - source A is fetched at t1 with jobs 1 and 2, then at t2 with job 1 only;
  - `mark_fetched(A, t2)`;
  - open postings hold job 1 only.
- `test_failed_source_keeps_last_known`: source B was fetched at t1 and not marked since; its t1
  postings are open while `now - t1` is within 7 days.
- `test_stale_source_hidden`: with `now - t1` over 7 days, B's postings are not open, and
  `stale_sources` lists B.
- `test_removed_source_hidden`: removing B hides its postings.
- `test_open_postings_narrowed_by_sources`: `sources=[A]` excludes postings seen only on B.
- `test_merged_posting_open_via_either_source`: a posting merged from A and B is open while either
  sighting is current.
- `test_external_ids`: the ids recorded for a source, and none from another.
- `test_posting_sources`: a posting merged from A and B lists both refs, sorted.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests` passes (C1 tests
included) and the gate is green. Commit `feat(core): list open postings`.

---

## Risks

- **Most risky: Task 3's matching inside `record`.** It runs inside C1's single `BEGIN IMMEDIATE`
  transaction (`store.py:152`), and two drafts in one batch can match each other. Each draft is looked up after the
  previous ones in the batch are written, so a second source's draft finds the first source's
  posting. The same-source exclusion stops a board merging its own jobs.
- A wrong `last_seen`/`last_fetched_at` equality would hide every posting. Both are written from the
  same `seen_at` value by the CLI (plan 10); timestamps use C1's fixed-width UTC text (`store.py:80-82`), so equal
  instants compare equal as text.
- Not done here: the filters (plan 03), and the CLI that calls `import_seeds` and `mark_fetched`
  (plan 10).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-core` passes, the C1 store tests included, unchanged.

## Review findings

Reviewer verdict: blocking `dedupe.py:67`. Gate re-run by the reviewer: all five commands exit 0, pytest 455 passed.

- correctness (blocking) — `canonical_url` calls `urlsplit`, which raises ValueError on a malformed URL (`https://[bad/1`) that SightingDraft accepted, so one bad job aborted and rolled back a whole `record` batch — `dedupe.py:67` — fixed: `SightingDraft.__post_init__` rejects an unparseable URL with InvalidValueError (`from` the ValueError); `test_sighting_draft_rejects_unparseable_url` failed first, then passed.
- test-mandate — no test that a canonical_url match beats a fingerprint match — `store.py:373-390` — fixed: `test_canonical_url_match_beats_fingerprint_match` (guard; mutation with `reversed(keys)` makes it fail).
- performance — `open_postings` scanned sightings with no index on `sightings.posting_id` (reviewer measured 0.29 s at 5k postings, 4.43 s at 20k) — `store.py:505-514` — fixed: migration 2 gains `CREATE INDEX sightings_posting_id ON sightings(posting_id)`; speedup not measured.
- design (deferred, needs spec decision) — a posting's `canonical_url` follows the last writer, so a later source carrying the first source's URL and no country can create a duplicate (`RecordResult(new=1)`) — `store.py:420-459` — not changed.
- nits fixed — canonical_url docstring states its precondition and Raises; `_open_ids` renamed `_open_titles`; needless `merged=0` reverted on a C1 assertion.
- nits/questions skipped — `_normalise_text` punctuation-only collision (behaviour change), untested `role_hint=""` branch, weak `ORDER BY id` guard, version-1 `canonical_url` backfill (needs spec amendment).
- Verified after the fix round (full gate, 2026-10-31): sync ok; `ruff format --check` → `88 files already formatted`; `ruff check --no-fix` → `All checks passed!`; `mypy --strict .` → `Success: no issues found in 44 source files`; `pytest -q` → `458 passed in 0.55s`.

## Probe results

- store.py structure the tasks assume — `grep -n "_MIGRATIONS\|BEGIN IMMEDIATE\|def " store.py` → `_MIGRATIONS` L30, `RecordResult` L68, `_to_text` L80 (fixed-width UTC, `isoformat(timespec="microseconds")`), `Store.open` L106, `record` L126, `BEGIN IMMEDIATE` L152, `_write_draft` L161, `postings` L212, `sighting` L245, `_migrate` L290 (as planned).

## Deviations

- 2026-10-31 — defect of the plan: the plan's Proof says "C1 store tests included, unchanged", but C1 tests asserted schema version 1. `test_open_creates_schema_version_1` became `test_fresh_database_is_version_2`, and `test_open_is_idempotent` asserts 2. Three C1 tests that recorded two sources with the same default URL now give source B a distinct URL, because those drafts merge as spec §4 intends. `test_record_is_atomic` imports the private `_Outcome`. Test intent is unchanged.
- 2026-10-31 — migration 2 has one statement more than spec §4 lists: the `sightings_posting_id` index (fix round). Migration 2 is unreleased, so it was added in place.
- 2026-10-31 — `SightingDraft` (plan 01's model) now rejects unparseable URLs (fix round).
- 2026-10-31 — `normalise_company` never strips the last remaining token ("Co" → "co"), so a key is never empty; the `utm_` match ignores case; `low_confidence` follows the latest draft on update or merge (outside the spec's non-empty rule, as plan Task 2 says).
- 2026-10-31 — `remove_source` and `mark_fetched` on an unknown ref are silent no-ops; `add_source` on an existing ref only clears `removed`; the 7-day window is inclusive; `stale_sources` ignores never-fetched rows.
