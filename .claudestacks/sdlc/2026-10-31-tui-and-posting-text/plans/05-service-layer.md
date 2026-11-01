---
status: done
created: 2026-11-01
depends-on: [01, 04]
built-on: b175307f42cd
---

# Service Layer Implementation Plan

**Goal:** `ijob_app.service.Service` owns every pipeline operation the CLI and the TUI run, with the existing `search` and `sources` commands calling it and behaving as before.

**Architecture:**
- New module `ijob_app.service` (spec §5). `Service` is built from an open `Store`, the database path, the client factory (`_ClientFactory`, `cli.py:77`, moved here as the public alias `ClientFactory`), an open HTTP cache and a clock.
- `open_service(db: Path | None, client_factory: ClientFactory, *, now: Callable[[], datetime], environ: Mapping[str, str]) -> Iterator[Service]` is a context manager that opens the seeded store at `db or default_db_path(environ)` and the cache at `default_cache_path(environ)`, as `_seeded_store` and `_open_cache` do today (`cli.py:186-225`), and closes both on exit. Passing `environ` lets tests point every path under `tmp_path`, as the CLI tests' `_env` does (`test_cli.py:105-111`). A cache that cannot be opened raises `CacheUnavailableError(InvalidValueError)`; the CLI maps it to today's message and exit 1 (`cli.py:216-222`).
- Network-touching methods are `async` (`refresh`, `detail`, `label`, `add_source`, `add_url`). The store and cache are used only on the thread that runs the event loop (spec §1.4, §5; `store.py:270`).
- The CLI keeps all printing and exit codes. The service returns values and raises `InvalidValueError` subclasses or `SourcesError`. These pipeline helpers move out of `cli.py`: `_collect`, `_new_row`, `_verify`, `_refuse_other_company`, `_choose_sources`, `_split_by_interval`, `_build_sources` and `_record` (`cli.py:110-124, 226-310, 461-575`), plus the logic of the `sources` commands (`cli.py:313-399`). This keeps the CLI a thin layer (`references/modularity.md`).
- Shared test fixtures go in `packages/ijob-app/tests/ijob_app_tests/conftest.py`: the temporary XDG environ, a ticking clock, and a `MockTransport` client factory. Test modules cannot import each other under `--import-mode=importlib` (`pyproject.toml:32`; guideline `test-mandate.md` § Shared test helpers). Plan 07 uses the same fixtures.

**Tech Stack:** Python 3.14, `asyncio`, `ijob_core.store` (plan 01), `ijob_sources.details` (plan 04), Typer, pytest with `httpx.MockTransport` and `typer.testing.CliRunner`.

---

## Files that change

```
packages/ijob-app/src/ijob_app/service.py                — [create] Service, open_service, RefreshReport, PostingDetail, the error types, the moved pipeline helpers
packages/ijob-app/src/ijob_app/cli.py                    — [modify] search and sources commands call the service; printing and exit codes stay
packages/ijob-app/tests/ijob_app_tests/conftest.py       — [create] shared fixtures: tmp XDG environ, ticking clock, MockTransport client factory
packages/ijob-app/tests/ijob_app_tests/test_service.py   — [create] service tests
packages/ijob-app/tests/ijob_app_tests/test_cli.py       — [modify] only to take the shared fixtures from conftest; no assertion changes
```

## Order of work

### Task 1 — `refresh` and `open_postings`; `search` calls them

**Files:**
- Create `packages/ijob-app/src/ijob_app/service.py`, `packages/ijob-app/tests/ijob_app_tests/conftest.py`
- Modify `packages/ijob-app/src/ijob_app/cli.py`
- Test `packages/ijob-app/tests/ijob_app_tests/test_service.py`, `test_cli.py`

**Change:**
- `RefreshReport` (frozen, slotted) with fields:
  - `due: list[SourceRow]`
  - `kept: list[SourceRow]`
  - `fetched: list[SourceRef]`, what `_record` returns (`cli.py:542-544`)
  - `rejected: dict[SourceRef, int]`, each fetched source's `SourceBatch.rejected` (`collect.py:45`) when above zero, so `search` keeps printing `<ref>: skipped N invalid jobs` (`cli.py:589-590`)
  - `failures: list[SourceFailure]`
  - `refused: dict[SourceRef, InvalidValueError]`, what `_build_sources` returns (`cli.py:520-522`)
  - `stale: list[SourceRow]`
