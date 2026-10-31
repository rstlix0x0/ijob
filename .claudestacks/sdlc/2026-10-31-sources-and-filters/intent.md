---
status: approved
created: 2026-10-31
milestone: ijob-v1/02
---

# Intent: Sources and filters

## Problem

The walking skeleton (`2026-10-31-walking-skeleton`, done) proved one thin slice: `ijob search
--greenhouse <token>` fetches named Greenhouse boards politely, stores them and lists them. Milestone
ijob-v1/02 is still far from reached:

- Only one of the milestone's sources can be read. The other hiring systems, the job boards and the
  Tier B sources have no adapter, and the verified seed companies in `docs/research/companies.csv`
  cannot be searched without typing every board token by hand.
- The same job seen on two sources is stored as two postings: there are no cross-source dedupe keys
  and no matching.
- Nothing is normalized or filtered. A search returns every posting of every board, including
  non-engineering roles, with no way to narrow by salary, country, remote scope, date or company.
- Every run refetches every board in full, because the polite client has no conditional requests.
- Users cannot keep their own list of sources or add a single posting by URL. A board token can also
  point at a different company than the one the user meant, and nothing checks it.

Every piece builds on the domain model and the source-adapter protocol the skeleton fixed, which is why
they could only start now.

## Affected systems

- `ijob-core`: normalization (salary, country, remote scope), cross-source dedupe keys and matching,
  the deterministic filters, and store migrations for the new columns and the user's sources.
- `ijob-sources`: the conditional-request (ETag) cache in the polite client, one adapter per
  milestone source, the generic RSS and schema.org JobPosting (JSON-LD) adapters, the source identity
  check, and manual-URL detection.
- `ijob-app`: `ijob search` across the seed companies and the user's sources with filter options, and
  commands to add, remove and list sources and manual URLs.
- Seed data: the verified rows of `docs/research/companies.csv`, shipped as package data.
- Recorded fixtures for every new source.

## Desired outcome

- `ijob search` with no source flag searches the seed companies and the user's sources, across every
  Tier A and Tier B source the milestone names:
  - Hiring systems: Greenhouse, Gem, Ashby, Lever, SmartRecruiters, Workable.
  - Job boards: Himalayas, Jobicy, RemoteOK, Arbeitnow, 4dayweek, MyCareersFuture, Kalibrr, and
    through one generic RSS adapter We Work Remotely, Python.org Jobs, Golangprojects, Elixir Jobs,
    Djinni and Berlin Startup Jobs.
  - Tier B: YC jobs, LetsGetRusty, JapanDev (sitemap and JSON-LD only), and HN "Who is hiring"
    (first-line parse, raw comment kept, marked low confidence).
- The same job found on two sources is stored as one posting with two sightings.
- Search results can be narrowed by role, date, company, salary and country/remote scope, and every
  filter keeps a posting when in doubt.
- A board that answers `304 Not Modified` is not downloaded again; the cached body is used, and no
  adapter changes for it.
- Users can add, remove and list their own sources and manual URLs. A manual URL resolves to a known
  hiring system, a schema.org JobPosting (JSON-LD) page or a feed, or is rejected with a reason.
- A source whose identity check fails is not searched, and the user is told why.
- The gate passes, and every new network path is tested with recorded fixtures.

## Constraints

- Design decisions already approved for milestone ijob-v1/02, which the spec honours rather than
  reopens:
  - One Posting, many Sightings. Across sources the key is the canonical apply URL, falling back to
    a normalized company + title + country fingerprint. The new keys arrive through a store
    migration.
  - Every Sighting keeps its raw payload, so normalization can be re-run without refetching.
  - Salary is `Money(min, max, currency, period)` in the posting's own currency, with no FX
    conversion. A posting without salary passes unless the user requires salary.
  - Country is ISO-3166 alpha-2 plus a remote scope (`none / country / region / worldwide`). An
    unknown country passes.
  - Role filter: include and exclude title regexes from configuration (including エンジニア and
    開発), plus hiring-system hints such as SmartRecruiters `function` and Ashby `department`;
    keep when in doubt.
  - The ETag cache lives inside the polite HTTP client and hands the cached body back on `304`.
  - Workable is spaced more slowly (about 3 s between requests), after the 429s seen in research.
  - Gem reuses the Greenhouse parser.
  - Seed companies come from the verified rows of `docs/research/companies.csv`, shipped as package
    data. User sources live in SQLite.
  - A manual URL is detected by the research regexes for hiring systems, else as a schema.org
    JobPosting page, else as a feed, else rejected. The source identity check is mandatory.
  - Tests use `httpx.MockTransport` with trimmed recorded fixtures.
  - Store on stdlib `sqlite3`, migrations tracked by `PRAGMA user_version`.
- The source-adapter protocol from the walking skeleton is extended, not changed: a new source kind
  is an addition to a closed set.
- The CLAUDE.md source rules hold for every new adapter: ijob's own honest User-Agent, robots.txt
  with longest-match semantics, no anti-bot bypass, no logged-in scraping, JapanDev through its
  sitemap and JSON-LD only.
- `ijob-core` imports no network library and no LLM library.
- Tests and CI never touch the network.
- The python guideline gate in CLAUDE.md must pass.
- One chain with several plans, in this order of dependency: filters, hiring-system adapters and
  job-board adapters in parallel, then source management, which waits for both adapter plans.

## Non-goals

- Everything the milestone excludes: LLM features (including parsing HN comment bodies beyond the
  first line), the TUI, Tier C and Tier D sources, and currency conversion.
- JobStreet beyond a deep link.
- Output formatting beyond a plain listing.
- Scheduling or background refresh; a search runs when the user runs it.
