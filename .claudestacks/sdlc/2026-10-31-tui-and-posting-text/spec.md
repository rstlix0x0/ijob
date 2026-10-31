---
status: approved
created: 2026-11-01
built-on: 31a2a8c75254
---

# Spec: TUI and posting text

This spec makes every posting carry its job description as normalized text, adds 👍/👎 labels with an
optional note and a stored CV, and puts a Textual TUI and matching CLI commands over one shared,
async service layer. It delivers milestone ijob-v1/03. It builds on the sources-and-filters model,
store, polite client and `Source` protocol, and changes only what §1 lists.

## 1. Design premises

1. **Extend, do not change, the existing contracts.** The `Source` protocol
   (`packages/ijob-sources/src/ijob_sources/collect.py:49-70`: `ref`, `async fetch(client) ->
   SourceBatch`), the store's existing methods and the CLI's existing flags keep their meaning. This
   chain makes these changes:

   | Element | Change |
   |---|---|
   | `SightingDraft` (`ijob_core/model.py:158-193`) | new field `description: str \| None = None` |
   | `Posting` (`ijob_core/model.py:224-259`) | new field `description: str \| None = None` |
   | store schema | migration 3 (§4) |
   | `Store` | new methods (§4) |
   | the adapters of the 17 kinds in §2.1 | expose `description_from_raw` and fill `draft.description` (§3.2) |
   | `ijob_sources` | new modules `description` (§3.1) and `details` (§3.3) |
   | `ijob_app` | new module `service` (§5); `cli` becomes a thin layer over it and gains commands (§6); new package `tui` (§7) |
   | `ijob search` output | a leading posting-id column (§6) |

2. **Description is normalized once, when recorded** (approach A, chosen in design over deriving it
   from `sightings.raw` on every read). When a posting has no stored description — a row stored before
   migration 3, a job a source no longer re-drafts, or a kind whose list carries none — it is derived
   the first time the user opens or labels the posting: from the stored `raw` for the 17 kinds of §2.1
   (no network), by one fetch for the 2 kinds of §2.2. The result is written to the same column.
3. **Normalized text is a Markdown subset** (decided in design): headings, paragraphs, lists, emphasis,
   links, code. It is the intent's "clean, normalized plain text" with simple structure kept, renders
   in Textual's `Markdown` widget, and reads as text in the CLI.
4. **One shared async service layer.** The search pipeline today lives inside the `search` command
   (`ijob_app/cli.py:635-711`). It moves to `ijob_app.service`, whose network-touching methods are
   `async`; the CLI drives them with `asyncio.run`, the TUI with async workers on its own event loop
   (§7, probe). Driven by the guideline's one-responsibility-per-module rule
   (`references/modularity.md`).
5. **Boundaries hold.** `ijob-core` imports no network and no LLM library (CLAUDE.md). The
   normalizer, `description_from_raw` functions and fetchers live in `ijob-sources`; the core stores
   text it is given.

## 2. What the sources return

`SourceKind` has 19 members (`ijob_core/model.py:37-55`), and `build_source` has one branch for each
(`ijob_sources/registry.py:72-108`). Each adapter stores `raw` as the whole job object or item (e.g.
`greenhouse.py:190,197-204`; `rss.py:388,416`; `japandev.py:202-209`; `manual.py:170,271,275`).

Fixtures were trimmed when recorded (sources-and-filters spec §11), so field presence was checked
against live payloads where a fixture could mislead. The live probes ran on 2026-11-01 through ijob's
own `PoliteClient` (robots.txt checked; User-Agent `polite_http.py:34-37`). Scripts and saved bodies:
`$SCRATCH/probes/{detail_probe,lever_workable_probe,list_and_jsonld_probe,kalibrr_probe}.py`,
`live_*.{json,html}`, `kalibrr_page.html`.

### 2.1 Description in the stored payload: 17 kinds

`TextForm` is defined in §3.1. `DETECT` means HTML when the text matches `<[a-zA-Z][^>]*>`, else PLAIN.

