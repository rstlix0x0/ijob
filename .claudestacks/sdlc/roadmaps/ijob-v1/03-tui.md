---
status: done
created: 2026-10-30
---

# Milestone: TUI

## Goal

A Textual TUI covers search with filters, results, a posting detail view, source management,
CV upload (stored for later use), and 👍/👎 labeling of results, on top of the same pipeline the
CLI uses. Each posting carries its job description as plain text wherever its source publishes
one, taken from the source's payload or fetched politely from the posting's own detail endpoint,
so the detail view and labels see the whole posting, not only its title.

## Non-goals

- LLM features (match reasons, judging, scoring arrive with the LLM layer).
- New pipeline capabilities beyond posting description text.
- Descriptions from sources that do not publish one through an allowed route (no HTML scraping
  past the source access rules).