- `UnknownSourceError(InvalidValueError)`, raised with field `source` and the reason today's message gives (`cli.py:483-485`), for a named source that has no row or was removed.
- `async Service.refresh(refs: Sequence[SourceRef]) -> RefreshReport`. It reads the clock twice, as `search` does today: once to split by interval before fetching (`cli.py:698`), and once after fetching as `seen_at` for recording and staleness (`cli.py:701`).
- `Service.open_postings(spec: FilterSpec, refs: Sequence[SourceRef]) -> list[Posting]`: `store.open_postings` plus `keep` at one `now()` (`cli.py:704-707`). A named ref that has no row or was removed raises `UnknownSourceError`, using the same check as `refresh`.
- `search` opens the service and runs `asyncio.run(service.refresh(refs))`. It prints the `kept <ref>: fetched less than N min ago` lines from `report.kept` (`cli.py:510-514`), the problem lines from `failures`, `refused` and `rejected` (`cli.py:576-591`), the stale lines (`cli.py:593-603`) and the filtered postings. It keeps the exit-1 rule `due and not fetched and not kept` (`cli.py:709-710`). `UnknownSourceError` maps to today's message and exit 2 (`cli.py:482-487`).

**Proof:** written first, seen failing, then passing.
- `test_refresh_records_due_sources` (`test_service.py`): with a seeded temporary database and a `MockTransport` serving one Greenhouse board, the report has the source in `due` and in `fetched` with no failures, and `open_postings` returns its postings.
- `test_refresh_reports_rejected_jobs`: a board with one invalid job gives `rejected == {ref: 1}`.
- `test_refresh_keeps_recent_sources`: a second refresh within the interval puts the source in `kept` and makes no board request.
- `test_refresh_unknown_source_raises`: naming an unknown `greenhouse:nope` raises `UnknownSourceError` whose field is `source`.
- `test_open_postings_unknown_source_raises`: the same for `open_postings`.
- `test_open_postings_applies_filters`: a `FilterSpec` excluding the postings' country returns none.
- Every existing test in `test_cli.py` passes with its assertions unchanged; this guards behaviour during the move. That includes `test_rejected_jobs_reported` (`test_cli.py:277-285`).

**Done when:** `uv run --locked pytest packages/ijob-app` passes and the gate in CLAUDE.md is green. Commit `refactor(app): move the search pipeline into a service layer`.

### Task 2 — `detail`, `labels` and `label`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/service.py`
- Test `packages/ijob-app/tests/ijob_app_tests/test_service.py`

**Change:**
- `PostingDetail` (frozen, slotted): `posting: Posting`, `sources: list[SourceRef]`, `label: Label | None`, `description: str | None`, `unavailable_reason: str | None`.
- `UnknownPostingError(InvalidValueError)`, with field `posting` and reason `no posting N`.
- `async Service.detail(posting_id: PostingId) -> PostingDetail` raises `UnknownPostingError` for an unknown id.
  - When the stored description is `None`, it opens one client from the factory and calls `details.describe` on each of `store.sightings_of(id)` in order. The first non-`None` text is stored with `set_description` and returned.
  - A `SourcesError` from `describe` is kept as `unavailable_reason` (its message), nothing is stored, and the remaining sightings are still tried.
  - With no text and no error, `unavailable_reason` is `no description published`.
- `Service.labels(ids: Collection[PostingId]) -> dict[PostingId, Label]` reads `store.labels(ids)` without touching descriptions or the network. The TUI's results list uses it (plan 07).
- `async Service.label(posting_id, vote: Vote, note: str | None) -> PostingDetail` calls `store.set_label(..., at=now())`, then runs the description step of `detail`.
- `Service.clear_label(posting_id) -> None`.
- `label` and `clear_label` raise `UnknownPostingError` for an unknown id.

