---
status: done
created: 2026-11-01
depends-on: [01, 02]
built-on: b175307f42cd
disproves: a9902751b191
---

# Payload Descriptions Implementation Plan

**Goal:** Every draft from the 17 payload kinds of spec §2.1 carries its normalized description, read by a per-module `description_from_raw`.

**Architecture:**
- Each adapter module of a §2.1 kind gains a public `description_from_raw(raw: str) -> str | None`. It parses the module's own `raw` JSON and delegates to a private `_description(...)` helper that the module's parser also calls when it builds a `SightingDraft`, so record time and the lazy path (plan 04) read the same field(s) the same way (spec §3.2).
- Field(s) and `TextForm` per kind are exactly spec §2.1's table. A missing or non-string field gives `None` and never rejects the job; a `raw` that is not a JSON object raises `ParseError` (`ijob_sources/errors.py:79`).
- Module ownership: `manual.py` owns the `jobposting` kind; the `feed` kind stores `rss.item_raw` (`manual.py:35,271`), so it is read by `rss.description_from_raw`. `rss.description_from_raw` handles both rss raw shapes: an item object (`rss.py:468-492`) and the detail wrapper `{"item": …, "jobposting": …}` (`rss.py:416`).
- Fixtures were trimmed with stub descriptions; each task replaces the stub of one job per fixture with a real description captured live (spec §9), trimmed to about its first 2,000 characters at a tag boundary.

**Tech Stack:** Python 3.14, `ijob_sources.description` (plan 02), `SightingDraft.description` (plan 01), pytest, `httpx.MockTransport` (existing adapter tests).

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/{greenhouse,gem,ashby,lever}.py                       — [modify] description_from_raw; drafts carry description
packages/ijob-sources/src/ijob_sources/{himalayas,jobicy,remoteok,arbeitnow,fourdayweek}.py  — [modify] same
packages/ijob-sources/src/ijob_sources/{mycareersfuture,kalibrr}.py                           — [modify] same
packages/ijob-sources/src/ijob_sources/{rss,manual,letsgetrusty,japandev,hn}.py               — [modify] same
packages/ijob-sources/tests/ijob_sources_tests/test_<module>.py                                — [modify] one per module above
packages/ijob-sources/tests/ijob_sources_tests/fixtures/<module>/…                             — [modify] a real description in one job per fixture
```

**Recording a fixture description** (every task): the implementer fetches the source once through ijob's `PoliteClient` (robots.txt and User-Agent as in production), copies the first ~2,000 characters of one job's description field(s), cut at a tag or line boundary, into that job in the existing fixture, and leaves every other key as it is. This is a one-off manual capture; no test touches the network.

## Order of work

### Task 1 — Hiring systems: greenhouse, gem, ashby, lever

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/greenhouse.py`, `gem.py`, `ashby.py`, `lever.py`
- Modify fixtures `greenhouse/tailscale_jobs.json`, `gem/retool_job_posts.json`, `ashby/ramp_job_board.json`, `lever/spotify_postings.json`
- Test `test_greenhouse.py`, `test_gem.py`, `test_ashby.py`, `test_lever.py`

**Change:**
- greenhouse: `content`, `html.unescape` once, then HTML.
- gem: `content`, HTML.
- ashby: `descriptionHtml`, HTML.
- lever: an HTML document assembled from `description`, then for each `lists[]` item an `<h2>` of its `text` (passed through `html.escape` first, so `&` or `<` in a heading stays text) followed by its `content`, then `additional`; normalized as HTML. The Lever fixture's first job gains `description`, `lists` (two items) and `additional` from a live capture (spec §2.3).

