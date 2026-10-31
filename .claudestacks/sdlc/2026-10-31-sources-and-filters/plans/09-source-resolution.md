---
status: approved
created: 2026-10-31
depends-on: [02, 06, 07, 08]
built-on: aeb01064881c
---

# Source Resolution Implementation Plan

**Goal:** Every sources-table row and every accepted manual URL becomes a verified, fetchable source.

**Architecture:**
- `ijob_sources.registry` is the one place that maps a `SourceKind` to its adapter class and its
  refresh interval.
- `ijob_sources.manual` adds the two manual-URL adapters, `jobposting:<h>` (a single JSON-LD job page)
  and `feed:<h>` (any RSS 2.0 feed), reusing plan 08's `jsonld` and `rss` readers.
- `ijob_sources.detect` resolves a pasted URL through the four steps of spec §6.5, and verifies a new
  source with one fetch before it is added.

**Tech Stack:** stdlib `hashlib`, `re`, `urllib.parse`, `datetime`, httpx `MockTransport`, pytest.

**Content authority:** spec §6.1 (rows `jsonld` and `feed`, manual-URL keys), §6.3 (refresh
interval), §6.4 (identity, add-time refusal), §6.5, §10.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/errors.py                 — [modify] UnsupportedUrlError
packages/ijob-sources/src/ijob_sources/manual.py                 — [create] JobPostingPage, FeedUrl, manual_key
packages/ijob-sources/src/ijob_sources/registry.py               — [create] build_source, refresh_interval
packages/ijob-sources/src/ijob_sources/detect.py                 — [create] Resolution, resolve_url, verify_new_source
packages/ijob-sources/tests/ijob_sources_tests/test_errors.py    — [modify]
packages/ijob-sources/tests/ijob_sources_tests/test_manual.py     — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_registry.py  — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_detect.py    — [create]
```

Tests reuse the fixtures of plans 06 and 08 by path. No new recorded body is needed.

## Order of work

### Task 1 — Fetch a manual JobPosting page or feed

**Files:** Modify `errors.py`; create `manual.py`; tests `test_errors.py`, `test_manual.py`.

**Change:**
- `UnsupportedUrlError(SourcesError)` carries `url` and `reason`.
- `manual_key(url) -> SourceKey` is the first 16 hex characters of the SHA-256 of the UTF-8 URL,
  passed through `parse_source_key` (spec §6.1).
- `JobPostingPage(key, *, target_url, company)`, ref `jobposting:<key>`:
  - one plain GET of `target_url`, then the pure `parse_jobposting_page(body, key, *, target_url,
    company) -> SourceBatch`;
  - the page's single `JobPosting` (spec §6.5) through `read_job_posting`;
  - `external_id` ← `identifier`, else `target_url`; `url` ← `url`, else `target_url`;
  - `raw` ← the JobPosting JSON;
  - `check_identity` (plan 06) with names `[company]` taken from the page;
  - zero or several `JobPosting`s → `ParseError`;
  - a page answering 410 surfaces as `HttpStatusError` from the client, as for any source.
- `FeedUrl(key, *, target_url, company)`, ref `feed:<key>`:
  - one conditional GET;
  - `parse_rss` items; `external_id` and `url` ← `link`; `title` ← `title`; `company` ← the row
    company; `posted_at` ← `pubDate`;
  - no identity check: a generic feed has no per-item company (spec §6.4).

**Proof:**
- `test_unsupported_url_error_fields`.
- `test_manual_key_is_16_hex_and_stable`.
- `test_jobposting_page_maps_fields`: the plan 08 JazzHR fixture yields one draft with salary, remote
  `COUNTRY`, and url.
- `test_jobposting_identity`: company `Eclipse Foundation` passes; `Acme` raises
  `IdentityMismatchError`.
- `test_page_without_jobposting_is_parse_error`.
- `test_feed_url_maps_items`: the plan 08 golang fixture, company from the row.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_manual.py
packages/ijob-sources/tests/ijob_sources_tests/test_errors.py` passes and the gate is green. Commit
`feat(sources): fetch manual job pages and feeds`.

### Task 2 — Build a source from a sources row

**Files:** Create `registry.py`; test `test_registry.py`.

**Change:**
- `build_source(row: SourceRow, *, known: frozenset[ExternalId] = frozenset()) -> Source` (`SourceRow`
  from plan 02) returns:

  | Kind | Source |
  |---|---|
  | `GREENHOUSE`, `GEM`, `ASHBY`, `LEVER`, `WORKABLE` | the plan 06 class, with `company=row.company` |
  | `HIMALAYAS` … `KALIBRR` | the plan 07 class, with the key |
  | `RSS` | `RssFeed(key, known=known)`; a key not in `FEEDS` raises `InvalidValueError` |
  | `YC`, `LETSGETRUSTY`, `HN` | the plan 08 class |
  | `JAPANDEV` | `JapanDev(key, known=known)` |
  | `JOBPOSTING`, `FEED` | the `manual` class; a row without `target_url` raises `InvalidValueError` |

  The match covers every `SourceKind` and closes with `assert_never` (python guideline, typing).