**Proof:** written first, seen failing, then passing.
- `test_detail_returns_stored_description_without_request`: for a posting recorded with a description, the handler records no request.
- `test_detail_fills_pre_migration_row_from_raw`: for a posting whose `description` was set to `NULL` directly in SQLite, with a greenhouse sighting, `detail` returns and stores the normalized description with no request.
- `test_detail_fetches_workable`: for a Workable posting without a description there is one `GET` to the v2 route (plan 04 fixture), and the text is stored. A second `detail` makes no request.
- `test_detail_failure_is_reported_and_not_stored`: when the Workable route answers `500`, `unavailable_reason` is set and the column stays `NULL`. The next `detail` requests again.
- `test_detail_unknown_posting_raises`: raises `UnknownPostingError`.
- `test_labels_reads_without_request`: with labels on two of three postings, `labels` returns exactly those two, and the handler records no request.
- `test_label_sets_label_and_fetches_description`: `label(id, Vote.UP, "fit")` returns a detail whose `label.vote` is `UP` and whose description is filled.
- `test_clear_label`: after `clear_label`, `detail(id).label is None`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_service.py` passes and the gate is green. Commit `feat(app): add posting detail and labels to the service`.

### Task 3 — `store_cv` and `cv`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/service.py`
- Test `packages/ijob-app/tests/ijob_app_tests/test_service.py`

**Change:**
- `Service.store_cv(path: Path) -> StoredCv` expands `~`. It raises `InvalidCvError(InvalidValueError)` (field `cv`) when:
  - the file is missing or unreadable;
  - it is larger than 20 MB (`MAX_CV_BYTES = 20_000_000`, spec §8);
  - it does not start with `b"%PDF-"`.
- Otherwise it copies the bytes to `cv.pdf` in the database file's directory, writing a temporary file there and replacing. It computes the SHA-256 and writes `StoredCv(name=path.name, sha256, size, stored_at=now())` with `store.set_cv`.
- `Service.cv() -> StoredCv | None`.

**Proof:** written first, seen failing, then passing. Each test uses a made-up PDF it writes itself (a `%PDF-1.4` header and a few bytes; no personal data).
- `test_store_cv_copies_next_to_database`: `cv.pdf` appears beside the temporary database with identical bytes, and `cv()` returns the row with the right size and hash.
- `test_store_cv_replaces_previous`: storing a second file replaces both the file and the row.
- These three each raise `InvalidCvError` naming the reason, and leave any earlier CV untouched:
  - `test_store_cv_rejects_missing_file`
  - `test_store_cv_rejects_non_pdf` (a file starting `"""Do`)
  - `test_store_cv_rejects_oversize` (a sparse file one byte over the limit)

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_service.py` passes and the gate is green. Commit `feat(app): store the user's CV`.

### Task 4 — Sources operations

**Files:**
- Modify `packages/ijob-app/src/ijob_app/service.py`, `packages/ijob-app/src/ijob_app/cli.py`
- Test `packages/ijob-app/tests/ijob_app_tests/test_service.py`, `test_cli.py`

**Change:** the logic of `sources list`, `add`, `remove` and `add-url` moves to these `Service` methods:
- `sources() -> list[SourceRow]`.
- `async add_source(ref: SourceRef, company: str) -> SourceRow`. It refuses a ref that another company already holds, as `_refuse_other_company` does. Then it builds the row with `_new_row` and fetches once through `_verify` (`cli.py:238-253`, `verify_new_source`) before storing, as `sources add` does today (`cli.py:350`; module docstring `cli.py:25-26`).
- `remove_source(ref: SourceRef) -> None`.
- `async add_url(url: str, company: str) -> SourceRow`, which resolves and verifies as `sources add-url` does (`cli.py:376-398`).

They raise `InvalidValueError` subclasses or `SourcesError` instead of printing. The commands print and exit exactly as today.

