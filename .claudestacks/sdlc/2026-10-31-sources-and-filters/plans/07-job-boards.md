---
status: approved
created: 2026-10-31
depends-on: [01, 04]
built-on: aeb01064881c
---

# Job Boards Implementation Plan

**Goal:** The seven job-board APIs are collected as sighting drafts.

**Architecture:**
- One module per board, each with a source class that satisfies the C1 `Source` protocol and a pure
  `parse_*` function per page.
- Paging loops stay inside each adapter (walking-skeleton spec §1 premise 1), capped at 5 pages
  (spec §6.3). The first page is requested with `conditional=True` (plan 04); later pages are not.
- Boards list many companies, so they carry no identity check (spec §6.4) and their constructors take
  only the key.

**Tech Stack:** stdlib `json`, `decimal`, `datetime`, `logging`, httpx `MockTransport`, pytest.

**Content authority:** spec §2.2, §5.1 (when a `Money` is built), §6.1 (rows `himalayas` …
`kalibrr`), §6.3.

**Fixture sources:** the bodies fetched for spec §2.2, saved under
`/private/tmp/claude-501/-Users-hiraq-Projects-rstlix0x0-ijob/97b526e9-32f9-40fa-acfd-bc20d7a8e436/scratchpad/c2-grounding/boards/`:

| Board | Saved bodies |
|---|---|
| Himalayas | `himalayas.p1.json`, `himalayas.p2.json` |
| Jobicy | `jobicy.p1.json`, `jobicy.p2.json` |
| RemoteOK | `remoteok.json` |
| Arbeitnow | `arbeitnow.p1.json`, `arbeitnow.p2.json` |
| 4dayweek | `4dw.v2.json`, `4dw.p2.json` |
| MyCareersFuture | `mcf.p0.json`, `mcf.p1.json` |
| Kalibrr | `kalibrr.p0.json`, `kalibrr.p1.json` |

Each fixture keeps 2–3 items per page, with the paging fields edited so a two-page walk ends, and with
descriptions shortened.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/himalayas.py          — [create] HimalayasBoard, parse_himalayas
packages/ijob-sources/src/ijob_sources/jobicy.py             — [create] JobicyBoard, parse_jobicy
packages/ijob-sources/src/ijob_sources/remoteok.py           — [create] RemoteOkBoard, parse_remoteok
packages/ijob-sources/src/ijob_sources/arbeitnow.py          — [create] ArbeitnowBoard, parse_arbeitnow
packages/ijob-sources/src/ijob_sources/fourdayweek.py        — [create] FourDayWeekBoard, parse_fourdayweek
packages/ijob-sources/src/ijob_sources/mycareersfuture.py    — [create] MyCareersFutureBoard, parse_mycareersfuture
packages/ijob-sources/src/ijob_sources/kalibrr.py            — [create] KalibrrBoard, parse_kalibrr
packages/ijob-sources/tests/ijob_sources_tests/fixtures/himalayas/{__init__.py,page1.json,page2.json}         — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/jobicy/{__init__.py,page1.json,page2.json}            — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/remoteok/{__init__.py,api.json}                      — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/arbeitnow/{__init__.py,page1.json,page2.json}         — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/fourdayweek/{__init__.py,page1.json,page2.json}       — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/mycareersfuture/{__init__.py,page0.json,page1.json}   — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/kalibrr/{__init__.py,page0.json,page1.json}           — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_himalayas.py        — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_jobicy.py           — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_remoteok.py         — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_arbeitnow.py        — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_fourdayweek.py      — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_mycareersfuture.py  — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_kalibrr.py          — [create]
```

The 4dayweek module and fixture directory are named `fourdayweek`, not spec §11's `<kind>` value
`4dayweek`, because a Python package name cannot start with a digit.

Every task follows the same shape:
- The class takes `key: SourceKey`, and its `ref` is the spec §6.1 ref.
- `fetch(client)` walks the pages and calls the pure parser on each.
- C1's per-job rejection contract applies: a job that fails validation is logged at `WARNING`,
  counted and skipped. A page that is not the expected JSON raises `ParseError`.
- `raw` is the item re-serialised as JSON.
- An integer id (Jobicy and Kalibrr, spec §2.5a) becomes its decimal text for `ExternalId`.
- A salary is a `Money` only when the currency and a positive bound are present. A period the task
  does not map gives `period=None` (spec §5.1).

## Order of work

### Task 1 — Collect Himalayas

**Files:** `himalayas.py`, `fixtures/himalayas/{__init__.py,page1.json,page2.json}`, `test_himalayas.py`.

**Change:**
- `himalayas:all`.
- Pages: `https://himalayas.app/jobs/api?limit=20`, then `…&cursor=<nextCursor>` while `nextCursor`
  is set, at most 5.
