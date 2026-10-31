---
status: approved
created: 2026-10-31
built-on: 8db7fea17909
---

# Spec: Sources and filters

This spec takes ijob from one hand-named Greenhouse board to the milestone ijob-v1/02 source set. It
covers every Tier A and Tier B source the intent names, the verified seed companies and the user's own
sources and manual URLs. Postings are deduplicated across sources and re-fetched with conditional
requests, and the printed listing can be narrowed with deterministic filters. It builds on the walking
skeleton's model, store, polite client and `Source` protocol.

## 1. Design premises

1. **Extend the C1 contracts; change only what is listed here.** The adapter protocol (`Source.ref`,
   `Source.fetch(client) -> SourceBatch`), the per-job rejection rule, `SightingDraft`'s existing
   fields and the store's existing operations keep their meaning (walking-skeleton spec §3, §4, §6.1).
   This chain makes these changes to C1 code:

   | C1 element | Change |
   |---|---|
   | `SourceKind` | new members (§5.1) |
   | `SightingDraft`, `Posting`, `SourceBatch`, `RecordResult` | new fields with defaults (§4, §5.1) |
   | store schema | migration 2 (§4) |
   | `Store.record` | gains the keyword `still_listed=()` and cross-source matching (§4) |
   | `Store` | new methods (§4) |
   | `PoliteClient` | new keywords `cache=None` and `host_intervals={}`; `get` gains the keyword `conditional=False` (§3) |
   | `RobotsPolicy` | new `crawl_delay(url) -> float \| None` (§3.2) |
   | `GreenhouseBoard` | new keyword `company`; its request adds `&pay_transparency=true`; it runs the identity check (§6.1, §6.4) |
   | C1 test | `test_greenhouse.py:249-257` (`test_fetch_requests_board_url_with_content`) asserts the old URL exactly and changes with it |
   | CLI | `--greenhouse` is replaced by `--source` (§9); `test_cli.py` changes with it |

2. **One sources table.** Seeds and user sources live in one table of the store (§4). The bundled seed
   list is imported into it once and idempotently, and the user adds and removes rows from there. A
   removed seed stays removed. `ijob search` reads every row that is not removed.
3. **The store keeps everything; filters only narrow what is printed.** Every valid draft is recorded.
   The filters (§8) are pure functions over postings, applied at listing time, and keep a posting
   whenever the data cannot decide.
4. **A listing shows what is open now.**
   - A posting is printed when some source's latest successful fetch saw it.
   - A failed source, or one skipped inside its refresh interval, keeps showing its last-known
     postings for 7 days after its last successful fetch. After that they are hidden and stderr says
     so (§9).
   - This replaces C1's rule of listing only the sources that succeeded in this run (walking-skeleton
     spec §8 step 3).
5. **Detail requests only for new jobs.** JapanDev, elixirjobs and djinni lack company or job fields
   in their index (§2.3, §2.5). They fetch a detail page only for jobs the store has not seen, at most
   100 per fetch, and report the jobs already known as still listed (§4). A full re-crawl is never
   repeated.
6. **Evidence.** Gathered on 2026-10-31.
   - "Fetched" means one `curl -sS` request with
     `User-Agent: ijob/0.1.0 (+https://github.com/rstlix0x0/ijob)`, ijob's own header
     (`packages/ijob-sources/src/ijob_sources/polite_http.py:30-33`; `ijob-sources` is `0.1.0`,
     `packages/ijob-sources/pyproject.toml:3`). It was sent after that host's `/robots.txt` had been
     read and judged with longest-match semantics, at most one request per second per host.
   - "Probe" means a throwaway script run with `uv run --locked python -I <probe>.py` on CPython
     3.14.10, over live responses or over the bodies saved from the fetches.
   - Quoted bodies are trimmed by hand.

## 2. What the sources actually return

Every row below was fetched on 2026-10-31. "304" means the same GET, sent again with the returned
validator, answered `304 Not Modified`. The title and location keys are those of the first item, as
printed by a probe over the saved body (for example `lever: n=89 text='Account Executive - Commercial,
Spotify Technology' categories.location='New York, NY'`).

### 2.1 Hiring systems

| System | Endpoint | robots.txt (deciding line) | Shape | Validators |
|---|---|---|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true&pay_transparency=true` | `User-agent: *` / `Disallow: /embed/` | Envelope `{"jobs":[…],"meta":{"total":N}}`; `tailscale`: 200, 782,422 bytes, 58 jobs. Title `title`, location `location.name` (`'Remote (Canada)'`). All 58 carry `content` and `pay_input_ranges`; 57 have at least one range, in currencies `['CAD','GBP','USD']`. First range: `{'min_cents': 20368000, 'max_cents': 25460000, 'currency_type': 'CAD', 'title': 'CAN Pay Range', 'blurb': ''}`, with no period field. `offices[].location` of the first job: `['Canada', 'United States']`. | `etag: W/"d5eef65f62bd864b901e8a635936c2f0"` → `304` |
| Gem | `https://api.gem.com/job_board/v0/{t}/job_posts/` | `/robots.txt` answered `403 {"message":"Forbidden"}` (§3.3) | A bare JSON array; `retool`: 200, 430,758 bytes, 26 items. Keys: `absolute_url, content, content_plain, created_at, departments, employment_type, first_published_at, id, internal_job_id, location, location_type, offices, requisition_id, title, updated_at`. Title `title`, location `location.name` (`'San Francisco, United States'`). No `company_name`. `id` is a string (`"4003629005"`; on `groq` an opaque `"am9icG9zdDqAUz7G7JlGLHQawWv7Sec2"`). `location_type` ∈ {`hybrid`, `in_office`, `remote`}. Unknown slug: 404 JSON. | none sent |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true` | `/robots.txt` answered `401 Unauthorized` (§3.3) | `{"apiVersion":"1","jobs":[…]}`; `ramp`: 200, 3,078,539 bytes, 171 jobs. Title `title`, which can lead with a space (`' Security Engineer, Cloud'`). Location `location` (`'New York, NY (HQ)'`). No organisation name. A salary component: `{"compensationType":"Salary","interval":"1 YEAR","currencyCode":"USD","minValue":211400,"maxValue":290600}`, in whole units. Salary intervals over all `summaryComponents`: `1 YEAR` 159, `1 MONTH` 5. `workplaceType`: Hybrid 136, OnSite 19, Remote 16. `isRemote` is true on 152, hybrid jobs included, so ijob does not use it. `address.postalAddress.addressCountry` holds `"USA"`, `"Canada"`. Unknown board: `404` `Not Found`. | `etag` → `304` |
| Lever | `https://api.lever.co/v0/postings/{t}?mode=json` | `User-agent: *` / `Allow: /` / `Crawl-delay: 1` | A bare array; `spotify` 89 items, `palantir` 299 (5,874,506 bytes). Title `text`, location `categories.location`. `createdAt` is epoch milliseconds (`1792767640768` = 2026-10-23T15:00:40Z). `country` is ISO-2 (`GB`, `US`, `SE`). `workplaceType` ∈ {`hybrid`, `onsite`, `remote`}. No `salaryRange` key in any of the 388 items, and no company name. An empty site answers `[]`; an unknown one `404 {"ok":false,"error":"Document not found"}`. | `ETag` → `304` |
| Workable | `https://apply.workable.com/api/v1/widget/accounts/{t}` | `User-agent: *` / `Disallow:` (empty) | `{"name","description","jobs":[…]}`; `name` reads `"Hugging Face"` and `"Mercari, inc."`. Title `title`, location `city` (`'Minato City'`). Also `shortcode`, `url`, `published_on` (`YYYY-MM-DD`), `telecommuting`, `function`, `locations[{country,countryCode,city,region}]`. No salary. Unknown account: `404 Not Found`. No 429 in 3 requests spaced 3 s apart. | none sent |
| SmartRecruiters | `https://api.smartrecruiters.com/v1/companies/{id}/postings` | `User-agent: LinkedInBot` / `Allow: /v1/companies/`, then `User-agent: *` / `Disallow: /` → **disallowed** | not fetched | — |

