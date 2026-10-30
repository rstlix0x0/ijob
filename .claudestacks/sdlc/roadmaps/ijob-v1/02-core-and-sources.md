---
status: approved
created: 2026-10-30
---

# Milestone: Core and sources

## Goal

`ijob search` (no LLM) fetches software-engineering postings from Tier A+B sources (Greenhouse,
Ashby, Lever, Gem, SmartRecruiters, Himalayas, MyCareersFuture, Kalibrr, Jobicy, RemoteOK, Arbeitnow,
4dayweek, JapanDev via sitemap, LetsGetRusty, We Work Remotely, HN Who is hiring, YC jobs, …) and the
seed companies. It applies polite HTTP with its own User-Agent, a longest-match robots.txt checker
and a source identity check, normalizes and dedupes postings into SQLite, and applies deterministic
role, date, company, salary and country filters. Users can add and remove sources and manual URLs.

## Non-goals

- LLM features.
- The TUI.
- Tier C and Tier D sources.
