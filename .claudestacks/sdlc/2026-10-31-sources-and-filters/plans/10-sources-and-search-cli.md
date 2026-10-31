---
status: done
created: 2026-10-31
depends-on: [02, 03, 05, 09]
built-on: aeb01064881c
---

# Sources and Search CLI Implementation Plan

**Goal:** `ijob search` lists the open, filtered postings of every source in the store's sources
table.

**Architecture:**
- The bundled seed list is package data in `ijob-sources`, imported idempotently on every run.
- `ijob_app.cli` keeps C1's `make_app` factory shape:
  - the client factory now receives the HTTP cache;
  - a `now` clock is injected so tests control refresh intervals and staleness.
- `search` wires together:
  - plan 02's store (seeds, `record` with `still_listed`, `mark_fetched`, `open_postings`);
  - plan 03's filters;
  - plans 04–05's client options;
  - plan 09's registry.
- A new `sources` command group manages the table through plan 09's resolution and verification.

**Tech Stack:** typer 0.27.3 (`CliRunner`), stdlib `csv`, `importlib.resources`, `asyncio`, httpx
`MockTransport`, pytest with `tmp_path`.

**Content authority:** spec §1 premises 2–4, §2.6, §6.6, §9, §11 (`test_seeds`, `test_cli`).

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/data/seeds.csv                — [create] kind,key,company rows
packages/ijob-sources/src/ijob_sources/seeds.py                      — [create] load_seeds
packages/ijob-sources/README.md                                      — [modify] "Adding a source": still_listed, conditional, identity, registry, seeds
packages/ijob-app/src/ijob_app/cli.py                                — [modify] make_app(client_factory, now), search rewrite, sources group
packages/ijob-sources/tests/ijob_sources_tests/test_seeds.py          — [create]
packages/ijob-app/tests/ijob_app_tests/test_cli.py                   — [modify] C1 tests move to --source; new tests
```

## Order of work

### Task 1 — Ship the seed list

**Files:** Create `data/seeds.csv` and `seeds.py`; test `test_seeds.py`.

**Change:**
- `seeds.csv` has the header `kind,key,company`, then two groups of rows.
- **Hiring systems.** One row per `docs/research/companies.csv` row whose `verified_status` is `200`
  and whose `ats` is `greenhouse`, `ashby`, `lever`, `workable` or `gem`: kind = `ats`, key =
  `board_token`, company = `company`. That is 150 rows (spec §2.6). The coder derives them once with
  the `csv` module; the test below keeps them equal.
- **Boards, feeds and Tier B**, 18 rows, each with company `-`:
  - `himalayas:all`, `jobicy:engineering`, `remoteok:all`, `arbeitnow:all`, `4dayweek:engineering`;
  - `mycareersfuture:software-engineer`, `kalibrr:software-engineer`;
  - `rss:wwr-programming`, `rss:wwr-backend`, `rss:python`, `rss:golang`, `rss:elixir`,
    `rss:djinni-python`, `rss:bsj`;
  - `yc:swe`, `letsgetrusty:all`, `japandev:all`, `hn:whoishiring`.
- `load_seeds() -> list[tuple[SourceRef, str]]` reads the package-data file. Each row goes through
  `parse_source_ref`; a bad row raises `InvalidValueError` naming its line.

**Proof:**
- `test_hiring_rows_equal_research_csv`: the hiring-system rows equal the filtered research CSV,
  read from the repository's `docs/research/companies.csv` (C1's tests already read across the tree
  by path, `test_cli.py:16-24`), and number 150.
- `test_board_rows`: exactly the 18 refs above.
- `test_every_seed_builds`: `registry.build_source` accepts every row.
- `test_no_duplicate_refs`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_seeds.py`
passes and the gate is green. Commit `feat(sources): ship the seed source list`.

### Task 2 — Manage sources from the command line

**Files:** Modify `cli.py`; test `test_cli.py`.

**Change:**
- `make_app(client_factory: Callable[[ConditionalCache | None], PoliteClient], *, now:
  Callable[[], datetime] = <UTC now>)`. The production `app` passes a factory that builds
  `PoliteClient(cache=cache, host_intervals=HOST_INTERVALS)`.
