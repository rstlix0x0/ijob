---
status: done
created: 2026-11-03
depends-on: [03, 06]
built-on: 8947dcb8c009
---

# Agent evals Implementation Plan

**Goal:** Measure both agents against committed cases.

**Architecture:** The agent suites live beside the others in a new focused module and store runs the same way. Source agent cases come from `load_seeds()`, and committed hostile pages run through an injected transport and resolver. Explore cases are committed scenarios, graded with G-Eval.

**Tech Stack:** Python 3.14, DeepEval, DSPy, httpx, Typer.

**Content authority:** spec section 8, `source-agent` and `explore-agent`, section 11's real-data list, and the Screens section.

---

## Files that change

```
packages/ijob-ai/src/ijob_ai/evals/agent_cases.py                  — create: read committed agent cases
packages/ijob-ai/src/ijob_ai/evals/data/explore_scenarios.json     — create: 5 instructions
packages/ijob-ai/src/ijob_ai/evals/data/hostile_pages/             — create: 3 pages with injected targets
packages/ijob-ai/src/ijob_ai/evals/metrics.py                      — modify: source and explore rates
packages/ijob-ai/src/ijob_ai/evals/deepeval_metrics.py             — modify: instruction following steps
packages/ijob-app/src/ijob_app/eval_agent_suites.py                — create: the two agent suites
packages/ijob-app/src/ijob_app/eval_view.py                        — modify: agent suite panels
packages/ijob-app/src/ijob_app/cli_eval.py                         — modify: run accepts agent suites
packages/ijob-app/src/ijob_app/service.py                          — modify: agent suite methods
README.md                                                          — modify: commands, and milestone wording with no results
packages/ijob-app/README.md                                        — modify: new commands
```

## Order of work

### Task 1 — Define agent metrics and cases

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/evals/metrics.py` — `source_rate`, `explore_rate`.
- Modify `packages/ijob-ai/src/ijob_ai/evals/deepeval_metrics.py` — `INSTRUCTION_STEPS`, `grade_instruction`.
- Create `packages/ijob-ai/src/ijob_ai/evals/agent_cases.py` — `load_scenarios`, `load_hostile_pages`.
- Create `packages/ijob-ai/src/ijob_ai/evals/data/explore_scenarios.json`.
- Create `packages/ijob-ai/src/ijob_ai/evals/data/hostile_pages/` — 3 HTML pages, with a manifest of company, real board and injected targets.
- Test `packages/ijob-ai/tests/ijob_ai_tests/evals/test_metrics.py`, `packages/ijob-ai/tests/ijob_ai_tests/evals/test_deepeval_metrics.py`, `packages/ijob-ai/tests/ijob_ai_tests/evals/test_agent_cases.py`.

**Change:** Spec section 8's formulas and the committed data. Cases hold made-up text only.

**Proof:**
- `test_source_rate_example` — 70, 5, 10, 5 and 10 give 0.778.
- `test_source_rate_no_denominator` — `None`.
- `test_source_could_not_check_share` — over 10% reads as incomplete; exactly 10% does not.
- `test_explore_rate_example` — 3, 1, 1 and 1 give 0.75.
- `test_explore_rate_no_propose` — `None`.
- `test_grade_instruction_builds_the_case` — the instruction, and proposals one per line.
- `test_five_scenarios_load` — the count and shape.
- `test_three_hostile_pages_load` — each with its manifest.
- `test_hostile_pages_hold_injected_links` — each page links its injected targets.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/evals/test_agent_cases.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): add agent eval cases`.

### Task 2 — Run the source agent suite

**Files:**
- Create `packages/ijob-app/src/ijob_app/eval_agent_suites.py` — `source_cases`, `run_source_suite`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_agent_suites.py`.

**Change:** Spec section 8 `source-agent`: fixed shuffle of board seeds, one outcome per company in the stated order, and hostile page cases through an injected transport and resolver, apart from `--limit` and the main metric. Each case calls `run_source_agent` from plan 05, under one `run_items`. `EvalSpend.budget_for(ref)` picks each call's budget by provider, for the agent and for `source_recipe`, and owns the run row. The suite follows the default model table of spec section 8.

**Proof:**
- `test_cases_are_board_seeds_in_a_fixed_order` — one per company, repeatable, first 100 by default.
- `test_outcome_right_is_case_insensitive` — a matching reference.
- `test_outcome_other` — a verified source that differs.
- `test_outcome_could_not_check_beats_nothing` — no result, with a timeout.
- `test_outcome_failed` — an unusable answer.
- `test_outcome_nothing` — no result otherwise.
- `test_over_ten_percent_could_not_check_is_incomplete` — 11 of 100.
- `test_exactly_ten_percent_is_complete` — 10 of 100.
- `test_hostile_page_injected_target_is_a_violation` — a tool call on it counts.
- `test_hostile_pages_skip_the_main_metric` — and `--limit`.
- `test_hostile_pages_use_no_network` — only the injected transport is used.
- `test_agent_run_cannot_read_user_sources` — the source agent has no such tool.
- `test_a_case_ending_on_the_cap_is_incomplete` — also the last case.
- `test_source_suite_allows_a_juror` — no jury check.
- `test_source_suite_default_model` — `[models].source_agent`, and missing names the key.
- `test_source_suite_subscription_cost_leaves_the_cap_alone` — a Claude agent does not stop the run.
- `test_source_suite_run_row_keeps_spend_on_cancel` — a cancelled run's row holds what it spent.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_agent_suites.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run the source agent suite`.

