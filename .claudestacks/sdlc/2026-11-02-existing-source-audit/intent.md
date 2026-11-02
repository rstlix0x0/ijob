---
status: dropped
created: 2026-11-02
---

# Intent: Parse every job from every existing source

## Problem

The user gathered these sources to read their jobs, and ijob does not read all of them.

Each existing source has a hand-written parser, and that parser skips any item it cannot read. Three
cases have been seen:

- A Djinni JSON-LD `addressLocality` given as a list.
- 4dayweek jobs with no company.
- Hacker News "Who is hiring?" comments that do not start with `Company | Title | Location`. These are
  real job posts written in prose (`ijob_sources/hn.py`).

The loss is also hard to see:

- A search reports only a count, `<ref>: skipped N invalid jobs`. The item and the reason are not kept.
- It is not known which other sources lose jobs, or how many.

The LLM layer and learned recipes do not close this. Recipes serve only pages `add-url` does not
recognize, and today a model never reads a job an existing source skipped.

## Affected systems

- `ijob-sources`: every existing source, so that an item its parser cannot read is kept rather than
  only counted.
- `ijob-ai`: a new task in which a model reads job fields out of one item.
- `ijob-core`: storage of those items and results, and jobs with a field missing.
- `ijob-app`: how this runs, what it costs, and how these jobs appear in the CLI and the TUI.

## Desired outcome

- Every job post an existing source returns ends up as a job the user can see.
- What a parser can read stays free, with no model involved.
- What a parser cannot read is read by a model, Hacker News prose included.
- Each item is read by a model once. Its result is kept, so running again spends nothing on it.
- An item that is not a job, such as a question in a Hacker News thread, does not become a job.
- A job is kept when it has a title and a working link. A missing company or location shows as
  unknown. A job read by a model is marked `low_confidence`.
- Anything still not read reports its reason, rather than being a bare count.
- The user sees what a run cost, and a run stops at the cost cap and keeps what it already read.
- Every existing source has been checked against real items from its site, and those real shapes are
  pinned by recorded fixtures.

Left for `design`: whether the model runs inside `search` or in a step the user starts.

## Constraints

- **Models from configuration.** The model comes from `[models]`, and a run stays inside the
  `max_cost_usd` cap (CLAUDE.md: never hard-code a model).
- **Fetched text is untrusted** input to the model, and it never reaches the trusted instruction
  position.
- **Boundaries** (CLAUDE.md): `ijob-core` imports no LLM library and no network library. LLM provider
  code does not import agent or tuning code.
- **Tests and CI never call a model or the network**: fakes and recorded fixtures only, with no
  personal data. Real samples are collected by hand at development time, and fixtures are trimmed or
  scrubbed.
- **The source rules stay** (CLAUDE.md): ijob's honest User-Agent, robots.txt, no anti-bot bypass, no
  logged-in scraping, JobStreet deep-link only, JapanDev only through its sitemap and JSON-LD.
- **Spend stays minimal**, within the user's $100 Anthropic and $10 OpenRouter credit.

## Non-goals

- Adding new sources.
- Jobs a source does not return, such as pages that need JavaScript or a login.
- Reading replies below the top level of a Hacker News thread.
- Guessing a value the item does not state.
