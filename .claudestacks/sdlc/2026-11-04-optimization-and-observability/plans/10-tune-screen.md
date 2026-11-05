---
status: done
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

## Review findings

One review of a frozen snapshot; the coder's scoped gate was green (534 passed), and the full gate runs once at the end of the run.

Blocking, fixed:

- `cli_tune.py:80`: `if yes or not paid: return` froze the split with no yes. claude-sdk models only, closed stdin, no `--yes`: exit 0, 100 split rows, 2 runs. Nothing missing, no split: exit 0, 100 rows, 0 calls. Now `_confirm` asks, or refuses on closed stdin, whenever the set has no split. `test_claude_only_screen_closed_stdin_freezes_nothing`, `test_nothing_missing_closed_stdin_freezes_nothing` and `test_claude_only_screen_no_freezes_nothing` failed first. `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py packages/ijob-app/tests/ijob_app_tests/test_tune_view.py -q`: `60 passed in 14.55s`.

Fixed before this review:

- `_divide` checked review on the current set, not the set given. `test_review_of_another_set_freezes_nothing` failed first, then passed; the reviewer confirmed no path round the check.

Left open:

- `cli_tune.py:126` risk (security): a provider's error text can show up as a suggested command, because `say_note` reads a trailing `; run: <command>` in `report.stopped`. `_say_stopped` in `cli_eval.py` exists to prevent this. Fix: `literal=True`.
- `tune_view.py:46`, `tune_view.py:57` risk: the consent prompt shows one cap beside several runs, but each run has its own cap, so it understates the most the user agrees to spend. Fix: label it per run and show what the ledger has left.
- `cli_tune.py:115` risk: the pair running at Ctrl-C prints `not run`, though its row and results exist and it may have spent money.
- `cli_tune.py:107` risk: nothing catches a reversion of the Ctrl-C path.
- `tune_ops.py:569-582` risk: the `CostUnknownError` and `ProviderUnavailableError` arms and the `cost_unknown` stop have no test; `test_screen_stops_on_provider_unavailable` raises `ProviderFailedError`.
- `tune_ops.py:170` nit: `check_split` says "write nothing", but `review_items` writes derived labels.
- `cli_tune.py:118` nit: the result drops why a run is incomplete, skipped changed postings and tracing warnings, which `eval run` shows.
- `cli_tune.py:166` nit: an error `run_screen` does not catch loses the lines of the pairs that ended.
- `service.py` nit: pricing per pair fetches the public OpenRouter model list twice per model.
- Nits: `tune_ops.py:359` overclaims what is refused there; `tune_view.py:61` repeats "skipped"; `tune_view.py:82` branches on a display sentinel; `cli_tune.py:20` is inaccurate about its imports; `_confirm` repeats `_confirm_run`; `AFFIRMATIVE` and `ask_question` live in `cli_eval.py`; `ReviewPlan.has_set` and `set_id` hold one fact; `test_cli_tune.py:385` accepts exit 0 or 1; `test_cli_tune.py:162` checks only the word `Estimate`; `test_tune_view.py:99` is a filler assertion.

## Deviations

- 2026-11-05: `tune_app(open_session, is_interactive)` has no `now`, since the service owns the clock.
- 2026-11-05: `cli_eval.py`, `eval_estimate.py` and `eval_ops.py` changed though unlisted: public `eval_failures`, `ask_question`, `AFFIRMATIVE` and `sum_costs` so the tune commands reuse them, and `ReviewPlan.set_id` for the review check.
- 2026-11-05: no `[tune]` table and no `baseline` are one case, because `read_tune_settings` makes them the same state. The error names both, with exit 2.
- 2026-11-05: `test_full_run_needs_every_current_case` uses 30 of 60 cases, not 100 of 200; it tests the same rule.

## Gate

Five commands in `CLAUDE.md` on the tree holding plans 06 to 10, 2026-11-05: format, check and mypy (300 files) clean; `4870 passed in 323.45s`. `test_cli_terminal.py` was not in plan 10's file list, but `test_every_command_is_checked_here` needs a check for each command, so it gained `test_tune_screen_without_a_baseline_gives_the_setting`.

## Risk fixes

2026-11-05, on the user's word to fix all open risks:

- `cli_tune.py:126` `literal=True`: `test_screen_stop_text_is_literal_with_a_piped_stdout_and_a_terminal_stderr`.
- `tune_view.py:46`, `:57` the consent prompt says the cap is per run and shows what the ledger has left: `test_plan_labels_the_cap_per_run_and_shows_what_spending_has_left`, `test_screen_plan_lines_show_the_cap_per_run_and_the_recorded_spending_left`. `tune_view.py` now builds its own panel, copying `plan_panel` to change one line (open nit).
- `cli_tune.py:107`, `:115` the pair running at Ctrl-C reads `interrupted, kept incomplete`: `test_ctrl_c_keeps_the_running_pair_incomplete_and_runs_no_later_pair`, with a real `SIGINT`. `test_a_cancelled_screen_does_not_name_an_earlier_incomplete_run` guards which row it names.
- `tune_ops.py:569-582` the `CostUnknownError` arm was dead and is removed, since `run_items` absorbs it. `test_screen_stops_when_a_cost_is_unknown`, `test_screen_stops_on_provider_unavailable`, `test_screen_stops_on_provider_failure`, `test_screen_stops_when_a_run_cannot_start_for_an_unavailable_provider`.
