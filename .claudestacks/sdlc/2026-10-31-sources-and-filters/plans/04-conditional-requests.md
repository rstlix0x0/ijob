---
status: approved
created: 2026-10-31
built-on: aeb01064881c
---

# Conditional Requests Implementation Plan

**Goal:** `PoliteClient` serves an unchanged index from its cache after a `304 Not Modified`.

**Architecture:**
- `PoliteClient` gains an optional `cache` that satisfies a `ConditionalCache` protocol, defined in
  `polite_http` beside its only consumer (python guideline, protocols rule).
- `get` gains a keyword `conditional=False`. Only a conditional request sends validators, replays a
  304 from the cache as a 200, and stores a validated response.
- `ijob_sources.http_cache.SqliteHttpCache` is the implementation. It lives in its own SQLite file,
  separate from the postings database.
- Adapters never see a 304.

**Tech Stack:** httpx 0.28 (`MockTransport`), stdlib `sqlite3`, `dataclasses`, `typing.Protocol`,
pytest with `tmp_path`.

**Content authority:** spec §1 premise 1, §3.1.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/polite_http.py                — [modify] ConditionalCache, CachedResponse, cache=, get(conditional=)
packages/ijob-sources/src/ijob_sources/http_cache.py                 — [create] SqliteHttpCache, default_cache_path
packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py   — [modify] tests for conditional get
packages/ijob-sources/tests/ijob_sources_tests/test_http_cache.py    — [create] tests for http_cache
```

## Order of work

### Task 1 — Send conditional requests and replay 304s

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/polite_http.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py`

**Change:**
- `CachedResponse` is a frozen slotted dataclass `(etag: str | None, last_modified: str | None, body:
  bytes)`.
- `ConditionalCache` is a `Protocol` with `lookup(url: str) -> CachedResponse | None` and
  `store(url: str, response: CachedResponse) -> None`.
- `PoliteClient.__init__` gains the keyword `cache: ConditionalCache | None = None`.
- `_send` gains an optional `headers` mapping, passed to `httpx`; robots.txt requests pass none.
- `get(url, *, conditional: bool = False)`:
  - robots check, as today;
  - when `conditional` and a cache entry exists, send `If-None-Match: <etag>` and/or
    `If-Modified-Since: <last_modified>`;
  - on `304` with an entry, return `httpx.Response(200, content=entry.body, request=<the request>)`;
  - when `conditional` and a 2xx response carries `ETag` or `Last-Modified`, store it;
  - every other path is unchanged (`polite_http.py:125-141`): retries, 404 →
    `SourceNotFoundError`, other non-2xx → `HttpStatusError`.

  A 304 without a cache entry stays a non-2xx answer and raises `HttpStatusError`, as today.
- Without `cache`, or with `conditional=False`, behaviour equals C1's.

**Proof:** written first, seen failing, then passing (a dict-backed fake cache in the test module) —
- `test_conditional_sends_both_validators`: with an entry holding both, the request carries both
  headers.
- `test_304_replayed_as_200_from_cache`: the transport answers 304; `get` returns status 200 with the
  cached body.
- `test_validated_response_is_stored`: a 200 with `ETag: W/"x"` stores `CachedResponse('W/"x"', None,
  body)`; a 200 with only `Last-Modified` stores that.
- `test_response_without_validator_not_stored`: a 200 with neither header leaves the cache empty.
- `test_unconditional_get_ignores_cache`: `conditional=False` sends no validator and stores nothing.
- `test_robots_request_never_conditional`: the robots.txt request carries no `If-None-Match`, even
  with a cache entry for its URL.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py`
passes (C1 tests included) and the gate is green. Commit
`feat(sources): send conditional requests through the polite client`.

### Task 2 — Keep validated bodies in an SQLite cache

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/http_cache.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_http_cache.py`

**Change:**
- `default_cache_path(environ) -> Path`: `$XDG_CACHE_HOME/ijob/http.sqlite`, else
  `~/.cache/ijob/http.sqlite` when `XDG_CACHE_HOME` is unset or empty.
- `SqliteHttpCache.open(path, *, now) -> SqliteHttpCache`, a context manager. It follows the C1
  `Store.open` pattern; spec §3.1's `SqliteHttpCache(path)` names the class. `now` is a
  `Callable[[], datetime]`, and plan 10 passes the CLI's injected clock.
  - On open, it:
  - creates the parent directory;
  - creates `responses(url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, body BLOB, used_at TEXT)
    STRICT` when absent;
  - deletes rows whose `used_at` is more than 30 days before `now()`.
- `lookup` returns the entry and sets `used_at`; `store` upserts it with `used_at = now()`.
- It satisfies `ConditionalCache` structurally and does not import it.

**Proof:** written first, seen failing, then passing —
- `test_store_then_lookup`: the entry round-trips, bytes included.
- `test_store_replaces`: a second `store` for the URL replaces the first.
- `test_lookup_missing_is_none`.
- `test_old_rows_evicted_on_open`: a row stored at t, then the cache reopened at t + 31 days →
  `lookup` is `None`; reopened at t + 29 days → still present.
- `test_lookup_refreshes_used_at`: a lookup at t + 20 days keeps the row alive at t + 31 days.
- `test_cache_path_xdg`: `XDG_CACHE_HOME` set, and unset or empty.
- `test_client_with_sqlite_cache` (integration): a `PoliteClient` over `MockTransport` with a
  `tmp_path` cache gets a 200 with an etag, then a 304; the second `get(conditional=True)` returns
  the first body.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_http_cache.py`
passes and the gate is green. Commit `feat(sources): cache validated responses in sqlite`.

---

## Risks

- **Most risky: Task 1's 304 path inside the retry loop.** A 304 must be turned into the cached 200
  before the success check, and only when an entry exists. Otherwise a stray 304 would look like
  success with an empty body.
- Bodies are large (Palantir's Lever board is 5.9 MB, spec §2.1); storing only `conditional` index
  URLs and evicting after 30 days bounds the file (spec §3.1).
- Not done here: which adapters pass `conditional=True` (plans 06–08), and the CLI that opens the
  cache (plan 10).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked pytest packages/ijob-sources` passes, the C1 tests included.