- A `sources` group whose commands all take `--db PATH`, defaulting as in C1, and import the seeds
  first:
  - `list` prints one line per row: `ref`, `company`, `origin` and `last_fetched_at` or `-`,
    tab-separated.
  - `add REF --company NAME`:
    - parses the ref (`parse_source_ref`; a bad ref exits 2);
    - builds the source through the registry;
    - verifies it with `verify_new_source`;
    - `add_source`, then prints `added <ref>`.

    A verification failure prints the reason to stderr and exits 1.
  - `remove REF` calls `remove_source` and prints `removed <ref>`. A ref with no row, or one already
    removed (`SourceRow.removed`), exits 2.
  - `add-url URL --company NAME`:
    - runs `resolve_url`;
    - when the resolution carries a batch (steps 2–3), it is already verified;
    - otherwise (step 1) it runs `verify_new_source`;
    - then `add_source` with `target_url`, and prints `added <ref>`.
  - **Errors** for `add` and `add-url`: every `SourcesError` prints its message on stderr and exits
    1. That covers `UnsupportedUrlError`, `IdentityMismatchError`, `RobotsDisallowedError`,
    `SourceNotFoundError`, `HttpStatusError`, `FetchError` and `ParseError`.

**Proof** (CliRunner with `env` setting `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` and `XDG_DATA_HOME` to
directories under `tmp_path`, so a developer's own config, cache or database never leaks in;
`tmp_path` database; MockTransport serving plan 06/08 fixtures):
- `test_sources_list_shows_seeds`: 168 lines on a fresh database.
- `test_sources_add_verifies_identity`: `add greenhouse:tailscale2 --company Tailscale`, served the
  Tailscale fixture, prints `added`. With `--company Ramp` it exits 1 and names the identity
  failure.
- `test_sources_add_refuses_empty_board`: a Lever `[]` → exit 1.
- `test_sources_remove_then_list`: the removed seed no longer lists, and stays removed after another
  command (re-import).
- `test_sources_add_url_jobposting`: the JazzHR fixture URL → `added jobposting:<h>`.
- `test_sources_add_url_unsupported`: exit 1 with the reason.
- `test_sources_add_url_robots_blocked`: a disallowed URL → exit 1 with the robots message and no
  traceback.
- `test_sources_remove_unknown_exits_2`.
- `test_sources_bad_ref_exits_2`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes and
the gate is green. Commit `feat(app): manage sources from the command line`.

### Task 3 — Search every source with filters

**Files:** Modify `cli.py` and `README.md`; test `test_cli.py`.

**Change:** `search`, per spec §9 steps 1–5.

- **Options:** `--source REF` (repeatable), `--country`, `--remote`, `--since`, `--company`,
  `--min-salary`, `--require-salary`, `--db`, `--config`. `--greenhouse` is removed.
- **Steps:**
  1. **Validate first** (spec §9 step 1):
     - build the `FilterSpec` from `load_filters(<--config, or default_config_path(os.environ)>,
       explicit=<--config given>)`;
     - each flag given replaces its config value (parsers from plan 03);
     - parse every `--source` ref.

     Any `InvalidValueError` exits 2 before the store opens or any request is sent. Then open the
     store, import the seeds, and open `SqliteHttpCache.open(default_cache_path(os.environ), now=now)`
     with the same injected `now`.
  2. **Choose the sources.**
     - With no `--source`: every row from `sources()`.
     - With `--source`: each ref must name a row whose `removed` is false. Otherwise exit 2 with
       `unknown source <ref>; add it with: ijob sources add <ref> --company NAME`.
     - Leave out rows whose `last_fetched_at` is within `refresh_interval(kind)` of `now()`; print
       `kept <ref>: fetched less than <n> min ago` to stderr for each.
  3. **Collect.** Build each source with `build_source(row, known=store.external_ids(row.ref))`, and
     run `collect` through one client from the factory.
  4. **Record.** `seen_at = now()`.
     - `record(all drafts, seen_at=seen_at, still_listed=<every batch's still_listed, paired with its
       ref>)`.
     - `mark_fetched(ref, seen_at)` for each source that succeeded.
  5. **Filter and print.**
     - Print `open_postings(now=seen_at, sources=<--source refs or None>)` that pass `keep` with the
       `FilterSpec` from step 1.
     - Each line: `title`, `company`, `location` or `-`, the posting's source refs comma-joined (from
       its sightings), `url`, tab-separated, with a `? ` prefix when `low_confidence`.
- **stderr:**
  - C1's failure lines, generalised from `greenhouse:<key>` to `<ref>`;
  - `<ref>: skipped <n> invalid jobs`;
  - `skipped <ref>: identity check failed: <reason>` (for `IdentityMismatchError`);
  - one `stale <ref>: last fetched <YYYY-MM-DD>, postings hidden` per `stale_sources(now=…)` row.