- Mapping (spec §6.1):
  - `guid` → id; `applicationLink` → url; `title`; `companyName`;
  - location: `locationRestrictions` joined by `", "`, or `None` when empty;
  - one restriction → that country and `COUNTRY`; `[]` → `WORLDWIDE`; several → `REGION`;
  - salary: `minSalary`/`maxSalary`/`currency`, period `annual`→`YEAR`, `monthly`→`MONTH`,
    `hourly`→`HOUR`;
  - `pubDate`: epoch seconds.

**Proof:**
- `test_parse_maps_fields`: Sri Lanka job → country `LK`, scope `COUNTRY`.
- `test_worldwide_and_region`: `[]` → `WORLDWIDE`; two countries → `REGION`.
- `test_salary_periods` (parametrised): annual, monthly, hourly.
- `test_period_without_amount_has_no_salary`: `annual` with null amounts → `salaries == ()`, job
  kept (spec §2.2: 22 of 40).
- `test_paging_follows_cursor_and_stops`: two pages, then no cursor → 2 requests.
- `test_paging_capped_at_five`: a transport that always returns a cursor → 5 requests.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_himalayas.py`
passes and the gate is green. Commit `feat(sources): collect himalayas`.

### Task 2 — Collect Jobicy

**Files:** `jobicy.py`, `fixtures/jobicy/{__init__.py,page1.json,page2.json}`, `test_jobicy.py`.

**Change:**
- `jobicy:engineering`.
- Pages: `https://jobicy.com/api/v2/remote-jobs?count=50&industry=engineering`, then `…&cursor=<nextCursor>`
  while `hasMore`, at most 5.
- Mapping:
  - `id` → id; `url` → url (the Jobicy page, per its terms); `jobTitle`; `companyName`;
  - location `jobGeo`; country `jobGeo` through `parse_country`;
  - salary: `salaryMin`/`salaryMax`/`salaryCurrency`, `yearly`→`YEAR`, `hourly`→`HOUR`;
  - `pubDate`: ISO.

**Proof:**
- `test_parse_maps_fields`: TeamSnap job, `USA` → `US`, `150000 USD YEAR` with `max None`.
- `test_multi_country_geo_takes_last_part`: `"Canada,  USA"` → country `US`, from `parse_country`'s
  last-part rule (plan 01 Task 2). Remote stays `None`, so the country filter still passes the job
  (spec §8).
- `test_paging_until_has_more_false`.
- `test_paging_capped_at_five`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_jobicy.py`
passes and the gate is green. Commit `feat(sources): collect jobicy`.

### Task 3 — Collect RemoteOK

**Files:** `remoteok.py`, `fixtures/remoteok/{__init__.py,api.json}` (the notice element plus 2
jobs), `test_remoteok.py`.

**Change:**
- `remoteok:all`.
- One conditional GET of `https://remoteok.com/api`.
- Skip element `[0]`, the legal notice.
- Mapping:
  - `id` → id; `url` → url; `position` → title; `company`;
  - `location`, with `""` → `None`;
  - `epoch`: seconds;
  - no salary (spec §2.2: no currency).