### 2.2 Job-board APIs

| Board | Endpoint | robots.txt (deciding line) | Shape | Validators |
|---|---|---|---|---|
| Himalayas | `https://himalayas.app/jobs/api?limit=20[&cursor=…]` | `User-Agent: *` / `Allow: /` | 20 per page at most (`limit=50` and `500` both returned 20); `nextCursor`. Title `title`, company `companyName`. No id; `guid` is unique. `pubDate` is epoch seconds. Location `locationRestrictions`, a list of country **names** (`['Sri Lanka']`); `[]` means worldwide. Over 40 jobs (2 pages), `salaryPeriod` was `annual` 33, `hourly` 4, `monthly` 3; 22 of them had a period but no amount. `minSalary`, `maxSalary` and `currency` are whole units. `applicationLink` is a himalayas.app page carrying `utm_source=api` (40 of 40). | `last-modified` → `304` |
| Jobicy | `https://jobicy.com/api/v2/remote-jobs?count=50&industry=engineering[&cursor=…]` | `User-agent: *`: no Disallow matches | 50 per page; `nextCursor` / `hasMore`. Title `jobTitle`, company `companyName`, location `jobGeo` (free text: `"USA"`, `"Canada,  USA"`). `id` is an int; `pubDate` is ISO with `+00:00`. `salaryMin`, `salaryMax` (sometimes absent), `salaryCurrency`, `salaryPeriod` ∈ {`yearly`, `hourly`}. The docs (`https://jobicy.com/jobs-rss-feed`, the 301 target of `https://jobi.cy/apidocs`) say, by line of the saved text: 809 "Polling a few times per day is normally sufficient and must not exceed once per hour."; 816 "Clearly credit Jobicy as the original source when displaying listings."; 890 "Apply links provided by our free API must continue to direct users to the original job pages on Jobicy." | none |
| RemoteOK | `https://remoteok.com/api` | `User-agent: *` / `Allow: /` / `Crawl-delay: 1` (named AI bots are disallowed; `ijob` is not named) | Element `[0]` is a notice `{"last_updated":…,"legal":"API Terms of Service: Please link back (with follow, and without nofollow!) to the URL on Remote OK and mention Remote OK as a source, …"}`, followed by 99 jobs. Title `position`, company `company`, location `location` (`""` in 56 of 99). `id` is a string; `epoch` is seconds. `salary_min` / `salary_max` use `0` for none, with no currency or period field. `url` == `apply_url` in all 99, and all 99 are written `https://remoteOK.com/…`. | none |
| Arbeitnow | `https://www.arbeitnow.com/api/job-board-api[?page=N]` | `User-agent: *` / `Disallow:` (empty) | `{"data":[…],"links":{…},"meta":{…}}`, 300 per page (page 1 is 2.7 MB). Title `title`, company `company_name`, location `location` (`'Berlin'`). `slug` ends in a numeric id; `created_at` is epoch seconds. `remote` is a bool (277 false, 23 true). No salary, no country. `meta.terms` reads "please do not abuse. I would appreciate linking back to the site." `x-ratelimit-limit: 50`. | `last-modified` → `304` |
| 4dayweek | `https://4dayweek.io/api/v2/jobs?category=engineering&limit=100[&page=N]` | `Allow: /api/v2` (the longer rule) beats `Disallow: /api/` | `{"data":[…],"page","limit","total","has_more"}`, 100 per page, `total` 6595. Title `title`, company `company.name`. `id` is a UUID; `posted_at` is ISO `Z`. `locations[]` holds objects `{city, country (name), …}`. `work_arrangement` ∈ {`hybrid`, `onsite`, `remote`}. Salary units are not documented: `/api/v2/openapi.json` and `/api-docs` answered 404. | none |
| MyCareersFuture | `https://api.mycareersfuture.gov.sg/v2/jobs?search=software%20engineer&limit=100&page=N&sortBy=new_posting_date` | `/robots.txt` answered 404 (allow) | `{"results":[…],"total":845,"_links":{…}}`, 100 per page, 0-based `page`. Title `title`, company `postedCompany.name`, `uuid`. `metadata.jobDetailsUrl` is the public URL; `metadata.newPostingDate` is a bare date. `address.isOverseas` was `false` on 100 of 100. Salary `{"minimum":6000,"maximum":10500,"type":{"salaryType":"Monthly"}}`, with no currency field. | `etag` → `304` |
| Kalibrr | `https://www.kalibrr.com/kjs/job_board/search?limit=15&offset=N&text=software%20engineer` | `User-agent: *`: `Disallow: /root`, `/candidate/profile`; nothing matches `/kjs/` | `{"jobs":[…],"count":317,…}`, 15 per page. Pages overlap: two ids repeat between offsets 0 and 15. Title `name`, company `company_name`, location `google_location.address_components.city` (`'Central Jakarta'`); also `…country`, `id`, `company.code`, `slug`, `activation_date` (ISO with an offset), `is_work_from_home`. Salary fields `base_salary`, `maximum_salary`, `salary_currency`, `salary_interval`, `salary_shown`. Over 30 items, `salary_interval` was `None` 25 and `month` 5; 23 had `salary_shown` with no amount; `salary_shown: false` coexists with numbers (id 275079). No URL field: the public page is `https://www.kalibrr.com/c/{company.code}/jobs/{id}/{slug}`. Fetching `/c/vlink-inc/jobs/274683/software-engineer-2` returned 200 with that same URL as `<link rel="canonical">`. | none (`no-store`) |