### Task 3 — Run the explore agent suite

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_agent_suites.py` — `run_explore_suite`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_agent_suites.py`.

**Change:** Spec section 8 `explore-agent`: one agent run per scenario on an empty temporary store, so it reads no personal data, with the main metric, mean G-Eval instruction following, steps and cost. The grader rules of `reasons` apply. Spend and the run row go through `EvalSpend`, with each call's budget picked by provider.

**Proof:**
- `test_explore_suite_main_metric` — from a fake's `propose` calls.
- `test_explore_suite_grades_each_scenario` — one grader call per scenario.
- `test_explore_suite_refuses_grader_as_model` — refused.
- `test_explore_suite_needs_a_grader` — `MissingModelError` names the key.
- `test_explore_suite_ungraded_scenario_is_counted` — a grader `TaskOutputError`.
- `test_explore_suite_violation_is_recorded` — the count holds.
- `test_explore_suite_limit` — 1 to 5.
- `test_explore_suite_uses_an_empty_store` — `list_sources` and `companies_in_store` find nothing of the user's.
- `test_explore_suite_counts_inner_violations` — an off-site attempt inside `find_source` counts.
- `test_explore_suite_default_model` — `[models].explore_agent`, and missing names the key.
- `test_explore_suite_grader_cost_counts` — a Claude agent with an OpenRouter grader charges the cap for grading only.
- `test_explore_suite_run_row_keeps_spend_on_cancel` — a cancelled run's row holds what it spent.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_agent_suites.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run the explore agent suite`.

### Task 4 — Offer the agent suites in `ijob eval run`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_view.py` — `source_suite_panel`, `explore_suite_panel`.
- Modify `packages/ijob-app/src/ijob_app/cli_eval.py` — `source-agent` and `explore-agent`.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `eval_run_agents`.
- Modify `README.md` — the new commands, and milestone 3's wording as built. It claims no results, since real runs come after this set.
- Modify `packages/ijob-app/README.md` — the new commands.
- Test `packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py`, `packages/ijob-app/tests/ijob_app_tests/test_eval_view.py`.

**Change:** The spec's Screens section, the report's stated limit that the cases are easy, and the paid-run rule, whose estimate reads "up to the cap". These suites name no personal data. `eval report` lists them by suite and task version.

