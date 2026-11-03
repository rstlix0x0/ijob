---
status: approved
created: 2026-11-03
depends-on: [02]
built-on: 8947dcb8c009
---

# Eval suites Implementation Plan

**Goal:** Measure a model with stored, comparable eval runs.

**Architecture:** Run-level numbers are pure functions in `ijob_ai/evals/metrics.py`. Only `deepeval_metrics.py` touches deepeval, inside one cached loader. `eval_suites.py` runs each suite through `run_items`, stores results and applies the common rules. `cli_eval.py` gains `run` and `report`.

**Tech Stack:** Python 3.14, DeepEval, DSPy, Typer, Rich.

**Content authority:** spec sections 3 and 8, except the agent suites, which plan 07 builds.

---

## Files that change

```
packages/ijob-ai/src/ijob_ai/evals/__init__.py          — create: package marker
packages/ijob-ai/src/ijob_ai/evals/metrics.py           — create: run-level metrics
packages/ijob-ai/src/ijob_ai/evals/deepeval_metrics.py  — create: DeepEval loader, metrics, grader
packages/ijob-ai/src/ijob_ai/evals/data/hostile.json    — create: made-up hostile pairs
packages/ijob-ai/src/ijob_ai/evals/hostile.py           — create: reads the hostile data
packages/ijob-app/src/ijob_app/eval_suites.py           — create: judge, scorer, hostile, reasons runs
packages/ijob-app/src/ijob_app/eval_report.py           — create: comparable runs and jury metrics
packages/ijob-app/src/ijob_app/eval_view.py             — modify: run and report screens
packages/ijob-app/src/ijob_app/cli_eval.py              — modify: run and report commands
packages/ijob-app/src/ijob_app/service.py               — modify: eval run and report methods
```

## Order of work

### Task 1 — Compute the metrics

**Files:**
- Create `packages/ijob-ai/src/ijob_ai/evals/__init__.py`.
- Create `packages/ijob-ai/src/ijob_ai/evals/metrics.py` — `judge_metrics`, `scorer_metrics`, `wilson`, `ndcg_at`, `spearman`, `attack_rate`.
- Create `packages/ijob-ai/tests/ijob_ai_tests/evals/__init__.py`.
- Test `packages/ijob-ai/tests/ijob_ai_tests/evals/test_metrics.py`.

**Change:** Each definition in spec section 8's metric table, with `None` where the table shows `-`. No model, no deepeval.

**Proof:**
- `test_keep_recall_precision_and_f1_example` — the checked example gives 0.75, 0.75 and 0.775.
- `test_right_rule_example` — gives 0.5, skipping labels with no rule.
- `test_wilson_example` — 36 of 40 gives (0.769, 0.96), ±0.095.
- `test_ndcg_examples` — 1.0, 0.43, 0.5 and 0.0 at k=2.
- `test_ndcg_ties_by_position` — 0.25 and 1.0 at k=1.
- `test_spearman_examples` — 1.0, -1.0 and 0.866.
- `test_mean_error_example` — 8.5.
- `test_undefined_values_are_none` — each "undefined" column cell gives `None`.
- `test_unusable_answer_counts_as_wrong` — it lowers recall, and ranks last.
- `test_attack_rule_examples` — every P-E row.
- `test_attack_rate_skips_unanswered_pairs` — it counts apart and is no attack.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/evals/test_metrics.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): compute eval metrics`.

### Task 2 — Wrap DeepEval

**Files:**
- Create `packages/ijob-ai/src/ijob_ai/evals/deepeval_metrics.py` — one cached loader that sets the two variables and defines all subclasses inside `warnings.catch_warnings`; `KeepMatch`, `ScoreClose`, `Grader`; `measure_case`; `grade_reason`; `FAITHFUL_STEPS`; `GRADER_VERSION`, recorded on the grader's spans.
- Test `packages/ijob-ai/tests/ijob_ai_tests/evals/test_deepeval_metrics.py`.

**Change:** `measure_case` builds an `LLMTestCase` and calls a metric directly with `_show_indicator=False`, never `evaluate()`. `Grader.a_generate` builds a `dspy.Predict` for the schema and goes through `run_call` with task `EVAL_GRADER`; `generate` raises. Case fields and steps follow spec section 8 `reasons`.

**Proof:**
- `test_keep_match_passes_on_the_same_answer` — keep vs keep passes; keep vs reject fails.
- `test_score_close_within_twenty` — 20 passes; 21 fails.
- `test_grader_charges_one_call` — one fake engine call, charged to the budget.
- `test_grade_reason_scores_zero_to_one` — a fake 7 reads 0.7, passing at 0.5.
- `test_measure_prints_and_writes_nothing` — no output, and no file in the working directory.
- `test_loader_sets_opt_outs_before_import` — both variables are set when deepeval loads.
- `test_grader_generate_raises` — the sync path is refused.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/evals/test_deepeval_metrics.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): wrap DeepEval metrics`.

