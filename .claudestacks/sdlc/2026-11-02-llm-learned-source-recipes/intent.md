---
status: approved
created: 2026-11-02
---

# Intent: LLM-learned source recipes

## Problem

A user can add a source only when its URL takes a shape ijob already knows. `ijob sources add URL`
accepts a known hiring system (Greenhouse, Lever, Ashby and the others), a page holding one
schema.org `JobPosting`, or an RSS 2.0 feed (`ijob_sources/detect.py:1-9`). Any other URL is
refused with `UnsupportedUrlError`. Most companies' own career pages, and many job boards, fit none
of these shapes, so the user cannot follow them at all.

Supporting a new shape today means hand-writing a new source module, with its own parser, fixtures
and tests, for each site. That does not scale to the sites one person wants to follow.

## Affected systems

- `ijob-sources`: how a new URL is recognised, and parsing of postings from sites with no
  dedicated parser.
- `ijob-ai`: a new task that studies a fetched sample of a source and describes how to read its
  postings.
- `ijob-app`: `ijob sources add` and a way to re-analyze a source, the stored per-source parsing
  description, and how `search` reports a source whose description no longer matches.
- `ijob-core`: storage of the per-source parsing description.

## Desired outcome

- Adding a URL of an unknown shape no longer ends in a refusal. ijob fetches a sample once, has a
  model study it, and proposes a parsing description (a "recipe") for that source.
- ijob tries the recipe on the sample and shows the user the postings it would produce. The recipe
  is saved only after the user confirms.
- From then on, `search` and the TUI read that source with the saved recipe and no model call. A
  search costs nothing extra and gives the same result for the same page.
- When a saved recipe stops matching because the site changed, `search` says so for that source,
  names the command that re-analyzes it, and carries on with the other sources. ijob never calls a
  model during a search.
- The user can re-analyze a source on request, again confirming the new recipe before it replaces
  the old one.

Candidate direction (for `design`, not settled here): the recipe is declarative data, such as where
the postings sit and which field holds each value, read by one generic parser.

## Constraints

- **No model at search time.** A model runs only on an explicit add or re-analyze command.
- **Data, never code.** A recipe is data that a generic parser reads. ijob never runs code a model
  wrote.
- **The user confirms** every recipe before it is saved or replaces another.
- **Models from configuration.** The analysis model comes from `[models]`, and the analysis stays
  inside the `max_cost_usd` cap (CLAUDE.md: never hard-code a model).
- **Source rules still bind** (CLAUDE.md): ijob's own honest User-Agent, robots.txt with
  longest-match semantics, no anti-bot bypass, no logged-in scraping, JobStreet deep-link only,
  JapanDev only through its sitemap and JSON-LD. The model sees only what one polite fetch
  returned.
- **Fetched page text is untrusted** input to the model, and it never reaches the trusted
  instruction position.
- **Tests and CI never call a model or the network**: fakes and recorded fixtures only, with no
  personal data.
- **Spend stays minimal**, within the user's $100 Anthropic and $10 OpenRouter credit.

## Non-goals

- Changing how known hiring systems, `JobPosting` pages or RSS feeds are parsed. They keep their
  hand-written parsers. Lenient parsing of their real rejected samples is a sibling chain (the
  existing-source audit).
- Calling a model per posting during a search, or extracting fields from free-form prose at run
  time (for example Hacker News comments).
- Re-analyzing a recipe automatically when it breaks.
- Pages that show their postings only after running JavaScript, or only after logging in.
