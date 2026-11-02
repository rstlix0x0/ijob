---
status: approved
created: 2026-11-02
depends-on: [07]
built-on: 6ee2c2bd4580
---

# Recipe Command Line Implementation Plan

**Goal:** Let the user confirm a learned recipe on the command line.

**Architecture:**
- **Shared text.** `ijob_app/llm_text.py` gains `recipe_proposal_lines(proposal)` and
  `cost_line(report)`. Plan 09's TUI shows the same proposal through them.
  - The lines are spec §7 step 7: `recipe: <format> at <url>`, then `read N jobs, M invalid`, then
    up to five tab-separated job lines.
  - The cost line is `cost $X.XXXX of $Y.YY`.
- **Error text.** `_error_text` moves from `cli.py` to `ijob_app/render.py` as `error_text`, and
  gains `NoRecipeError` among the errors reported by their bare reason. `cli.py` and the new module
  use it. `render.py` already imports from `service.py`, so there is no cycle.
- **`ijob_app/cli_recipes.py`** holds the recipe flow, so `cli.py` (1,028 lines) only wires it:
  - `recipe_failures()` maps errors to exits:
    - `InvalidValueError` → `error: <error_text>`, exit 2;
    - `SourcesError` and `LlmError` → `error: <message>`, exit 1.
  - `learn_page(...)` runs after `UnrecognizedPageError`.
  - `reanalyze_command(...)` registers `sources reanalyze`.
  - `confirm_and_store(...)` is shared by both.
- **Wiring.** `_sources_app` gains `llm_for`, `is_interactive` and `resolver`. `make_app` builds
  `llm_for` before the sources group, and gains a keyword `resolver: HostResolver = system_resolver`.
  Every `open_service` call in `cli.py` passes it on: `service_on` in `_sources_app` (`cli.py:373-375`),
  the `cv` group, the posting commands and `tui`. CLI tests pass a fake resolver through `make_app`,
  so no test resolves DNS. `add-url` gains `--yes` and `--config`, and `reanalyze` takes `REF`, `--yes`,
  `--db` and `--config`.
- **Order of checks** follows spec §7's step order:
  - `add-url` on `UnrecognizedPageError`: first the model (step 1), then confirmation (step 3),
    then the paid call (step 4).
  - `reanalyze` runs steps 3–8 only (spec §8), so its confirmation check comes first, and a missing
    model surfaces from the analysis as `MissingModelError`.
- **Unchanged.** `add-url` for every recognized URL behaves exactly as today, and needs no model and
  no `--yes`.

**Tech Stack:** Python 3.14, typer, `typer.testing.CliRunner`, fake engines, `httpx.MockTransport`.

---

## Files that change

```
packages/ijob-app/src/ijob_app/render.py                          — [modify] error_text (moved from cli.py, plus NoRecipeError)
packages/ijob-app/src/ijob_app/llm_text.py                        — [modify] recipe_proposal_lines, cost_line
packages/ijob-app/src/ijob_app/cli_recipes.py                     — [create] recipe_failures, learn_page, confirm_and_store, reanalyze_command
packages/ijob-app/src/ijob_app/cli.py                             — [modify] use render.error_text; make_app resolver; add-url --yes/--config and fallback; register reanalyze; pass llm_for, is_interactive and resolver to _sources_app
packages/ijob-app/tests/ijob_app_tests/test_render.py             — [modify] error_text
packages/ijob-app/tests/ijob_app_tests/test_llm_text.py           — [modify] proposal lines and cost line
packages/ijob-app/tests/ijob_app_tests/test_cli_recipes.py        — [create]
```

## Order of work

### Task 1 — Proposal text and error text

**Files:**
- Modify `render.py`, `llm_text.py`, `cli.py` (the `_error_text` move only), `test_render.py` and
  `test_llm_text.py`.

**Change:**
- `error_text(error: InvalidValueError) -> str` returns `error.reason` for `UnknownSourceError`,
  `SourceCompanyError`, `InvalidCvError` and `NoRecipeError`, and `str(error)` otherwise. `cli.py`
  imports it, and its old private copy is removed.
- `recipe_proposal_lines(proposal) -> list[str]`:
  - `recipe: <recipe_format> at <recipe_url>`;
  - `read <len(jobs)> jobs, <invalid> invalid`;
  - then, for the first five jobs, `<title>\t<company>\t<location or ->\t<salary or ->\t<url>`.
    Salary is `money_text` of each `Money`, joined with `; `.
- `cost_line(report) -> str` is `cost $<spent:.4f> of $<cap:.2f>`. `_cost_and_stop` builds on it, so
  the existing summaries do not change.

**Proof:** written first, seen failing, then passing:
- `test_recipe_proposal_lines`: seven jobs give 2 + 5 lines. A job with no location or salary shows
  `-` in both columns. A salary shows `50000-74999 USD/year`.