### Task 3 — Run the judge and scorer suites

**Files:**
- Create `packages/ijob-app/src/ijob_app/eval_suites.py` — `prepare_suite`, `run_judge_suite`, `run_scorer_suite`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py`.

**Change:** Default models and `--limit` from spec section 8's table, on labelled cases by position, marking a limited run. Common rules: model on the set's jury refused, fewer than 10 labels refused, no set exits 1 with the fix, changed postings skipped and counted, spend and the run row through `EvalSpend`, `incomplete` on a stop. Latency is kept per case.

**Proof:**
- `test_default_models` — each suite's default.
- `test_missing_model_names_the_key` — `MissingModelError`.
- `test_model_on_the_stored_jury_is_refused` — also a former juror of this set.
- `test_too_few_labels_is_refused` — it names the fix.
- `test_changed_posting_is_skipped_and_counted` — it is left out of the metrics.
- `test_limited_run_is_marked` — the row records it.
- `test_stop_stores_incomplete` — a cap or provider stop.
- `test_results_are_stored_per_case` — answer, pass and latency.
- `test_judge_metrics_are_stored` — the run's JSON holds the run-level numbers.
- `test_suite_run_row_keeps_spend_on_cancel` — a cancelled run's row holds what it spent.
- `test_limit_splits_labelled_cases` — the split of spec section 6, by position.
- `test_no_set_names_the_fix` — no jury, rules or profile.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run the judge and scorer suites`.

### Task 4 — Run the hostile suite

**Files:**
- Create `packages/ijob-ai/src/ijob_ai/evals/data/hostile.json` — 5 judge pairs, 5 scorer pairs, made-up rules and profile.
- Create `packages/ijob-ai/src/ijob_ai/evals/hostile.py` — `load_hostile`, reading it from package data.
- Modify `packages/ijob-app/src/ijob_app/eval_suites.py` — `run_hostile_suite`.
- Test `packages/ijob-ai/tests/ijob_ai_tests/evals/test_hostile.py`, `packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py`.

**Change:** Runs each pair's clean and hostile posting with the model under test, for both tasks. The hostile data holds no personal text. The run's `task_version` holds both versions, as `judge-1+scorer-1`. Results follow spec section 8 `hostile`.

**Proof:**
- `test_hostile_data_has_five_pairs_each` — the counts and shapes hold.
- `test_hostile_pairs_differ_only_by_injection` — the hostile text contains the clean text.
- `test_hostile_flip_counts_as_attack` — a fake flips, and the rate shows it.
- `test_hostile_allows_a_juror` — no jury check.
- `test_hostile_needs_an_answered_pair` — all unusable fails the run.
- `test_hostile_incomplete_fails` — a stop reports failure.
- `test_hostile_records_both_task_versions` — the combined string.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/evals/test_hostile.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run the hostile suite`.

### Task 5 — Run the reasons suite

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_suites.py` — `run_reasons_suite`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py`.

**Change:** Grades answers from the model's latest complete, unlimited runs at current task versions, per spec section 8 `reasons`. The run's `task_version` holds both, as in Task 4, and the common rules apply.

**Proof:**
- `test_reasons_needs_both_runs` — each missing one names its command.
- `test_reasons_ignores_limited_and_incomplete_runs` — neither qualifies.
- `test_reasons_takes_ten_and_ten` — and `--limit` splits evenly, the odd one to the judge.
- `test_reasons_refuses_grader_as_model` — exit reason names it.
- `test_reasons_ungraded_case_is_counted` — a grader `TaskOutputError` leaves it out of the mean.
- `test_reasons_all_ungraded_shows_undefined` — the mean is `None`.
- `test_reasons_grader_provider_failure_stops` — the run is `incomplete`.
- `test_reasons_builds_the_case_fields` — rules or profile, then posting; reason or reasons.
- `test_reasons_refuses_a_juror` — the model on the set's jury.
- `test_reasons_refuses_too_few_labels` — it names the fix.
- `test_reasons_skips_changed_postings` — skipped and counted.
- `test_reasons_needs_a_grader` — `MissingModelError` names `models.eval_grader`.
- `test_grader_openrouter_cost_counts` — a Claude model with an OpenRouter grader charges the cap for grading only.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run the reasons suite`.

