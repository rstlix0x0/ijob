---
status: approved
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