Only Kalibrr ever carries the employer's own apply URL (`apply_redirect_url`, 1 of 30 items). The
other boards link to their own pages.

### 2.3 RSS feeds and detail pages

All feeds are RSS 2.0, answer 200 and are allowed by their robots.txt.

| Source key | URL (as fetched) | Company is in | Title · location · notes |
|---|---|---|---|
| `rss:wwr-programming` | `https://weworkremotely.com/categories/remote-programming-jobs.rss` | title `Company: Role`, split at the first `": "` (`Yooli: FULL TIME: Software Engineer …` has two) | title after the split · `region` element · 25 items; the etag changes per request, so no 304 |
| `rss:wwr-backend` | `https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss` | same | same · 9 items |
| `rss:python` | `https://www.python.org/jobs/feed/rss/` | title `Role, Company`, split at the last `", "`; lossy (`… FULLY REMOTE-WORLDWIDE, ActivePrime, Inc.`) | title before the split · the description's first line (`London, UK <p>…`) · 20 items, **no `pubDate`** |
| `rss:golang` | `https://www.golangprojects.com/rss.xml` | title `Role\xa0@\xa0Company` (U+00A0 on both sides) | title before `@` · the description text before `" - "` (`Remote - Established…`) · 15 items, no `pubDate`; etag → `304` |
| `rss:elixir` | `https://elixirjobs.net/rss` | **not in the item**; the detail page | from the detail page · from the detail page · 10 items, CDATA; `pubDate` without a weekday (`15 Oct 2026 14:21:22 +0000`) |
| `rss:djinni-python` | `https://djinni.co/jobs/rss/?primary_keyword=Python` | **not in the item**; the detail page | from the detail page · from the detail page · 100 items (all tagged `Python`), 476,586 bytes; `pubDate` `Fri, 30 Oct 2026 18:37:12 +0200`; `/jobs/rss/` is not matched by `Disallow: /jobs2` |
| `rss:bsj` | `https://berlinstartupjobs.com/engineering/feed/` | title `Role // Company`, split at the last `" // "` | title before the split · none · 12 items; `guid isPermaLink="false"`, so `link` is the key; etag → `304` |

**Detail pages.** One request each:

- `https://elixirjobs.net/offers/treat-health-full-stack-engineer-elixir-liveview-react-35604f82`
  returned 200, 22,740 bytes, with 3 `ld+json` blocks. One is a `JobPosting` with
  `hiringOrganization.name` `'Treat Health'` and `jobLocation.address` `'Helsinki, Finland'` (a
  string).
- `https://djinni.co/jobs/856062-senior-ai-platform-engineer/` returned 200, 39,216 bytes, with a
  `JobPosting`: `hiringOrganization.name` `'JustMarkets Tech'`, `identifier` `856062`,
  `jobLocationType` `'TELECOMMUTE'`, and `datePosted` `'2026-10-30T18:37:12.691765'` (no offset).

**Stdlib behaviour.** The probe printed:

```text
python 3.14.10 expat_2.9.0
rfc822 'Fri, 30 Oct 2026 18:37:12 +0200' 2026-10-30T18:37:12+02:00
rfc822 '15 Oct 2026 14:21:22 +0000' 2026-10-15T14:21:22+00:00
bomb ParseError limit on input amplification factor (from DTD and entities) breached: line 1, column 327
```

- `email.utils.parsedate_to_datetime` reads both date shapes.
- `xml.etree.ElementTree` refuses an entity-expansion bomb (seven entities, a 10-character base
  expanded tenfold six times, so 10⁷ characters).

### 2.4 schema.org JobPosting pages

| Page (fetched) | Blocks | Relevant fields |
|---|---|---|
| `https://career.teamtailor.com/jobs/8636263-solution-architect-bilingual-french-english` | 1 object | `title`, `datePosted` `2026-10-29T13:25:57+01:00`, `hiringOrganization.name` `Teamtailor`, `jobLocation` is a **list** with `addressLocality` `Stockholm`/`Paris` and `addressCountry` `"SE"`/`"FR"`, `identifier.value` `"8636263"` |
| `https://eclipsefoundation.applytojob.com/apply/gdWcbFq2mr/Open-Source-Software-Release-Engineer` | 2 objects (an `Organization`, then the `JobPosting`) | `url`, `datePosted` `2026-10-07` (date only), `jobLocation.address.addressLocality` `Brussels`, `baseSalary {"currency":"EUR","value":{"unitText":"YEAR","minValue":55000,"maxValue":80000}}`, `jobLocationType` `TELECOMMUTE`, `applicantLocationRequirements {"@type":"Country","name":"BE"}` |
| `https://jobs.lever.co/epoch-ai/d172645e-a11f-44a0-88d0-7a989e0a28f6` | 1 object | `title`, `datePosted` `2026-06-29`, `hiringOrganization.name` `Epoch AI`, `jobLocation.address.addressLocality` `Remote`, `employmentType` `"Full Time"` (free text) |

Closed JazzHR jobs answered 410 (three URLs tried). No page used `@graph`.

### 2.5 Tier B

| Source | Requests | Facts |
|---|---|---|
| YC jobs (`yc:swe`) | `https://www.ycombinator.com/jobs/role/software-engineer` and `…/software-engineer/remote` | robots `Allow: /`. One `data-page` attribute holds HTML-escaped JSON, with postings at `props.jobPostings`: 41 on the main page and 38 on `/remote`, 22 of them not on the main page. Keys used: `id`, `title`, `companyName`, `location` (a `" / "`-joined string, `"Remote / Remote (US)"`), `url` (a relative path, `/companies/sully/jobs/DuoVsUf-…`). `createdAt` is **relative** (`"3 months"`, `"9 days"`). `applyUrl` was `account.ycombinator.com/authenticate…` (a login page) on 41 of 41. `salaryRange` is a free string (`"$200K - $300K"`, `""`). An unknown role slug returns the same 41, not a 404. The etag changes on every request. |
| LetsGetRusty (`letsgetrusty:all`) | `https://jobs.letsgetrusty.com/?page=N` | robots disallow `/*?*page=*job=` and `/*?*job=*page=` and allow `/?page=N` alone. 10 jobs per page; the page text reads "Showing 1-10 of 194 jobs". Each page has one `ItemList` JSON-LD of `JobPosting`s with `title`, `datePosted` (date), `hiringOrganization.name`, `jobLocation.address.addressCountry` (a country name or `"Remote"`) and `url` `https://jobs.letsgetrusty.com?job=rec…`. `https://letsgetrusty.com/jobs` answered `302` to `https://jobs.letsgetrusty.com`. No validators. |
| JapanDev (`japandev:all`) | `https://japan-dev.com/cdn/sitemaps/sitemap.xml`, then each new `/jobs/…` page | robots `User-agent: *` has only two salary-guide disallows. The sitemap held 290 `/jobs/` URLs and has no `<lastmod>`. Detail pages are about 210–225 KB with no etag; their `last-modified` equals the request time. JSON-LD `JobPosting` carries `title`, `datePosted` (date), `url`, `hiringOrganization.name`, `jobLocation.address.{addressLocality, addressCountry "JP"}`, `identifier.value` (the URL slug), and `jobLocationType` `TELECOMMUTE` on the remote job, which lists about 240 countries in `applicantLocationRequirements`. `baseSalary` appeared on 1 of 3 pages: `{"currency":"JPY","value":{"minValue":8000000,"maxValue":16000000,"unitText":"YEAR"}}`. |
| HN (`hn:whoishiring`) | `https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring`, then `https://hn.algolia.com/api/v1/search?tags=comment,story_{id}&hitsPerPage=1000` | `hn.algolia.com/robots.txt` answered 404 (allow). The same author also posts "Who wants to be hired?", so the thread is chosen by the title prefix `Ask HN: Who is hiring?`. Latest: 49922569, "Ask HN: Who is hiring? (October 2026)", 391 comments in one page (`nbPages` 1), of which 261 are top level (`parent_id` == story id). `comment_text` is HTML with entities (`&#x2F;`, `&#x27;`, `&amp;`); 253 of 261 use `<p>`. 229 of 261 first lines contain at least two `|`, in no fixed field order. |