| Kind | Field(s) | Form | Evidence |
|---|---|---|---|
| greenhouse | `content` | HTML after one `html.unescape` | fixture `greenhouse/tailscale_jobs.json:35` `&lt;p&gt;Trimmed job description.&lt;/p&gt;` |
| gem | `content` | HTML | `gem/retool_job_posts.json:4` |
| ashby | `descriptionHtml` | HTML | `ashby/ramp_job_board.json:53` |
| lever | `description`, then each `lists[]` as `## text` + `content`, then `additional` | HTML | live, §2.3 |
| himalayas | `description` | HTML | `himalayas/page1.json:39` |
| jobicy | `jobDescription` | HTML | `jobicy/page1.json:28` |
| remoteok | `description` | HTML | `remoteok/api.json:18` |
| arbeitnow | `description` | HTML | `arbeitnow/page1.json:7` |
| fourdayweek | `description` | MARKDOWN | `fourdayweek/page1.json:17` `#### Company Description…` |
| mycareersfuture | `description` | HTML | live list: first result's keys include `description`; value `<p><strong>Location: </strong>Central (Hybrid) </p>…` |
| kalibrr | `description`, then `qualifications` | HTML | live list: job keys include `description` and `qualifications`; values `<p><strong><u>Key Responsibilities</u>…` and `<p><strong><u>Requirements</u>…` |
| rss | item `<description>` (list feeds); `jobposting.description` (detail feeds) | DETECT | list: `rss.py:135` already unescapes, so no second unescape; detail: live djinni page JSON-LD `has tags: False` `We are looking for a Senior AI Platform Engineer…` |
| letsgetrusty | `description` | MARKDOWN | live page JSON-LD: `has tags: False`, `**About the Role** We are hiring a **Backend Software Engineer…` |
| japandev | `jobposting.description` | HTML | live job page JSON-LD: `has tags: True`, `<p><strong>Location requirement</strong></p>…` |
| hn | `comment_text` | HTML | `hn/comments.json:6` `…REMOTE<p>We build warehous…` |
| jobposting (manual page) | `description` | DETECT | any site's JSON-LD: the three live samples above show HTML, Markdown and plain text |
| feed (manual feed) | item `<description>` | DETECT | same reader as rss (`manual.py:271`) |

### 2.2 No description in the list payload: 2 kinds, verified detail routes

| Kind | Live list evidence | Detail route | Built from | Live detail probe |
|---|---|---|---|---|
| workable | widget job keys: `application_url, city, code, country, created_at, department, education, employment_type, experience, function, industry, locations, published_on, shortcode, shortlink, state, telecommuting, title, url` | `https://apply.workable.com/api/v2/accounts/{key}/jobs/{shortcode}` | source key + `external_id` (= `shortcode`, `workable.py:167`) | `200 application/json`; keys include `description`, `requirements`, `benefits`; `description: <p></p><p>At Hugging Face…` |
| yc | page `props.jobPostings` (`parse_yc_page`, `yc.py:90-120`): 40 jobs; keys `applyUrl, askUs, companyBatchName, companyLogoUrl, companyName, companyOneLiner, companyUrl, createdAt, ctaText, ctaUrl, equityRange, hiringManager, id, isIncomplete, lastActive, location, minExperience, minSchoolYear, prettyRole, role, roleSpecificType, salaryRange, skills, title, type, url, visa` | the job page | `_ORIGIN` + `raw["url"]` (`yc.py:31,165,174`; `raw` holds the site path) | `200 text/html`; one JSON-LD JobPosting; `description: <p><strong>WHO WE ARE</strong></p>…` (form DETECT) |

### 2.3 Lever's full description

The recorded Lever fixture's job keys are `applyUrl, categories, country, createdAt, descriptionPlain,
hostedUrl, id, text, workplaceType`, and `lists` is empty. A live probe of
`https://api.lever.co/v0/postings/spotify?mode=json&limit=1` returned `additional, additionalPlain,
applyUrl, categories, country, createdAt, description, descriptionBody, descriptionBodyPlain,
descriptionPlain, hostedUrl, id, lists, opening, openingPlain, text, workplaceType`; `lists` items
carry `text` (`What You'll Do`, `Who You Are`) and HTML `content`.

## 3. Sources (`ijob_sources`)

### 3.1 Normalizer (`ijob_sources.description`)

`normalize(text: str, form: TextForm) -> str | None`, with `TextForm` a `StrEnum` `HTML | MARKDOWN |
PLAIN | DETECT`. Built on the standard library's `html.parser`, already used by `jsonld.py:15,63` and
`yc.py:14,76`; no new dependency and no GPL converter.

