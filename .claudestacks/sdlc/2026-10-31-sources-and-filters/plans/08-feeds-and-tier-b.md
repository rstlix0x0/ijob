---
status: done
created: 2026-10-31
depends-on: [01, 04]
built-on: aeb01064881c
---

# Feeds and Tier B Implementation Plan

**Goal:** The seven RSS feeds and the four Tier B sources are collected as sighting drafts.

**Architecture:**
- `SourceBatch` gains `still_listed`, so sources that fetch detail pages only for new jobs can report
  the jobs they already know (spec §1 premise 5). Those sources take the set of known ids in their
  constructor; plan 10 fills it from `Store.external_ids`.
- Two shared readers serve several sources:
  - `ijob_sources.rss` parses RSS 2.0 with `xml.etree.ElementTree`, which refuses entity bombs
    (spec §2.3 probe);
  - `ijob_sources.jsonld` reads schema.org `JobPosting` blocks with `html.parser`.
- Each source is its own module.

**Tech Stack:** stdlib `xml.etree.ElementTree`, `html.parser`, `html`, `email.utils`, `json`, `re`,
httpx `MockTransport`, pytest.

**Content authority:** spec §1 premise 5, §2.3, §2.4, §2.5, §6.1 (rows `rss`, `yc`, `letsgetrusty`,
`japandev`, `hn`), §6.2, §11 (fixtures).

**Fixture sources:** the bodies fetched for spec §2.3–§2.5, saved under
`/private/tmp/claude-501/-Users-hiraq-Projects-rstlix0x0-ijob/97b526e9-32f9-40fa-acfd-bc20d7a8e436/scratchpad/c2-grounding/`:

| Source | Saved bodies |
|---|---|
| RSS feeds | `feeds/wwr.body`, `wwr_be.body`, `py.body`, `go.body`, `ex.body`, `dj.body`, `bsj.body` |
| RSS detail pages | `feeds/ex_job.html`, `dj_job.html` |
| JSON-LD pages | `feeds/tt_job.body`, `jh_live.body`, `lv_job.body` |
| YC | `tierb/yc_swe.html`, `yc_remote.html` |
| LetsGetRusty | `tierb/lgr_p1.html`, `lgr_p2.html` |
| JapanDev | `tierb/jd_sitemap.xml`, `jd_d1.html`, `jd_d2.html`, `jd_d3.html` |

HTML fixtures keep only the elements the parser reads (the `ld+json` scripts, the `data-page`
attribute, the "Showing … of … jobs" text) around 2–3 items.

