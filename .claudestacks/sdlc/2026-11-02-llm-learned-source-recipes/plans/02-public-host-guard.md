---
status: done
created: 2026-11-02
built-on: 6ee2c2bd4580
---

# Guarded Polite Fetching Implementation Plan

**Goal:** Give recipe sources a polite client that follows redirects and refuses non-public or reserved hosts on every request.

**Architecture:**
- **`PoliteClient`** (`ijob_sources/polite_http.py`) gains two things, and an unguarded,
  non-following client behaves exactly as today:
  - `RequestGuard`, a PEP 695 alias for `Callable[[httpx.Request], Awaitable[None]]`;
  - `guard_requests(guard)`, which appends a guard to its httpx client's `request` event hooks.
    It is a method, not a constructor argument, so every existing `ClientFactory` stays unchanged.
    The service attaches it only to the clients it uses for recipe sources (plans 05 and 07).
  - `get(url, *, conditional=False, follow_redirects=False)` follows redirects by hand when asked
    (spec §6 "Redirects"). Each hop is a new `get` round: robots.txt for the hop's origin, pacing,
    the 429/503 retry, and every request hook.
- **Guard errors pass through.** `PoliteClient._send` catches only `httpx.RequestError`
  (`polite_http.py:311-312`), so a guard's `SourcesError` reaches the caller unwrapped. robots.txt
  goes through `_send` too (`polite_http.py:262-264`, `robots.py:131-135`), so it is guarded.
- **`ijob_sources/public_host.py`** holds the §6 rules:
  - `HostResolver`, an async callable from host and port to address strings;
  - `system_resolver`, using the running loop's `getaddrinfo`;
  - `refuse_reserved_host(url)`;
  - `public_host_guard(resolve) -> RequestGuard`.
- **New errors** in `ijob_sources/errors.py`:
  - `NotPublicAddressError(SourcesError)` reads `<url> is not a public address`, where `<url>` is
    the refused request's URL (spec §6);
  - `ReservedHostError(SourcesError)` reads `<host> is read only by its own source kind`.

**Tech Stack:** Python 3.14, httpx 0.28 event hooks, stdlib `ipaddress` and `asyncio`, pytest.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/polite_http.py                  — [modify] RequestGuard, guard_requests, get(follow_redirects=…)
packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py     — [modify] guard and redirect tests
packages/ijob-sources/src/ijob_sources/errors.py                       — [modify] NotPublicAddressError, ReservedHostError
packages/ijob-sources/src/ijob_sources/public_host.py                  — [create] HostResolver, system_resolver, refuse_reserved_host, public_host_guard
packages/ijob-sources/tests/ijob_sources_tests/test_errors.py          — [modify] the two new messages
packages/ijob-sources/tests/ijob_sources_tests/test_public_host.py     — [create]
```

## Order of work

### Task 1 — Request guards and redirect following on the polite client

**Files:**
- Modify `polite_http.py` and `test_polite_http.py`.

**Change:**
- Add `RequestGuard` and `PoliteClient.guard_requests(guard)`. The class docstring says a guard sees
  every request, robots.txt and redirect hops included.
- `get(..., follow_redirects: bool = False)`:
  - Unset, the behaviour is unchanged. A 3xx raises `HttpStatusError`, as
    `test_3xx_and_5xx_raise_http_status_error` pins.
  - Set, a 301, 302, 303, 307 or 308 answer with a `Location` header is followed. The location is
    resolved against the current URL, and the new URL goes through the whole `get` round again
    (robots, pacing, retry).
  - At most 5 redirects are followed. The sixth raises `FetchError(<url>, "more than five redirects")`.
  - A redirect without `Location` raises `HttpStatusError`.
  - The returned response's `url` is the final hop's.
  - `conditional=True` together with `follow_redirects=True` raises `ValueError`. No caller combines
    them (spec §6).
- Update `get`'s docstring: `Args` gains `follow_redirects`, and `Raises` gains the redirect cases.

**Proof:** written first, seen failing, then passing. Each test uses `httpx.MockTransport`, records
the URLs the transport receives, and uses stub guards (a recording one and a raising one):
- `test_guard_sees_robots_and_page`: a recording guard sees `/robots.txt` and then the page URL.
- `test_raising_guard_stops_before_sending`: a guard raising a `SourcesError` subclass makes `get`
  raise that error. The transport receives nothing.
- `test_follow_redirects_reaches_final_url`: `/a` → 302 → `/jobs/` returns the `/jobs/` body, and
  `response.url` ends with `/jobs/`.
- `test_redirect_hop_checks_robots_of_new_origin`: a 302 to another origin fetches that origin's
  `/robots.txt`. A disallow there raises `RobotsDisallowedError` for the hop URL.
- `test_guard_sees_each_redirect_hop`: a 302 from `http://example.com/a` to `http://10.0.0.1/b`. A
  recording guard sees both page URLs, and a guard refusing `10.0.0.1` stops the hop before the
  transport receives `/b`.
