---
status: done
created: 2026-10-31
depends-on: [01, 02, 04]
built-on: aeb01064881c
---

# Hiring Systems Implementation Plan

**Goal:** Greenhouse, Gem, Ashby, Lever and Workable boards are collected as identity-checked drafts.

**Architecture:**
- One module per hiring system, each with a source class that satisfies the C1 `Source` protocol and
  a pure `parse_*` function, as in C1's `greenhouse.py`.
- A shared `ijob_sources.identity` checks a parsed batch against the company the user or seed list
  named. On failure it raises `IdentityMismatchError`, so `collect` reports the source and records
  nothing.
- Each source requests its one board URL with `conditional=True` (plan 04).

**Tech Stack:** stdlib `json`, `decimal`, `datetime`, `logging`, httpx `MockTransport`, pytest.

**Content authority:** spec §1 premise 1, §2.1, §2.7, §5.1 (when a `Money` is built), §6.1 (rows
`greenhouse` … `workable`), §6.4, §10.

**Fixture sources:** the bodies fetched for spec §2.1, saved under
`/private/tmp/claude-501/-Users-hiraq-Projects-rstlix0x0-ijob/97b526e9-32f9-40fa-acfd-bc20d7a8e436/scratchpad/c2-grounding/ats/`:

| Fixture | Saved body |
|---|---|
| Greenhouse | `gh_both.body` |
| Gem | `gem_jobs.body` (Retool) |
| Ashby | `ashby_ramp.body` |
| Lever | `lever_spotify.body` |
| Workable | `wk_hf.body` and `wk_mercari.body` |

Each fixture keeps 2–3 jobs with descriptions shortened, keeps every key the mapping reads, and
contains no personal data.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/errors.py                       — [modify] IdentityMismatchError
packages/ijob-sources/src/ijob_sources/identity.py                     — [create] check_identity
packages/ijob-sources/src/ijob_sources/greenhouse.py                   — [modify] company=, pay ranges, country, role hint, identity, conditional
packages/ijob-sources/src/ijob_sources/gem.py                          — [create] GemBoard, parse_gem
packages/ijob-sources/src/ijob_sources/ashby.py                        — [create] AshbyBoard, parse_ashby
packages/ijob-sources/src/ijob_sources/lever.py                        — [create] LeverSite, parse_lever
packages/ijob-sources/src/ijob_sources/workable.py                     — [create] WorkableAccount, parse_workable
packages/ijob-sources/tests/ijob_sources_tests/fixtures/{gem,ashby,lever,workable}/__init__.py — [create] empty (test-mandate)
packages/ijob-sources/tests/ijob_sources_tests/fixtures/greenhouse/tailscale_jobs_pay.json      — [create] trimmed gh_both.body
packages/ijob-sources/tests/ijob_sources_tests/fixtures/gem/retool_job_posts.json              — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/ashby/ramp_job_board.json              — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/lever/spotify_postings.json            — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/workable/huggingface_widget.json       — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_errors.py           — [modify] IdentityMismatchError
packages/ijob-sources/tests/ijob_sources_tests/test_identity.py         — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_greenhouse.py       — [modify] new URL and new fields
packages/ijob-sources/tests/ijob_sources_tests/test_gem.py              — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_ashby.py            — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_lever.py            — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_workable.py         — [create]
```

## Order of work

### Task 1 — Check a board's identity

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/errors.py`
- Create `packages/ijob-sources/src/ijob_sources/identity.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_errors.py` and `test_identity.py`

**Change:**
- `IdentityMismatchError(SourcesError)` carries `ref: SourceRef` and `reason: str`.
- `check_identity(ref, company, *, names: Iterable[str], texts: Iterable[str], count: int) -> None`
  applies spec §6.4. It normalises with `ijob_core.dedupe.normalise_company` (plan 02 Task 1): one
  normalisation, not two (python guideline, one canonical type per concept).
  - return when `count == 0`;
  - the candidates are the normalised company, plus, when the company has a parenthesised part,
    the normalised part before the parentheses and the normalised part inside them
    (`Anysphere (Cursor)` → `anysphere cursor`, `anysphere`, `cursor`);
  - pass when any normalised name equals, contains or is contained in a candidate;
  - else pass when any text, normalised the same way (whole text through the same NFKC, casefold
    and non-alphanumeric-to-space steps), contains a candidate as a whole-word run;
  - both sides always go through the same normalisation, so `Checkout.com` matches a description
    that writes `Checkout.com` (spec §6.4);
  - else raise `IdentityMismatchError(ref, "company '<company>' not found on the board")`.

**Proof:** written first, seen failing, then passing —
- `test_identity_error_fields`: `.ref`, `.reason`, `str()`, subclass of `SourcesError`.
- `test_empty_batch_passes`.
- `test_name_match` (parametrised): `"Tailscale"` vs `"Tailscale"`, and `"Mercari"` vs
  `"Mercari, inc."` both pass.