- HTML: entities are unescaped by the parser (`HTMLParser().convert_charrefs` defaults to `True`;
  probe: feeding `<p>R&amp;D &lt;b&gt; caf&eacute;</p>` yields data `R&D <b> café`). Tags map to
  Markdown: `h1–h6` → `#`…, `p`/`div` → paragraph, `br` → line break, `ul`/`ol`/`li` → `-` / `1.`
  items, `strong`/`b` → `**`, `em`/`i` → `*`, `a href` → `[text](href)` for `http(s)` hrefs only,
  `pre`/`code` → fenced or inline code. `script`, `style`, `img`, `iframe` and their content are
  dropped; every attribute but `href` is dropped; unknown tags keep their text. Text nodes are
  Markdown-escaped (`\ * _ [ ] ` + "`" + ` <`, and `#` at a line start), so source text cannot form
  markup.
- PLAIN: lines and paragraphs kept, text Markdown-escaped as above.
- MARKDOWN: passed through, except that a link or image whose target is not `http(s)` is replaced by
  its text and every image is dropped.
- Greenhouse's escaped HTML is unescaped once with `html.unescape` by its adapter before
  `normalize(…, HTML)` (probe: the fixture's `&lt;p&gt;Trimmed job description.&lt;/p&gt;` becomes
  `<p>Trimmed job description.</p>`).
- Whitespace is collapsed (no more than one blank line in a row); the result is cut at 50,000
  characters; empty → `None`.

Cost on the search path: one `html.parser` pass over the first 100 live MyCareersFuture descriptions
(312,977 characters) takes 11.9 ms per batch, 119 µs per description (timeit, 10 runs). The polite
client spaces requests to one host by at least `min_interval=1.0` s (`polite_http.py:105`), so
normalizing a page of postings costs well under one request's spacing; search stays as fast as today
to within that.

### 3.2 Adapters: `description_from_raw`

Each adapter module of a §2.1 kind gains `description_from_raw(raw: str) -> str | None`, which reads
the §2.1 field(s) from its own `raw` and returns `normalize(…, form)`. Its parser calls it to set
`SightingDraft.description`. A missing or non-string field gives `None`; it never rejects the job (the
per-job rejection rule is unchanged).

### 3.3 Lazy description (`ijob_sources.details`)

```python
async def describe(sighting: Sighting, client: PoliteClient) -> str | None
```

A `match` on `sighting.source.kind` with one `case` per `SourceKind` member, closed by
`assert_never`, as `registry.py:72-110` does. The 17 §2.1 kinds return
`<adapter>.description_from_raw(sighting.raw)` with no request. Workable and YC `GET` their §2.2 route
through `client` (robots, spacing and User-Agent as today) and normalize: Workable joins
`description`, `requirements` and `benefits` under `## Requirements` / `## Benefits`; YC reads the
page's JSON-LD with `jsonld.job_postings` (`jsonld.py:114`) and normalizes its `description` with
DETECT. Errors raise the existing `SourcesError` subclasses (`ijob_sources/errors.py:6-116`); a page
or response without the field raises `ParseError`.

## 4. Store migration 3 (`ijob_core.store`)

Appended to `_MIGRATIONS` (`store.py:41`) and applied by `_migrate` (`store.py:720-737`):

```sql
ALTER TABLE postings ADD COLUMN description TEXT;
CREATE TABLE labels (
    posting_id INTEGER PRIMARY KEY REFERENCES postings(id),
    vote TEXT NOT NULL CHECK (vote IN ('up', 'down')),
    note TEXT, labeled_at TEXT NOT NULL
) STRICT;
CREATE TABLE cv (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, stored_at TEXT NOT NULL
) STRICT;
```

- `record` writes `description` on insert, and on update with `COALESCE(?, description)`, the rule
  already used for `country` and `remote` (`store.py:428-430`): a draft without a description keeps
  the stored one. Existing rows keep `NULL` until a re-draft or the lazy path (§1.2) fills them.
- New types in `ijob_core.model`: `Vote(StrEnum)` `UP | DOWN`; frozen slotted `Label(posting_id:
  PostingId, vote: Vote, note: str | None, labeled_at: datetime)`; `StoredCv(name: str, sha256: str,
  size: int, stored_at: datetime)`.