### 2.6 Seed companies

`docs/research/companies.csv` has 235 rows. A probe (`csv.DictReader`, because the notes column
contains commas) over the rows with `verified_status == '200'` printed:

```text
[('greenhouse', 75), ('ashby', 55), ('lever', 11), ('workable', 7), ('workday', 5), ('herp', 5), ('smartrecruiters', 3), ('hrmos', 3), ('custom', 3), ('gem', 2), ('talentio', 2), ...]
```

The seeds are the verified rows whose `ats` is `greenhouse`, `ashby`, `lever`, `workable` or `gem`
— **150 rows**, taken from the `board_token` and `company` columns. The rest are out of scope (§12).

### 2.7 Identity evidence

A probe over the saved bodies counted the jobs whose description text contains the company name,
ignoring case:

```text
ashby ramp jobs 171 mention Ramp 171
lever spotify jobs 89 mention Spotify 74
gem retool jobs 26 mention Retool 26
```

It searched `descriptionPlain` (Ashby, Lever) and `content_plain` (Gem). Greenhouse carries
`company_name` per job (`"Tailscale"`); Workable carries `name` at the top level.

## 3. Polite HTTP additions (`ijob_sources.polite_http`, `ijob_sources.http_cache`)

### 3.1 Conditional requests

- `PoliteClient(..., cache: ConditionalCache | None = None)`. The protocol sits beside its consumer
  in `polite_http`: `lookup(url) -> CachedResponse | None` and `store(url, CachedResponse)`.
  `CachedResponse` is a frozen dataclass `(etag: str | None, last_modified: str | None, body: bytes)`.
- `get(url, *, conditional=False)`:
  - With `conditional=True` and a cached entry, the request carries `If-None-Match` and/or
    `If-Modified-Since`.
  - On `304` it returns a `200` response built from the cached body, so an adapter never sees a 304.
  - With `conditional=True`, a 2xx response carrying `ETag` or `Last-Modified` is stored.
- Adapters pass `conditional=True` only for a fixed index URL: a hiring-system board, a feed, a
  board's first page, the YC pages, the JapanDev sitemap. They never pass it for cursor pages or
  detail pages, whose bodies would never be asked for again.
- Both headers earn their place. ETag-only sources answer 304 (§2.1, §2.2); Himalayas and Arbeitnow
  send only `last-modified` and answer 304 to it.
- `SqliteHttpCache(path)` implements the protocol in its own SQLite file, separate from the postings
  database, with one table: `responses(url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, body
  BLOB, used_at TEXT) STRICT`.
  - Opening it deletes rows unused for 30 days.
  - The default path is `$XDG_CACHE_HOME/ijob/http.sqlite`, or `~/.cache/ijob/http.sqlite` when
    `XDG_CACHE_HOME` is unset or empty.
  - Deleting the file only costs full downloads.
- robots.txt requests never use it; their in-memory 24 h cache is unchanged (walking-skeleton spec
  §5.2).

### 3.2 Per-host spacing

The interval between two requests to a host is the largest of three values:

- the client's `min_interval` (1.0 s);
- `RobotsPolicy.crawl_delay(url)`, the robots.txt `Crawl-delay` for `ijob`;
- `host_intervals[host]`. The app passes `{"apply.workable.com": 3.0}`, the intent's Workable rule.

A probe printed `crawl_delay ijob 1` for the lines `User-agent: *` / `Allow: /` / `Crawl-delay: 1`,
and `crawl_delay none None` for `User-agent: *` / `Disallow: /`.

### 3.3 robots.txt answering 401 or 403

The C1 policy already treats any 4xx other than 429 as "allow all"
(`packages/ijob-sources/src/ijob_sources/robots.py:112-113`; walking-skeleton spec §5.2, RFC 9309
§2.3.1.3). A probe gave `RobotsPolicy` a `send` that answers each status and printed
`robots 401 allowed True` and `robots 403 allowed True`. Gem and Ashby (§2.1) are therefore allowed,
and nothing changes here.

## 4. Store migration 2 (`ijob_core.store`)

```sql
ALTER TABLE postings ADD COLUMN salaries TEXT NOT NULL DEFAULT '[]';   -- JSON list of Money
ALTER TABLE postings ADD COLUMN country TEXT;                          -- ISO 3166-1 alpha-2
ALTER TABLE postings ADD COLUMN remote TEXT;                           -- RemoteScope value
ALTER TABLE postings ADD COLUMN role_hint TEXT;
ALTER TABLE postings ADD COLUMN low_confidence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE postings ADD COLUMN canonical_url TEXT;
ALTER TABLE postings ADD COLUMN fingerprint TEXT;
CREATE INDEX postings_canonical_url ON postings(canonical_url);
CREATE INDEX postings_fingerprint ON postings(fingerprint);
CREATE TABLE sources (
  ref TEXT PRIMARY KEY, company TEXT NOT NULL, target_url TEXT,
  origin TEXT NOT NULL CHECK (origin IN ('seed', 'user')),
  removed INTEGER NOT NULL DEFAULT 0, last_fetched_at TEXT
) STRICT;
```

A probe made the C1 `postings` table, inserted one row, applied the `ALTER`/`CREATE INDEX`
statements above, and printed:

```text
sqlite 3.53.1
v1 row after migration [('[]', None, None, None, 0, None, None)]
```

Version 1 rows therefore read back with the defaults.

| Operation | Behaviour |
|---|---|
| `record(drafts, *, seen_at, still_listed=())` | As in C1 for a known `(source, external_id)`. For a new one, it attaches the sighting to an existing posting only if that posting has **no sighting from the same source** and matches on `canonical_url`, or failing that on `fingerprint` (§7). Otherwise it creates a posting, as C1 does. The same-source exclusion keeps distinct jobs of one board apart: in the saved `palantir` body, 166 of 299 jobs share a normalised title and country with another job. On a match: the display fields follow the latest draft (C1). `country`, `remote`, `role_hint` and `salaries` take the draft's value only when it is non-empty. `still_listed` is a collection of `(SourceRef, ExternalId)`; each known pair gets `last_seen = seen_at` on its sighting and its posting, and unknown pairs are ignored. `RecordResult` gains `merged: int = 0`, which counts new sightings attached to an existing posting. |
| `mark_fetched(ref, at)` | Sets `sources.last_fetched_at`. |
| `open_postings(*, sources=None, now)` | The postings that have a sighting whose `last_seen` equals its source's `last_fetched_at`. The source must not be removed, its `last_fetched_at` must be within 7 days of `now`, and it must be in `sources` when that is given. Ordered as C1's `postings`. |
| `stale_sources(*, now)` | The sources not removed whose `last_fetched_at` is older than 7 days. |
| `external_ids(ref)` | The ids a source has shown, used by premise 5. |
| `import_seeds(rows)` | Inserts `origin='seed'` rows whose `ref` is absent, and leaves every present row untouched, removed ones included. Returns the count inserted. |
| `add_source(ref, company, target_url=None)` | Inserts a `user` row, or clears `removed` on an existing row and keeps its `origin`. |
| `remove_source(ref)` | Sets `removed = 1`. |
| `source(ref)` / `sources()` | Read one row, or every row not removed. |

C1's `postings(sources=…)` and `sighting(…)` are unchanged.

## 5. Model additions (`ijob_core.model`, `ijob_core.money`, `ijob_core.countries`)

### 5.1 Types

| Type | Shape | Validation |
|---|---|---|
| `SourceKind` members | `GEM, ASHBY, LEVER, WORKABLE, HIMALAYAS, JOBICY, REMOTEOK, ARBEITNOW, FOURDAYWEEK = "4dayweek", MYCAREERSFUTURE, KALIBRR, RSS, YC, LETSGETRUSTY, JAPANDEV, HN, JOBPOSTING, FEED`; values are the lower-case names unless shown | closed set |
| `parse_source_ref(text) -> SourceRef` | `<kind>:<key>` | the kind must be a `SourceKind` value, and the key goes through `parse_source_key`; otherwise `InvalidValueError` |
| `Period` | `StrEnum`: `HOUR = "hour"`, `MONTH = "month"`, `YEAR = "year"` | — |
| `Money` | frozen slotted: `min: Decimal \| None`, `max: Decimal \| None`, `currency: str`, `period: Period \| None` | at least one bound; bounds ≥ 0; `min ≤ max`; `currency` matches `[A-Z]{3}` |
| `RemoteScope` | `StrEnum`: `NONE, COUNTRY, REGION, WORLDWIDE` | — |
| `CountryCode` | `NewType`, built by `parse_country(text) -> CountryCode \| None` (§5.2) | — |
| new `SightingDraft` fields; defaults keep C1 callers valid | `salaries: tuple[Money, ...] = ()`, `country: CountryCode \| None = None`, `remote: RemoteScope \| None = None`, `role_hint: str \| None = None`, `low_confidence: bool = False` | — |
| new `Posting` fields | the same five, read back from migration 2 | built by the store |
| new `SourceBatch` field (`ijob_sources.collect`) | `still_listed: tuple[ExternalId, ...] = ()` | — |

**When an adapter builds a `Money`.** Only when the currency and at least one positive bound are
present, so the 22 Himalayas jobs that have a period but no amount and the 23 Kalibrr jobs that are
shown but empty (§2.2) simply carry no salary. A period value the adapter does not map becomes
`period=None`; it does not reject the job. `remote=None` means the scope is unknown, which covers a
remote job whose source does not say how far the remoteness reaches.

### 5.2 Countries

`ijob_core/data/iso3166.tab` is a verbatim copy of tzdata's `iso3166.tab`. Its header, from the
local `/usr/share/zoneinfo/iso3166.tab`, begins "This file is in the public domain, so clarified as
of 2009-05-17 by Arthur David Olson". The file has 249 data rows (`grep -c -v '^#'` printed `249`),
for example `US	United States`, `GB	Britain (UK)` and `KR	Korea (South)`.

`parse_country(text)` tries the whole text and then the part after its last `", "`. A part is
accepted, case-insensitively, when it is:

- an alpha-2 code in the table;
- a name in the table;
- an alias for a spelling seen in the fetched bodies that the table spells differently: `USA`
  (Ashby, §2.1), `UK` (Jobicy `jobGeo`, one item in each saved page) and `Czechia` (Himalayas
  `locationRestrictions`; the table has `CZ	Czech Republic`).

Anything else returns `None`, and a posting without a country passes every country filter (§8).
`"San Francisco, United States"` and `"Toronto, Ontario, Canada"` therefore resolve to `US` and `CA`.

The data ships as package data. A probe wheel built with `uv build --wheel` (uv 0.13.6, backend
`uv_build`) from a copy of `ijob-sources` with an added `src/ijob_sources/data/seeds.csv` listed
`ijob_sources/data/seeds.csv`, which shows that a non-Python file under `src/<module>/` ships. The
same holds for `ijob_core/data/iso3166.tab` and `ijob_app/data/default_config.toml`.

## 6. Sources (`ijob_sources`)

### 6.1 Adapter table

Every adapter satisfies the C1 `Source` protocol and follows the C1 per-job rejection contract
(walking-skeleton spec §6.1). Each has a pure `parse_*` function. Hiring-system adapters take the
source row's `company`. In a cell, `—` means the field keeps its default. Every keyed value
below is recorded in §2.

