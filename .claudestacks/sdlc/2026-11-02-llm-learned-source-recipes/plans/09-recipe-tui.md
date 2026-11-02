---
status: approved
created: 2026-11-02
depends-on: [07, 08]
built-on: 6ee2c2bd4580
---

# Recipe Proposal in the TUI Implementation Plan

**Goal:** Let the user accept or cancel a learned recipe from the TUI sources screen.

**Architecture:**
- `SourcesScreen._add_url` (`tui/sources.py:145-154`) catches `UnrecognizedPageError` before its
  general `SourcesError` arm. It then runs the analysis in the same exclusive `sources` worker group
  (spec §7 step 9). The company is still required, as `_read_company` already makes it.
- A new modal `tui/recipe.py` `RecipeProposalScreen(ModalScreen[bool])`:
  - it shows `recipe_proposal_lines(proposal)` (plan 08);
  - it has **Accept** and **Cancel** buttons;
  - Escape cancels;
  - it dismisses with `True` on Accept.
- On Accept, the sources screen calls `service.accept_recipe(proposal)` and reloads its table.
- **Status and errors:**
  - the cost goes on a status line, using `cost_line(report)`. The sources screen gains a `#status`
    `Static`, since it has only `#error` today;
  - any `LlmError` (including `MissingModelError`), `SourcesError` or `InvalidValueError` from the
    analysis shows on the existing `#error` line (spec §7 step 9).
- **No `--yes` here.** The Accept button is the confirmation. Nothing is stored before Accept.
- **Import guard.** The last task proves spec §12's guard: starting `search`, `tui` and
  `sources list` with a recipe source stored loads no `dspy` and no `claude_agent_sdk`.

**Tech Stack:** Python 3.14, Textual (`ModalScreen`, `@work`, `App.run_test` pilot), fake engines,
`httpx.MockTransport`, pytest.

---

## Files that change

```
packages/ijob-app/src/ijob_app/tui/recipe.py                     — [create] RecipeProposalScreen
packages/ijob-app/src/ijob_app/tui/sources.py                    — [modify] analysis fallback, status line, accept on dismiss
packages/ijob-app/tests/ijob_app_tests/tui/test_recipe.py        — [create]
packages/ijob-app/tests/ijob_app_tests/tui/test_sources.py       — [modify]
packages/ijob-app/tests/ijob_app_tests/tui/conftest.py           — [modify] pass a fake resolver to open_service (:177, :216)
packages/ijob-app/tests/ijob_app_tests/test_recipe_imports.py    — [create] subprocess import guard
```

## Order of work

### Task 1 — Proposal modal and the sources-screen fallback

**Files:**
- Create `tui/recipe.py` and `tui/test_recipe.py`.
- Modify `tui/sources.py`, `tui/test_sources.py` and `tui/conftest.py`.

**Change:**
- `RecipeProposalScreen(proposal)`:
  - it composes one `Static` per proposal line, with `markup=False` because the text is untrusted
    page content, and the Accept and Cancel buttons;
  - Accept dismisses `True`; Cancel and Escape dismiss `False`.
- In `SourcesScreen._add_url`, an `UnrecognizedPageError` makes the screen:
  - clear `#error` and set `#status` to `analyzing <url>…`;
  - await `service.analyze_url(url, company)`;
  - set `#status` to `cost_line(proposal.report)`;
  - push `RecipeProposalScreen` and wait for its result with `push_screen_wait`, inside the worker.
- On `True`, it calls `accept_recipe`, reloads the table, and sets `#status` to `added <ref>`.
- On `False`, it sets `#status` to `not stored`.
- Errors from the analysis or the accept show on `#error` through `_error_text`
  (`tui/sources.py:35-39`), which widens to take `LlmError` too and returns its message.
  `_show_error` is unchanged.
- `tui/conftest.py`'s two `open_service` calls pass a fake resolver, so no TUI test resolves DNS.

**Proof:** written first, seen failing, then passing. Each test uses the existing TUI pilot fixtures,
fake engines, a fake resolver and `MockTransport`:
- `test_modal_shows_lines_and_buttons`: the proposal's lines are present, unrendered as markup (a
  title holding `[b]x[/b]` shows literally).
- `test_modal_accept_and_cancel`: Accept gives `True`; Cancel and Escape give `False`.
- `test_add_url_unrecognized_accept_stores`: after Accept, the table lists `recipe:<key>`, and the
  status reads `added recipe:<key>`.
- `test_add_url_unrecognized_cancel_stores_nothing`: the status reads `not stored`.
- `test_add_url_without_model_shows_error`: `#error` shows the `MissingModelError` message, and no
  modal opens.
- `test_add_url_invalid_recipe_shows_error`: `#error` shows `the model's recipe is invalid: …`.
- `test_add_url_recognized_unchanged`: a `JobPosting` page is added as before, with no modal.
- `test_status_shows_cost`: the status line shows `cost $…` while the modal is open.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/tui`
passes and the CLAUDE.md gate is green. Commit `feat(app): confirm learned recipes in the TUI`.

### Task 2 — Import guard with recipe sources stored

**Files:**
- Create `test_recipe_imports.py`.

**Change:**
- Add a subprocess test. It builds a home and database holding a recipe source and its recipe through
  `Store.set_recipe_source`, then runs one Python process per entry point and prints whether `dspy`
  or `claude_agent_sdk` is in `sys.modules`:
  - `search`, as `ijob search` runs it (`cli.py:984-990`): `open_service(...).refresh(...)`, then
    `.search(...)`. The client factory serves the recipe page from `MockTransport`, with a fake
    resolver. `Service.search` alone never fetches (`service.py:401-455`), so the refresh is what
    runs `RecipeSource`;
  - `sources list`, through `make_app` and `CliRunner`;
  - `tui` start, through the TUI app's `run_test()` inside the subprocess. It opens the sources
    screen and exits. The real `tui` command would block.
- The pattern follows the existing guard tests (`test_service.py:1030-1056` and
  `test_cli_llm.py:240`).

**Proof:** written first, seen failing, then passing:
- `test_search_with_recipe_source_loads_no_llm_library`: it prints `False`, and the search lists the
  recipe source's jobs that the refresh stored.
- `test_sources_list_with_recipe_source_loads_no_llm_library`: it prints `False`.
- `test_tui_start_with_recipe_source_loads_no_llm_library`: it prints `False`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_recipe_imports.py`
passes and the CLAUDE.md gate is green. Commit `test(app): guard recipe sources against LLM imports`.

## Risks

- **Riskiest step:** Task 1's `push_screen_wait`. It must be called from a worker, so the analysis
  and the wait stay inside the `@work` coroutine `_add_url` already is.
- **Import guard false pass.** If the TUI subprocess exits before its sources screen module loads,
  the guard proves less. The TUI test imports `ijob_app.tui.sources` and `ijob_app.tui.recipe`
  explicitly before it checks `sys.modules`.
- **Not done:** reanalysis from the TUI. The spec gives it only on the command line (spec §8).

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The gate exits 0.
