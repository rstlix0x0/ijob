---
status: draft
created: 2026-11-02
---

# Milestone: Fast ranked search

## Goal

`ijob search` works for any role and lists stored postings in under a second. Fetching never delays the results. `--title` narrows postings before any model call, and `--score` scores the matches against the CV, printing each as it arrives and ending with the list best first, with reasons. Every command draws tidy tables and panels on a terminal and stays plain in a pipe. A run on the real postings shows the output and the timings.

## Non-goals

- Judging every posting that passes the filters.
