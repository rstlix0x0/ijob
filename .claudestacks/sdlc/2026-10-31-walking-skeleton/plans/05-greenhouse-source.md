---
status: done
created: 2026-10-31
depends-on: [02, 04]
built-on: aafd5e32e8cd
---

# Greenhouse Source Implementation Plan

**Goal:** Greenhouse boards are collected into sighting drafts through the documented `Source`
protocol.

**Architecture:** `ijob_sources.collect` owns the adapter contract — the `Source` protocol, the
`SourceBatch` an adapter returns, and `collect`, which runs every source concurrently and isolates
failures. `ijob_sources.greenhouse` is the first implementation: one request through `PoliteClient`
and a pure `parse_jobs`. The contract is documented where an adapter author looks: the `collect`
module docstring and the package README.

**Tech Stack:** `asyncio.TaskGroup`, `typing.Protocol`, stdlib `json`, `logging`, httpx
`MockTransport`, pytest.

**Content authority:** spec §1 premise 1, §6, §7.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/collect.py                                 — [create] Source, SourceBatch, SourceFailure, CollectResult, collect
packages/ijob-sources/src/ijob_sources/greenhouse.py                              — [create] GreenhouseBoard, parse_jobs
packages/ijob-sources/README.md                                                   — [modify] "Adding a source" section
packages/ijob-sources/tests/ijob_sources_tests/fixtures/__init__.py               — [create] empty; every test-tree directory carries one (test-mandate)
packages/ijob-sources/tests/ijob_sources_tests/fixtures/greenhouse/__init__.py    — [create] empty, same reason
packages/ijob-sources/tests/ijob_sources_tests/fixtures/greenhouse/tailscale_jobs.json — [create] trimmed recorded response
packages/ijob-sources/tests/ijob_sources_tests/test_collect.py                    — [create] tests for collect
packages/ijob-sources/tests/ijob_sources_tests/test_greenhouse.py                  — [create] tests for greenhouse
```

## Order of work

### Task 1 — Define the source contract and collect concurrently

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/collect.py`.
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_collect.py`.

**Change:**
- `Source` — `Protocol` with a `ref: SourceRef` property and `async fetch(client: PoliteClient) ->
  SourceBatch`.
- `SourceBatch` — frozen slotted dataclass `(drafts: list[SightingDraft], rejected: int)`.
- `SourceFailure` — frozen slotted dataclass `(ref: SourceRef, error: SourcesError)`.
- `CollectResult` — frozen slotted dataclass `(batches: dict[SourceRef, SourceBatch], failures:
  list[SourceFailure])`.
- `async collect(sources, client) -> CollectResult` — one task per source in an `asyncio.TaskGroup`;
  each task catches `SourcesError` only and records a `SourceFailure`.
- The module docstring states the adapter contract from spec §6.1: build each draft inside the parse
  function; catch `InvalidValueError` per job, log it at `WARNING` with the job's id and reason, count
  it in `rejected`, keep going; raise `ParseError` for an unreadable body; let no other exception
  leave `fetch` except a client `SourcesError`.

**Proof:** written first, seen failing, then passing — fake sources implement `Source` without
importing it:
- `test_collect_returns_each_source_batch` — two fake sources returning batches give both, keyed by
  ref, and no failures.
- `test_failing_source_does_not_cancel_others` — one fake raising `SourceNotFoundError` gives a
  `SourceFailure` carrying that ref and error, while the other source's batch is returned.
- `test_non_sources_error_propagates` — a fake raising `RuntimeError` makes `collect` raise (an
  `ExceptionGroup` containing it), showing that the isolation is limited to `SourcesError`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_collect.py`
passes and the gate is green. Commit `feat(sources): define the source contract and collect
concurrently`.

### Task 2 — Parse Greenhouse board responses

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/greenhouse.py` — `parse_jobs`.
- Create `packages/ijob-sources/tests/ijob_sources_tests/fixtures/__init__.py` and
  `.../fixtures/greenhouse/__init__.py` — empty.
- Create `packages/ijob-sources/tests/ijob_sources_tests/fixtures/greenhouse/tailscale_jobs.json` —
  the response recorded during design at
  `/private/tmp/claude-501/-Users-hiraq-Projects-rstlix0x0-ijob/97b526e9-32f9-40fa-acfd-bc20d7a8e436/scratchpad/c1-ground/gh_tailscale_jobs.json`,
  trimmed to its first two jobs with each `content` shortened to a short string and `meta.total` set
  to 2. If that file is gone, record it once with
  `curl -sS -A 'ijob/0.1.0 (+https://github.com/rstlix0x0/ijob)' 'https://boards-api.greenhouse.io/v1/boards/tailscale/jobs?content=true'`
  and trim the same way (never from a test).
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_greenhouse.py`.

