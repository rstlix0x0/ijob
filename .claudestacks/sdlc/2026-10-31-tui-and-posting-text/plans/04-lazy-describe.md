---
status: done
created: 2026-11-01
depends-on: [02, 03]
built-on: b175307f42cd
---

# Lazy Describe Implementation Plan

**Goal:** `ijob_sources.details.describe` returns a sighting's normalized description for every `SourceKind`, from its stored `raw` or, for Workable, YC and LetsGetRusty, from one polite fetch.

**Architecture:**
- New module `ijob_sources.details` with `async def describe(sighting: Sighting, client: PoliteClient) -> str | None` (spec §3.3).
- One `match` on `sighting.source.kind` with a `case` per `SourceKind` member (19, `ijob_core/model.py:37-55`), closed by `assert_never`, in the shape of `registry.py:72-110`. The 16 §2.1 kinds call the owning module's `description_from_raw(sighting.raw)` from plan 03 (the `feed` kind calls `rss.description_from_raw`) and make no request.
- Workable and YC each have a private fetch helper in `details.py`. Workable: `GET https://apply.workable.com/api/v2/accounts/{key}/jobs/{shortcode}` with `key` from `sighting.source.key` and `shortcode` from `sighting.external_id` (`workable.py:167`); joins `description`, then `## Requirements` + `requirements`, then `## Benefits` + `benefits`, each as HTML, skipping empty parts. YC: `GET` of `https://www.ycombinator.com` + the `url` site path in `raw` (`yc.py:31,165,174`); reads the page with `jsonld.job_postings` (`jsonld.py:114`) and normalizes the first JobPosting's `description` with DETECT.
- Errors are the existing `SourcesError` subclasses (`ijob_sources/errors.py:6-116`) raised by the client; a response or page without the field raises `ParseError`.

**Tech Stack:** Python 3.14, `PoliteClient` (`polite_http.py:94-166`), `ijob_sources.description` (plan 02), `ijob_sources.jsonld`, pytest with `httpx.MockTransport`.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/details.py                     — [create] describe and the Workable and YC fetch helpers
packages/ijob-sources/tests/ijob_sources_tests/test_details.py         — [create] tests for every kind group and both fetches
packages/ijob-sources/tests/ijob_sources_tests/fixtures/workable/huggingface_job.json — [create] trimmed v2 job response
packages/ijob-sources/tests/ijob_sources_tests/fixtures/yc/job_page.html             — [create] trimmed YC job page with its JSON-LD
```

Fixtures are captured once, live, through `PoliteClient` (spec §2.2 routes), and trimmed: the Workable response keeps `shortcode`, `title`, `description`, `requirements`, `benefits` with ~2,000 characters of text in total; the YC page keeps only the `<script type="application/ld+json">` block with a description cut to ~2,000 characters.

## Order of work

### Task 1 — `describe` from raw for the 16 payload kinds

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/details.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_details.py`

**Change:** `describe` with its full `match`. The 16 §2.1 cases return `<module>.description_from_raw(sighting.raw)`. The `workable`, `yc` and `letsgetrusty` cases call helpers that raise `NotImplementedError` in this task only, replaced in Tasks 2 and 3.

**Proof:** written first, seen failing, then passing —
- `test_describe_payload_kinds_use_raw` — parametrized over the 16 kinds, each with a `Sighting` whose `raw` is taken from that kind's fixture draft (plan 03); `describe` returns the draft's `description`, and the `MockTransport` handler records zero requests.
- `test_describe_feed_kind_reads_rss_raw` — a `feed` sighting whose raw is `rss.item_raw(...)` returns the item's normalized description.
- `test_describe_raw_without_field_gives_none` — a greenhouse sighting whose raw lacks `content` returns `None`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_details.py` passes and the gate in CLAUDE.md is green. Commit `feat(sources): describe postings from stored payloads`.

### Task 2 — Workable detail fetch

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/details.py`
- Create fixture `workable/huggingface_job.json`
- Test `test_details.py`

**Change:** the Workable helper as described in Architecture.

**Proof:** written first, seen failing, then passing — with a `PoliteClient` built as `test_workable.py:279-291` builds one (fake clock and sleep, robots.txt served by the handler):
- `test_describe_workable_fetches_v2_job` — the handler sees exactly one `GET /api/v2/accounts/huggingface/jobs/<shortcode>` (besides robots.txt), and the result holds the description text, then `## Requirements` and its text, then `## Benefits` and its text.
- `test_describe_workable_skips_empty_parts` — with `benefits` empty, no `## Benefits` heading appears.
- `test_describe_workable_without_description_raises` — a response with no `description` raises `ParseError`.
- `test_describe_workable_http_error_propagates` — a `404` raises `SourceNotFoundError`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_details.py` passes and the gate is green. Commit `feat(sources): fetch Workable descriptions on demand`.

### Task 3 — YC detail fetch

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/details.py`
- Create fixture `yc/job_page.html`
- Test `test_details.py`

