---
status: draft
created: 2026-11-03
---

# Milestone: TUI rewrite

## Goal

A new Textual interface replaces the old one. It opens at once on the stored postings, filters as you type, and shows scores and reasons best first. Fetching and model calls run in the background and never freeze the screen. Its tests use no real-time waits, and a session on the real postings shows the result.

## Non-goals

- A web interface.