- `refresh_interval(kind) -> timedelta`: `JOBICY` is 1 hour (spec §6.3); every other kind is zero.
- `HOST_INTERVALS = {"apply.workable.com": 3.0}`, the intent's Workable rule (spec §3.2), for the CLI
  to pass to `PoliteClient`.

**Proof:**
- `test_every_kind_builds` (parametrised over `SourceKind`): a row of that kind (with a `target_url`
  for the manual kinds, a known `FEEDS` key for `RSS`) builds a source whose `ref` equals the row's.
- `test_hiring_system_gets_company`: a Greenhouse row's source checks identity against the row
  company. Fetching the plan 06 fixture with company `Ramp` raises `IdentityMismatchError`.
- `test_unknown_rss_key_rejected`.
- `test_exempt_sources_skip_identity` (spec §6.4 exemptions): a board source (Himalayas fixture,
  many companies) and a `feed:<h>` source both fetch successfully with a row company that appears
  nowhere in their data.
- `test_manual_kind_needs_target_url`.
- `test_refresh_interval`: Jobicy 1 h; Greenhouse 0.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_registry.py`
passes and the gate is green. Commit `feat(sources): build sources from the sources table`.

### Task 3 — Resolve and verify a manual URL

**Files:** Create `detect.py`; test `test_detect.py`.

**Change:** per spec §6.5.

- `Resolution` is a frozen dataclass `(ref: SourceRef, target_url: str | None, batch: SourceBatch |
  None)`. `batch` is the already-verified batch when the resolution fetched the page itself.
- `resolve_url(url, *, company, client) -> Resolution` tries, in order:
  1. **Hiring-system patterns** on the URL itself:
     `^https?://(boards|job-boards)\.greenhouse\.io/([^/?#]+)`, `^https?://jobs\.lever\.co/([^/?#]+)`,
     `^https?://jobs\.ashbyhq\.com/([^/?#]+)`, `^https?://apply\.workable\.com/([^/?#]+)` and
     `^https?://jobs\.gem\.com/([^/?#]+)`.
     - A Workable token of `j` or `api` does not match (spec §6.5: a job link names no account), so
       the URL goes on to step 2.
     - A match gives `<kind>:<token>` with `target_url=None` and `batch=None`; the token goes
       through `parse_source_key`.
  2. Otherwise **one plain `client.get(url)`**. When `job_postings` finds **exactly one**
     JobPosting, the result is `jobposting:<manual_key(url)>` with `target_url=url`. Its batch comes
     from that same body through `JobPostingPage`'s parser, identity check included (spec §6.5: no
     second fetch). Zero or several JobPostings go on to step 3.
  3. Otherwise, when `parse_rss` accepts the same body, `feed:<manual_key(url)>`, with the batch
     from that body.
  4. Otherwise `UnsupportedUrlError(url, "no hiring system, job posting or feed found")`.
- `verify_new_source(source, client) -> SourceBatch` is used only for step-1 resolutions and for
  `sources add`. It fetches once:
  - an empty batch raises `UnsupportedUrlError(str(source.ref), "no jobs; cannot verify the
    company")` (spec §6.4). The `Source` protocol exposes only `ref`, so the ref text stands in for
    the URL;
  - `IdentityMismatchError` propagates;
  - otherwise it returns the batch.
- Task 1's `JobPostingPage` exposes its parse as a pure function of the body (`parse_jobposting_page(body,
  key, *, target_url, company) -> SourceBatch`, identity check included), so step 2 can reuse it.

**Proof:**
- `test_hiring_system_urls` (parametrised): one URL per pattern, including an Ashby token with dots
  (`jobs.ashbyhq.com/mistral.ai`) → the expected ref, and no request sent.
- `test_workable_job_link_not_an_account`: `https://apply.workable.com/j/81B46579FE` is not resolved
  to `workable:j`; it is fetched (step 2).
- `test_jobposting_page_detected`: the JazzHR fixture served for an unknown host →
  `jobposting:<h>`, with a non-empty batch and exactly one page request.
- `test_listing_page_with_several_postings_not_a_jobposting`: a page with two JobPostings (the
  LetsGetRusty `ItemList` shape) is not resolved as `jobposting:<h>`.
- `test_feed_detected`: the golang feed fixture → `feed:<h>`.
- `test_unsupported_rejected`: an HTML page without JSON-LD → `UnsupportedUrlError`.
- `test_robots_applies`: a robots.txt disallowing the path → `RobotsDisallowedError`, with no page
  request.
- `test_verify_refuses_empty_board`: a Lever `[]` → `UnsupportedUrlError`.
- `test_verify_identity_mismatch_propagates`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_detect.py`
passes and the gate is green. Commit `feat(sources): resolve manual urls`.

---

## Risks

- **Most risky: Task 2's exhaustiveness.** A `SourceKind` added later without a registry branch must
  fail type checking, not at run time; `assert_never` makes mypy enforce it.
- Step 1 never fetches, so a typo'd token is only caught by `verify_new_source`. The CLI (plan 10)
  always verifies before adding.
- Not done here: the CLI commands and the sources table writes (plan 10).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-sources` passes.