**The HN fixture is synthetic.** It follows the recorded shape (spec §2.5) and uses invented
companies and no names or contact details, because real comments carry personal data (CLAUDE.md).

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/collect.py          — [modify] SourceBatch.still_listed
packages/ijob-sources/src/ijob_sources/jsonld.py           — [create] job_postings, read_job_posting
packages/ijob-sources/src/ijob_sources/rss.py              — [create] parse_rss, FEEDS, RssFeed
packages/ijob-sources/src/ijob_sources/yc.py               — [create] YcJobs, parse_yc_page
packages/ijob-sources/src/ijob_sources/letsgetrusty.py     — [create] LetsGetRusty, parse_lgr_page
packages/ijob-sources/src/ijob_sources/japandev.py         — [create] JapanDev, parse_sitemap
packages/ijob-sources/src/ijob_sources/hn.py               — [create] HnWhoIsHiring, parse_thread, parse_comments
packages/ijob-sources/tests/ijob_sources_tests/fixtures/{rss,jsonld,yc,letsgetrusty,japandev,hn}/ — [create] __init__.py plus trimmed bodies
                                                            (`jsonld/` holds the JobPosting pages; it is named after the reader module rather than spec §11's `jobposting` kind)
packages/ijob-sources/tests/ijob_sources_tests/test_collect.py  — [modify] still_listed default
packages/ijob-sources/tests/ijob_sources_tests/test_jsonld.py   — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_rss.py      — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_yc.py       — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_letsgetrusty.py — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_japandev.py — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_hn.py       — [create]
```

## Order of work

### Task 1 — Report jobs that are still listed

**Files:** Modify `collect.py`; test `test_collect.py`.

**Change:**
- `SourceBatch` gains `still_listed: tuple[ExternalId, ...] = ()` after its existing fields.
- The module docstring's adapter contract says what it means: known jobs the source still lists but
  did not re-fetch.

**Proof:**
- `test_batch_still_listed_defaults_empty`: `SourceBatch(drafts=[], rejected=0).still_listed == ()`.
- Every C1 collect test passes unchanged.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_collect.py`
passes and the gate is green. Commit `feat(sources): report jobs that are still listed`.

### Task 2 — Read schema.org JobPosting blocks

**Files:** Create `jsonld.py`, `fixtures/jsonld/{__init__.py,teamtailor.html,jazzhr.html,lever.html}`;
test `test_jsonld.py`.

**Change:** per spec §6.2.

- `job_postings(html_text: str) -> list[dict[str, Any]]` returns every object of `@type`
  `JobPosting` found in `<script type="application/ld+json">` elements:
  - it walks top-level lists and `@graph`;
  - a block that is not valid JSON is skipped.
- `read_job_posting(obj) -> JobPostingFields` returns a frozen dataclass with:
  - `title`; `company` ← `hiringOrganization.name`;
  - `location` ← the first `jobLocation` `address.addressLocality`; or `address` itself when it is
    a string; or else the first `addressCountry` (spec §6.2: LetsGetRusty carries only that);
  - `country` ← the first `addressCountry` accepted by `parse_country`, or else a string `address`
    through `parse_country` (spec §6.2);
  - `remote`: `TELECOMMUTE` with exactly one `applicantLocationRequirements` country → `COUNTRY`,
    otherwise `None`; a job that is not `TELECOMMUTE` → `None`, because the scope is not stated;
  - `salaries` ← `baseSalary`, `unitText` `YEAR`/`MONTH`/`HOUR`, under the spec §5.1 rule;
  - `posted_at` ← `datePosted`: a date → 00:00 UTC; an offset datetime kept; a naive one → `None`;
  - `identifier` ← `identifier.value` as text, or `identifier` when it is a scalar;
  - `url`.
- `jobLocation` may be an object or a list.

**Proof:**
- `test_teamtailor_page`: title; company `Teamtailor`; location `Stockholm`; country `SE` (list
  `jobLocation`); identifier `"8636263"`; `posted_at` offset kept.
- `test_jazzhr_page_picks_jobposting_block`: two blocks, the `Organization` ignored; salary `EUR
  55000–80000 YEAR`; remote `COUNTRY` (TELECOMMUTE plus `BE`); `posted_at` 2026-10-07T00:00Z.
- `test_lever_page_minimal`: company `Epoch AI`, location `Remote`, country `None`.
- `test_country_only_address`: a `jobLocation` whose address holds only `addressCountry: "Canada"`
  (the LetsGetRusty shape) → location `Canada`, country `CA`.
- `test_string_address_and_naive_date`: the elixirjobs shape (`address` `"Helsinki, Finland"` → `FI`)
  and the djinni `datePosted` without an offset → `None`.
- `test_invalid_json_block_skipped`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_jsonld.py`
passes and the gate is green. Commit `feat(sources): read schema.org job postings`.

### Task 3 — Collect the RSS feeds

**Files:** Create `rss.py` and `fixtures/rss/` (one trimmed feed per key, plus `elixir_offer.html`
and `djinni_job.html`); test `test_rss.py`.

**Change:** per spec §2.3 and the §6.1 `rss` row.

- `parse_rss(body: bytes) -> list[RssItem]` parses RSS 2.0 items into a frozen dataclass of
  `title`, `link`, `pub_date` (`parsedate_to_datetime`, or `None` when absent), `description` (text,
  HTML entities unescaped) and `extras` (every other child element's tag → text).
  - A body that does not parse as XML, or that has no `channel`, raises `ParseError`.
- `FEEDS` maps each key to a `FeedSpec`: URL, title rule, location rule, and whether it needs a
  detail page. All URLs are exactly those of spec §2.3.

  | Key | Title rule | Location rule | Detail page |
  |---|---|---|---|
  | `wwr-programming`, `wwr-backend` | `Company: Role`, split at the first `": "` | the `region` extra | no |
  | `python` | `Role, Company`, split at the last `", "` | the description's first line, up to the first `<` | no |
  | `golang` | `Role @ Company`, after replacing U+00A0 with a space | the description text before `" - "` | no |
  | `bsj` | `Role // Company`, split at the last `" // "` | none | no |
  | `elixir`, `djinni-python` | none | from the detail page | yes |

- A title that lacks its separator is rejected under C1's per-job contract.
- `RssFeed(key, *, known: frozenset[ExternalId] = frozenset())`, ref `rss:<key>`:
  - one conditional GET of the feed URL;
  - `external_id` and `url` ← `link`; `raw` ← the JSON object of the item's fields;
  - for detail feeds:
    - items whose link is in `known` go to `still_listed`;
    - each other item gets one plain GET of its link, at most 100 per fetch, and is mapped from
      `read_job_posting` (title, company, location, country, remote, salaries; spec §2.3 takes the
      title from the detail page too); `posted_at` comes from the item's `pubDate`;
    - `raw` is `{"item": …, "jobposting": …}`;
    - a `SourcesError` on a detail request rejects that item only, logged at `WARNING` and counted;
    - items beyond the cap are neither drafts nor still listed.

**Proof:**
- `test_title_rules` (parametrised; the first two titles are quoted in spec §2.3, the last two are
  verbatim from the saved `go.body` and `bsj.body`):
  - `Yooli: FULL TIME: Software Engineer Position - React and Rest` → company `Yooli`;
  - `SENIOR Back-End Python Engineer - FULLY REMOTE-WORLDWIDE, ActivePrime, Inc.` → company `Inc.`;
    this is the documented lossy case, kept as the spec states;
  - `Senior Software Engineer\xa0@\xa0Sotheby’s` → `Sotheby’s`;
  - `Senior Backend Developer – Go & Kubernetes // DATATRONiQ` → `DATATRONiQ`.
- `test_missing_pubdate_is_none`: the python and golang fixtures.
- `test_pubdate_shapes`: `15 Oct 2026 14:21:22 +0000` and `Fri, 30 Oct 2026 18:37:12 +0200`.
- `test_location_rules`: one per feed with a rule.
- `test_detail_feed_fetches_new_only`: known link → `still_listed`, no request; new link → one detail
  request, company `Treat Health`.
- `test_detail_cap_100`: 120 new items → 100 detail requests and 100 drafts.
- `test_failed_detail_rejects_only_its_item`: one detail answering 410 → `rejected == 1`, the others
  drafted.
- `test_entity_bomb_is_parse_error`: the spec §2.3 bomb body → `ParseError`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_rss.py`
passes and the gate is green. Commit `feat(sources): collect rss job feeds`.

### Task 4 — Collect YC jobs

**Files:** Create `yc.py`, `fixtures/yc/{__init__.py,software_engineer.html,remote.html}`; test
`test_yc.py`.

**Change:**
- `YcJobs(key)`, ref `yc:swe`.
- Two GETs, both conditional (spec §3.1 names "the YC pages"):
  `https://www.ycombinator.com/jobs/role/software-engineer` and `…/software-engineer/remote`.
- `parse_yc_page(html_text) -> list[dict]`:
  - reads the single `data-page` attribute with `html.parser` (the parser unescapes it) as JSON;
  - returns `props.jobPostings`;
  - raises `ParseError` when there is no `data-page` attribute or no postings list.
- Postings from both pages are merged by `id`.
- Mapping: `id` (a JSON integer, `sully`'s is `100474`) as decimal text; url
  `https://www.ycombinator.com` + `url`; `title`; `companyName`; `location`; `posted_at=None`; `raw`
  ← the posting JSON.

**Proof:**
- `test_parse_maps_fields`: the `sully` posting's URL is absolute.
- `test_pages_merged_by_id`: an id on both pages yields one draft.
- `test_relative_dates_ignored`: `createdAt: "3 months"` → `posted_at is None`.
- `test_missing_data_page_is_parse_error`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_yc.py`
passes and the gate is green. Commit `feat(sources): collect yc jobs`.

### Task 5 — Collect LetsGetRusty

**Files:** Create `letsgetrusty.py`, `fixtures/letsgetrusty/{__init__.py,page1.html,page2.html}`;
test `test_letsgetrusty.py`.

**Change:**
- `LetsGetRusty(key)`, ref `letsgetrusty:all`.
- Page 1 is `https://jobs.letsgetrusty.com/?page=1`, conditional.
- The total comes from page 1's text `Showing <a>-<b> of <total> jobs`. Pages
  `2..min(ceil(total/10), 20)` follow, each `/?page=N` alone; no URL carries a `job` parameter (spec
  §2.5 robots).
- Each page's `ItemList` JSON-LD items are read through `read_job_posting`, where:
  - `external_id` ← the `job` query value of `url`;
  - `addressCountry` `"Remote"` → country `None`, remote `None`.
- `ParseError` when page 1 has no total or no `ItemList`.

**Proof:**
- `test_parse_maps_fields`: the fixture's first item → external id equal to the `job` value in its
  `url` (`https://jobs.letsgetrusty.com?job=rec…` gives `rec…`), its `hiringOrganization.name`, and
  location and country from its `addressCountry` name.
- `test_remote_address_country`: `"Remote"` → country `None`, location `Remote`.
- `test_page_count_from_total`: total 19 → 2 requests; total 250 → 20 requests (the cap binds; 25
  pages would be needed).
- `test_no_job_param_requested`: every requested URL lacks `job=`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_letsgetrusty.py`
passes and the gate is green. Commit `feat(sources): collect letsgetrusty jobs`.

### Task 6 — Collect JapanDev through its sitemap

**Files:** Create `japandev.py`, `fixtures/japandev/{__init__.py,sitemap.xml,job_remote.html,job_salary.html}`;
test `test_japandev.py`.

**Change:**
- `JapanDev(key, *, known: frozenset[ExternalId] = frozenset())`, ref `japandev:all`.
- One conditional GET of `https://japan-dev.com/cdn/sitemaps/sitemap.xml`.
- `parse_sitemap(body) -> list[str]` keeps `<loc>` URLs whose path starts with `/jobs/`. Each URL's
  last path segment is its id; spec §2.5 records that `identifier.value` is that slug.
- Known ids go to `still_listed`.
- New ids get one plain GET each, at most 100 per fetch, mapped from `read_job_posting`:
  - `external_id` ← `identifier`, else the slug;
  - `url` ← `url`;
  - `raw` ← `{"item": <sitemap URL>, "jobposting": …}`;
  - a failed detail request rejects only that job.
- The adapter never calls any search API or `__NUXT_DATA__` endpoint (CLAUDE.md, spec §12).

**Proof:**
- `test_sitemap_keeps_job_urls`: non-job `<loc>` entries are dropped.
- `test_known_jobs_still_listed_without_request`.
- `test_new_job_mapped`: the remote job → `TELECOMMUTE` with many countries → remote `None`, country
  `JP`. The salary job → `JPY 8000000–16000000 YEAR`.
- `test_detail_cap_100_and_failure`: 120 new ids → 100 requests; a 404 detail → rejected only.
- `test_only_sitemap_and_job_pages_requested`: every requested path is the sitemap or `/jobs/…`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_japandev.py`
passes and the gate is green. Commit `feat(sources): collect japandev through its sitemap`.

### Task 7 — Collect HN "Who is hiring"

**Files:** Create `hn.py`, `fixtures/hn/{__init__.py,threads.json,comments.json}`; both are
**synthetic**, in the spec §2.5 shape and with the spec §2.5a JSON types (a string `objectID`; an
integer `parent_id` and `story_id`), with invented companies and no real names, emails or handles.
Test `test_hn.py`.

**Change:**
- `HnWhoIsHiring(key)`, ref `hn:whoishiring`.
- `GET https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring`.
  `parse_thread(body) -> int` returns the `objectID` of the newest hit whose title starts with
  `Ask HN: Who is hiring?`, converted to an integer, or raises `ParseError` when there is none. The
  JSON types are recorded in spec §2.5a: a thread `objectID` is a string, and a comment's
  `parent_id` and `story_id` are integers.
- Then `GET https://hn.algolia.com/api/v1/search?tags=comment,story_{id}&hitsPerPage=1000`.
  `parse_comments(body, story_id: int)` keeps hits whose integer `parent_id` equals `story_id`.
  Comparing the string `objectID` with `parent_id` keeps none of them (spec §2.5a).
- The first line is `comment_text` with entities unescaped, cut at the first `<p>`, and tags
  removed. It is split on `|` with each field stripped:
  - field 1 → company; field 2 → title; field 3, when present, → location;
  - fewer than two fields → rejected.
- `external_id` ← `objectID`; `url` ← `https://news.ycombinator.com/item?id=<objectID>`; `posted_at`
  ← `created_at`; `low_confidence=True`; `raw` ← the hit JSON.

**Proof:**
- `test_thread_selected_by_title`: a newer "Who wants to be hired?" hit is skipped.
- `test_top_level_only`: replies are ignored.
- `test_fixture_keeps_recorded_types`: the fixture's thread `objectID` is a `str`, and every
  `parent_id` is an `int`. A top-level comment is kept even though its `parent_id` and the thread
  `objectID` differ in JSON type.
- `test_first_line_fields`: `Acme Robotics | Senior Backend Engineer | Berlin | REMOTE` → company,
  title, location; entities such as `&#x2F;` are unescaped.
- `test_single_field_rejected`.
- `test_all_low_confidence`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_hn.py`
passes and the gate is green. Commit `feat(sources): collect hn who is hiring`.

---

## Risks

- **Most risky: Tasks 3 and 6's detail-only-when-new logic.** Only the known set decides what is
  fetched. An empty known set at first run means up to 100 detail requests per source, paced at
  1/s, which is about 100 s each.
- python.org's `Role, Company` rule is lossy by nature (spec §2.3). Its test documents the loss
  instead of hiding it.
- **Personal data:** the HN fixtures must be invented; reviewers check that no real author, email or
  handle appears.
- The fixture bodies live in a session scratch directory. If it is gone, re-fetch the spec §2.3–§2.5
  URLs once with ijob's User-Agent and trim again.
- Not done here: the `feed:<h>` and `jobposting:<h>` manual-URL sources (plan 09); passing known ids
  from the store (plan 10).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-sources` passes, the C1 tests included.
- `grep -rn "meili\|__NUXT_DATA__" packages/ijob-sources/src` prints nothing.

## Review findings

One `claudestacks:reviewer` pass (2026-10-31), report `.airsstack/.../20261031-130611-d66e/22-reviewer-08.md`.
Verdict: COMPLIANT-WITH-AMENDMENTS. Blocking set: B1–B3, all fixed in one fix round.

| Id | Finding | Disposition |
|---|---|---|
| B1 🔴 | RSS detail-page GETs followed an item link on any host | fixed: https and the feed URL's hostname only; else rejected with no request (7 host cases tested) |
| B2 🔴 | a NaN/Infinity salary bound raised `decimal.InvalidOperation` out of `read_job_posting`, crashing the run | fixed: `InvalidValueError` |
| B3 🔴 | an unknown declared XML encoding raised `LookupError` (rss, japandev) | fixed: `ParseError` |
| 🟡 | JapanDev `known` matched the slug while `external_id` was the identifier | fixed: the URL slug is the id everywhere |
| 🟡 | HN `int()` on an over-long objectID raised `ValueError` | fixed: at most 18 digits, else skipped/rejected |
| ❓ | LetsGetRusty carried a private copy of the ld+json collector | fixed: public `jsonld.ld_json_blocks`; `job_postings` built on it, unchanged |
| 🔵 (from plan 06 review) | `collect.py` adapter-contract docstring stale | fixed |
| 🟡 | HN `hitsPerPage=1000` never checks `nbPages` | kept; a thread above 1000 top-level comments is truncated |
| ❓ | a LetsGetRusty page after page 1 without an ItemList fails the fetch | kept; same rule as YC |
| 🔵 | duplicated private helpers across adapters | deferred: cross-plan follow-up |

Fix round re-verification (scoped): 14 files formatted, ruff passed, mypy clean on 14 files, pytest on
the seven plan-08 test files 295 passed; the `meili|__NUXT_DATA__` grep prints nothing.

## Probe results

- Whole-workspace gate after plans 06–08 were coded: 141 files formatted, ruff passed, mypy 97 files
  clean, 1238 passed; the plan's grep prints nothing.
- Entity bomb: on Python 3.14 the C `XMLParser` exposes no `.parser`, so no expat entity handler can be
  installed (coder probe). `parse_rss` refuses a body containing `<!ENTITY` or NUL before parsing; the
  sitemap parser relies on expat's amplification limit, which raises and maps to `ParseError` (tested
  with a real bomb body).

## Deviations

- 2026-10-31, spec §6.1 rss row vs plan Task 3/§6.2: country, remote and salaries of detail-page feeds
  are read from the detail page's JobPosting, as the plan says; the §6.1 row says they stay default.
- 2026-10-31, plan Task 3: detail fetches are pinned to the feed's host (review B1). A UTF-16 feed is
  refused by the `<!ENTITY` byte check.
- 2026-10-31, plan Task 7: HN tags are stripped before entities are unescaped (an escaped `&lt;3` must
  not be read as a tag); `parse_comments(body, story_id, key)` takes the key a draft needs.
- 2026-10-31, plan Task 4: `parse_yc_page` returns `list[object]`; malformed elements are rejected per job.
- 2026-10-31, plan Task 6: `parse_sitemap` also requires https, host `japan-dev.com`, no query or
  fragment; the JapanDev id is always the URL slug.
- 2026-10-31, plan Task 2: `JobPostingFields` fields are optional (`None` when absent); a present but
  unusable field raises `InvalidValueError`; `remote` counts only `@type == "Country"` requirements.
