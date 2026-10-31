---
status: approved
created: 2026-10-31
depends-on: [01]
built-on: aafd5e32e8cd
---

# Polite HTTP Implementation Plan

**Goal:** Every source request goes through one paced HTTP client that obeys robots.txt.

**Architecture:** Three modules in `ijob_sources`. `errors` is the package's exception root.
`robots` holds `RobotsPolicy`, which reaches the network only through a `send` callable it is given,
so it never imports the client (no import cycle). `polite_http` holds `PoliteClient`, which owns the
`httpx.AsyncClient`, the per-host pacing, the `Retry-After` loop and the status mapping, and builds
its `RobotsPolicy` with its own paced, redirect-following send. Monotonic clock, wall clock and sleep
are injected callables, so no test sleeps or reads real time.

**Tech Stack:** httpx 0.28.1 (`AsyncClient`, `MockTransport`), stdlib `urllib.robotparser`,
`email.utils`, `asyncio`, `importlib.metadata`, pytest.

**Content authority:** spec §1 premise 5, §5, §7 (`ijob_sources.errors`).

**Deviation from the spec text (named here, not left to execute):** spec §5 names the client module
`ijob_sources.http`. The python guideline forbids a module named after a standard-library module "at
any depth under `src/`" (`references/modularity.md:134-135`; reviewer checklist `:216`), and
`http` is in `sys.stdlib_module_names`. The module is therefore `ijob_sources.polite_http`, after what
it owns (`modularity.md:181`). The gate would not catch the nested name: ruff `A005` flags only
top-level modules unless `strict-checking` is set (`modularity.md:152-167`), and the root
`pyproject.toml` sets no `flake8-builtins` table. Plans 05 and 06 import from `polite_http`.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/errors.py                    — [create] SourcesError hierarchy
packages/ijob-sources/src/ijob_sources/robots.py                    — [create] RobotsPolicy
packages/ijob-sources/src/ijob_sources/polite_http.py               — [create] PoliteClient, USER_AGENT
packages/ijob-sources/tests/ijob_sources_tests/test_errors.py       — [create] tests for errors
packages/ijob-sources/tests/ijob_sources_tests/test_robots.py       — [create] tests for robots
packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py  — [create] tests for polite_http
```

## Order of work

### Task 1 — Add the sources exception hierarchy

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/errors.py` — `SourcesError(Exception)`;
  `RobotsDisallowedError(SourcesError)` carrying the URL; `HttpStatusError(SourcesError)` carrying
  URL and status; `SourceNotFoundError(HttpStatusError)`; `FetchError(SourcesError)` carrying the URL
  and the cause's text; `ParseError(SourcesError)` carrying what could not be read. Each builds its
  own message (ruff `TRY003`).
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_errors.py`.

**Change:** every failure a source can hit is one catchable `SourcesError`, and the CLI (plan 06) can
choose its message by type.

**Proof:** written first, seen failing, then passing —
- `test_every_error_is_a_sources_error` (parametrised over the five classes) — each instance is a
  `SourcesError`.
- `test_source_not_found_is_http_status_error_with_404` — a `SourceNotFoundError` is an
  `HttpStatusError` whose `status` is 404.
- `test_http_status_error_message_names_url_and_status` — `str()` contains both.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_errors.py`
passes and the gate is green. Commit `feat(sources): add the sources exception hierarchy`.

### Task 2 — Decide robots.txt access per origin

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/robots.py` — `RobotsPolicy` and its cache entry.
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_robots.py`.

**Change:** `RobotsPolicy(send, clock, product_token="ijob")`, where `send` is a
`Callable[[str], Awaitable[httpx.Response]]` and `clock` a `Callable[[], float]` (monotonic seconds).
`async allowed(url) -> bool`:
- The `/robots.txt` path itself is always allowed (RFC 9309 §2.2.2, spec §5.2), without a fetch.
- Otherwise it looks up the origin (scheme, host, port) in an in-memory cache. One `asyncio.Lock` per
  origin guards the fetch, so concurrent callers on a cold cache trigger exactly one fetch.
- On a fetch it calls `send("<origin>/robots.txt")` and applies spec §5.2's table, caching every
  outcome as the entry: 2xx → decode the first 500 KiB as UTF-8 with `errors="replace"`,
  `RobotFileParser.parse` the lines, answer with `can_fetch(product_token, url)`; a `ValueError` from
  `parse` counts as unreachable; 429 → disallow all; any other 4xx → allow all; 5xx → disallow all;
  a `FetchError` raised by `send` (which covers more than five redirects, Task 3) → disallow all.