**Change:** `parse_jobs(body: bytes, key: SourceKey) -> SourceBatch` is pure. It reads the JSON
object's `jobs` list and maps each job per spec §6.2: `id` (as text) → `external_id`; `title`;
`company_name`, or the board key when absent or empty → `company`; `location.name`, or `None` →
`location`; `absolute_url` → `url`; `first_published` through `datetime.fromisoformat`, or `None` →
`posted_at`; the job object re-serialised with `json.dumps` → `raw`. A job is rejected — logged at
`WARNING` with its `id` when it has one, otherwise its index in `jobs`, plus the reason, and counted
in `rejected` — when it is not a JSON object; misses `id`, `title` or `absolute_url`; has a
`location` that is neither an object nor absent; has a `first_published` that
`datetime.fromisoformat` cannot read; or fails `SightingDraft` validation. These per-job rejections
keep spec §6.1's rule that no exception other than a `SourcesError` leaves `fetch`; spec §6.2 names
the first and last cases, and the middle ones are the same rule applied to the other fields this
mapping reads. A body that is not JSON, or has no `jobs` list, raises `ParseError` (`from` the decode
error where there is one).

**Proof:** written first, seen failing, then passing —
- `test_parse_fixture_maps_first_job` — on the fixture, the first draft's fields equal the fixture's
  first job mapped as above; with the design recording these are `4721252005`,
  `'Analytics Engineer, Data'`, `'Tailscale'`, `'Remote (Canada)'`,
  `'https://job-boards.greenhouse.io/tailscale/jobs/4721252005'`, and
  `2026-08-04T14:21:41-04:00` as an aware datetime (spec §6.2).
- `test_parse_fixture_returns_two_drafts_none_rejected` — two drafts, `rejected == 0`.
- `test_company_falls_back_to_board_key` — a job with no `company_name`, and one with `""`, get the
  board key.
- `test_absent_location_and_first_published_are_none`.
- `test_invalid_job_is_rejected_and_others_kept` (parametrised: empty `title`, non-http
  `absolute_url`, missing `id`, a job that is a string, `location` that is a string,
  `first_published` of `"yesterday"`) — one draft survives, `rejected == 1`, and a `WARNING` record
  (pytest `caplog`) names the job by `id`, or by index for the missing-`id` and string cases.