- `test_cost_line_format`: 0.01234 of 0.5 gives `cost $0.0123 of $0.50`.
- `test_summaries_unchanged`: the existing search and score summary tests stay green.
- `test_error_text_bare_reasons` in `test_render.py`: `NoRecipeError` gives `greenhouse:x has no
  recipe`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_llm_text.py packages/ijob-app/tests/ijob_app_tests/test_render.py packages/ijob-app/tests/ijob_app_tests/test_cli.py`
passes and the CLAUDE.md gate is green. Commit `refactor(app): share proposal, cost and error text`.

### Task 2 — `add-url` learns an unrecognized page

**Files:**
- Create `cli_recipes.py` and `test_cli_recipes.py`.
- Modify `cli.py`.

**Change:**
- `add-url URL --company C [--yes] [--db] [--config]` calls `service.add_url` as today. Only on
  `UnrecognizedPageError` does it call `learn_page`:
  1. Under `recipe_failures()`, so a runtime error such as `ProviderUnavailableError` or a bad
     config value is reported and not a traceback: when `service.learns_recipes()` is false, it
     prints `error: <the error>` and then
     `note: set [models].source_recipe to let ijob learn this page`, both on stderr, and exits 1.
  2. When `--yes` is absent and `is_interactive()` is false, it prints
     `error: confirmation needed; pass --yes` and exits 1. No model call is made.
  3. It calls `service.analyze_url(url, company)` under `recipe_failures()`.
  4. It prints `recipe_proposal_lines` on stdout. `cost_line`, then `report_notes`, go on stderr.
  5. It calls `confirm_and_store`.
- `confirm_and_store(service, proposal, *, yes, is_interactive)`:
  - with `--yes`, it stores without asking;
  - otherwise it prompts `Store this source? [y/N] ` (`typer.prompt` with `default=""`,
    `show_default=False` and `prompt_suffix=" "`, as `cli_llm.py:181-186` does).
    `y` or `yes`, in any case and after stripping, stores. Anything else prints `not stored` on
    stdout and exits 1.
  - Storing calls `service.accept_recipe(proposal)` and prints `added <ref>` (`added recipe:<key>`).
- The service opens with the LLM options from `llm_for(home, config)`, as the `cv` and
  `instructions` groups do.

**Proof:** written first, seen failing, then passing. Each test uses `CliRunner`, fake engines, a fake
resolver and `httpx.MockTransport`:
- `test_add_url_without_model_prints_note`: exit 1. Stderr has today's error line, then the `note:`
  line.
- `test_add_url_non_interactive_needs_yes`: exit 1, the confirmation message, and the fake engine
  received no request.
- `test_add_url_yes_stores_recipe`: stdout has `recipe: html at …`, `read 2 jobs, 0 invalid`, the job
  lines and `added recipe:<key>`. Stderr has `cost $…`. `sources list` shows the `recipe:` ref.
- `test_add_url_interactive_accepts`: `is_interactive` true and input `YES\n` store the source.
- `test_add_url_interactive_declines`: input `n\n` prints `not stored`, exits 1, and stores nothing.
- `test_add_url_already_added`: exit 1, and the exact `already added; run: ijob sources reanalyze …`
  message.
- `test_add_url_invalid_recipe`: exit 1 with `error: the model's recipe is invalid: …`, and nothing is
  stored.
- `test_add_url_no_jobs`: exit 1 with `error: the proposed recipe read no jobs from the page`.
- `test_add_url_cap_before_json_call`: exit 1 with the cap message.
- `test_add_url_provider_failure`: exit 1 with `error: …`, and nothing is stored.
- `test_add_url_private_host`: exit 1 with `error: http://<host>/robots.txt is not a public address`.
- `test_add_url_refused_cases`: parametrized. A JobStreet host, a body over 5 MiB, and `text/plain` on
  the analysis fetch each exit 1 with `error: …`, and nothing is stored.
- `test_add_url_prompt_text`: the interactive prompt reads exactly `Store this source? [y/N] `.
- `test_add_url_learns_recipes_runtime_error`: a config with an unknown `[llm]` key exits 2 with
  `error: …` and no traceback.
- `test_add_url_job_posting_unchanged`: a `JobPosting` page is added as today, with no model
  configured and no `--yes`.
- `test_add_url_empty_feed_unchanged`: today's error, with no note.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_recipes.py packages/ijob-app/tests/ijob_app_tests/test_cli.py`
passes and the CLAUDE.md gate is green. Commit `feat(app): learn unrecognized pages from add-url`.

### Task 3 — `sources reanalyze`

**Files:**
- Modify `cli_recipes.py`, `cli.py` and `test_cli_recipes.py`.

**Change:**
- `reanalyze_command(sources_group, ...)` registers `reanalyze REF [--yes] [--db] [--config]`:
  1. `REF` is parsed as `sources remove` parses it. A bad ref exits 2.
  2. Without `--yes` and not interactive, it prints `error: confirmation needed; pass --yes` and exits
     1, with no call.
  3. It calls `service.analyze_source(ref)` under `recipe_failures()`.
  4. Its output and confirmation are the same as `add-url`'s: `recipe_proposal_lines`, `cost_line`
     and `confirm_and_store`.
- The `sources` group help and `_sources_app`'s docstring name `reanalyze`.

**Proof:** written first, seen failing, then passing:
- `test_reanalyze_replaces_recipe`: with `--yes`, the stored recipe changes and the company stays.
- `test_reanalyze_unknown_ref`: exit 2, `error: unknown source recipe:…`.
- `test_reanalyze_removed_ref`: exit 2.
- `test_reanalyze_other_kind`: exit 2, `error: greenhouse:x has no recipe`.
- `test_reanalyze_missing_model`: exit 1, `error: …` naming `source_recipe`.
- `test_reanalyze_needs_yes_non_interactive`: exit 1, with no call.
- `test_reanalyze_declined_keeps_old_recipe`: input `n\n` leaves the stored recipe unchanged.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_recipes.py`
passes and the CLAUDE.md gate is green. Commit `feat(app): add sources reanalyze`.

## Risks

- **Riskiest step:** Task 2's wiring change in `make_app`. `_sources_app` now needs `llm_for`,
  which `_llm_groups` builds today. Build `llm_for` once, before both groups. The existing
  `test_cli.py` and `test_cli_llm.py` tests guard against regressions.
- **`not stored` on stdout.** The spec fixes the words, not the stream. It is the answer to the
  prompt the user just saw, so it goes beside the proposal on stdout.
- **Not done:** the TUI. That is plan 09.

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The gate exits 0.
