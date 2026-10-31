---
status: approved
created: 2026-10-31
milestone: ijob-v1/03
---

# Intent: TUI and posting text

## Problem

ijob can only be used through `ijob search` and `ijob sources`, which print plain listings. Milestone
ijob-v1/03 needs more than that, and four things are missing:

- **No job description.** A posting stores title, company, location, salary, country and URL, but not
  the body of the job ad. Some sources include the description in the payload already kept in
  `sightings.raw`, where nothing reads it; others leave it out of their job lists entirely. The text
  that arrives is HTML in a different shape for each source (escaped, plain, or HN's own markup).
- **No way to look at one posting.** Neither the CLI nor any other interface shows a single posting in
  full.
- **No feedback.** The user cannot mark a posting 👍 or 👎 or say why. Milestone ijob-v1/05 tunes the
  relevance judge from exactly these labels, and a label given on a title alone is weak feedback.
- **No CV on file.** Milestone ijob-v1/04 turns the user's PDF CV into a profile, but ijob has no
  place to keep the CV.

There is also no interactive interface: browsing, narrowing and labeling hundreds of results line by
line in a terminal printout is impractical.

## Affected systems

- `ijob-core`: the posting model and store, which gain description text, labels and the stored CV
  reference through migrations.
- `ijob-sources`: description extraction for every source, from the payload where present and from
  the posting's own detail endpoint where not.
- `ijob-app`: a new Textual TUI, plus new CLI commands alongside `search` and `sources`.
- Recorded fixtures with real descriptions from each source.

## Desired outcome

- Every posting can show its job description as clean, normalized plain text, the same shape whatever
  the source, wherever the source publishes one through an allowed route.
- A description already in the source's payload is extracted at search time. One that is not is
  fetched politely on demand, the first time the user opens or labels that posting, and then stored.
  Search is no slower than today.
- `ijob tui` opens a Textual app over the same pipeline the CLI uses, covering:
  - search with every existing filter, and a results list;
  - a posting detail view with metadata, description and link;
  - source management (list, add, remove sources and manual URLs);
  - CV upload: the user picks a PDF and ijob keeps a copy for later use;
  - 👍/👎 on a posting with an optional short note, changeable and removable.
- The CLI can do everything new the TUI does: show one posting with its description, label it, and
  store a CV.
- Labels attach to the deduped posting and survive later searches.
- The gate passes; the TUI and every new network path are tested without the network.

## Constraints

- Decisions already made, which the spec honours rather than reopens:
  - Textual for the TUI, Typer for the CLI.
  - Descriptions are fetched lazily, on open or label, never for every posting during search.
  - One shared HTML-to-text normalizer for all sources, not one per source. Formatting tags
    (headings, paragraphs, lists, emphasis, links, code) are kept as simple text structure; scripts,
    styles, images and attributes are dropped.
  - A label is 👍 or 👎 plus an optional free-text note.
  - Candidate direction: the standard library's `html.parser`, already used in `jsonld.py` and
    `yc.py`; no GPL-licensed converter (ijob is Apache-2.0).
- The CLAUDE.md source rules hold for every detail fetch: ijob's own honest User-Agent, robots.txt
  with longest-match semantics, no anti-bot bypass, no logged-in scraping, JobStreet deep-link only,
  JapanDev through its sitemap and JSON-LD only.
- The CV, labels and descriptions are personal data. They live in the user's data directory and are
  never committed; fixtures use public postings and a made-up CV.
- `ijob-core` imports no network library and no LLM library.
- Tests and CI never touch the network or an LLM.
- The python guideline gate in CLAUDE.md must pass.
- One chain with several plans.

## Non-goals

- Everything milestone ijob-v1/03 excludes: LLM features (match reasons, judging, scoring).
- Reading or parsing the CV's contents; the CV is only stored (parsing arrives with ijob-v1/04).
- Splitting descriptions into fields such as requirements, benefits or tech stack.
- Descriptions from sources with no allowed route to one, text inside images, and OCR.
- Exporting labels or building datasets for tuning (ijob-v1/05).
- Scheduling or background refresh.
