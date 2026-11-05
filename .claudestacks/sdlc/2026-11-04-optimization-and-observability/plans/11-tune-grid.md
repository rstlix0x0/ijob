---
status: approved
created: 2026-11-04
depends-on: [08, 09, 10]
built-on: 671aa742d4fc
---

# Tune grid Implementation Plan

**Goal:** Turn every chosen combination into a tested program with `ijob tune run`.

**Architecture:** `tune_grid.py` walks models x optimizers x tasks in sequence. Each combination gets its own `Tracing`, `operation_trace` named `tune-<task>`, one `eval_runs` row with suite `tune`, one `EvalSpend` and one `RailGuard`, then calls `tune_program`. A saved program runs through the normal suite on held-out postings only, under its version, in a `limited` row. `prepare_suite` gains `only` and `program` for that. No held-out posting reaches an optimizer.

**Tech Stack:** Python 3.14, DSPy, Typer.

**Content authority:** spec section 3 (Budgets, One `EvalSpend`), section 8 (`tune run`, Skipping and rerunning, Grid, Cost, Output), section 9 (`tune list`), section 10 (first paragraph), section 13 (refusal rows), section 15 and section 16.

---

## Files that change

```
packages/ijob-app/src/ijob_app/tune_grid.py              — create: grid plan, combinations, held-out runs
packages/ijob-app/src/ijob_app/eval_suites.py            — modify: only and program on a labelled run
packages/ijob-app/src/ijob_app/eval_run.py               — modify: plan_run passes only and program
packages/ijob-app/src/ijob_app/service.py                — modify: tune_prepare_run, tune_run, tune_list
packages/ijob-app/src/ijob_app/tune_view.py              — modify: grid plan, progress, table, list
packages/ijob-app/src/ijob_app/cli_tune.py               — modify: run and list
packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py — modify: held-out runs
packages/ijob-app/tests/ijob_app_tests/test_tune_grid.py — create: the grid
packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py  — modify: run and list
packages/ijob-app/tests/ijob_app_tests/test_tune_view.py — modify: the table
```

## Order of work

### Task 1 — Run a labelled suite on chosen postings with a program

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_suites.py` — `prepare_suite(only=, program=)`; `SuitePlan.program`, `SuitePlan.task_version`; `_Asker` passes the program; `run_judge_suite` and `run_scorer_suite` store under `plan.task_version`.
- Modify `packages/ijob-app/src/ijob_app/eval_run.py` — `plan_run(only=, program=)`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py`.

**Change:** `only` keeps the cases whose posting is in it, and the plan is then `limited`. A program sets the version of the run row and of every call. Without them nothing changes.