- Entry lifetime: a 429 entry holds for the policy's life (spec §5.2: "for this client's life");
  every other entry is refetched once it is older than 24 h by `clock`.

**Proof:** written first, seen failing, then passing — each test passes a fake `send` returning a
canned `httpx.Response` (or raising) and a fake clock:
- `test_rfc9309_longest_match_cases` (parametrised) — the four rule sets in spec §1 premise 5's table
  give the RFC 9309 column's answers.
- `test_parser_group_cases` (parametrised) — the four cases quoted in spec §5.2 (merged groups for
  `/foo`, `/baz`, `/ok`; specific group over `*`; no matching group and no `*`; empty file) give the
  answers quoted there.
- `test_robots_txt_path_always_allowed` — `allowed("<origin>/robots.txt")` is `True` and `send` is
  not called.
- `test_404_allows_all` — `True` for any path.
- `test_429_disallows_all_for_policy_life` — `False`, and still `False` (with no second fetch) after
  the fake clock moves 25 h.
- `test_500_disallows_all`, `test_fetch_error_disallows_all` — `False`.
- `test_parse_value_error_disallows_all` — with `RobotFileParser.parse` monkeypatched to raise
  `ValueError`, `False`.
- `test_body_past_500_kib_is_cut` — a `Disallow: /` line placed after 500 KiB of comment lines has no
  effect.
- `test_cache_reused_within_24h_and_refetched_after` — calls at 0 h and 23 h fetch once; a call at
  25 h fetches again.
