---
status: approved
created: 2026-11-01
built-on: cae3bcdf3b23
---

# Trace Store Implementation Plan

**Goal:** Keep trace spans in their own SQLite file with listing, tree reads and size-capped pruning.

**Architecture:**
- New module `ijob_core.traces` with `TraceStore` over `traces.db` (spec §8). It has its own
  `PRAGMA user_version` migrations, version 1 with one `spans` table indexed on `start_ns`.
- Connections set `busy_timeout = 5000` ms.
- Span rows are plain dataclasses. The store knows nothing of OpenTelemetry; plan 05 converts
  `ReadableSpan` into rows.

**Tech Stack:** Python 3.14, sqlite3, pytest.

---

## Files that change

```
packages/ijob-core/src/ijob_core/traces.py                    — [create] TraceStore, SpanRow, TraceSummary, TraceTree, PruneReport
packages/ijob-core/tests/ijob_core_tests/test_traces.py       — [create] store, listing, tree and pruning tests
```

## Order of work

### Task 1 — Schema, insert and read

**Files:** Create `packages/ijob-core/src/ijob_core/traces.py`; Test `test_traces.py`.

**Change:** add these types and methods.

- `SpanRow` fields: `trace_id`, `span_id` (hex strings), `parent_id | None`, `name`, `start_ns`,
  `end_ns`, `status`, and `attributes: Mapping[str, object]`, stored as JSON.
- `TraceStore.open(path)` creates and migrates the file and sets `busy_timeout`.
- `insert(rows)` is idempotent on `(trace_id, span_id)`.
- `summaries(limit) -> list[TraceSummary]` returns the newest first. Each `TraceSummary` holds:
  - `trace_id`;
  - `started`, the root span's start, as a UTC datetime;
  - `operation`, the root span's name;
  - `calls`, the count of spans that have a `gen_ai.request.model` attribute;
  - `cost_usd`, the sum of `gen_ai.usage.cost`;
  - `duration_ms`, the root span's duration.
- **Incomplete traces.** A trace whose root row is missing (a killed process, where children were
  exported first) is still listed. `started` is its earliest span's start, `operation` is
  `(incomplete)`, and `duration_ms` runs from its earliest start to its latest end.
- `tree(trace_id) -> TraceTree` returns nested nodes ordered by start time. An unknown id raises
  `UnknownTraceError`, an `InvalidValueError` subclass.
- `close()` and context-manager methods.

**Proof:** written first, seen failing, then passing:
- `test_open_creates_and_migrates`: a fresh file reaches `user_version` 1, and `busy_timeout` reads
  5000.
- `test_insert_child_before_parent_reads_as_tree`: a child row inserted before its parent reads back
  nested under it.
- `test_insert_is_idempotent`: inserting the same rows twice leaves one copy.
- `test_summaries_newest_first_with_calls_and_cost`: two traces give the right order, call count and
  summed cost.
- `test_tree_unknown_id_raises`: an unknown id raises `UnknownTraceError`.
- `test_summary_of_trace_without_root`: a trace holding only child rows is listed with operation
  `(incomplete)`, started at its earliest span.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_traces.py` passes and
the CLAUDE.md gate is green. Commit `feat(core): add trace store`.

### Task 2 — Size-capped pruning

**Files:** Modify `traces.py`; Test `test_traces.py`.

**Change:** add `prune(max_bytes) -> PruneReport(removed, size_bytes)`.
- When `page_count × page_size` exceeds `max_bytes`, it deletes whole traces, ordered by each
  trace's earliest `start_ns` (whether or not it has a root), until the live size fits. Live size is (`page_count` − `freelist_count`) ×
  `page_size`.
- It then runs `VACUUM`.
- It is a no-op under the cap.

**Proof:** written first, seen failing, then passing:
- `test_prune_under_cap_is_noop`: nothing is removed, and the report shows the current size.
- `test_prune_removes_oldest_whole_traces`: with many traces over a small cap, the oldest traces
  disappear entirely, no orphan spans remain, the newest survive, and the final file size is at most
  the cap.
- `test_prune_reports_removed_count`: `removed` equals the number of traces deleted.
- `test_prune_removes_rootless_traces`: an old trace with no root row is pruned by its earliest span.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_traces.py` passes and
the gate is green. Commit `feat(core): prune trace store by size`.

## Risks

- **`VACUUM` cost.** On a large file it takes time. It runs only when over the cap.
- **Concurrent writers.** A CLI and a TUI writing at once rely on `busy_timeout`. Plan 05 turns a
  remaining `sqlite3.Error` into a dropped-span count.
- **Not done:** pinning traces. There are no tune or bench runs in M2.

## Proof

- `uv run --locked pytest packages/ijob-core` passes.
- The gate exits 0.