### Task 6 — Add `ijob eval run`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_view.py` — `suite_panel`, `hostile_panel`.
- Modify `packages/ijob-app/src/ijob_app/cli_eval.py` — the `run` command.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `eval_run`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py`, `packages/ijob-app/tests/ijob_app_tests/test_eval_view.py`.

**Change:** The screens from the spec's Screens section, plain `key value` lines in a pipe, the paid-run rule, and the exit status rules of spec section 8. `hostile` refuses `--limit`.

**Proof:**
- `test_run_judge_screen` — the metrics, ±, cost, speed and next command appear.
- `test_run_scorer_screen` — NDCG@10, Spearman, mean error, cost and speed appear.
- `test_run_provider_stop_exits_1_cap_exits_0` — for `judge` and `scorer`.
- `test_run_cost_unknown_exits_1` — the run stops.
- `test_hostile_asks_but_names_no_data` — on OpenRouter, a question with no data notice.
- `test_suite_panel_shows_undefined_as_dash` — a `None` metric, in `test_eval_view.py`.
- `test_hostile_panel_marks_changed_pairs` — in `test_eval_view.py`.
- `test_run_pipe_prints_key_values` — no box characters.
- `test_paid_run_asks_first` — OpenRouter model, or OpenRouter grader with a Claude model.
- `test_paid_run_names_the_data_and_vendors` — the line of spec section 8.
- `test_claude_only_run_does_not_ask` — no question.
- `test_paid_run_in_a_pipe_needs_yes` — exit 1 before any call.
- `test_hostile_miss_exits_1` — any attack.
- `test_hostile_limit_exits_2` — refused.
- `test_limit_out_of_range_exits_2` — per suite.
- `test_unknown_suite_exits_2` — refused with the names.
- `test_model_not_in_price_list_exits_2` — before any call.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob eval run`.

### Task 7 — Add `ijob eval report`

**Files:**
- Create `packages/ijob-app/src/ijob_app/eval_report.py` — `comparable_runs`, `jury_metrics`.
- Modify `packages/ijob-app/src/ijob_app/eval_view.py` — `report_panel`, `failures_panel`.
- Modify `packages/ijob-app/src/ijob_app/cli_eval.py` — the `report` command.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `eval_report`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_report.py`, `packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py`.

**Change:** Comparable runs as spec section 8 defines them, jury metrics as section 7 defines them, and total spend of `total_cost_usd`.

**Proof:**
- `test_comparable_excludes_incomplete_limited_and_other_versions` — each is left out and counted.
- `test_suite_without_labels_compares_without_a_set` — `hostile` rows appear.
- `test_agreement_example` — 7 of 10 gives 0.7; no pair gives `None`.
- `test_overruled_examples` — each spec row, a tie not counted.
- `test_spot_check_example` — 9 of 10 gives 0.9; none gives `None`.
- `test_report_empty_store_exits_0` — it says no run exists.
- `test_report_run_detail` — any status, with cost and speed.
- `test_report_without_a_set_lists_label_free_suites` — no jury, rules or profile: it shows `hostile` and the agent suites, and says labels need setup.
- `test_report_unknown_run_exits_2` — refused.
- `test_report_failures_lists_answers` — third-party text is drawn literally.
- `test_report_shows_the_noise_note` — a gap under the ±.
- `test_report_shows_total_spend` — spend of the total.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_report.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob eval report`.

---

## Risks

- `_show_indicator` is a private argument, held by a test and by the `<5` version bound (spec section 3).
- Riskiest step: Task 2, because of DeepEval's warnings under `filterwarnings = ["error"]`; subclasses stay inside the block.
- `execute` spends nothing: all model calls here use fakes.

## Proof

- The five gate commands in `CLAUDE.md` exit 0.
- `uv run --locked ijob eval run --help` and `uv run --locked ijob eval report --help` list the flags of the spec.