| Module · ref | Requests per fetch | external_id · url | title · location | company · country · remote | salaries · posted_at · role_hint |
|---|---|---|---|---|---|
| `greenhouse` · `greenhouse:<t>` | 1 | as C1 | as C1 | as C1 · first `offices[].location` accepted by `parse_country` · — | each `pay_input_ranges` item → `Money(min_cents/100, max_cents/100, currency_type, None)` · as C1 · first `departments[].name` |
| `gem` · `gem:<t>` | 1 | `id` · `absolute_url` | `title` · `location.name` | row company · `location.name` · `location_type` `in_office`/`hybrid`→`NONE`, `remote`→`None` | — · `first_published_at` · first `departments[].name` |
| `ashby` · `ashby:<t>` | 1 | `id` · `jobUrl` | `title` stripped · `location` | row company · `address.postalAddress.addressCountry` · `workplaceType` `OnSite`/`Hybrid`→`NONE`, `Remote`→`None` | `summaryComponents` with `compensationType == "Salary"` → `Money(minValue, maxValue, currencyCode, {"1 YEAR": YEAR, "1 MONTH": MONTH})` · `publishedAt` · `department` |
| `lever` · `lever:<t>` | 1 | `id` · `hostedUrl` | `text` · `categories.location` | row company · `country` · `workplaceType` `onsite`/`hybrid`→`NONE`, `remote`→`None` | — · `createdAt` ms → UTC · `categories.department` |
| `workable` · `workable:<t>` | 1 (host spacing 3 s) | `shortcode` · `url` | `title` · `city` | row company · first `locations[].countryCode` · `telecommuting` false→`NONE`, true→`None` | — · `published_on` at 00:00 UTC · `function` when non-empty |
| `himalayas` · `himalayas:all` | ≤ 5 pages | `guid` · `applicationLink` | `title` · `locationRestrictions` joined by `", "`, or `None` when empty | `companyName` · the only `locationRestrictions` entry · one entry→`COUNTRY`, `[]`→`WORLDWIDE`, several→`REGION` | `currency` with `minSalary`/`maxSalary`; `annual`→`YEAR`, `monthly`→`MONTH`, `hourly`→`HOUR` · `pubDate` s · — |
| `jobicy` · `jobicy:engineering` | ≤ 5 pages | `id` · `url` (the Jobicy page, per line 890) | `jobTitle` · `jobGeo` | `companyName` · `jobGeo` · — | `salaryCurrency` with `salaryMin`/`salaryMax`; `yearly`→`YEAR`, `hourly`→`HOUR` · `pubDate` · — |
| `remoteok` · `remoteok:all` | 1; element `[0]` skipped | `id` · `url` | `position` · `location` (`""`→`None`) | `company` · — · — | — (no currency) · `epoch` s · — |
| `arbeitnow` · `arbeitnow:all` | ≤ 5 pages | `slug` · `url` | `title` · `location` | `company_name` · — · `remote` false→`NONE`, true→`None` | — · `created_at` s · — |
| `fourdayweek` · `4dayweek:engineering` | ≤ 5 pages | `id` · `url` | `title` · first `locations[]` as `"city, country"` | `company.name` · first `locations[].country` · `work_arrangement` `onsite`/`hybrid`→`NONE`, `remote`→`None` | — (units undocumented) · `posted_at` · — |
| `mycareersfuture` · `mycareersfuture:software-engineer` | ≤ 5 pages | `uuid` · `metadata.jobDetailsUrl` | `title` · — | `postedCompany.name` · `SG` when `address.isOverseas` is false · — | — (no currency field) · `metadata.newPostingDate` at 00:00 UTC · — |
| `kalibrr` · `kalibrr:software-engineer` | ≤ 5 pages; ids repeated on later pages are dropped | `id` · the built URL (§2.2) | `name` · `google_location.address_components.city` | `company_name` · `…address_components.country` · — | when `salary_shown`: `salary_currency` with `base_salary`/`maximum_salary`, `month`→`MONTH` · `activation_date` · — |
| `rss` · `rss:<feed>` (§2.3) | 1, plus 1 detail per **new** item for `elixir` and `djinni-python` | `link` · `link` | per-feed rule (§2.3) | per-feed rule, or the detail page's `hiringOrganization.name` · — · — | — · `pubDate` when present, else `None` · — |
| `yc` · `yc:swe` | 2 | `id` · `https://www.ycombinator.com` + `url` | `title` · `location` | `companyName` · — · — | — · `None` (relative dates) · — |
| `letsgetrusty` · `letsgetrusty:all` | ⌈total/10⌉ pages, ≤ 20 | the `?job=` value · `url` | `title` · `addressCountry` | `hiringOrganization.name` · `addressCountry` · `"Remote"`→`None` | — · `datePosted` at 00:00 UTC · — |
| `japandev` · `japandev:all` | 1 sitemap, plus 1 per **new** job (≤ 100); known jobs go to `still_listed` | `identifier.value` · `url` | §6.2 | §6.2 | §6.2 |
| `hn` · `hn:whoishiring` | 2 | comment `objectID` · `https://news.ycombinator.com/item?id=<objectID>` | first-line field 2 · field 3 when present | first-line field 1 · — · — | — · `created_at` · — ; `low_confidence=True` |
| `jsonld` · `jobposting:<h>` | 1 (`target_url`) | `identifier.value`, else `target_url` · `url`, else `target_url` | §6.2 | §6.2 | §6.2 |
| `rss` · `feed:<h>` | 1 (`target_url`) | `link` · `link` | `title` · — | row company · — · — | — · `pubDate` · — |

- **Manual-URL keys.** `<h>` is the first 16 hex characters of the SHA-256 of the manual URL. Hex
  digits are inside `parse_source_key`'s alphabet (`packages/ijob-core/src/ijob_core/model.py:20`).
- **Detail failures.** A failed detail request (any `SourcesError`) rejects only that job. It is
  logged and counted in `rejected` under the C1 contract, and the source still succeeds. New jobs
  beyond the 100-request cap are left for the next fetch, so a first JapanDev pass of 290 jobs
  completes over three runs.
- **HN parsing.** The adapter keeps only top-level comments (`parent_id` == story id). The first
  line is `comment_text` with HTML entities unescaped, cut at the first `<p>`, with tags removed,
  then split on `|` with each field stripped. A comment with fewer than two fields is rejected.
  §2.5 shows the field order is not fixed. Postings with the fields in another order are
  mis-assigned, and `low_confidence` marks every HN posting for that reason.
- **`raw`.**
  - A JSON item: the item re-serialised, as in C1.
  - An RSS item: a JSON object of its child elements' tag → text.
  - A detail-page job: `{"item": <the RSS item or sitemap URL>, "jobposting": <the JobPosting>}`.
  - A YC posting: its `jobPostings` element.
  - An HN comment: its Algolia hit.
  - A JSON-LD page: the JobPosting object.

  Normalisation can therefore be re-run from `raw` alone, as the intent requires.

### 6.2 JSON-LD reading (`jsonld`, `japandev`, `letsgetrusty`, RSS detail pages)

- Every `<script type="application/ld+json">` is read with `html.parser`, keeping the objects whose
  `@type` is `JobPosting`. Top-level lists and `@graph` are also accepted, though none was seen
  (§2.4).