**Proof:** written first, seen failing, then passing — in each of the four test modules:
- `test_draft_carries_description` — parsing the fixture gives a first draft whose `description` equals the expected Markdown for the recorded text (compared with a literal written from the fixture).
- `test_description_from_raw_matches_draft` — `description_from_raw(draft.raw) == draft.description`.
- `test_description_missing_gives_none` — a job without the field still yields a draft, with `description is None`.
- In `test_lever.py` additionally `test_lever_description_includes_lists_and_additional` — the output contains `## What You'll Do`-style headings for each list item and the `additional` text, in that order.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_{greenhouse,gem,ashby,lever}.py` passes and the gate in CLAUDE.md is green. Commit `feat(sources): read descriptions from hiring-system payloads`.

### Task 2 — Job-board APIs: himalayas, jobicy, remoteok, arbeitnow, fourdayweek

**Files:**
- Modify `himalayas.py`, `jobicy.py`, `remoteok.py`, `arbeitnow.py`, `fourdayweek.py`
- Modify fixtures `himalayas/page1.json`, `jobicy/page1.json`, `remoteok/api.json`, `arbeitnow/page1.json`, `fourdayweek/page1.json`
- Test `test_himalayas.py`, `test_jobicy.py`, `test_remoteok.py`, `test_arbeitnow.py`, `test_fourdayweek.py`

**Change:** himalayas `description`, jobicy `jobDescription`, remoteok `description`, arbeitnow `description`, all HTML; fourdayweek `description`, MARKDOWN.

**Proof:** in each of the five test modules, the same three tests as Task 1: `test_draft_carries_description`, `test_description_from_raw_matches_draft`, `test_description_missing_gives_none`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_{himalayas,jobicy,remoteok,arbeitnow,fourdayweek}.py` passes and the gate is green. Commit `feat(sources): read descriptions from job-board payloads`.

### Task 3 — mycareersfuture and kalibrr

**Files:**
- Modify `mycareersfuture.py`, `kalibrr.py`
- Modify fixtures `mycareersfuture/page0.json`, `kalibrr/page0.json`
- Test `test_mycareersfuture.py`, `test_kalibrr.py`

**Change:** mycareersfuture `description`, HTML. kalibrr: `description`, then `qualifications`, concatenated as one HTML document and normalized as HTML (spec §2.1). The first job of each fixture gains these keys from a live capture; the fixtures had none (`grep -c description` → 0 in both `page0.json`; spec §2.1 live evidence, §9).

**Proof:** in both test modules, `test_draft_carries_description`, `test_description_from_raw_matches_draft` and `test_description_missing_gives_none`; in `test_kalibrr.py` additionally `test_kalibrr_description_appends_qualifications` — the output holds the description text followed by the qualifications text.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_{mycareersfuture,kalibrr}.py` passes and the gate is green. Commit `feat(sources): read descriptions from MyCareersFuture and Kalibrr payloads`.

### Task 4 — Feeds and JSON-LD: rss, manual, letsgetrusty, japandev, hn

**Files:**
- Modify `rss.py`, `manual.py`, `letsgetrusty.py`, `japandev.py`, `hn.py`
- Modify fixtures `rss/python.xml`, `rss/djinni_job.html`, `rss/golang.xml`, `letsgetrusty/page1.html`, `japandev/job_plain.html`, `hn/comments.json`, `jsonld/jazzhr.html` (the last two of the rss/jsonld set are the ones `test_manual.py:40-41` reads)
- Test `test_rss.py`, `test_manual.py`, `test_letsgetrusty.py`, `test_japandev.py`, `test_hn.py`

**Change:**
- rss: item `description` (already unescaped once by `rss.py:135`, never again) and, for detail feeds, `jobposting.description`; both DETECT. `description_from_raw` picks the shape by the presence of the `jobposting` key.
- manual (`jobposting` kind): JSON-LD `description`, DETECT. The `feed` kind needs no code here: its drafts go through `rss` item parsing and its raw is read by `rss.description_from_raw`; the parser in `manual.py` sets `draft.description` with it.
- letsgetrusty: JSON-LD `description`, MARKDOWN.
- japandev: `jobposting.description`, HTML.
- hn: `comment_text`, HTML.

**Proof:** in each of the five test modules, `test_draft_carries_description`, `test_description_from_raw_matches_draft` and `test_description_missing_gives_none`; additionally —
- `test_rss_list_item_not_unescaped_twice` (`test_rss.py`) — an item whose stored description (after `rss.py:135`) is `AT&amp;T pays 5 < 6` has no tag, so DETECT picks PLAIN and the result is `AT&amp;T pays 5 \< 6`: the entity is not unescaped a second time.
- `test_rss_detail_feed_reads_jobposting` (`test_rss.py`) — a detail-feed draft's description comes from the Djinni page's JSON-LD.
- `test_manual_feed_draft_carries_description` (`test_manual.py`) — a manual feed URL's draft carries its item description, and `rss.description_from_raw(draft.raw)` gives the same text.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_{rss,manual,letsgetrusty,japandev,hn}.py` passes and the gate is green. Commit `feat(sources): read descriptions from feeds, JSON-LD pages and HN`.