- **Exit codes:**
  - `0` when at least one chosen source succeeded or was kept;
  - `1` when every chosen source failed;
  - `2` on a bad flag, ref or config (`InvalidValueError` from the config loader or a parser).
- **Source column.** It comes from `Store.posting_sources(posting_id)` (plan 02 Task 5).
- **README.** "Adding a source" gains four steps: register the kind in `registry.py`; add a seed row
  when the source is global; report known jobs through `still_listed` when the source fetches
  details; pass `conditional=True` only for a fixed index URL.

**Proof** (the same `env` isolation as Task 2):

- **C1 tests in `test_cli.py`, one by one:**

  | C1 test | Disposition |
  |---|---|
  | `test_search_prints_postings` | moved to `--source greenhouse:tailscale`; lines gain the source column |
  | `test_absent_location_prints_a_dash` | moved |
  | `test_second_search_adds_no_postings` | moved |
  | `test_robots_blocked_board_reported` | moved; line reads `skipped greenhouse:tailscale: disallowed by robots.txt` |
  | `test_unknown_board_reported_not_found` | rewritten: the 404 board is added as a source row first, then both are passed with two `--source` |
  | `test_http_and_fetch_failures_reported` | moved |
  | `test_parse_failure_reported` | moved |
  | `test_rejected_jobs_reported` | moved |
  | `test_bad_key_exits_2` | rewritten as `test_bad_source_ref_exits_2`: `--source "greenhouse:a/b"` → exit 2, no request, and no database file created, because validation precedes opening the store (step 1) |
  | `test_bad_key_among_good_ones_exits_2_without_fetching` | rewritten with two `--source`, one bad → exit 2, no request |
  | `test_missing_greenhouse_option_exits_2` | deleted: no `--source` now means every row, covered by `test_search_without_source_reads_every_row` |
  | `test_keys_are_trimmed_and_fetched_once` | rewritten as `test_repeated_source_fetched_once`: the same `--source` twice → one board request |
  | `test_failures_are_reported_in_key_order` | rewritten: stderr lines follow the order of the `--source` flags |
  | `test_failed_board_lists_none_of_its_stored_postings` | replaced by `test_failed_source_keeps_last_known_postings` (spec §1 premise 4) |
  | `test_unusable_db_fails_before_any_request` | moved |
  | `test_production_app_shows_help`, `test_help_lists_the_options` | rewritten to assert `--source` and `--db` are listed and `--greenhouse` is not |

- `test_bad_flag_or_config_sends_no_request` (parametrised): `--since 7x`, `--config` naming a
  missing file, and a config with `since = "7x"` each exit 2 with no request.
- `test_unknown_source_exits_2_with_hint`.
- `test_search_without_source_reads_every_row`: a fresh database with all seeds, a transport
  answering 404 for every board except one → that board's postings print, and the exit is 0.
- `test_filters_from_config_and_flags`: a config dropping `sales`, and `--country DE` dropping an
  on-site US posting while a remote-unknown one stays.
- `test_jobicy_kept_inside_refresh_interval`: two runs 10 minutes apart (injected `now`) → the second
  sends no Jobicy request and prints `kept jobicy:engineering`. The postings still print.
- `test_stale_source_hidden`: injected `now` 8 days after the last fetch, with the source failing →
  a `stale` line, and its postings are absent.
- `test_still_listed_keeps_detail_jobs_open`: a JapanDev run, then a second run whose sitemap lists
  the same job → no detail request; the posting still prints.
- `test_cross_source_posting_lists_both_refs`: the same job from two sources prints once, with both
  refs.
- `test_low_confidence_prefix`: an HN posting line starts with `? `.
- `test_identity_failure_line`: a Greenhouse row with company `Ramp`, served the Tailscale fixture →
  stderr `skipped greenhouse:tailscale: identity check failed: company 'Ramp' not found on the
  board`, and none of its postings is recorded.
- `test_conditional_cache_used`: a second run receives a 304 and still prints the postings.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes,
`uv run --locked ijob search --help` lists the new options and no `--greenhouse`, and the gate is
green. Commit `feat(app): search every source in the sources table`.

---

## Risks

