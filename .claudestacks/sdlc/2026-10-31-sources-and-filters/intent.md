---
status: draft
created: 2026-10-31
milestone: ijob-v1/02
---

# Intent: Sources and filters

## Problem

Everything in milestone ijob-v1/02 after the walking skeleton, built on the domain model and
source-adapter protocol the skeleton fixes. Expected plans: filters, hiring-system adapters, job-board
adapters, source management (the last waits for the two adapter plans).

HTTP cache: an ETag cache inside the polite HTTP client that stores ETag and body and hands the
cached body back on `304` (Greenhouse answers `If-None-Match` with `304`), so no adapter changes.

Filters (normalization, cross-source dedupe keys and matching through a store migration, and
deterministic filters in `ijob-core`):

- Salary is `Money(min, max, currency, period)` in the posting's own currency, with no FX
  conversion; postings without salary pass unless `--require-salary` is set.
- Country is ISO-3166 alpha-2 plus a remote scope (`none / country / region / worldwide`); an
  unknown country passes.
- Role filter: include/exclude title regexes from config (including エンジニア / 開発) plus
  hiring-system hints (SmartRecruiters `function`, Ashby `department`); keep when in doubt.
- Date and company filters.

Hiring-system adapters: Gem (reuses the Greenhouse parser), Ashby, Lever, SmartRecruiters and
Workable (slower spacing, about 3 s, after the 429s seen in research), the mandatory source identity
check, and import of the seed companies from the verified rows of `docs/research/companies.csv`,
shipped as package data.

Job-board adapters: Himalayas, Jobicy, RemoteOK, Arbeitnow, 4dayweek, MyCareersFuture and Kalibrr;
one generic RSS adapter covering We Work Remotely, Python.org Jobs, Golangprojects, Elixir Jobs,
Djinni and Berlin Startup Jobs; one generic schema.org JobPosting (JSON-LD) adapter; YC jobs
(data-page JSON); LetsGetRusty (ItemList JSON-LD); JapanDev through its sitemap and JSON-LD only; and
HN "Who is hiring" with a first-line parse (`Company | Role | Location | REMOTE`), the raw comment
kept and a low-confidence flag.

Source management: users add and remove sources and manual URLs, stored in SQLite. A manual URL is
resolved by detecting the hiring system with the research regexes, else a schema.org JobPosting
(JSON-LD) page, else a feed, else it is rejected; the identity check is mandatory.

## Affected systems

Not explored yet — parked by milestone ijob-v1/02.

## Desired outcome

Not explored yet — parked by milestone ijob-v1/02.

## Constraints

Not explored yet — parked by milestone ijob-v1/02.

## Non-goals

Not explored yet — parked by milestone ijob-v1/02.