- `test_sixth_redirect_is_fetch_error`: six chained 302s raise `FetchError` whose reason is
  `more than five redirects`.
- `test_redirect_without_location_is_http_status_error`.
- `test_conditional_with_follow_redirects_refused`: it raises `ValueError`.
- `test_unguarded_client_unchanged`: the existing `test_polite_http.py` tests stay green.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_polite_http.py`
passes and the CLAUDE.md gate is green. Commit `feat(sources): let a polite client guard requests and follow redirects`.

### Task 2 — Address and host rules

**Files:**
- Modify `errors.py` and `test_errors.py`.
- Create `public_host.py`.
- Test `test_public_host.py`.

**Change:**
- `refuse_reserved_host(url)` raises `ReservedHostError` when:
  - any dot-separated label of the host, compared case-insensitively, is `jobstreet`;
  - or the host is `japan-dev.com` or ends with `.japan-dev.com`.
- `public_host_guard(resolve) -> RequestGuard` returns an async hook taking an `httpx.Request`. The
  hook:
  - raises `NotPublicAddressError(str(request.url))` when the scheme is not `http` or `https`;
  - calls `refuse_reserved_host`;
  - checks an IP-literal host directly, without resolving;
  - otherwise resolves the host with `resolve(host, port)`;
  - raises `NotPublicAddressError` unless every address parses with `ipaddress.ip_address` and
    `is_global`.
  - A resolver `OSError`, including `socket.gaierror`, becomes `FetchError(url, reason)`, as today's
    fetch errors do (spec §6).
- `system_resolver(host, port)` returns the distinct addresses from
  `asyncio.get_running_loop().getaddrinfo(host, port)`. Tests never call it.

**Proof:** written first, seen failing, then passing. Each test uses a fake resolver mapping hosts to
fixed addresses:
- `test_private_and_special_addresses_refused`: parametrized over the spec §6 list. `127.0.0.1`,
  `10.1.2.3`, `192.168.1.5`, `172.16.0.1`, `169.254.169.254`, `::1`, `fc00::1` and `100.64.0.1`
  each raise `NotPublicAddressError`, with the message `<request url> is not a public address`.
- `test_global_addresses_pass`: `8.8.8.8` and `2606:4700::1111` pass.
- `test_one_private_address_among_many_refused`: `['8.8.8.8', '10.0.0.1']` is refused.
- `test_localhost_refused`: a resolver giving `['127.0.0.1', '::1']` for `localhost` is refused.
- `test_ip_literal_checked_without_resolving`: `http://10.0.0.1/x` is refused, and the resolver is
  never called.
- `test_non_http_scheme_refused`: `ftp://example.com/x` raises `NotPublicAddressError`, and the
  resolver is never called.
- `test_jobstreet_labels_refused`: `www.jobstreet.com`, `jobstreet.co.id` and `my.JobStreet.com.my`
  raise `ReservedHostError`. `notjobstreet.com` passes the host rule.
- `test_japandev_hosts_refused`: `japan-dev.com` and `www.japan-dev.com` are refused.
  `notjapan-dev.com` passes the host rule.
