---
status: approved
created: 2026-10-30
---

# Milestone: Core and sources

## Goal

`ijob search` (no LLM) fetches software-engineering postings from a closed set of Tier A and Tier B
sources and the seed companies:

- Tier A, hiring systems: Greenhouse, Gem, Ashby, Lever, Workable.
- Tier A, job boards: Himalayas, Jobicy, RemoteOK, Arbeitnow, 4dayweek, MyCareersFuture, Kalibrr,
  and through one generic RSS adapter We Work Remotely, Python.org Jobs, Golangprojects, Elixir
  Jobs, Djinni and Berlin Startup Jobs.
- Tier B: YC jobs, LetsGetRusty, JapanDev (sitemap and JSON-LD only), and HN "Who is hiring"
  (first-line parse, raw comment kept, marked low confidence).

It applies polite HTTP with its own User-Agent, a longest-match robots.txt checker and a source
identity check, normalizes and dedupes postings into SQLite, and applies deterministic role, date,
company, salary and country filters. Users can add and remove sources and manual URLs; a manual URL
resolves to a known hiring system, a schema.org JobPosting (JSON-LD) page or a feed, or is rejected.

## Non-goals

- LLM features, including parsing HN comment bodies beyond the first line.
- The TUI.
- Tier C and Tier D sources: Workday, HRMOS, HERP, Talentio, Eightfold, Oracle Recruiting Cloud,
  iCIMS, SuccessFactors, Taleo, key-gated APIs (Adzuna, Reed, USAJobs, Findwork, Careerjet) and the
  HTML-heavy boards (Built In, JustJoin.it, Arc.dev, Welcome to the Jungle).
- Currency conversion in salary filters.