- `title` ← `title`; company ← `hiringOrganization.name`.
- `jobLocation` may be an object or a list (§2.4):
  - location ← the first `address.addressLocality`, or `address` itself when it is a string
    (elixirjobs, §2.3);
  - country ← the first `addressCountry` that `parse_country` accepts.
- `jobLocationType == "TELECOMMUTE"` makes the job remote. The scope is `COUNTRY` when
  `applicantLocationRequirements` names exactly one country, and `None` otherwise.
- `baseSalary.value.{minValue,maxValue}` with `currency` and `unitText` `YEAR`/`MONTH`/`HOUR` becomes
  one `Money` (§5.1 rules).
- `datePosted`:
  - a date becomes 00:00 UTC;
  - a datetime with an offset is kept;
  - a datetime without one (djinni, §2.3) becomes `None`.

### 6.3 Paging cap and refresh interval

- A paged board stops after 5 pages, or earlier when the source says there are no more. Five pages
  bound one fetch at 100 jobs (Himalayas), 250 (Jobicy), 1,500 (Arbeitnow), 500 (4dayweek, MCF) and
  75 (Kalibrr), from the page sizes in §2.2.
- A `SourceKind` may declare a minimum refresh interval. Only Jobicy does, 3,600 s, from its terms
  (§2.2 line 809). `ijob search` skips a source whose `last_fetched_at` is newer than that, whether
  the source was chosen with `--source` or not.

### 6.4 Identity check (`ijob_sources.identity`)

The check runs after parsing, against the source row's company normalised as in §7. It passes on an
empty batch; otherwise it needs one of these:

| Source | Passes when |
|---|---|
| `greenhouse`, `workable` | Greenhouse: any job's `company_name`; Workable: the top-level `name`. The value must equal or contain the company, or be contained by it. |
| `gem`, `ashby`, `lever` (and also greenhouse and workable) | any job's description (`content`, `content_plain`, `descriptionPlain`, `description`) contains the company. §2.7 shows this holds on real boards. |
| `jobposting:<h>` | the page's `hiringOrganization.name` matches the company, as in the first row |

- **Exempt sources.** Boards, feeds and Tier B sources are exempt: each lists many companies, so
  there is no single company to match. A `feed:<h>` has no per-item company field (§2.3), so the
  user's `--company` is the only company it has.
- **On failure.** The check raises `IdentityMismatchError` (a `SourcesError`), so `collect` reports
  the source and records none of its drafts.