- New `Store` methods: `posting(id) -> Posting | None`; `sightings_of(posting_id) -> list[Sighting]`
  (id order); `set_description(posting_id, text)`; `set_label(posting_id, vote, note, *, at)` (insert
  or replace); `clear_label(posting_id)`; `label(posting_id) -> Label | None`; `labels(ids:
  Collection[PostingId]) -> dict[PostingId, Label]`; `set_cv(StoredCv)`; `cv() -> StoredCv | None`.
  Timestamps reject naive datetimes, as `record` does (`store.py:314-315`).
- A label stays on its posting because a posting's sightings are never moved to another posting and no
  posting is deleted. Search: `grep -n "UPDATE sightings\|DELETE FROM" store.py` → only `:334` and
  `:354`, both `UPDATE sightings SET last_seen …` with no `posting_id`; control: the same grep method
  finds `UPDATE postings` at `:340` and `:426`.

## 5. Service layer (`ijob_app.service`)

A `Service` built from an open `Store`, the database path, the client factory (`_ClientFactory`,
`cli.py:77`), an HTTP cache and a clock. `open_service(db, client_factory, *, now, environ)` opens the
seeded store at `db or default_db_path(environ)` and the cache at `default_cache_path(environ)`, as
`_seeded_store` and `_open_cache` do (`cli.py:186-225`); taking `environ` lets tests keep every path
under a temporary directory, as the CLI tests' `_env` does (`test_cli.py:105-111`). A cache that
cannot be opened raises `CacheUnavailableError`, which the CLI maps to today's message and exit 1
(`cli.py:216-222`). The store and cache are opened, and every service method runs, on the thread that
runs the event loop: `asyncio.run` in the CLI, Textual's loop in the TUI (§7). `Store.open` uses a
default `sqlite3.connect` (`store.py:270`), which binds the connection to its thread.