- `test_non_json_body_raises_parse_error`, `test_missing_jobs_list_raises_parse_error`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_greenhouse.py`
passes and the gate is green. Commit `feat(sources): parse greenhouse board responses`.

### Task 3 — Fetch a Greenhouse board

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/greenhouse.py` — add `GreenhouseBoard` (the module
  created in Task 2).
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_greenhouse.py` (the file created in
  Task 2).

**Change:** `GreenhouseBoard(key: SourceKey)` implements `Source` without importing it. `ref` is
`SourceRef(SourceKind.GREENHOUSE, key)`. `fetch(client)` calls
`client.get("https://boards-api.greenhouse.io/v1/boards/<key>/jobs?content=true")` and returns
`parse_jobs(response.content, key)`. Client errors (`SourceNotFoundError` for an unknown board,
spec §6.2) pass through.

**Proof:** written first, seen failing, then passing — a `PoliteClient` (plan 04,
`ijob_sources.polite_http`) over `httpx.MockTransport` that serves `User-agent: *` /
`Disallow: /embed/` for `/robots.txt` (the live file, spec §5.2), built with a fake clock and a fake
sleep that advances it, so no test really sleeps (spec §9):
- `test_fetch_requests_board_url_with_content` — the transport sees exactly
  `/v1/boards/tailscale/jobs?content=true` on `boards-api.greenhouse.io`, and the batch equals
  `parse_jobs` of the fixture.
- `test_fetch_unknown_board_raises_source_not_found` — a 404 with body
  `{"status":404,"error":"Job not found"}` raises `SourceNotFoundError`.
- `test_greenhouse_board_satisfies_source_protocol` — a `GreenhouseBoard` is accepted by `collect`
  (type-checked by mypy in the gate, exercised once at runtime).

**Done when:** `uv run --locked pytest packages/ijob-sources` passes and the gate is green. Commit
`feat(sources): fetch greenhouse boards`.

### Task 4 — Document how to add a source

**Files:**
- Modify `packages/ijob-sources/README.md` — replace "(planned; the package is empty today)" with the
  current state, and add an "Adding a source" section.

**Change:** the section lists, in order: implement `Source` (`ref`, `fetch`) in a new module; add the
`SourceKind` member in `ijob_core.model` (an extension, spec §3); make every request through
`PoliteClient.get`; keep parsing in a pure function returning `SourceBatch`; reject invalid jobs per
the contract in `ijob_sources.collect`; add a trimmed recorded fixture under
`tests/ijob_sources_tests/fixtures/<kind>/`; never call the network from a test.

**Proof:** documentation only; no test. Reviewed against spec §6.1.

**Done when:** `grep -n "## Adding a source" packages/ijob-sources/README.md` prints one line, the
README no longer says "planned", and the gate is green. Commit
`docs(sources): document how to add a source`.

---

## Risks

- **Most risky step: the fixture.** The live board changes; the design recording pins the asserted
  values, but it sits in a session scratchpad that disappears with the session. Execution of this
  plan copies it first, before any other work, while the file exists. If it has to be re-recorded,
  the first-job test asserts whatever the new first job holds (the mapping, not the specific
  posting), and the deviation is noted.
- The fixture is public job-board data, not personal data, so committing it is allowed by CLAUDE.md.
- `test_non_sources_error_propagates` documents a deliberate limit: an adapter bug crashes the run
  instead of being hidden. That is spec §7's narrow isolation, not an oversight.
- Not done: Gem, other adapters, Greenhouse pay ranges and the EU host (spec §10).

## Proof

- `uv run --locked pytest packages/ijob-sources` passes; the five-command gate is green.
- `grep -rn "boards-api.greenhouse.io" packages/ijob-sources/tests` shows only `MockTransport` routes
  and fixture data, never a real client.

## Review findings

- spec §6.1 contract — `raw=json.dumps(job)` could raise `RecursionError` for a job `json.loads` decoded but cannot re-encode (reviewer reproduced it with ~57k nested `[`), escaping `fetch` and crashing `collect` — `greenhouse.py:132` (fixed and verified: `_raw` turns it into a per-job `InvalidValueError`; `test_job_that_cannot_be_re_encoded_is_rejected` red before, green after)
- test-mandate — `RecursionError` at decode unguarded — `greenhouse.py:81` (fixed: `test_deeply_nested_body_raises_parse_error`, asserts the cause)
- downstream fitness — a lone surrogate (`"\ud800"`) in id, title, company, url or location passed `parse_jobs` and would make `Store.record` raise `UnicodeEncodeError`, losing every source in plan 06's single `record` — `greenhouse.py:146-157` (fixed and verified: `_encodable` rejects it; `test_text_that_is_not_utf8_encodable_is_rejected` ×5 red before, green after)
- security — a board's string `id` reached the log raw (`"a\nFAKE LOG"` forged a line) — `greenhouse.py:97-110` (fixed and verified: label uses `repr`; `test_job_id_is_logged_without_control_characters` red before, green after)
- modularity — `_job_label` duplicated `_external_id`'s id rule (fixed: one `_raw_id` helper)
- docstring — float id reported as "not text or a number" (fixed: "not text or an integer"); bad `location.name` reported as field `name` (fixed: `location.name`)
- test-mandate — null-as-absent and the logged reason unguarded (fixed: `test_null_fields_are_treated_as_absent`; rejection test asserts `invalid` in the message)
- modularity — duplicate refs are fetched twice and `batches[ref]` keeps the last; failure order is completion order — `collect.py:111` (not fixed here; plan 06 dedupes keys before building boards)
- `json` NaN/Infinity round-trip into `raw`; empty `location.name` kept as `""` (nits, not fixed)
- Reviewer gate re-run before fixes: all five exit 0, `145 passed`; adversarial bodies (empty, non-UTF-8, top-level list, `jobs` string/null, >4300-digit ints, 100k-deep nesting, null jobs, bad dates) all ended as `ParseError` or per-job rejection.

## Probe results

- Fixture — copied from the design recording before any other work (`.airsstack/gh_tailscale_jobs.recording.json`, 694,940 bytes, `meta` `{'total': 52}`), trimmed to the first two jobs, `content` shortened, `meta.total` 2 (3,452 bytes); first job read back as `4721252005`, `'Analytics Engineer, Data'`, `'Tailscale'`, `{'name': 'Remote (Canada)'}`, `https://job-boards.greenhouse.io/tailscale/jobs/4721252005`, `2026-08-04T14:21:41-04:00`.
- Final gate after the fix round — `ruff check --no-fix` → `All checks passed!`; `mypy --strict .` → `Success: no issues found in 30 source files`; `pytest -q` → `154 passed in 0.21s`.

## Deviations

- 2026-10-31 — spec §6.2 says `None` "when absent"; built: JSON `null` counts as absent for `company_name`, `location`, `location.name` and `first_published` (tested).
- 2026-10-31 — rejections beyond the plan's list, under its "same rule applied to the other fields": non-text title, url, company_name, location.name or first_published; list, bool or float id; text not encodable as UTF-8; a job too deeply nested to re-encode.
- 2026-10-31 — all four tasks built by one coder spawn; the fix round ran inline; per-task commits not made, commits are the user's act (CLAUDE.md).