- `test_text_match`: no names, one text containing `"Ramp"` passes for company `Ramp`.
- `test_dotted_company_matches_text` (parametrised): `Checkout.com` and `Crypto.com` each pass
  against a text that writes the name as is (`"Join Checkout.com's team"`).
- `test_parenthesised_company` (parametrised): `Anysphere (Cursor)` passes against a text naming only
  `Cursor`, and against one naming only `Anysphere`.
- `test_mismatch_raises`: company `Prisma`, the names and texts of another company → raises, with the
  company in the reason.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_identity.py
packages/ijob-sources/tests/ijob_sources_tests/test_errors.py` passes and the gate is green. Commit
`feat(sources): check a hiring-system board's identity`.

### Task 2 — Read Greenhouse pay, country and department

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/greenhouse.py`
- Create `fixtures/greenhouse/tailscale_jobs_pay.json`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_greenhouse.py`

**Change:** per the spec §6.1 `greenhouse` row.

- `GreenhouseBoard(key, *, company: str | None = None)`. With a company, `fetch` runs
  `check_identity` with names `company_name` and texts `content`. `None` skips the check; only C1
  callers pass `None`, and plan 09 always passes one.
- The URL becomes `…/jobs?content=true&pay_transparency=true`, fetched with `conditional=True`.
- `parse_jobs` adds, per job:
  - `salaries`: one `Money` per `pay_input_ranges` item, `Money(min_cents/100, max_cents/100,
    currency_type, None)`, built only under the spec §5.1 rule;
  - `country`: the first `offices[].location` accepted by `parse_country`;
  - `role_hint`: the first `departments[].name`.
- Every C1 mapping is unchanged.
- C1's `test_fetch_requests_board_url_with_content` (`test_greenhouse.py:249-257`) changes to assert
  the new URL. Every other C1 test stays.

**Proof:** written first, seen failing, then passing —
- `test_pay_ranges_become_money`: on the new fixture, the first job's salaries are
  `(Money(Decimal("203680"), Decimal("254600"), "CAD", None),)`.
- `test_country_from_first_parsable_office`: first job (`offices` `Canada`, `United States`) → `CA`.
- `test_role_hint_from_department`: the first job's hint is `Data` (spec §2.5a).
- `test_job_without_pay_has_no_salary`.
- `test_fetch_requests_pay_url_conditionally`: the request URL ends in
  `content=true&pay_transparency=true`.
- `test_identity_checked_when_company_given`: company `Tailscale` passes; company `Ramp` makes `fetch`
  raise `IdentityMismatchError`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_greenhouse.py`
passes and the gate is green. Commit `feat(sources): read greenhouse pay ranges and offices`.

### Task 3 — Collect Gem boards

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/gem.py`, `fixtures/gem/__init__.py`,
  `fixtures/gem/retool_job_posts.json`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_gem.py`

**Change:**
- `GemBoard(key, *, company: str)`; `ref` is `gem:<key>`.
- One conditional GET of `https://api.gem.com/job_board/v0/{key}/job_posts/`.
- `parse_gem(body, key, company) -> SourceBatch` reads a bare array, per the spec §6.1 `gem` row:
  - `external_id` ← `id` (a string);
  - `title` ← `title`; `location` ← `location.name`; `url` ← `absolute_url`;
  - `company` ← the row company;
  - `country` ← `location.name` through `parse_country`;
  - `remote`: `location_type` `in_office`/`hybrid` → `NONE`, `remote` → `None`;
  - `posted_at` ← `first_published_at`; `role_hint` ← the first `departments[].name`;
  - `raw` ← the item JSON.
- C1's rejection contract applies. A body that is not a JSON array raises `ParseError`.
- After parsing, `check_identity` runs with texts `content_plain`.

**Proof:** written first, seen failing, then passing —
- `test_parse_gem_maps_fields`: on the fixture, the first job's id `"4003629005"`, title
  `"Software Engineer"`, location, URL, country `US` (from `"San Francisco, United States"`), remote
  `NONE` (it is `hybrid`) and `posted_at` `2022-03-25T18:46:39Z`.
