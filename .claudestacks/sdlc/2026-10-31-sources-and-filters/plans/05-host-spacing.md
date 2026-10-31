---
status: approved
created: 2026-10-31
depends-on: [04]
built-on: aeb01064881c
---

# Host Spacing Implementation Plan

**Goal:** `PoliteClient` spaces requests to each host by the strictest of its default, the host's
robots.txt `Crawl-delay`, and a per-host override.

**Architecture:**
- `RobotsPolicy` exposes the `Crawl-delay` it has already parsed for a URL's origin, through the
  stdlib parser's `crawl_delay`. It never fetches to answer.
- `PoliteClient._send` computes each host's interval as the maximum of `min_interval`, that delay
  and `host_intervals[host]`.
- The CLI (plan 10) passes `{"apply.workable.com": 3.0}`.
- This plan edits the same file as plan 04, so it follows it.

**Tech Stack:** stdlib `urllib.robotparser`, httpx `MockTransport`, the fake clock and sleep of the
C1 tests.

**Content authority:** spec §1 premise 1, §3.2.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/robots.py                     — [modify] crawl_delay
packages/ijob-sources/src/ijob_sources/polite_http.py                — [modify] host_intervals=, interval per host
packages/ijob-sources/tests/ijob_sources_tests/test_robots.py        — [modify] tests for crawl_delay
packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py   — [modify] tests for spacing
```

## Order of work

### Task 1 — Read the robots.txt Crawl-delay

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/robots.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_robots.py`

**Change:**
- `RobotsPolicy.crawl_delay(url: str) -> float | None` returns
  `RobotFileParser.crawl_delay(<product token>)` from the cached entry for the URL's origin, as a
  float.
- It returns `None` in three cases:
  - no entry is cached yet;
  - the entry is a blanket allow or deny (no file was parsed);
  - the file sets no delay.
- It never sends a request.

**Proof:** written first, seen failing, then passing —
- `test_crawl_delay_read`: after `allowed()` on an origin whose robots.txt is `User-agent: *` /
  `Allow: /` / `Crawl-delay: 1`, `crawl_delay` is `1.0`. This is the spec §3.2 probe's file.
- `test_crawl_delay_absent`: a file without the line → `None`.
- `test_crawl_delay_unknown_origin`: before any `allowed()` call → `None`, and the transport saw no
  request.
- `test_crawl_delay_blanket_rules`: a 404 robots.txt (allow all) → `None`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_robots.py`
passes and the gate is green. Commit `feat(sources): read the robots.txt crawl delay`.

### Task 2 — Space each host by its strictest interval

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/polite_http.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py`

**Change:**
- `PoliteClient.__init__` gains the keyword `host_intervals: Mapping[str, float] | None = None`
  (`None` means none).
- `_send` waits, since the previous request to that host started, the largest of three values:
  `min_interval`, the robots `crawl_delay` for the URL (zero when `None`), and the host's
  `host_intervals` entry (zero when absent). The host is the lower-cased netloc, as today
  (`polite_http.py:183`).
- `_retry_wait`'s fallback stays `min_interval`.
- Update the `_send` and class docstrings to state the rule.

**Proof:** written first, seen failing, then passing; each test records the fake sleep's waits —
- `test_default_interval_unchanged`: two requests to one host without overrides wait 1.0 s (the C1
  behaviour).
- `test_crawl_delay_raises_interval`: robots.txt `Crawl-delay: 2` → the second request waits 2.0 s.
- `test_host_override_raises_interval`: `host_intervals={"apply.workable.com": 3.0}` → the second
  request to that host waits 3.0 s; another host still waits 1.0 s.
- `test_strictest_wins`: `Crawl-delay: 2` and an override of 3.0 → 3.0 s.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests` passes and the
gate is green. Commit `feat(sources): space requests by the strictest host interval`.

---

## Risks

- **Most risky: Task 2 reading the robots entry inside `_send`.** `_send` also fetches robots.txt
  itself, while no entry exists yet. `crawl_delay` must answer `None` without fetching, or the
  robots request would recurse.
- Not done here: passing the Workable override (plan 10).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-sources` passes, the C1 tests included.