- **Most risky: Task 3's equality of `seen_at`.** `open_postings` depends on each sighting's
  `last_seen` equalling its source's `last_fetched_at`. Both must be written from the one `seen_at`
  value of the run, never two calls to `now()`.
- A search with no `--source` touches about 168 sources. At 1 request per second per host,
  Greenhouse's 75 boards alone take at least 75 s. That is accepted and is not a defect. A manual
  acceptance run should use `--source` first.
- CLI tests must always pass `--source` or a transport that answers every seed host, or they would
  try 168 sources. The 404-everything transport in `test_search_without_source_reads_every_row`
  keeps that test offline.
- Not done here: output formatting beyond the plain listing (spec §12).

## Proof

- The five-command gate is green across the workspace.
- `uv run --locked ijob search --help` and `uv run --locked ijob sources --help` list their options.
- **Manual acceptance, run once by the user:**
  1. `uv run --locked ijob search --source greenhouse:tailscale --source himalayas:all --db
     /tmp/ijob-c2.db` prints postings with source refs.
  2. An immediate second run is served by 304s for Greenhouse.
  3. `uv run --locked ijob sources list --db /tmp/ijob-c2.db` shows `last_fetched_at` on the two
     sources.

## Review findings

One `claudestacks:reviewer` pass (2026-10-31), report `.airsstack/.../20261031-130611-d66e/34-reviewer-10.md`.
Verdict: COMPLIANT-WITH-AMENDMENTS, blocking set empty. The reviewer recomputed `seeds.csv`: the 150
hiring rows equal the research rows with `verified_status` 200 (75 greenhouse, 55 ashby, 11 lever,
7 workable, 2 gem, same order), plus the 18 board rows; 168 unique refs.

| Id | Finding | Disposition |
|---|---|---|
| C4 🟡 | the top risk (one `seen_at`) had no regression guard: the test clock was constant | fixed: ticking clock; splitting the stamp fails 15 tests |
| C3 🟡 | nothing tested that the production client passes `HOST_INTERVALS` (Workable 3 s) | fixed: dropping it fails the new test |
| C6 🟡 / S1 ❓ | `sources add` on an existing ref with another company printed "added" but kept the old company | fixed: exit 2 `<ref> is already a source for <company>` before any request; the same company revives a removed row |
| C9 🟡 | an unusable cache path raised an uncaught `NotADirectoryError` after seeding the store | fixed: cache opened first; `error: cannot open the HTTP cache …`, exit 1 |
| C7, C11 🟡/🔵 | lists in frozen types | fixed: tuples |
| C1, C2, C5, C8, C12 🔵 | docstrings, `Final` exit codes, blank `--company` in search | fixed |
| C10, C13 🔵 | `cli.py` size (one module); loose request bound in one test | kept |
| S2–S7 🔵 | additions and drifts | recorded below |

Fix round re-verification: whole-workspace gate green (see Probe results).

## Probe results

- Final whole-workspace gate after every fix round (orchestrator run): sync ok, 149 files formatted,
  ruff passed, mypy clean on 105 source files, **1500 passed**.
- `uv run --locked ijob search --help` lists `--source`, `--country`, `--remote`, `--since`, `--company`,
  `--min-salary`, `--require-salary/--no-require-salary`, `--db`, `--config` and no `--greenhouse`;
  `uv run --locked ijob sources --help` lists `list`, `add`, `remove`, `add-url`.
- Plan 08's proof grep `grep -rn "meili\|__NUXT_DATA__" packages/ijob-sources/src` now matches
  `data/seeds.csv:129: lever,meili,Meilisearch`, a company's Lever board token from
  `docs/research/companies.csv:172`, not a Meilisearch API call. Over `*.py` it prints nothing.
- Manual acceptance against the live sources is the user's step and has not been run.

## Deviations

- 2026-10-31, plan Task 3: stale notices cover only the sources chosen for the run; `--country`,
  `--remote`, `--company` are repeatable; `--require-salary` is tri-state; a row the registry refuses
  prints `failed <ref>: <reason>` and counts as failed.
- 2026-10-31, plan Task 2: a blank `--company` exits 2 on add/add-url; `add` on an existing ref with a
  different company exits 2 (review C6); errors read `error: <message>` like C1's.
- 2026-10-31, plan Task 1: `load_seeds` also checks the header, column count and blank companies.
- 2026-10-31, plan Task 3: README "Adding a source" gained five steps (an identity-check step added);
  the filters test uses a `\bdata\b` exclusion instead of "dropping sales".