- `test_remote_location_type_is_unknown_scope`: `location_type: "remote"` → `remote is None`.
- `test_opaque_string_id_kept`: an id like `"am9icG9zdDqAUz7G7JlGLHQawWv7Sec2"` is kept.
- `test_not_an_array_is_parse_error`: `{"jobs": []}` → `ParseError`.
- `test_invalid_job_rejected_others_kept`.
- `test_fetch_runs_identity`: company `Retool` passes; `Groq` raises `IdentityMismatchError`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_gem.py`
passes and the gate is green. Commit `feat(sources): collect gem boards`.

### Task 4 — Collect Ashby boards

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/ashby.py`, `fixtures/ashby/__init__.py`,
  `fixtures/ashby/ramp_job_board.json`. Keep 3 jobs: one with a `1 YEAR` salary, one with a
  `1 MONTH` salary, and one with an unmapped interval or no salary.
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_ashby.py`

**Change:**
- `AshbyBoard(key, *, company: str)`, ref `ashby:<key>`.
- One conditional GET of
  `https://api.ashbyhq.com/posting-api/job-board/{key}?includeCompensation=true`.
- `parse_ashby` reads `{"jobs": [...]}`, per the spec §6.1 `ashby` row:
  - `external_id` ← `id`; `title` ← `title`, stripped; `location` ← `location`; `url` ← `jobUrl`;
  - `company` ← the row company; `country` ← `address.postalAddress.addressCountry` through
    `parse_country`;
  - `remote`: `workplaceType` `OnSite`/`Hybrid` → `NONE`, `Remote` → `None`; `isRemote` is not read;
  - `salaries`: each `compensation.summaryComponents` item with `compensationType == "Salary"`,
    interval `1 YEAR` → `YEAR`, `1 MONTH` → `MONTH`, any other → `None`;
  - `posted_at` ← `publishedAt`; `role_hint` ← `department`.
- `check_identity` texts: `descriptionPlain`.

**Proof:** written first, seen failing, then passing —
- `test_parse_ashby_maps_fields`: the fixture's first job (`" Security Engineer, Cloud"` stripped,
  country `US` from `"USA"`, remote `NONE` for `Hybrid`, salary `211400–290600 USD YEAR`).
- `test_monthly_salary`: a `1 MONTH` component → `Period.MONTH`.
- `test_unmapped_interval_has_no_period`: interval `"NONE"` on a Salary component → `period is None`;
  the job is kept.
- `test_non_salary_components_ignored`: `EquityPercentage` adds no `Money`.
- `test_remote_workplace_is_unknown_scope`.
- `test_fetch_runs_identity`: company `Ramp` passes.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_ashby.py`
passes and the gate is green. Commit `feat(sources): collect ashby boards`.

### Task 5 — Collect Lever sites

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/lever.py`, `fixtures/lever/__init__.py`,
  `fixtures/lever/spotify_postings.json`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_lever.py`

**Change:**
- `LeverSite(key, *, company: str)`, ref `lever:<key>`.
- One conditional GET of `https://api.lever.co/v0/postings/{key}?mode=json`.
- `parse_lever` reads a bare array, per the spec §6.1 `lever` row:
  - `external_id` ← `id`; `title` ← `text`; `location` ← `categories.location`; `url` ←
    `hostedUrl`;
  - `country` ← `country`;
  - `remote`: `workplaceType` `onsite`/`hybrid` → `NONE`, `remote` → `None`;
  - `posted_at` ← `createdAt` in milliseconds → aware UTC;
  - `role_hint` ← `categories.department`.
- An empty array is an empty batch.
- `check_identity` texts: `descriptionPlain`.

**Proof:** written first, seen failing, then passing —
- `test_parse_lever_maps_fields`: the fixture's first job; `createdAt` `1792767640768` →
  `2026-10-23T15:00:40.768Z`.
- `test_empty_site_is_empty_batch`: `[]` → no drafts, `rejected == 0`.
- `test_remote_workplace_is_unknown_scope`.
- `test_fetch_runs_identity`: company `Spotify` passes.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_lever.py`
passes and the gate is green. Commit `feat(sources): collect lever sites`.

### Task 6 — Collect Workable accounts

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/workable.py`, `fixtures/workable/__init__.py`,
  `fixtures/workable/huggingface_widget.json`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_workable.py`

**Change:**
- `WorkableAccount(key, *, company: str)`, ref `workable:<key>`.
- One conditional GET of `https://apply.workable.com/api/v1/widget/accounts/{key}`. The 3 s spacing
  is the client's `host_intervals`, wired in plan 10, not the adapter's.
- `parse_workable` reads `{"name","description","jobs"}`, per the spec §6.1 `workable` row:
  - `external_id` ← `shortcode`; `title` ← `title`; `location` ← `city`; `url` ← `url`;
  - `country` ← the first `locations[].countryCode`;
  - `remote`: `telecommuting` false → `NONE`, true → `None`;
  - `posted_at` ← `published_on` at 00:00 UTC;
  - `role_hint` ← `function` when non-empty.
- `check_identity`: names `[name]`, texts `[description]`, the widget's top-level company blurb
  (spec §2.5a: "Here at Hugging Face, we're on a journey…").