| Method | Does |
|---|---|
| `async refresh(refs) -> RefreshReport` | the pipeline of `cli.py:696-703`: choose sources, split by interval at one clock read (`cli.py:698`), build, collect, record at a second read taken after fetching (`cli.py:701`). `RefreshReport` holds `due`, `kept`, `fetched` (`_record`'s refs), `rejected` (each fetched source's `SourceBatch.rejected` above zero, `collect.py:45`), `failures`, `refused` (`_build_sources`'s refusals, `cli.py:520-522`) and `stale`, so the CLI keeps today's `kept <ref>: …` lines (`cli.py:510-514`), problem lines including `<ref>: skipped N invalid jobs` (`cli.py:576-591`), stale lines (`cli.py:593-603`) and exit 1 rule (`cli.py:709-710`). An unknown or removed named source raises `UnknownSourceError(InvalidValueError)` naming it (today `cli.py:480-487`) |
| `open_postings(spec, refs) -> list[Posting]` | `store.open_postings` + `keep` (`cli.py:704-707`); a named ref that is unknown or removed raises `UnknownSourceError`, the same check as `refresh` |
| `labels(ids) -> dict[PostingId, Label]` | `store.labels(ids)`, with no description step and no request; the TUI's label column reads it |
| `async detail(posting_id) -> PostingDetail` | posting, its sources, label and description. When the description is `None`, calls `details.describe` on each sighting in id order and stores the first text found; a `SourcesError` is kept as `unavailable_reason`, nothing is stored, and the next call tries again |
| `async label(posting_id, vote, note)` / `clear_label(posting_id)` | through the store with `at=now()`; `label` also runs the description step of `detail` |
| `store_cv(path) -> StoredCv` | checks the file (§8), copies it to `cv.pdf` in the directory of the database in use, writes the row |
| sources operations | the existing `sources` command bodies, moved: `sources()`, `remove_source(ref)`, and async `add_source(ref, company)` and `add_url(url, company)`, each of which fetches once to verify the new source before storing it, as `sources add` and `add-url` do today (`_verify`, `cli.py:238-253`, called at `cli.py:350`) |

## 6. Command line (`ijob_app.cli`)

| Command | Behaviour |
|---|---|
| `ijob search …` | flags unchanged; each line gains the posting id as its first tab-separated column, before the `? ` low-confidence flag (`cli.py:604-611`) |
| `ijob show ID` | title, company, location, salaries, country and remote scope, posted date, sources, URL, label and note, then the description; `description unavailable: <reason>` when none |
| `ijob label ID up\|down [--note TEXT]` | sets or replaces the label |
| `ijob label ID --clear` | removes the label |
| `ijob cv set PATH` | stores the CV (§8) |
| `ijob cv show` | name, size, stored date, or `no CV stored` |
| `ijob tui` | opens the TUI |
| `ijob sources …` | unchanged |

Every new command takes `--db` as `search` does; `tui` also takes `--config`. Exit codes reuse
`_EXIT_REFUSED = 1` and `_EXIT_BAD_ARGUMENT = 2` (`cli.py:74-75`).

## 7. TUI (`ijob_app.tui`, Textual 8.2.8)

Textual 8.2.8 is the latest release on PyPI (`$SCRATCH/probes/pypi_textual.json`, fetched 2026-11-01:
`info.version` = `8.2.8`); its wheel METADATA states MIT, Requires-Python `>=3.9,<4.0` and the
`Python :: 3.14` classifier, and it ships `textual/py.typed`. It is added to `ijob-app`'s dependencies.

```
┌ ijob ──────────────────────────────────────────────────────────────────────────┐
│ source [  ] country [  ] remote [  ] since [  ] company [  ] min salary [  ] [x] salary required │
├──────────────────────────────┬─────────────────────────────────────────────────┤
│ results (DataTable)           │ detail (Markdown)                                │
│ label · title · company · loc │ metadata · label + note · URL · description      │
├──────────────────────────────┴─────────────────────────────────────────────────┤
│ r refresh · u 👍 · d 👎 · n note · x clear · s sources · c CV · q quit             │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **Filters** cover every existing search option: comma-separated `source` (`kind:key`, read by
  `parse_source_ref`), `country`, `remote` and `company`, single-value `since` and `min salary`
  `Input`s, and `require salary` as a `Checkbox` (`widgets/_checkbox.py:8`). They are read by the
  functions the CLI uses (`ijob_app/config.py`: `parse_since`, `parse_min_salary`, `parse_remote`,
  `parse_country_flag`), through the CLI's flag reader moved to `ijob_app.config` as `FilterFlags` and
  `read_filters` (today `_FilterFlags`, `_read_filters`, `cli.py:403-460`). The inputs start filled
  with the config file's values, rendered back to text by `flags_from_spec`; as they change,
  `spec_from_flags` sets every flag-able field from the inputs, so an emptied input removes that
  filter. An invalid value marks its input with the `-invalid` class Textual's `Input` styles
  (`widgets/_input.py:222-226`).
- **Results** open on stored open postings, with no fetch; the first column shows the label.
- **Detail** shows the fields of `ijob show`. A missing description is fetched in an async worker.
- **Workers**: every awaiting service call runs in an async `@work` worker (`_work_decorator.py:74`)
  on the app's loop, in its own group — `refresh`, `detail`, `label`, `sources` — exclusive within that
  group only. `exclusive` cancels all workers in the same group (`_work_decorator.py:82-84,95`;
  `worker_manager.py:75-76` `cancel_group`), so a shared group would let moving the cursor cancel a
  running refresh.
- **Refresh** (`r`) runs `Service.refresh` in a `refresh` worker, then reloads the list; failures appear as notifications (`App.notify`,
  `app.py:4621`). Textual refuses a non-async worker unless it is a thread worker
  (`_work_decorator.py:110-115`); async workers keep the store on the loop's thread. Probe
  (`$SCRATCH/probes/test_worker_probe.py`, run under the project's pytest configuration with
  `uv run --locked --with textual==8.2.8 pytest -c pyproject.toml --rootdir . …`): an app opens a
  sqlite connection, an `@work(exclusive=True)` async method awaits an `httpx.MockTransport` request
  then writes and reads the connection and fills a `DataTable`; after `pilot.press("r")` and
  `app.workers.wait_for_complete()` (`worker_manager.py:172`) `row_count == 1` → `2 passed in 0.72s`
  together with the widget probe of §9.
- **Links**: the detail `Markdown` is built with `open_links=False` (`widgets/_markdown.py:1035,1045`)
  and handles `Markdown.LinkClicked` (`:1107`), opening only `http(s)` targets with `App.open_url`
  (`app.py:4790`).
- **Sources** (`s`): a screen listing sources, with add (`kind:key` and company), add URL (URL and
  company) and remove, as `ijob sources add/add-url/remove` take them (`cli.py:337-398`).
- **CV** (`c`): a screen with a path `Input` (`~` expanded) and the current CV's details. Textual has
  no file-open dialog: `grep -rln "class .*FileOpen\|class .*FilePicker\|class .*FileDialog"` over the
  wheel finds nothing, while the same search method finds `class DirectoryTree`
  (`widgets/_directory_tree.py:33`).
- **Note** (`n`): a `ModalScreen` (`screen.py:2158`) with an `Input`, saved with the posting's
  current vote. On a posting with no label it opens nothing and asks for a vote first, so a note never
  creates a vote the user did not give.
- **Labels in the list** come from `Service.labels(ids)` (§5), so listing never fetches.
- Widgets used: `DataTable` (`widgets/_data_table.py:268`), `Markdown` (`widgets/_markdown.py:962`),
  `Input` (`widgets/_input.py:71`), `Checkbox`, `ModalScreen`.

## 8. Errors

| Situation | CLI | TUI |
|---|---|---|
| unknown posting id | exit 2, `no posting N` | not reachable (rows come from the list) |
| unknown or removed named source | exit 2 with today's hint (`cli.py:482-487`) | the `source` field is marked invalid |
| description fetch fails, robots disallows, or no field | shown with `description unavailable: <reason>`, exit 0 | a note in the detail pane |
| CV path missing or unreadable, not starting with `%PDF-`, or over 20 MB | exit 2 naming the reason | message under the input |
| a source fails during refresh | stderr line, as today (`cli.py:576-591`) | notification; other sources continue |
| bad filter value | exit 2, as today | the field is marked invalid; the list keeps its last valid filter |

The PDF check reads the first five bytes. A real PDF starts with them (`head -c 8` of
`/System/Library/CoreServices/SystemUIServer.app/Contents/Resources/menuitemFlipped.pdf` →
`%PDF-1.3`); a Python source file does not (`model.py` → `"""Do`). CV errors and
`UnknownSourceError` are `InvalidValueError`s (`ijob_core/errors.py:8`). New handlers use
`raise … from` (`references/errors.md`).

## 9. Testing

Tests and CI never touch the network or an LLM.

| Unit | Test |
|---|---|
| `description.normalize` | table tests: each form; escaped and raw HTML; each mapped tag; dropped `script`/`style`/`img`; non-http `href`; Markdown escaping of text; MARKDOWN link and image filtering; DETECT both ways; whitespace; the 50,000-character cut; empty → `None` |
| fixtures | each §2.1 kind's fixture gets one real description, captured live during execution and trimmed to its first ~2,000 characters at a tag boundary, so tests see each source's real shape; Lever is re-recorded with `description`, `lists`, `additional`; MyCareersFuture and Kalibrr with `description` (and `qualifications`) |
| `description_from_raw` × 17 kinds | the fixture's draft carries the expected normalized description |
| `details.describe` | a §2.1 kind makes no request; Workable and YC detail fixtures served with `httpx.MockTransport`; a response without the field raises `ParseError` |
| store | migration 2 → 3 on a version-2 database; COALESCE keeps a stored description; label set, replace, clear; `labels(ids)`; CV row |
| service | `MockTransport` and a temporary database, as `test_cli.py:96,131-133`; the lazy path fills a pre-migration row from `raw` |
| CLI | `typer.testing.CliRunner` (`test_cli.py:25,133`): search id column, show, label, cv set/show, unknown source |
| TUI | tests mirror the modules under `ijob_app_tests/tui/` (guideline `test-mandate.md`); `App.run_test()` with Pilot inside `asyncio.run`. Widget probe (`$SCRATCH/probes/test_textual_probe.py`): `DataTable` + `Markdown`, `pilot.press("down")`, `row_count == 1`; with the §7 worker probe under the project's pytest configuration → `2 passed in 0.72s` |
| CV | a tiny made-up PDF; no personal data |

`$SCRATCH` is `/private/tmp/claude-501/-Users-hiraq-Projects-rstlix0x0-ijob/414123fb-5882-46e9-9bbe-ce9eef8996d3/scratchpad`.

## 10. Non-goals

- LLM features (match reasons, judging, scoring) — milestone ijob-v1/04.
- Reading or parsing the CV's contents.
- Splitting descriptions into fields (requirements, benefits, stack).
- Descriptions for sources without an allowed route, text in images, and OCR.
- Fetching descriptions for every posting during search.
- Exporting labels or building tuning datasets — milestone ijob-v1/05.
- A file-browser widget for the CV.
- Scheduling or background refresh outside an explicit `r` or `ijob search`.
