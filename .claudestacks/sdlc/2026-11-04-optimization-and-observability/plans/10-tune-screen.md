---
status: approved
created: 2026-11-04
depends-on: [04, 06, 07]
built-on: 671aa742d4fc
---

# Tune screen Implementation Plan

**Goal:** Measure every candidate model on all current labels with `ijob tune screen`.

**Architecture:** `tune_ops.py` works out which model and suite pairs are still needed and runs each through the `eval run` path, so each is an ordinary run with its own `EvalSpend`. A new `cli_tune.py` holds the `tune` command group, and `tune_view.py` its screens. The split is frozen after the user says yes and before the first call.

**Tech Stack:** Python 3.14, Typer, Rich.

**Content authority:** spec section 2 (asking), section 7 (who freezes), section 8 (`tune screen`), section 13 (refusal rows) and section 16 (no profile).

---

## Files that change

```
packages/ijob-app/src/ijob_app/tune_ops.py              — modify: screen plan, full runs, juror checks
packages/ijob-app/src/ijob_app/eval_suites.py           — modify: public juror check
packages/ijob-app/src/ijob_app/service.py               — modify: tune_prepare_screen, tune_screen
packages/ijob-app/src/ijob_app/tune_view.py             — create: the screen's plan and result
packages/ijob-app/src/ijob_app/cli_tune.py              — create: the tune group and screen
packages/ijob-app/src/ijob_app/cli.py                   — modify: register the group
packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py — modify: planning and running
packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py — create: the command
packages/ijob-app/tests/ijob_app_tests/test_tune_view.py — create: the plan panel
```

## Order of work

### Task 1 — Plan a screen

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_suites.py` — split `refuse_juror(store, eval_settings, model)` out of `_context` (`eval_suites.py:361-391`), with behaviour unchanged.
- Modify `packages/ijob-app/src/ijob_app/tune_ops.py` — `full_run`, `ScreenPlan`, `prepare_screen`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py`.

**Change:**
- `full_run(store, task, model, eval_set)` finds the newest run that is complete, not limited, at the current version, and whose results cover every case of the set with a label for the task and a posting `unchanged_posting` still returns. A changed posting can never have a result, since suites skip it, so it does not count against a run. A later `limited` row, such as a held-out test, does not hide a full run. Runs 11 and 12, with 100 cases, do not count on a 200-case set.
- `prepare_screen` lists `[tune].candidates`, `[tune].baseline` and whichever of `[models].judge` and `[models].scorer` are set, each once, for both `judge` and `scorer`, skipping any pair with a full run.
- It refuses when there is no `[tune]` table or no `baseline`, naming the key, and when a juror is in `candidates` or `baseline`, before any spend. A juror as the configured judge or scorer model is skipped, and the plan says so.
- With `candidates` empty it screens the baseline and configured models. Before any split, it checks what `freeze_split` would refuse, so a refusal comes before the question.
- A missing profile or a stale one is refused with the existing eval message, through `prepare_suite`.

**Proof:**
- `test_full_run_needs_every_current_case` — a 100-case run on a 200-case set is not full.
- `test_full_run_ignores_limited_and_incomplete_runs` — neither counts.
- `test_full_run_needs_the_current_version` — a `judge-1` run does not count.
- `test_a_changed_posting_does_not_stop_a_run_being_full` — a run missing only that posting is full.
- `test_a_later_limited_row_does_not_hide_a_full_run` — a held-out row after it leaves it the full run.
- `test_an_unset_configured_model_is_left_out` — no `[models].judge` raises nothing, and the rest stay.
- `test_screen_lists_each_model_for_both_suites` — candidates, baseline and configured models each give two pairs, with a repeat read once.
- `test_screen_skips_a_full_pair` — that pair is left out.
- `test_screen_without_tune_table_is_refused` — names `[tune]`.
- `test_screen_without_baseline_is_refused` — names `tune.baseline`.
- `test_juror_in_candidates_is_refused_before_spend` — `InvalidValueError`, and no run row.
- `test_juror_as_configured_model_is_skipped` — the plan lists it as skipped, and the rest stay.
- `test_empty_candidates_screens_the_rest` — baseline and configured models remain.
- `test_screen_refuses_too_few_keeps_before_asking` — `TooFewKeepsError`, with nothing frozen.
- `test_screen_without_a_usable_profile_is_refused` — a missing profile and a stale one each give the existing eval message.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py` passes and the Definition of Done is green. Commit `feat(ijob-app): plan a tuning screen`.

### Task 2 — Run a screen

**Files:**
- Modify `packages/ijob-app/src/ijob_app/tune_ops.py` — `run_screen`, `ScreenReport`.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `tune_prepare_screen`, which prices each pair through `eval_prepare_run`, and `tune_screen`.
- Create `packages/ijob-app/src/ijob_app/tune_view.py` — the plan panel, with the skipped models and the data notice from `data_notice`, and the result table.
- Create `packages/ijob-app/src/ijob_app/cli_tune.py` — `tune_app(open_session, is_interactive, now)`, `screen`, with `--yes`, `--db`, `--config`.
- Modify `packages/ijob-app/src/ijob_app/cli.py` — `app.add_typer(tune_app(...), name="tune")`.
- Create `packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py`, `test_tune_view.py`.

**Change:** Every `tune` command takes `--db` and `--config`. The command shows the estimate and asks, as `eval run` does, and a closed stdin without `--yes` refuses. On yes it freezes the split, then runs each pair in order with a fresh `EvalSpend`. `EvalBudgetSpentError`, `ProviderUnavailableError`, `CostUnknownError` or Ctrl-C ends the screen, with the rows already written standing. It ends with one line per pair, and exits 1 if any is incomplete. A rerun runs only what is still missing, and the split stays as it was.

**Proof:**
- `test_screen_asks_with_the_estimate` — the plan shows the cost, and no is the default.
- `test_screen_closed_stdin_refuses` — with no `--yes`, nothing runs and nothing is frozen.
- `test_screen_no_freezes_nothing` — answering no leaves the table empty.
- `test_screen_yes_freezes_then_runs` — the rows exist before the first fake call.
- `test_screen_writes_full_runs` — after it, each pair has a full run.
- `test_screen_rerun_keeps_the_split_and_runs_only_what_is_missing` — one missing pair gives one more run, with the same rows.
- `test_screen_stops_on_spent_budget` — the next pairs do not run, and it exits 1.
- `test_screen_stops_on_provider_unavailable` — the same.
- `test_screen_exits_1_on_an_incomplete_run` — the table shows it.
- `test_eval_commands_write_no_split` — `eval report` and `eval label` leave the table empty. Plans 11 and 12 test their own commands the same way.
- `test_cli_import_leaves_tuning_unloaded` — importing `ijob_app.cli` loads no DSPy and no optuna.
- `test_plan_panel_lists_skipped_models` — `tune_view`'s plan names a skipped juror.
- `test_plan_panel_shows_the_data_notice` — an OpenRouter model brings the notice, and claude-sdk alone does not.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob tune screen`.

---

## Risks

- `tune screen` is the first command that spends across several runs, so each run's cap is separate. The ledger bounds them all (spec section 19).
- `service.py` and `cli_tune.py` grow in plans 11 to 13, so those plans must run in sequence, not side by side.

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The five commands in `CLAUDE.md` exit 0.