**Proof:** written first, seen failing, then passing —
- `test_parse_workable_maps_fields`: the fixture's first job (country `FR`, `posted_at`
  `2026-05-29T00:00Z`).
- `test_empty_function_is_no_hint`: `function: ""` → `role_hint is None`.
- `test_fetch_runs_identity`: company `Hugging Face` passes; `Mercari` raises.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_workable.py`
passes and the gate is green. Commit `feat(sources): collect workable accounts`.

---

## Risks

- **Most risky: Task 2 changes a C1 adapter.** The C1 Greenhouse tests are the guard. Only the URL
  test changes, and the CLI keeps building `GreenhouseBoard(key)` without a company until plan 10.
- The identity text rule passes on 74 of 89 Lever jobs, not all (spec §2.7). It needs only one job,
  so a real board passes, but a test fixture must keep at least one job whose description names the
  company.
- The fixture bodies live in a session scratch directory. If it is gone, re-fetch each spec §2.1 URL
  once with ijob's User-Agent and trim again.
- Not done here: building these sources from the sources table (plan 09), and Workable's host
  interval (plan 10).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-sources` passes, the C1 tests included.
- `grep -rn "salaryRange\|isRemote" packages/ijob-sources/src` prints nothing. Neither field is used
  (spec §2.1).

## Review findings

One `claudestacks:reviewer` pass (2026-10-31), report `.airsstack/.../20261031-130611-d66e/21-reviewer-06.md`.
Verdict: NON-COMPLIANT (spec §5.2 text). Blocking set: identity substring match, spec §5.2.

| Id | Finding | Disposition |
|---|---|---|
| 🔴 identity.py:28 | names matched as raw substrings: `Scale` passed a Tailscale board, `Ramp` passed Trampoline, `OpenAI` passed `AI` | fixed: whole-token runs; near-miss tests seen failing first |
| 🔴 spec §5.2 | `countries.py` accepts an alpha-2 code only as the whole text; spec §5.2 still says either part | recorded below as a spec defect; spec text goes to `design` |
| 🟡 | the identity `count` meant listed items in Lever/Workable but valid drafts in Greenhouse/Gem/Ashby | fixed: `count` = jobs listed; texts and names from every listed item; identity runs after parsing everywhere (Lever reordered) |
| 🟡 | an unreadable role hint, and some Lever/Workable optional fields, rejected the job | fixed: every field beyond id, title, url and company is optional; unreadable → `None`, job kept |
| 🟡 | Ashby fixture had an invented `1 MONTH` salary and a removed compensation | fixed: regenerated from `ashby_ramp.body`, descriptions trimmed only; real `1 MONTH` intern job used |
| fix-round find | Gem `location_type` and Ashby `workplaceType` used `in frozenset` on unhashable JSON (TypeError escaped) | fixed: text check first, tested |
| 🔵 | `collect.py:15` contract line stale | fixed in plan 08's fix round |
| 🔵 | salary bound 0 → `None`; role hint = first non-empty name | kept (spec §5.1 positive bound; recorded) |
| 🔵 | private helpers duplicated across adapters | deferred: cross-plan follow-up |

Fix round re-verification (scoped): 13 files formatted, ruff passed, mypy clean on 13 files, pytest
(with doctests) 337 passed.

## Probe results

- Against the spec (§5.2), before Task 2: `parse_country("San Francisco, CA")` returned `CA` (Canada),
  `"Atlanta, GA"` → `GA`, `"Wilmington, DE"` → `DE`. New test `test_last_comma_part_is_not_a_code`
  failed 3/3 before the change and passes after; `packages/ijob-core` + `packages/ijob-app` 322 passed.
- Plan-06 scoped gate before review: 14 files formatted, ruff passed, mypy 14 files clean, 302 passed.
- Whole-workspace gate after plans 06–08 were coded: 141 files formatted, mypy 97 files clean, 1238 passed.

## Deviations

- 2026-10-31, **spec §5.2 (spec defect)**: an alpha-2 code after the last `", "` was read as a country,
  so US "City, ST" locations became other countries. Built instead: an alpha-2 code matches only as the
  whole text; after the last comma only names and aliases match (`countries.py`, doctest and tests).
  Cost: `"Berlin, DE"` gives `None` (keep-when-in-doubt). `disproves` was not written, so as not to
  freeze plans 07–10 mid-run; the spec text is corrected by `design` after this chain's run.
- 2026-10-31, plan: `parse_gem/ashby/lever/workable(body, key, company)` take the company a draft needs.
- 2026-10-31, spec §5.1/§6.1: every field beyond id, title, url and company is optional; an unreadable
  value is dropped and the job kept.
- 2026-10-31, spec §6.4: `count` is the number of jobs the board listed; identity texts come from every
  listed job, rejected ones included.