**Proof:**
- `test_only_keeps_the_named_postings` — the plan holds those cases, and is `limited`.
- `test_program_sets_the_run_version` — the row reads the program's version.
- `test_program_reaches_each_call` — the fake LM's prompt holds the program's instructions.
- `test_held_out_row_stays_out_of_the_report_table` — `comparable_runs` lists it in `other_ids`.
- `test_latest_run_skips_a_held_out_row` — `_latest_run` returns the full run.
- `test_no_only_or_program_is_unchanged` — a plain run reads as before.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run a suite on chosen postings with a program`.

### Task 2 — Plan the grid

**Files:**
- Create `packages/ijob-app/src/ijob_app/tune_grid.py` — `Combination`, `GridPlan`, `prepare_grid`, `combination_done`.
- Create `packages/ijob-app/tests/ijob_app_tests/test_tune_grid.py`.

**Change:**
- The grid is `[tune].models` x optimizers x judge and scorer, narrowed by `--model`, `--optimizer` and `--task`. `--model` not in `[tune].models` is allowed.
- It refuses before any spend: `models` empty, saying to read `tune report` and set it; a juror in `models` or `--model`; a model with no full run, pointing at `tune screen`; a missing split, pointing at `tune screen`; a missing `[models].tuner` when GEPA or MIPROv2 is in the grid, with `MissingModelError`; a missing or stale CV profile, or no rules, with the existing eval message through `stored_profile` and `stored_rules`.
- A combination is done when it has a `complete` `tune` row for its task, model, optimizer, base version and set, including `no change`, read from the row's metrics. `--again` runs it again and writes a new row.
- A saved program with no complete held-out run is planned as a retry of the test alone.
- The estimate uses the measured calls of P5, the 2.5 factor for demo calls of spec section 8, and the val pass of plan 09, priced by `read_prices`. Held-out calls are priced the same way.

**Proof:**
- `test_grid_is_models_by_optimizers_by_tasks` — two models give 12 combinations.
- `test_filters_narrow_the_grid` — each of the three options.
- `test_model_outside_the_list_is_allowed` — `--model` adds it, with a full run present.
- `test_empty_models_is_refused` — says to read `tune report` and set `[tune].models`.
- `test_juror_model_is_refused_before_spend` — `InvalidValueError`, and no run row.
- `test_model_without_a_full_run_is_refused` — names `tune screen`.
- `test_no_split_is_refused` — names `tune screen`, and writes no split.
- `test_missing_tuner_is_refused_for_gepa_and_mipro` — `MissingModelError`, while Bootstrap alone runs without it.
- `test_a_stale_profile_is_refused_before_spend` — the existing eval message, with no run row.
- `test_a_done_combination_is_skipped` — a complete row skips it.
- `test_no_change_counts_as_done` — that row skips it too.
- `test_again_plans_a_done_combination` — it is back in the plan.
- `test_an_untested_program_is_retried_alone` — planned with no tuning.
- `test_estimate_uses_the_measured_calls` — one model's six runs give 3,655 call-equivalents plus the val passes, at a fake price.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_grid.py` passes and the Definition of Done is green. Commit `feat(ijob-app): plan the tuning grid`.

### Task 3 — Run a combination

**Files:**
- Modify `packages/ijob-app/src/ijob_app/tune_grid.py` — `run_combination`, `CombinationResult`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_tune_grid.py`.

**Change:**
- Budgets: the run budget, `[tune].max_cost_usd`, is charged by every call. OpenRouter calls are also charged to an operation budget capped at `EvalSpend.cap`, through `budget_for`; claude-sdk calls go to `EvalSpend`'s subscription budget.
- Train and val come only from the frozen split, with labels as answers. Changed postings are left out and counted.
- The row uses suite `tune` and the base version. The guard's `on_call` calls `EvalSpend.record` after each call, so a killed process leaves its spend in the ledger (`eval_records.py:769`). Its metrics hold task, optimizer, calls, failures, unusable, baseline val, best val and program id. Baseline val is computed with the plan 09 metric from the model's full run, limited to val keys, so it costs nothing.
- A saved program goes through `save_program`, with `bound_sha256` the rules hash or the profile hash, then to the held-out run through `plan_run`, with a fresh `EvalSpend`. `no change` saves nothing and tests nothing. `incomplete` saves nothing.
- The root span is `ijob tune-<task>`. The tuner LM has the task `TUNER`, and its calls count on the same run budget.

**Proof:**
- `test_row_holds_its_metrics` — every field of the list above is in the JSON.
- `test_spend_joins_the_ledger` — OpenRouter spend moves `total_spend`, and subscription spend does not.
- `test_spend_is_recorded_after_each_call` — `total_spend` moves while the run is still open.
- `test_row_shows_the_unusable_count` — a model with no usable answer gives `no change`, with the count in its metrics.
- `test_run_cap_is_charged_by_both_providers` — a tuner call on the subscription moves the run budget only.
- `test_ledger_limit_is_charged_by_openrouter_only` — a claude-sdk call leaves that budget still.
- `test_no_held_out_posting_reaches_the_optimizer` — the fake LM never sees a held-out posting's text during tuning.
- `test_baseline_val_costs_nothing` — it comes from stored results, with no call.
- `test_program_row_binds_to_rules_or_profile` — judge and scorer each store their hash.
- `test_identical_result_reuses_the_row` — `--again` with the same answer keeps one program row.
- `test_accepted_program_is_never_touched` — `--again` leaves its accepted time.
- `test_a_program_is_tested_on_held_out_only` — the `limited` row covers exactly the held-out cases.
- `test_retry_tests_without_tuning` — no new `tune` row, and one new held-out row.
- `test_no_change_tests_nothing` — no program and no held-out row.
- `test_root_span_is_named_after_the_task` — `ijob tune-judge`.
- `test_new_set_ignores_old_rows` — a row of another set does not mark a combination done.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_grid.py` passes and the Definition of Done is green. Commit `feat(ijob-app): tune and test one combination`.

