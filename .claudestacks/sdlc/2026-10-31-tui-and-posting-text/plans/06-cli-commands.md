---
status: done
created: 2026-11-01
depends-on: [05]
built-on: b175307f42cd
---

# CLI Commands Implementation Plan

**Goal:** Every new service operation is reachable from the command line.

**Architecture:**
- All new commands live in `ijob_app.cli` as thin Typer commands over `Service` (plan 05): they parse arguments, call the service through `open_service`, print, and map `InvalidValueError` subclasses to exit 2 (`_EXIT_BAD_ARGUMENT`, `cli.py:75`) (spec §6, §8).
- Every new command takes `--db` with the same help text as `search` (`_DB_HELP`, `cli.py:79`). `cv` is a Typer sub-app, as `sources` is (`cli.py:313, 629`).
- The `tui` command belongs to plan 07.

**Tech Stack:** Python 3.14, Typer, `typer.testing.CliRunner`, `httpx.MockTransport`.

---

## Files that change

```
packages/ijob-app/src/ijob_app/cli.py                 — [modify] id column; show, label, cv set, cv show
packages/ijob-app/tests/ijob_app_tests/test_cli.py    — [modify] tests for the new output and commands
```

## Order of work

### Task 1 — Posting id in `search` output

**Files:**
- Modify `packages/ijob-app/src/ijob_app/cli.py`
- Test `packages/ijob-app/tests/ijob_app_tests/test_cli.py`

**Change:** `_posting_line` (`cli.py:604-611`) puts the posting id first, then a tab, then the existing line, so the `? ` low-confidence flag follows the id. Update the `search` help text and module docstring that describe the columns.

**Proof:** written first, seen failing, then passing —
- `test_search_lines_start_with_posting_id` — each printed line's first tab-separated field is the integer id of a stored posting.
- `test_search_low_confidence_flag_follows_id` — an HN posting's line reads `<id>\t? <title>…`.
- Existing `search` tests that compare whole lines are updated for the new first column in this task; no other assertion changes.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes and the gate in CLAUDE.md is green. Commit `feat(app): print posting ids in search results`.

### Task 2 — `ijob show ID`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/cli.py`
- Test `test_cli.py`

**Change:** `show ID [--db PATH]` runs `asyncio.run(service.detail(id))` and prints, one per line: title, company, location, salaries, country and remote scope, posted date, sources, URL, label and note when present, then a blank line and the description, or `description unavailable: <reason>` when it has none. An unknown id raises `UnknownPostingError`, which the uniform mapping prints as `error: invalid posting: no posting N` (`InvalidValueError.__str__`, `ijob_core/errors.py:23`) and exits 2.

**Proof:** written first, seen failing, then passing —
- `test_show_prints_metadata_and_description` — after a search records a Greenhouse posting, `show <id>` exits 0 and prints its title, URL and normalized description.
- `test_show_unavailable_description` — a Workable posting whose v2 route answers `500` prints `description unavailable:` and exits 0.
- `test_show_unknown_posting` — `show 999` exits 2 with `no posting 999`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes and the gate is green. Commit `feat(app): add the show command`.

### Task 3 — `ijob label`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/cli.py`
- Test `test_cli.py`

**Change:** `label ID [up|down] [--note TEXT] [--clear] [--db PATH]`. `up`/`down` with an optional note calls `service.label`; `--clear` alone calls `service.clear_label`. Giving both a vote and `--clear`, or neither, exits 2 with a usage message. Success prints the posting's id, vote and note.

**Proof:** written first, seen failing, then passing —
- `test_label_up_with_note` — `label <id> up --note "Go + payments"` exits 0; `show <id>` then prints the vote and note.
- `test_label_replace_and_clear` — `label <id> down` replaces the vote; `label <id> --clear` removes it.
- `test_label_usage_errors` — `label <id>` and `label <id> up --clear` each exit 2.
- `test_label_unknown_posting` — exits 2 with `no posting`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes and the gate is green. Commit `feat(app): add the label command`.

### Task 4 — `ijob cv set` and `ijob cv show`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/cli.py`
- Test `test_cli.py`

**Change:** a `cv` sub-app with `set PATH [--db PATH]` (calls `service.store_cv`, prints the stored name, size and hash) and `show [--db PATH]` (prints name, size and stored date, or `no CV stored`). `InvalidCvError` prints `error: <reason>` and exits 2.

**Proof:** written first, seen failing, then passing — with a made-up PDF the test writes:
- `test_cv_set_then_show` — `cv set <file>` exits 0; `cv show` prints the file name and size.
- `test_cv_show_none` — on a new database prints `no CV stored`.
- `test_cv_set_rejects_non_pdf` — a text file exits 2 naming the reason.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes and the gate is green. Commit `feat(app): add the cv commands`.

---

## Risks

- **Task 1 changes output that scripts may parse**: every field shifts one column right. `README.md` has no `ijob search` example (`grep -n "ijob search" README.md` → empty), so only tests change.
- Not done: the TUI and its `tui` command (plan 07).

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The five gate commands in CLAUDE.md exit `0`.
- `uv run --locked ijob --help` lists `search`, `show`, `label`, `cv` and `sources`.

## Review findings

- test-mandate — `cv set` error text only substring-checked — `test_cli.py` — fixed: exact `error: <path> is not a PDF: it does not start with %PDF-`
- test-mandate — CacheUnavailableError branch of `_refusals` untested for show/label — fixed: both exit 1, no DB created
- test-mandate — no test of `show`'s location/salary/country/remote/posted lines — fixed: exact block incl. `120000-150000 USD/year`, `from 120000 USD`, `up to 150000 EUR/month`
- correctness — after plan 05's fix, a failed CV copy raised a raw `OSError` traceback — `cli.py` `cv set` — fixed and verified: `test_cv_set_copy_failure_exits_1` (failed first, then passed); `uv run --locked pytest -q packages/ijob-app/tests/ijob_app_tests/test_cli.py` → `92 passed in 1.96s`
- deferred nits — module docstring command list; `_refusals` duplicating search/sources handling; `cv show` prints a timestamp (spec says date); dead fallback in `_detail_lines`; `label --clear` opens the cache; `{ID}` metavar; guard readability; unreachable else in `label`; tab/newline in a note breaks the one-line output; `startswith("20")` check

## Probe results

- `uv run --locked ijob --help` lists show, label, search, sources, cv (coder and reviewer runs).
- Full gate after plan 06: `168 files already formatted`; `All checks passed!`; `114 source files`; `2013 passed`.

## Deviations

- 2026-11-01 — `show` layout fixed as: title, company, `location:`, `salary:` per range, `country:`, `remote:`, `posted:`, `sources:`, `url:`, `label:`/`note:` when present, blank line, description or `description unavailable: <reason>`.
- 2026-11-01 — `label` prints `<id>\t<vote>\t<note or ->`; `--clear` prints `<id>\tcleared`; `--clear` with `--note` is a usage error.
- 2026-11-01 — `cv set` maps a failed copy (`OSError`) to `error: cannot store the CV: …`, exit 1.
- 2026-11-01 — Per-task commits not made (CLAUDE.md).