**Proof:**
- `test_notice_element_skipped`: the notice is neither a draft nor a rejection.
- `test_parse_maps_fields`: the `Front End Developer` job; its URL keeps `remoteOK.com` as given
  (canonicalising is the store's job).
- `test_empty_location_is_none`.
- `test_salary_never_mapped`: `salary_min 80000` → `salaries == ()`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_remoteok.py`
passes and the gate is green. Commit `feat(sources): collect remoteok`.

### Task 4 — Collect Arbeitnow

**Files:** `arbeitnow.py`, `fixtures/arbeitnow/{__init__.py,page1.json,page2.json}`, `test_arbeitnow.py`.

**Change:**
- `arbeitnow:all`.
- Pages: `https://www.arbeitnow.com/api/job-board-api`, then `links.next` while it is set, at most 5.
- Mapping:
  - `slug` → id; `url`; `title`; `company_name`; `location`;
  - `remote` false→`NONE`, true→`None`;
  - `created_at`: epoch seconds;
  - no salary, no country.

**Proof:**
- `test_parse_maps_fields`: the BringLiesel job → title `Senior Key Account Manager`, company
  `BringLiesel GmbH`, location `Berlin`, remote `None` (`remote: true`), and
  `posted_at` = `1793397632` as UTC.
- `test_remote_flag` (parametrised): `false` → `NONE`; `true` → `None`.
- `test_paging_follows_links_next`.
- `test_paging_capped_at_five`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_arbeitnow.py`
passes and the gate is green. Commit `feat(sources): collect arbeitnow`.

### Task 5 — Collect 4dayweek

**Files:** `fourdayweek.py`, `fixtures/fourdayweek/{__init__.py,page1.json,page2.json}`,
`test_fourdayweek.py`.

**Change:**
- `4dayweek:engineering`.
- Pages: `https://4dayweek.io/api/v2/jobs?category=engineering&limit=100`, then `…&page=<response
  page + 1>` while `has_more`, at most 5. The next page number comes from the response's own `page`
  field, not from an assumed starting index.
- Mapping:
  - `id` → id; `url`; `title`; `company.name`;
  - location: the first `locations[]` as `"city, country"`; country: its `country` through
    `parse_country`;
  - `work_arrangement` `onsite`/`hybrid` → `NONE`, `remote` → `None`;
  - `posted_at`;
  - no salary (spec §2.2: units undocumented).

**Proof:**
- `test_parse_maps_fields`: the Costa Mesa job → `"Costa Mesa, United States"`, `US`, `NONE`.
- `test_salary_never_mapped`.
- `test_paging_uses_response_page`: page 1 answers `page: 1, has_more: true` → the next URL has
  `page=2`.
- `test_paging_capped_at_five`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_fourdayweek.py`
passes and the gate is green. Commit `feat(sources): collect 4dayweek`.

### Task 6 — Collect MyCareersFuture

**Files:** `mycareersfuture.py`, `fixtures/mycareersfuture/{__init__.py,page0.json,page1.json}`,
`test_mycareersfuture.py`.

**Change:**
- `mycareersfuture:software-engineer`.
- Pages: `https://api.mycareersfuture.gov.sg/v2/jobs?search=software%20engineer&limit=100&page=0&sortBy=new_posting_date`,
  then `_links.next.href` as given while it is set, at most 5.
- Mapping:
  - `uuid` → id; `metadata.jobDetailsUrl` → url; `title`; `postedCompany.name`;
  - country `SG` when `address.isOverseas` is false;
  - `metadata.newPostingDate` at 00:00 UTC;
  - no salary (spec §2.2: no currency field).

**Proof:**
- `test_parse_maps_fields`: the Scientec job, country `SG`.
- `test_overseas_has_no_country`: `isOverseas: true` → `None`.
- `test_paging_follows_next_href`.
- `test_paging_capped_at_five`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_mycareersfuture.py`
passes and the gate is green. Commit `feat(sources): collect mycareersfuture`.

### Task 7 — Collect Kalibrr

**Files:** `kalibrr.py`, `fixtures/kalibrr/{__init__.py,page0.json,page1.json}` (page 1 repeats one
id from page 0), `test_kalibrr.py`.

**Change:**
- `kalibrr:software-engineer`.
- Pages: `https://www.kalibrr.com/kjs/job_board/search?limit=15&offset=<n>&text=software%20engineer`
  from `offset=0`, adding 15 while `offset < count`, at most 5.
- An id already seen in this fetch is dropped, and is neither a draft nor a rejection.
- Mapping:
  - `id` → id; url `https://www.kalibrr.com/c/{company.code}/jobs/{id}/{slug}`; `name` → title;
    `company_name`;
  - location `google_location.address_components.city`; country `…country` through
    `parse_country`;
  - when `salary_shown`: `salary_currency` with `base_salary`/`maximum_salary`, `month` → `MONTH`;
  - `activation_date`.

**Proof:**
- `test_parse_maps_fields`: the `vlink-inc` job URL equals
  `https://www.kalibrr.com/c/vlink-inc/jobs/274683/software-engineer-2`.
- `test_salary_shown_respected`: `salary_shown: false` with numbers → no salary (id 275079's
  shape).
- `test_shown_but_empty_has_no_salary`.
- `test_repeated_id_dropped`.
- `test_paging_by_offset_and_cap`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_kalibrr.py`
passes and the gate is green. Commit `feat(sources): collect kalibrr`.

---

## Risks

- **Most risky: paging loops.** A wrong stop condition fetches far more than intended: 4dayweek
  reports 6,595 jobs, and Arbeitnow serves 2.7 MB pages. Each task tests both the natural stop and
  the 5-page cap.
- Jobicy's terms allow one poll per hour. The adapter does not enforce that; plan 10 enforces plan 09's
  `refresh_interval`, so the adapter must not be called in loops by tests that touch a real transport
  (none do).
- The fixture bodies live in a session scratch directory. If it is gone, re-fetch each spec §2.2 URL
  once with ijob's User-Agent and trim again.
- Not done here: registering these sources and their seed rows (plans 09 and 10).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-sources` passes.