---

## Risks

- **Fixture capture** is the step most likely to go wrong: a real description that is too long bloats fixtures, and editing JSON by hand can break it. Trim to ~2,000 characters, and run the module's existing tests after each fixture edit.
- Existing adapter tests that compare whole drafts with `==` will fail once drafts carry a description; update their expected values in the same task, never weaken an assertion.
- Not done: the workable and yc kinds (plan 04), and backfilling stored rows (the lazy path, plans 04–05).

## Proof

- `uv run --locked pytest packages/ijob-sources` passes.
- The five gate commands in CLAUDE.md exit `0`.
- `grep -ln "def description_from_raw" packages/ijob-sources/src/ijob_sources/*.py` lists exactly 16 modules — greenhouse, gem, ashby, lever, himalayas, jobicy, remoteok, arbeitnow, fourdayweek, mycareersfuture, kalibrr, rss, manual, letsgetrusty, japandev, hn — covering the 17 kinds (`feed` shares `rss`).

## Review findings

- correctness — a lone surrogate in a description crashed `Store.record` (7 modules unguarded) — `description.py` input sanitising — fixed at the root and verified: `normalize` strips U+D800–U+DFFF; `test_strips_lone_surrogates`, `test_a_lone_surrogate_in_a_comment_does_not_stop_the_batch_being_stored`; reviewer repro `crash.py` → `RecordResult(new=2, updated=0, merged=0)`, `postings stored: (2,)`; `uv run --locked pytest packages/ijob-sources packages/ijob-core` → `1785 passed in 1.86s`
- modularity — JSON-object loader duplicated 16×, `_readable` 4×, encode guard 5× — consolidated into `ijob_sources/payload.py` `raw_object` (+ `test_payload.py`, 12 tests); per-module guards removed
- docstring-discipline — kalibrr/mycareersfuture docstrings misnamed the parser path — fixed
- naming — lever `_descriptions` vs `_description` — renamed `_identity_texts`
- test-mandate — hn, japandev, manual, rss tests used partial literals — now full expected constants
- fixtures — gem/ashby/lever plain-text stubs no longer match the HTML — declined: used only by identity checks, which pass
- spec — live-data quirks (arbeitnow/remoteok partly escaped HTML, remoteok mojibake, fourdayweek raw HTML in Markdown) — recorded in spec §2.1 as not handled in this chain

## Probe results

- Live capture of every §2.1 fixture source through `PoliteClient` (`$SCRATCH/capture/capture.py`) — all 18 requests `200`.
- LetsGetRusty list payload — `ItemList` items carry a 153-character snippet ending `...`; the page's one standalone JobPosting is a different job — AGAINST THE SPEC (§2.1 letsgetrusty row).
- LetsGetRusty job page `https://jobs.letsgetrusty.com?job=recKZTPUl4654gtLM` — `200`, one JobPosting, same title, 2,338-character description.
- Lever fixture had no `lists` key (spec §2.3 said empty) — against the spec wording; live `lists` content uses `•` bullets in `<p>`.

## Deviations

- 2026-11-01 — Defect of the spec (§2.1): LetsGetRusty's list payload holds only snippets. `disproves` written; the user chose lazy fetch. Spec amended in place (LetsGetRusty → §2.2 fetch group, form MARKDOWN; 16 payload kinds); `letsgetrusty.description_from_raw` removed, drafts carry `description=None`. 15 modules define `description_from_raw`.
- 2026-11-01 — HN fixture comment replaced wholesale with real comment 50095921; two header-field tests take its values. JapanDev fixture cut early to avoid naming private individuals. Lever fixture keeps two of three list items. Kalibrr/MCF fixtures got a closing `</ul>` after the cut.
- 2026-11-01 — Shared `payload.raw_object` added (not in the plan) to remove 16 copies.
- 2026-11-01 — Per-task commits not made (CLAUDE.md: commit only when the user asks).