**Proof:** written first, seen failing, then passing.
- `test_add_source_verifies_with_one_fetch` (`test_service.py`): with a `MockTransport` serving the board, `add_source` makes one board request, then `sources()` lists the source.
- `test_add_source_refuses_empty_board`: a board listing no jobs raises a `SourcesError` and stores nothing.
- `test_remove_source`: after `remove_source`, the source is hidden from `sources()`.
- `test_add_url_resolves_greenhouse`: a Greenhouse board URL served by `MockTransport` is added as a `greenhouse:` source.
- Every existing `sources` test in `test_cli.py` passes with its assertions unchanged, including `test_sources_add_verifies_identity`, `test_sources_add_refuses_empty_board` and `test_sources_add_fetch_failure_exits_1` (`test_cli.py:494,518,534`).

**Done when:** `uv run --locked pytest packages/ijob-app` passes and the gate is green. Commit `refactor(app): move source management into the service layer`.

---

## Risks

- **Tasks 1 and 4 are the riskiest steps.** Moving the pipeline can change stderr lines, exit codes or clock reads. The `test_cli.py` suite with unchanged assertions is the guard; the only edit allowed there is taking fixtures from `conftest.py`.
- SQLite connections are bound to their thread (`store.py:270`), so every caller must run service methods on the loop thread that opened the store. Plan 07 relies on this through async workers (spec §7 probe).
- `RefreshReport.rejected`, `Service.labels`, `CacheUnavailableError` and `open_service`'s `environ` parameter are recorded in the amended spec §5; they keep the CLI's problem reports and feed spec §7's label column.
- Not done here: new CLI commands (plan 06) and the TUI (plan 07).

## Proof

- `uv run --locked pytest packages/ijob-app` passes, with every assertion in pre-existing `test_cli.py` tests unchanged.
- The five gate commands in CLAUDE.md exit `0`.
- `grep -n "asyncio.run\|typer" packages/ijob-app/src/ijob_app/service.py` finds nothing: the service neither prints nor drives its own loop.

## Review findings

- risk — `sources` commands' `use_cache=False` unguarded — `test_cli.py` — fixed: blocked XDG_CACHE_HOME + `sources list` exits 0, no cache created
- risk — `store_cv` replaced cv.pdf before `set_cv`; a failed DB write left file and row out of step — `service.py` — fixed and verified: old bytes restored (or new file removed) when `set_cv` raises; a failed copy raises plain `OSError`, not `InvalidCvError`; `uv run --locked pytest test_cli.py test_service.py` → `133 passed in 1.22s`
- test-mandate — no service tests for `stale`, `refused`, first-error-kept — fixed (test_service 35 → 42)
- docstring-discipline — module docstring scope, `RefreshReport.due` wording — fixed
- test hygiene — `_SEEN` ordering; 20 MB CV size tests; `test_report_is_immutable` tested nothing of ours — fixed (MAX_CV_BYTES patched to 64; test removed)
- deferred nits — FIFO CV path; CV helpers in own module; three clock reads in `search`; blank-description `InvalidValueError` undocumented; `_error_text` UnknownSourceError branch; add-url refusal test
- spec — test_cli.py not moved to conftest fixtures (~25 lines duplicated) — declined: conversion would thread fixtures through ~90 tests; conftest serves test_service and the TUI tests

## Probe results

- Behaviour preservation: HEAD test_cli.py (unchanged assertions) against the frozen plan-05 cli.py → `62 passed` (reviewer run).
- Full gate after plan 05: `168 files already formatted`; `All checks passed!`; `Success: no issues found in 114 source files`; `1990 passed in 2.62s`.

## Deviations

- 2026-11-01 — Added `open_service(..., use_cache=True)`; `sources` commands pass False (they never opened the cache).
- 2026-11-01 — Added `SourceCompanyError` and `Service.posting_sources(posting_id)` (not in the plan) so the CLI keeps its messages without touching the store.
- 2026-11-01 — Test inputs changed: keep test uses `jobicy:engineering` (only kind with a refresh interval); filter test uses `companies`.
- 2026-11-01 — `kept` lines print after the fetch; final stderr order unchanged.
- 2026-11-01 — Per-task commits not made (CLAUDE.md).