### Task 4 — Run the grid from the command line

**Files:**
- Modify `packages/ijob-app/src/ijob_app/tune_grid.py` — `run_grid`, `GridReport`, `tune_rows`.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `tune_prepare_run`, `tune_run`, `tune_list`.
- Modify `packages/ijob-app/src/ijob_app/tune_view.py` — plan, progress of calls and spend on stderr, table, list.
- Modify `packages/ijob-app/src/ijob_app/cli_tune.py` — `run` with `--model`, `--optimizer`, `--task`, `--again`, `--yes`; `list`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_tune_grid.py`, `test_cli_tune.py`.

**Change:** The grid follows the table in spec section 8. Run cap reached, over a quarter failed, or `compile` raised: goes on to the next. `EvalBudgetSpentError`, `ProviderUnavailableError`, `CostUnknownError` or Ctrl-C: stops. An empty ledger stops subscription-only combinations too, since `EvalSpend` refuses to build. It ends with a table of every combination and its state, each with its reason, and exits 1 if any is `incomplete`. Messages name `[tune].max_cost_usd` and `[eval].max_cost_usd` as in spec section 15. `tune list` shows each program's id, task, model, optimizer, version and `program_status`.

**Proof:**
- `test_cap_goes_on_to_the_next` — the next combination runs, and it exits 1.
- `test_quarter_failed_goes_on` — the message holds the count, the first error and the `ijob traces` hint.
- `test_compile_raised_goes_on` — the type and message are shown.
- `test_removed_model_goes_on` — its row is `incomplete`, and the next runs.
- `test_spent_budget_stops` — no later row.
- `test_provider_unavailable_stops` — no later row.
- `test_unknown_cost_stops` — no program, no later row.
- `test_ctrl_c_stops_and_drains` — the row is `incomplete` with its spend, and the stop flag is set.
- `test_empty_ledger_stops_subscription_combinations` — `EvalBudgetSpentError`, naming `eval.total_cost_usd`.
- `test_table_shows_every_combination` — done, tuned, `no change` with its unusable count, and `incomplete` each appear; in `test_tune_view.py`.
- `test_run_and_list_write_no_split` — the split table stays as it was.
- `test_run_asks_with_the_estimate` — no is the default; a closed stdin without `--yes` refuses.
- `test_an_incomplete_run_retries_next_time` — a rerun plans it again.
- `test_progress_goes_to_stderr` — stdout holds only the table.
- `test_list_shows_status` — `tested` and `untested` both appear.
- `test_list_writes_nothing` — no split or row appears.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_grid.py packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob tune run and list`.

---

## Risks

- This is the plan most likely to go wrong: it joins the rails, the engine, the ledger and the store. Task 3's tests run on fakes, and real cost and timing are for spec section 18.
- Two commands at once are not guarded (spec section 16).
- The size of `tune_grid.py`: keep the view and the run logic apart, as `eval_view.py` and `eval_suites.py` are.

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The five commands in `CLAUDE.md` exit 0.