- `test_unresolvable_host_is_fetch_error`: a resolver raising `socket.gaierror` gives `FetchError`.
- `test_guard_on_polite_client_names_robots_url`: a `PoliteClient` guarded with `public_host_guard`
  and a resolver giving `10.0.0.1` raises `NotPublicAddressError` naming
  `http://example.com/robots.txt`, the first request to the origin (spec §6).
- `test_error_messages` in `test_errors.py`: the two new messages, exactly.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_public_host.py packages/ijob-sources/tests/ijob_sources_tests/test_errors.py`
passes and the CLAUDE.md gate is green. Commit `feat(sources): add public host rules for recipe sources`.

## Risks

- **Riskiest step:** Task 1's hand-followed redirects, which must reuse `get`'s robots, pacing and
  retry path without making it recursive past five hops. Use a loop over hops, not recursion.
- **DNS rebinding** between the guard's lookup and httpx's connect is accepted (spec §13).
- **Not done:** attaching the guard to real clients. Plans 05 and 07 do that.

## Proof

- `uv run --locked pytest packages/ijob-sources` passes.
- The gate exits 0.

## Review findings

- fixed — `polite_http.py:244` a host-less absolute `Location` (`javascript:alert(1)`, `mailto:a@b.c`) raised raw `httpx.InvalidURL`; `_send` now maps it to `FetchError`. `uv run --locked pytest packages/ijob-sources` → `1635 passed`.
- fixed — `polite_http.py:266` the unreached `except httpx.InvalidURL` around the `Location` join was removed (fix above makes it dead).
- fixed — `public_host.py:62` trailing-dot bypass (`https://japan-dev.com./jobs`); the host is now `.lower().rstrip(".")` before both reserved checks. Same run as above.
- open — `polite_http.py:185` no test checks guard order, or a guard's `FetchError` on robots.txt turning into deny-all, through `PoliteClient`.
- fixed — `public_host.py:60` an invalid punycode host (`http://xn--japan-dev.com/x`) raised `idna` errors (a `UnicodeError`). httpx raises it while building the request, before any guard runs. `PoliteClient._send` now maps `UnicodeError` to `FetchError`, so a page or redirect to such a host is a `FetchError` (a `RobotsDisallowedError` when it is the robots.txt fetch) and nothing is sent. `refuse_reserved_host` compares `raw_host` (ASCII) and no longer decodes. `uv run --locked pytest` → `2764 passed`.
- fixed — `public_host.py:115` NAT64 `64:ff9b::/96` addresses are global only when their embedded IPv4 address is (`64:ff9b::a00:1` refused, `64:ff9b::808:808` passes, both resolved and as a literal). `::ffff:10.0.0.1`, `64:ff9b:1::a00:1` and `2002:a00:1::` were already refused by `is_global` and are now pinned by tests. Same run as above.

## Deviations

- 2026-11-02 — The board row was not clean at start (another chain, `2026-11-01-llm-layer`, reads `spec disproven by plan 07`); this chain's row was clean.
- 2026-11-02 — Architecture says a guard's errors pass through unwrapped. That holds for `NotPublicAddressError` and `ReservedHostError`, but `robots.py` `_fetch` treats a `FetchError` on robots.txt as deny-all. So an unresolvable host reaches a `PoliteClient` caller as `RobotsDisallowedError`. `test_unresolvable_host_is_fetch_error` calls the guard hook directly. The `guard_requests` docstring states this.
- 2026-11-02 — Beyond the plan: an empty resolver answer is refused with `NotPublicAddressError` (otherwise "every address is global" passes vacuously). An unusable `Location` gives `FetchError`.
- 2026-11-02 — Test doubles in `test_kalibrr.py`, `test_fourdayweek.py` and `test_mycareersfuture.py` override `get`; their signature gained `follow_redirects` so mypy accepts the override (`mypy --strict .` reported `Signature of "get" incompatible with supertype` in all three).
- 2026-11-02 — Plans 01 and 02 were reviewed together by one reviewer over the combined diff. The task-briefer ledger and briefs were skipped: the coders read the plan's task sections directly.
- 2026-11-02 — Not committed: the instruction to execute did not ask for commits.