**Change:** the YC helper as described in Architecture.

**Proof:** written first, seen failing, then passing —
- `test_describe_yc_fetches_job_page` — the handler sees one `GET` of the job page path taken from the sighting's raw `url`, and the result is the normalized JSON-LD description.
- `test_describe_yc_page_without_jobposting_raises` — a page with no JobPosting raises `ParseError`.
- `test_describe_yc_robots_disallow_propagates` — robots.txt disallowing `/companies/` raises `RobotsDisallowedError` and makes no page request.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_details.py` passes and the gate is green. Commit `feat(sources): fetch YC descriptions on demand`.

### Task 4 — LetsGetRusty detail fetch

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/details.py`
- Create fixture `packages/ijob-sources/tests/ijob_sources_tests/fixtures/letsgetrusty/job_page.html`
- Test `test_details.py`

**Change:** the `letsgetrusty` helper: `GET` of the item `url` held in the sighting's `raw` (`https://jobs.letsgetrusty.com?job=<id>`, spec §2.2); reads the page with `jsonld.job_postings` and normalizes the first JobPosting's `description` with MARKDOWN. Fixture trimmed from the live capture to its JSON-LD JobPosting block, description cut to ~2,000 characters.

**Proof:** written first, seen failing, then passing —
- `test_describe_letsgetrusty_fetches_job_page` — the handler sees one `GET` of the `?job=<id>` URL from raw, and the result is the normalized JSON-LD description.
- `test_describe_letsgetrusty_page_without_jobposting_raises` — raises `ParseError`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_details.py` passes and the gate is green. Commit `feat(sources): fetch LetsGetRusty descriptions on demand`.

---

## Risks

- **Import fan-in**: `details.py` imports 16 adapter modules. They import nothing from `details`, so no cycle forms (`references/modularity.md`); the gate's mypy run would surface one.
- Each opened Workable or YC posting costs one request, spaced by the client's existing per-host rules (`PoliteClient`, `polite_http.py:105`); nothing fetches in bulk.
- Not done: storing the result (plan 05 owns persistence), retries beyond `PoliteClient`'s own.

## Proof

- `uv run --locked pytest packages/ijob-sources` passes.
- The five gate commands in CLAUDE.md exit `0`.

## Review findings

- test-mandate — YC site-path check had no test for `@evil.test/x` or an absolute URL — `test_details.py` — fixed and verified: two cases added; `uv run --locked pytest -q packages/ijob-sources/tests/ijob_sources_tests/test_details.py` → `46 passed in 0.09s`
- modularity — `_json_object` duplicated `payload.raw_object` — `details.py` — fixed: raw read with `raw_object`; Workable body read by `_response_object` returning `dict[str, object]`; same run, ruff and mypy clean
- spec — plan Task 4 said DETECT while spec §2.2 says MARKDOWN — plan text corrected
- non-blocking nits declined for now — robots.txt request not mentioned in "one request" wording; no test of `quote` on Workable key/shortcode; LetsGetRusty host check ignores port/userinfo; no all-blank Workable test; shared Response object in tests; fixtures parsed at collection

## Probe results

- Fixtures from live captures (no fetch during execution): Workable v2 job `9E2A4C02C7` (description 2,733, requirements 2,239, benefits 1,884 chars before trim); YC job page duffel `Loght1V` (one JSON-LD JobPosting); LetsGetRusty `?job=recKZTPUl4654gtLM` (one JobPosting, 2,338 chars).
- LetsGetRusty description is Markdown (`**About Baton Corporation**`, `- ` bullets): DETECT escaped it — against the amended spec row as first written (DETECT); corrected to MARKDOWN, guarded by `test_describe_letsgetrusty_fetches_job_page`.

## Deviations

- 2026-11-01 — Task 4 (LetsGetRusty fetch) added after plan 03 disproved spec §2.1; user chose lazy fetch.
- 2026-11-01 — LetsGetRusty URL from raw must be `https` on `jobs.letsgetrusty.com` (mirrors `rss._detail_link`), else `ParseError`; not in the plan.
- 2026-11-01 — Per-task commits not made (CLAUDE.md: commit only when the user asks).