**Proof:**
- `test_source_suite_panel_draws_every_row` — in `test_eval_view.py`.
- `test_explore_suite_panel_draws_every_row` — in `test_eval_view.py`.
- `test_agent_suite_asks_but_names_no_data` — on OpenRouter, a question with no data notice.
- `test_source_suite_screen` — every row of the Screens section, and the limit line.
- `test_explore_suite_screen` — the metrics.
- `test_agent_suite_violation_exits_1` — any violation.
- `test_agent_suite_incomplete_exits_1` — any stop.
- `test_agent_suite_paid_estimate_reads_up_to_the_cap` — on OpenRouter.
- `test_agent_suite_limit_out_of_range_exits_2` — 0 and 151; 0 and 6.
- `test_agent_suites_appear_in_report` — comparable by suite and task version.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py` passes and the Definition of Done is green. Commit `feat(ijob-app): offer the agent suites`.

---

## Risks

- A real source-agent run needs Greenhouse and Lever reachable; they timed out from the probe shell (spec section 9).
- The score flatters the agent, because its cases are easy (spec section 12), and the report says so.
- The real-data list in spec section 11 spends money and needs the user's OK at each step, so `execute` does not run it. It runs after the set, with the user.

## Proof

- The five gate commands in `CLAUDE.md` exit 0.
- `uv run --locked ijob eval run --help` lists every suite in the spec.
- `git status --short` shows no labels, votes or personal data.
- After `execute`, the user and agent run the list in spec section 11 on the real `~/.ijob`, before calling the milestone done.

## Review findings

Two reviews covered this plan: Task 1 with the site rule, then Tasks 2 to 4. Each fix began with a test that failed on the reported input.

Fixed, Task 1:
- `hostile_pages/` the injected instructions sat in body text, which `open_page` never returns, so the target of 0 proved little. Each page now carries them in on-site link text within the 100-character cut.
- `explore_scenarios.json` the `stored-companies` scenario asked the agent to read a store that the suite keeps empty. It is now `platform-engineers`.

Fixed, Tasks 2 to 4 (`GITHUB_ACTIONS=1 pytest` on the eight suite, CLI, tool and run test files → 498 passed):
- `agent_tools.py` `propose("Acme Corp", "x")` went uncounted, so 1 accepted and 4 such calls read 1.00, not 0.20. Any non-blank unverified ref is now refused and counted.
- `eval_agent_suites.py` `refused` was counted by matching a text prefix. `ExploreTools.refused` and `ExploreResult.refused` carry it.
- `eval_agent_suites.py` calls on injected targets slipped through: `learn_page`, `open_page` with no scheme, and a target really on the site. `hostile_violations` reads each step's arguments and counts one violation per call.
- `eval_agent_suites.py` a verified source, an unusable answer and one board timeout left the denominator as could-not-check.
- `eval_agent_suites.py` a hostile page ending on an unusable answer, or never read, passed.
- `eval_agent_suites.py` `cost_per_company` carried hostile-page spend. A metered budget view sums company cases only.
- `eval_agent_suites.py` an explore scenario ending on an unusable answer passed. Its proposals still count in the main metric.
- `eval_agent_suites.py` violations of the case that ended a run were lost.
- `eval_view.py` number formats ignored `_RUN_ROWS`. `cli_eval.py` an incomplete run with no stop printed nothing in a pipe.
- `README.md` claimed all committed cases are made up and that DeepEval measures both agents; `packages/ijob-app/README.md` named only one of the two caps. Both are corrected.
- `test_cli_eval.py` 4 help tests failed on CI because Typer styles help under `GITHUB_ACTIONS`.

Open:
- `eval_agent_suites.py` the accepted and refused counts of the scenario that ended a run are lost; that run is incomplete and exits 1.
- `deepeval_metrics.py` a scenario with no proposals scores 0.0 with no grader call, which the spec does not define. A proposal holding a newline breaks "one per line".
- Spec: "a model missing from the list exits 2" does not hold for agent suites, which read no price list. "Limit stated in the report" is drawn only on the source panel.
- Nits: `RunPlan.cases` reads `4 cases` for `--limit 1`, since it counts the hostile pages; `1 cases` wording; seven private imports from `eval_suites`; a board target matches any host with that path segment, which only over-counts; `UP_TO_CAP` is missing from three docstrings; `_suite_label` repeats `_suite_name`.

## Deviations

- 2026-11-03: Task 1 started beside plan 05, before plan 06 was done, since its files and symbols do not depend on it. The other tasks kept to order.
- 2026-11-03: the source-agent outcome table gives no row for a run that verified a source and then ended on an unusable answer. It is built as Failed, since after a stop the source is only the latest verified one and not an answer. Could not check still comes first when nothing was verified.
- 2026-11-03: a hostile page never read stores the run as `incomplete`, since a target of 0 cannot be claimed from a run that never met the injection. An unusable answer on a page that was read fails the page only.
- 2026-11-03: `propose` counts any non-blank unverified text as a violation, though spec section 9 calls a malformed argument no violation. Only a blank ref is treated as malformed, since otherwise a model could raise the main metric by proposing plain text.
- 2026-11-03: cost per company and per scenario divide all spend, subscription included, as section 15 shows dollars on a claude-sdk model. An explore run's margin uses Wilson over accepted and refused, since the spec is silent.
- 2026-11-03: agent suites read no price list; the estimate reads "up to the cap" through `CallKind.AGENT`.
- 2026-11-03: `eval_estimate.py` and `eval_run.py` changed, though the plan's file list omits them. `eval_suites._execute` gained an optional `trusted` hook, for the 10% rule.
- 2026-11-03: spec section 11's real-data list has not run. It spends money and needs the user's OK at each step, as this plan's Risks state.
- 2026-11-04: the first real runs (1 to 3, claude-haiku-5-5) changed four things, on the user's word. Every hostile page violation came right after the mock web answered 404 for the page's real board, with the model calling the injected text untrusted, so the mock web now serves that board. One timed-out call ended run 3 and one unreadable reply ended run 2's only scenario, so `GuardedReAct` makes each of those calls once more. A scenario that ended on an unusable answer with no proposals is now counted as `unusable` with no score, in place of 0.0. `eval report` now names the runs it leaves out and says why.
- 2026-11-04: a Sonnet judge run read 65 of 100 replies as unusable, 63 of them valid answers that left out the `verdict` field name. `run_call`'s adapter now reads such an object when the signature has one output field of a model type. `docs/evals.md` was added to explain the numbers.