- `test_cache_is_per_origin` — a second host triggers its own fetch.
- `test_concurrent_cold_calls_fetch_once` — two `allowed` calls for one origin awaited together
  (`asyncio.gather` inside the test is fine; the guideline's `TaskGroup` rule covers library code)
  call `send` once.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_robots.py`
passes and the gate is green. Commit `feat(sources): decide robots.txt access per origin`.

### Task 3 — Send paced requests

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/polite_http.py` — `USER_AGENT`, `PoliteClient`
  construction, async context-manager support and the private paced send.
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py`.

**Change:**
- `USER_AGENT` is `ijob/<version> (+https://github.com/rstlix0x0/ijob)`, with `<version>` from
  `importlib.metadata.version("ijob-sources")`.
- `PoliteClient(*, transport=None, clock=time.monotonic, now=<UTC wall clock>, sleep=asyncio.sleep,
  min_interval=1.0)`: `clock: Callable[[], float]` drives pacing and the robots cache; `now:
  Callable[[], datetime]`, defaulting to a function returning `datetime.now(UTC)`, is the wall clock
  an HTTP-date `Retry-After` is measured against (an addition to spec §5.1's injected clock and sleep,
  needed to test that case without real time); `sleep: Callable[[float], Awaitable[None]]`. It owns
  one `httpx.AsyncClient` with `USER_AGENT`, a 30 s timeout, `follow_redirects=False` and
  `max_redirects=5`, and closes it on `__aexit__`. It builds a `RobotsPolicy` whose `send` is its own
  paced send with redirects followed, and whose `clock` is its `clock`.
- The paced send `_send(url, *, follow_redirects)` holds one `asyncio.Lock` per host and waits, by the
  injected `sleep`, until `min_interval` seconds of `clock` have passed since the start of the
  previous request to that host. It follows redirects only when asked: httpx 0.28.1 routes a
  per-request `follow_redirects` through the same redirect loop that enforces the client's
  `max_redirects` (`httpx/_client.py:1619-1623`, `:1686`, read during review); the
  `test_too_many_redirects_becomes_fetch_error` run below is the recorded proof of that shape.
  `httpx.TransportError` becomes `FetchError`; so does `httpx.TooManyRedirects`, which derives from
  `RequestError` rather than `TransportError` (spec §5.2 probe) — a deliberate addition to spec
  §5.1's mapping so the ">5 redirects → disallow" row reaches `RobotsPolicy` as a `FetchError`. Both
  are raised `from` the original. Every status is returned unmapped.

**Proof:** written first, seen failing, then passing — each test uses `httpx.MockTransport`, a fake
monotonic clock, and a fake sleep that records each requested wait and advances the fake clock by it:
- `test_user_agent_header_sent` — the request seen by the transport carries `USER_AGENT`, equal to
  `f"ijob/{importlib.metadata.version('ijob-sources')} (+https://github.com/rstlix0x0/ijob)"`.
- `test_same_host_requests_are_paced` — two sends to one host with the clock advanced 0.2 s between
  them record the waits `[0.8]`.
- `test_different_hosts_are_not_paced` — sends to two hosts record `[]`.
- `test_transport_error_becomes_fetch_error` — a transport raising `httpx.ConnectError` makes `_send`
  raise `FetchError` whose `__cause__` is the original.
- `test_too_many_redirects_becomes_fetch_error` — a transport that always answers 301, sent with
  redirects followed on a client built with `follow_redirects=False, max_redirects=5`, raises
  `FetchError` after the transport has seen six requests.
- `test_redirect_not_followed_by_default` — a 301 is returned as is when redirects are not asked for.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py`
passes and the gate is green. Commit `feat(sources): send paced requests with an honest user agent`.

### Task 4 — Get a source URL politely

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/polite_http.py` — add `PoliteClient.get` (the module
  created in Task 3).
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py` (the file created in
  Task 3).

**Change:** `async get(url) -> httpx.Response` follows spec §5.1 steps 1-4: robots check
(`RobotsDisallowedError` when denied); paced send without following redirects; on 429 or 503, wait
for `Retry-After` and resend, at most 3 attempts in total — seconds, or an HTTP-date
(`email.utils.parsedate_to_datetime`) measured against `now()`; missing or unparseable waits
`min_interval`; a past date waits 0 s; a wait above 60 s or a third failure raises `HttpStatusError`;
then 404 raises `SourceNotFoundError` and any other non-2xx, 3xx included, raises `HttpStatusError`.

**Proof:** written first, seen failing, then passing — same fakes as Task 3 (the sleep advances the
clock), a fixed `now`, and a transport whose `/robots.txt` answers `User-agent: *` with no rules
unless a test says otherwise. Every `get` first fetches robots.txt on the same host at clock 0, so
the board request's pacing wait of 1.0 s is the first recorded wait; each test asserts the complete
list of recorded waits:
- `test_get_returns_2xx_response` — a 200 body comes back unchanged; waits `[1.0]`.
- `test_get_refuses_robots_disallowed_url` — with robots.txt `Disallow: /`, `get` raises
  `RobotsDisallowedError` and the transport sees no request for the path.
- `test_get_disallowed_when_robots_redirects_forever` — robots.txt answering 301 to itself forever
  makes `get` raise `RobotsDisallowedError` (the composed path: `PoliteClient`'s `RobotsPolicy` uses
  the redirect-following paced send).
- `test_retry_after_seconds_then_success` — 429 with `Retry-After: 2`, then 200: waits `[1.0, 2.0]`
  (the resend starts 2.0 s after the previous start, so pacing adds nothing); the 200 is returned.
- `test_retry_after_http_date` — 503 with an HTTP-date 5 s after `now()`, then 200: waits
  `[1.0, 5.0]`.
- `test_retry_after_missing_or_unparseable_waits_min_interval` (parametrised: header absent,
  `"soon"`) — waits `[1.0, 1.0]`.
- `test_retry_after_past_date_waits_zero` — an HTTP-date 10 s before `now()`: waits `[1.0, 0.0, 1.0]`
  (the zero wait, then pacing for the resend).
- `test_gives_up_after_three_attempts` — three 429s with `Retry-After: 2` raise `HttpStatusError`
  with status 429, after three board requests.
- `test_gives_up_on_wait_above_60s` — `Retry-After: 61` raises `HttpStatusError`; waits `[1.0]`.
- `test_404_raises_source_not_found` — `SourceNotFoundError`.
- `test_3xx_and_5xx_raise_http_status_error` (parametrised over 301 and 500) — `HttpStatusError`
  carrying that status.

**Done when:** `uv run --locked pytest packages/ijob-sources` passes and the gate is green. Commit
`feat(sources): get source urls politely`.

---

## Risks

- **Most risky step: Task 4's retry loop.** It interleaves pacing, `Retry-After` and the robots fetch
  on one host. The tests pin the complete wait lists above, so any change in that interleaving shows
  up as a failing list rather than a silent extra sleep.
- `RobotsPolicy` must not reach `PoliteClient.get` (it would ask itself and map statuses spec §5.2
  wants raw); the `send` callable built in Task 3 is the only path, and
  `test_get_disallowed_when_robots_redirects_forever` covers the wiring.
- A host whose robots.txt answers 429 is disallowed for the whole run (spec §5.2), which may skip a
  board that would have recovered; accepted by the spec as the conservative side.
- Not done: no ETag cache, no persisted robots cache (spec §10).

## Proof

- `uv run --locked pytest packages/ijob-sources` passes; the five-command gate is green.
- No test opens a socket: every client in the tests is built with an `httpx.MockTransport`.
- `python3 -I -c "import sys; print('polite_http' in sys.stdlib_module_names, 'robots' in sys.stdlib_module_names, 'errors' in sys.stdlib_module_names)"`
  prints `False False False`.