- **On add.** `ijob sources add` fetches once and also refuses a board with 0 jobs, which cannot be
  verified. The research saw real but empty slugs (`docs/research/ats_endpoints.md:67`, Ashby:
  "Some boards exist but are empty"; `:77`, Workable: "many generic slugs are real but empty
  accounts").

### 6.5 Manual URLs (`ijob_sources.detect`)

`ijob sources add-url URL --company NAME` resolves in this order:

1. **A supported hiring system.** The URL matches a pattern for one, taken from
   `docs/research/ats_endpoints.md:216` ("Detecting the ATS behind a careers URL") and narrowed to
   `(boards|job-boards).greenhouse.io/{t}`, `jobs.lever.co/{t}`, `jobs.ashbyhq.com/{t}`,
   `apply.workable.com/{t}` and `jobs.gem.com/{t}`. It becomes that kind's source with key `{t}`.
2. **A JobPosting page.** Otherwise the page is fetched. One `JobPosting` in its JSON-LD makes it a
   `jobposting:<h>` source.
3. **A feed.** Otherwise, when the body parses as RSS 2.0, it becomes a `feed:<h>` source.
4. **Rejected.** Otherwise `UnsupportedUrlError` names the reason.

Every step goes through `PoliteClient`, so robots rules apply. A source from steps 1–2 must pass the
identity check (§6.4) on that fetch before it is added.

### 6.6 Seeds (`ijob_sources/data/seeds.csv`)

- `kind,key,company` rows: the 150 seed rows of §2.6, plus one row per board, feed and Tier B
  source of §6.1, with company `-` because their adapters never read it.
- A test asserts that the hiring-system rows equal the filtered research CSV, so the two cannot
  drift.

## 7. Cross-source dedupe (`ijob_core.dedupe`)

- **`canonical_url(url)`.**
  - Lower-case the scheme and host. RemoteOK writes its host `remoteOK.com` (§2.2).
  - Drop the fragment and the `utm_*` query parameters. Himalayas appends `utm_source=api` (§2.2).
  - Drop a trailing `/` from the path.
- **`fingerprint(company, title, country)`.** Built only when `country` is known:
  - NFKC, casefold, every non-alphanumeric run becomes one space, trim;
  - the company drops up to two trailing legal-suffix tokens from {`inc`, `llc`, `ltd`, `gmbh`,
    `corp`, `co`, `pte`, `pty`}, so `SCIENTEC CONSULTING PTE. LTD.` (MCF) and `Dijital Team Pty Ltd`
    (Himalayas), both in the saved bodies, reduce to their names;
  - the parts are joined as `company|title|CC`.

  With no country there is no fingerprint, so two jobs with the same title at the same company in
  unknown places are never merged.
- Matching is across sources only (§4), and boards rarely carry the employer's URL (§2.2). Most
  merges therefore come from the fingerprint.

## 8. Filters and configuration (`ijob_core.filters`, `ijob_app.config`)

`FilterSpec` is a frozen dataclass of the fields below. `keep(posting, spec, *, now) -> bool` is pure.
Every rule keeps a posting when the data cannot decide.

| Filter | Drops a posting only when |
|---|---|
| role | the title matches an `exclude` pattern; or it matches no `include` pattern, its `role_hint` is present, and the hint matches none either |
| country | `countries` is set, the posting's country is known and not in it, and its remote scope is `NONE` or `COUNTRY`. An unknown (`None`), `REGION` or `WORLDWIDE` scope passes. |
| remote | `remote` is set, and the posting's scope is known and not in it |
| since | `posted_at` is known and older than `now - since` |
| company | `companies` is set and no entry is a case-insensitive substring of the company |
| salary | `min_salary` is set, some `Money` has the same currency and a comparable period (`MONTH` ×12 = `YEAR`), and none of those reaches it (on `max`, else `min`). With `require_salary`, a posting with no salaries is dropped. |

- Role patterns are Python regular expressions, matched with `re.search` and `re.IGNORECASE`.
- **Configuration.**
  - Location: `$XDG_CONFIG_HOME/ijob/config.toml`, else `~/.config/ijob/config.toml`, read with
    `tomllib`. A probe printed `tomllib {'filters': {'role': {'include': ['エンジニア']}}}`.
  - Tables: `[filters.role] include, exclude` and `[filters] countries, remote, since, companies,
    min_salary = {amount, currency, period}, require_salary`.
  - `since` is a string `<n>d` or `<n>h`; `period` is `hour`, `month` or `year`; `remote` values are
    `none`, `country`, `region` or `worldwide`.
  - With no file, `ijob_app/data/default_config.toml` applies. Its include patterns are
    `\bengineer`, `\bdeveloper`, `エンジニア` and `開発`; its excludes are `\bsales\b`, `\brecruit`,
    `\bmarketing\b` and `\baccount executive\b`.
  - A per-run flag replaces its config value. An invalid config exits `2` and names the key.
- **Default-pattern probe.**

  | Title | include | exclude |
  |---|---|---|
  | `Toptal: Salesforce Developers with AI-Assisted Coding Experience` | True | False |
  | `Software Engineer` | True | False |
  | `Engineering` | True | False |
  | `28卒対象 / セキュリティエンジニア インターンシップ- Mercari Group` | True | False |
  | `Account Executive - Commercial, Spotify Technology` | False | True |
  | `Sales Engineer` | True | True |

  A "Sales Engineer" is therefore dropped by default, as a sales role.

## 9. Command line (`ijob_app.cli`)

```text
ijob search [--source REF]... [--country CC]... [--remote SCOPE]... [--since 7d]
            [--company TEXT]... [--min-salary 6000:SGD:month] [--require-salary]
            [--db PATH] [--config PATH]
ijob sources list | add REF --company NAME | remove REF | add-url URL --company NAME   [--db PATH]
```

1. **Open and seed.** Open the store and import the seeds.
2. **Choose the sources.** Without `--source`, every row not removed. Each `--source REF` must parse
   with `parse_source_ref` and name a row that is not removed; otherwise exit `2` with
   `unknown source <ref>; add it with: ijob sources add <ref> --company NAME`. Either way, sources
   inside their refresh interval are left out.
3. **Collect.** Run `collect` through one `PoliteClient` with the HTTP cache and Workable's host
   interval.
4. **Record.** `record` the drafts and the `still_listed` ids, and `mark_fetched` the sources that
   succeeded.
5. **Print.** Print `open_postings(sources=<the --source refs, if any>)` that pass the filters, one
   tab-separated line each: `title, company, location or -, source refs (comma-joined), url`. A
   `low_confidence` line starts with `? `. The source column names Jobicy, Remote OK and Arbeitnow
   beside their links, which meets their attribution requests (§2.2).

- **stderr.** C1's lines (walking-skeleton spec §8 step 4) stay, with the ref in place of
  `greenhouse:<key>`. New lines:
  - `skipped <ref>: identity check failed: <reason>`;
  - `kept <ref>: fetched less than <n> min ago`;
  - `stale <ref>: last fetched <date>, postings hidden`.
- **Exit codes.** `0` when at least one chosen source succeeded or was kept; `1` when every chosen
  source failed; `2` on a bad flag, ref or config.
- `--greenhouse` is removed.

## 10. Errors

- `ijob_sources.errors` gains `IdentityMismatchError(SourcesError)` (ref, reason) and
  `UnsupportedUrlError(SourcesError)` (url, reason).
- `Money`, `parse_source_ref`, `parse_country` and config validation raise `InvalidValueError`.
- Walking-skeleton spec §7 holds: `raise … from` in every handler, and `collect` is the one isolation
  point. The per-job catch of a detail request's `SourcesError` (§6.1) sits inside the adapter, like
  the C1 per-job `InvalidValueError` catch.

## 11. Testing

No test touches the network. Every logic-bearing module gets a `test_<module>.py`.

- **Fixtures.** Trimmed from the §2 bodies to 2–3 items with shortened descriptions, under
  `packages/ijob-sources/tests/ijob_sources_tests/fixtures/<kind>/`. Bodies whose real content is
  personal are **synthetic** in the recorded shape, because CLAUDE.md forbids committing personal
  data; this applies to HN comments, which carry authors and contact details.
- **Per adapter.**
  - the §6.1 row's mapping on its fixture, title and location included, and the per-job rejection
    contract;
  - the Money rule of §5.1, including empty salaries (Himalayas, Kalibrr) and an unmapped period;
  - paging stops at the cap and at "no more"; Kalibrr's repeated ids; RemoteOK's notice element;
  - the RSS title rules on the verbatim titles in §2.3;
  - detail-only-when-new for `japandev`, `rss:elixir` and `rss:djinni-python`: `still_listed`, the
    cap of 100, and a failed detail request rejecting only its job.
- **Cross-cutting.**
  - `test_http_cache`: a 304 replayed as 200; both validators sent; nothing stored without a
    validator or without `conditional`; 30-day eviction.
  - `test_polite_http`: spacing takes the maximum of the default, `Crawl-delay` and `host_intervals`.
  - `test_robots`: `crawl_delay`.
  - `test_identity`: each row of §6.4, plus the exemptions.
  - `test_detect`: each step of §6.5 through `MockTransport`.
  - `test_dedupe`: the §7 URL cases and suffix examples; no fingerprint without a country.
  - `test_filters`: each row of §8, both the drop and the pass-when-unknown case, plus the
    default-pattern table.
  - `test_store`:
    - migration 1 → 2 keeps rows;
    - a merge by URL and by fingerprint, but never within one source;
    - `merged` counts;
    - non-empty fields win on merge;
    - `still_listed`;
    - `open_postings` across fetched, failed, kept and stale sources;
    - an idempotent seed import that respects removal;
    - revival keeps `origin`.
  - `test_countries`, `test_model` (`parse_source_ref`, `Money`), `test_seeds` (equality with the
    research CSV).
  - `test_cli`: the search flags and config, the unknown `--source` exit, the sources commands, the
    exit codes, the new stderr lines and columns.

## 12. Non-goals

- SmartRecruiters: §2.1 shows its API's robots.txt disallows ijob.
- Verified seed rows on systems without an adapter here: Workday, HERP, HRMOS, Talentio, iCIMS,
  Eightfold, Personio, Oracle, Teamtailor as a hiring system, Deel, `custom`, `custom_feed`, and the
  Greenhouse EU board.
- Lever's EU host, other WWR category feeds and other Djinni keyword feeds: none was fetched for this
  spec.
- Salaries from RemoteOK, 4dayweek, MyCareersFuture and YC. Each lacks a stated currency, a stated
  unit or a number (§2.2, §2.5); the raw payload keeps them.
- Currency conversion; comparing hourly or period-less salaries.
- Parsing HN comment bodies beyond the first line; any use of an LLM.
- JapanDev's internal search API and `__NUXT_DATA__`; JobStreet beyond a deep link.
- A persisted robots.txt cache; scheduling or background refresh.
- Output formatting beyond the plain listing.
