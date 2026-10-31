---
status: approved
created: 2026-10-31
depends-on: [01, 03, 05]
built-on: aafd5e32e8cd
---

# Search CLI Implementation Plan

**Goal:** `ijob search --greenhouse <tokens>` lists the current postings of the named boards.

**Architecture:** One module, `ijob_app.cli`, builds the typer application through a factory that
takes the `PoliteClient` factory, so tests inject an `httpx.MockTransport` and production uses the
real network. The command wires the pieces from plans 03 and 05: parse keys, open the store, run
`collect`, record, list, report failures, choose the exit code.

**Tech Stack:** typer 0.27.3 (`typer.testing.CliRunner`), `asyncio`, pytest with `tmp_path`.

**Content authority:** spec §8, §2 (console script).

---

## Files that change

```
packages/ijob-app/src/ijob_app/cli.py                  — [create] make_app, app, main
packages/ijob-app/pyproject.toml                       — [modify] [project.scripts] ijob = "ijob_app.cli:main"
uv.lock                                                — [modify] only if `uv lock` changes it
packages/ijob-app/tests/ijob_app_tests/test_cli.py     — [create] tests for cli
```

## Order of work

### Task 1 — Search Greenhouse boards from the command line

**Files:**
- Create `packages/ijob-app/src/ijob_app/cli.py`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_cli.py`.

**Change:**
- `make_app(client_factory) -> typer.Typer`, where `client_factory` returns a new `PoliteClient`;
  `app` is `make_app` over the real client; `main()` runs `app`.
- The `search` command takes `--greenhouse TEXT` (comma-separated keys) and `--db PATH` (default
  `default_db_path(os.environ)` from plan 03). In order (spec §8):
  1. Parse each key with `parse_source_key`; on `InvalidValueError` print the reason and exit `2`.
  2. Open the `Store`, build one `GreenhouseBoard` per key, run `collect` with `asyncio.run` inside the
     client's context.
  3. `Store.record` every batch's drafts with `seen_at` = now in UTC; print one line per posting from
     `Store.postings(sources=<refs that succeeded>)`: title, company, location, URL.
  4. Print to stderr, chosen by the type of `SourceFailure.error`:
     `skipped greenhouse:<key>: disallowed by robots.txt` (`RobotsDisallowedError`),
     `not found: greenhouse:<key>` (`SourceNotFoundError`),
     `failed greenhouse:<key>: HTTP <status>` (`HttpStatusError`),
     `failed greenhouse:<key>: <reason>` (`FetchError`, `ParseError`); and for a batch with
     `rejected > 0`, `greenhouse:<key>: skipped <n> invalid jobs`.
  5. Exit `0` when at least one source succeeded, `1` when every source failed.

**Proof:** written first, seen failing, then passing — `CliRunner` against `make_app` with a factory
building a `PoliteClient` (`ijob_sources.polite_http`, plan 04) over `httpx.MockTransport` with a
fake clock and a fake sleep that advances it, and `--db` under `tmp_path`; unless a test says
otherwise, the transport serves Greenhouse's robots.txt (`User-agent: *` / `Disallow: /embed/`) and
the plan-05 fixture:
- `test_search_prints_postings` — exit `0`; stdout has one line per fixture job with its title,
  company, location and URL.
- `test_second_search_adds_no_postings` — running twice leaves the store with the same number of
  postings, and the second run still prints them.
- `test_robots_blocked_board_reported` — with robots.txt `Disallow: /v1/`, stderr has
  `skipped greenhouse:tailscale: disallowed by robots.txt` and the exit is `1`.
- `test_unknown_board_reported_not_found` — a 404 board alongside a good one: stderr has
  `not found: greenhouse:<key>`, exit `0`.
- `test_http_and_fetch_failures_reported` (parametrised: the board path answering 500, and the board
  path raising `httpx.ConnectError`; in both cases robots.txt is still served normally, so the
  failure reaches the board request rather than the robots check) — the matching `failed …` line,
  exit `1`.
- `test_parse_failure_reported` — the board path answering 200 with a non-JSON body gives
  `failed greenhouse:<key>: <reason>` on stderr, exit `1`.
- `test_rejected_jobs_reported` — a body with one invalid job prints
  `greenhouse:<key>: skipped 1 invalid jobs` on stderr.
- `test_bad_key_exits_2` — `--greenhouse "a/b"` exits `2` and names the key.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes and
the gate is green. Commit `feat(app): search greenhouse boards from the command line`.

### Task 2 — Install the `ijob` command

**Files:**
- Modify `packages/ijob-app/pyproject.toml` — add `[project.scripts]` with
  `ijob = "ijob_app.cli:main"`.
- Modify `uv.lock` — run `uv lock`; commit it only if it changed.

**Change:** `uv sync` installs an `ijob` executable that runs `main`.

**Proof:** a manifest entry, not a Python module, so no unit test; the installed executable itself is
the check in "Done when" (running it proves the entry point resolves).

**Done when:** `uv sync --locked --all-groups --all-packages` succeeds, `uv run --locked ijob search
--help` lists `--greenhouse` and `--db`, and the gate is green. Commit
`feat(app): install the ijob command`.

---

## Risks

- **Most risky step: Task 1's async wiring.** One `asyncio.run` must enclose both the client's
  context and `collect`, so the `httpx.AsyncClient` is opened, used and closed on the same event loop.
- The output is a plain listing on purpose (spec §10); column formatting would make the stdout tests
  brittle for no gain now.
- A real `ijob search` run is not part of any test; one manual run against a real board is the
  acceptance check below, made by a person, not by CI.
- Not done: other source flags, seed import, filters (spec §10).

## Proof

- The five-command gate is green across the workspace.
- Manual acceptance, run once by the user: `uv run --locked ijob search --greenhouse tailscale --db
  /tmp/ijob-check.db` prints postings; a second run prints the same postings and the database still
  holds one posting per job.
