---
status: done
created: 2026-11-04
depends-on: [01, 02, 08]
built-on: 671aa742d4fc
---

# Tuning engine Implementation Plan

**Goal:** Tune one task on one model with one optimizer under the rails.

**Architecture:** A new `ijob_ai/tuning/` package. `metrics.py` holds the scores. `optimizers.py` builds each optimizer with the settings of spec section 8. `engine.py` runs `compile` under `asyncio.to_thread`, keeps the output quiet, always sets the stop flag and judges the run by the guard's state, never by what `compile` returned. It knows nothing about the store or the grid, and `ijob_ai/llm/` imports nothing from it.

**Tech Stack:** Python 3.14, DSPy 3.4.1, gepa 0.1.4, optuna 5.

**Content authority:** spec section 3 (Stop, Run result), section 8 (Optimizer table, Metric, Output, No change) and section 14 (optuna).

---

## Files that change

```
packages/ijob-ai/pyproject.toml                                  — modify: optuna>=5,<6
uv.lock                                                          — modify: optuna
packages/ijob-ai/src/ijob_ai/tuning/__init__.py                  — create: package marker
packages/ijob-ai/src/ijob_ai/tuning/metrics.py                   — create: scores for each optimizer
packages/ijob-ai/src/ijob_ai/tuning/optimizers.py                — create: Optimizer, build_optimizer
packages/ijob-ai/src/ijob_ai/tuning/engine.py                    — create: TuneRequest, TuneOutcome, tune_program
packages/ijob-ai/tests/ijob_ai_tests/tuning/__init__.py          — create: package marker
packages/ijob-ai/tests/ijob_ai_tests/tuning/test_metrics.py      — create: the examples
packages/ijob-ai/tests/ijob_ai_tests/tuning/test_optimizers.py   — create: settings and demos
packages/ijob-ai/tests/ijob_ai_tests/tuning/test_engine.py       — create: runs on fake LMs
packages/ijob-ai/tests/ijob_ai_tests/test_package.py             — modify: import guards
```

## Order of work

### Task 1 — Score an answer

**Files:**
- Create `packages/ijob-ai/src/ijob_ai/tuning/__init__.py`, `metrics.py`.
- Create `packages/ijob-ai/tests/ijob_ai_tests/tuning/__init__.py`, `test_metrics.py`.

**Change:** `judge_score` is 1 when keep matches the label, else 0, and reads `keep` only. `scorer_score` is `1 - abs(answer - label) / 100`. `gepa_metric(task)` gives a five-argument function returning `Prediction(score, feedback)`, and `float_metric(task)` gives a function returning a float, since a `Prediction` is always truthy to Bootstrap (`bootstrap.py:206-209`). `demo_threshold(task)` is 1.0 for the judge and 0.8 for the scorer.

**Proof:**
- `test_judge_score_examples` — a match gives 1, a mismatch gives 0.
- `test_judge_score_reads_keep_only` — a right keep with an odd rule gives 1.
- `test_scorer_score_examples` — 80 against 40 gives 0.6; 100 against 0 gives 0.
- `test_gepa_metric_returns_score_and_feedback` — five arguments, with a `Prediction` holding both.
- `test_float_metric_returns_a_float` — a plain float for both tasks.
- `test_demo_thresholds` — 1.0 and 0.8.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tuning/test_metrics.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): add the tuning metrics`.

### Task 2 — Build the optimizers

**Files:**
- Modify `packages/ijob-ai/pyproject.toml`, `uv.lock` — `uv add --package ijob-ai 'optuna>=5,<6'`.
- Create `packages/ijob-ai/src/ijob_ai/tuning/optimizers.py` — `Optimizer(StrEnum)` with `gepa`, `bootstrap`, `mipro`; `build_optimizer(optimizer, task, *, tuner_lm, num_threads)`, where `tuner_lm` may be `None` for Bootstrap, and `None` with GEPA or MIPROv2 raises `ValueError`.
- Create `packages/ijob-ai/tests/ijob_ai_tests/tuning/test_optimizers.py`.
- Modify `packages/ijob-ai/tests/ijob_ai_tests/test_package.py` — add `optuna` to `HEAVY_TOP_PACKAGES`, with a test that importing `ijob_ai.tuning.metrics` loads none of it. Optuna loads only inside `build_optimizer`. `gepa` needs no guard (P6).

**Change:** The exact settings of spec section 8: GEPA `auto="light"`, reflection on the tuner LM. Bootstrap `max_bootstrapped_demos=2`, `max_labeled_demos=0`, `num_candidate_programs=6`, `max_errors=1_000_000`. MIPROv2 `auto="light"`, `minibatch=False`, proposals on the tuner LM, same demos and errors. `num_threads` is `[llm].concurrency`. `metric_threshold` comes from `demo_threshold`.

**Proof:**
- `test_bootstrap_settings` — the four values, and the threshold per task, with no tuner.
- `test_gepa_and_mipro_need_a_tuner` — `None` raises `ValueError` for each.
- `test_gepa_uses_the_tuner_for_reflection` — reflection calls reach the tuner fake.
- `test_mipro_uses_the_tuner_for_proposals` — proposal calls reach the tuner fake, with minibatch off.
- `test_threshold_keeps_a_wrong_judge_answer_out_of_demos` — after compile on a fake, no demo's keep differs from its label (P15).
- `test_threshold_keeps_a_far_scorer_answer_out_of_demos` — an answer 21 points off is not a demo, and one 20 off may be.
- `test_tuning_metrics_import_is_light` — importing the metrics module loads no optuna.
- `test_llm_layer_imports_no_tuning_code` — importing `ijob_ai.llm.railed` loads no `ijob_ai.tuning` module.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tuning/test_optimizers.py packages/ijob-ai/tests/ijob_ai_tests/test_package.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): build the three optimizers`.

### Task 3 — Run one tuning under the rails

**Files:**
- Create `packages/ijob-ai/src/ijob_ai/tuning/engine.py` — `TuneRequest`, `TuneStatus`, `TuneOutcome`, `tune_program`.
- Create `packages/ijob-ai/tests/ijob_ai_tests/tuning/test_engine.py`.

**Change:**
- `tune_program` runs one worker function under `asyncio.to_thread`, on train and val examples the caller gives. No posting outside them can reach it. The worker compiles the task's base `dspy.Predict` with the task's `RailedLM`, then makes the val pass below.
- During the worker: stdout redirected, `TQDM_DISABLE=1`, `logging.disable(logging.CRITICAL)`, all restored in `finally` (P18). Progress is the guard's `on_call`, which the caller draws on stderr.
- The adapter is `CountingJSONAdapter(ForgivingJSONAdapter)`, which counts each `AdapterParseError` its `parse` raises and raises it again. That is the only place an unreadable reply can be seen, since it fails above the rail and scores 0 uncounted (spec section 3, P14). `TuneOutcome.unusable` holds the count.
- A `finally` around the worker sets the guard's stop flag, so a cancel drains the worker without spending, and the function then awaits the worker's end (P17). The flag is set on every run, and so is no sign of an interrupted one. A cancel or Ctrl-C reaching `tune_program` sets a separate `interrupted` mark.
- Status is `incomplete`, saving no program, when the cap was reached, `interrupted` is set, a fatal error is set, over a quarter of its calls failed, or `compile` raised, with its type and message. Otherwise `complete`.
- A complete run returns instructions with `with_untrusted_statement` applied and the demos as JSON text, keeping only instructions and demos (P3). The same instructions and no demos give `no change`, with no program.
- When the compile raises nothing, reaches no cap, sets no fatal error and leaves failures at a quarter or under, and the program changed, the worker makes one pass over val at `num_threads` concurrency, with the tuned predictor, the same adapter and the float metric, giving `best_val`. It runs before the `finally`, so the flag does not refuse it. With `no change`, `best_val` is `None`. The pass's calls count like any others, so a cap reached there makes the run `incomplete`.

**Proof:**
- `test_all_three_save_a_program_that_survives_json` — on a fake LM, each optimizer's outcome reloads through JSON with its demos.
- `test_a_changed_program_ends_complete_with_best_val` — a normal compile ends `complete`, with `best_val` set, although the stop flag is set afterwards.
- `test_budget_stop_is_incomplete` — a cap reached mid-run gives `incomplete` and no program, though `compile` returned one (P1).
- `test_a_cap_reached_in_the_val_pass_is_incomplete` — a cap the compile just fits under gives `incomplete` and no program.
- `test_compile_raising_is_incomplete` — a raising compile gives `incomplete`, with type and message.
- `test_thirty_percent_failures_finish` — all three end `complete` (P14).
- `test_over_a_quarter_failed_is_incomplete` — a higher rate gives `incomplete`, with the count and first error.
- `test_all_unreadable_gives_no_change` — all three end `no change` (P14), with `unusable` above 0 and `failed` at 0.
- `test_a_removed_model_is_incomplete` — every call raising `ProviderFailedError` gives `incomplete`, not a raised error.
- `test_unknown_cost_is_fatal` — `fatal` holds `CostUnknownError`, and no program.
- `test_untrusted_sentence_is_restored` — a fake MIPROv2 rewrite (P4) comes back holding the sentence.
- `test_cancel_stops_spending` — cancelling mid-compile sets the flag, the model count stops, and the worker ends.
- `test_compile_prints_nothing` — no stray stdout, and `logging.disable` and `TQDM_DISABLE` are restored, also when `compile` raises.
- `test_best_val_is_scored_through_the_rails` — the pass's calls show in the guard's count, and `best_val` matches the fake's answers.
- `test_no_change_makes_no_val_pass` — the call count stops at the compile's.
- `test_outcome_counts_calls_failures_and_unusable` — the outcome carries all three.
- `test_cancel_is_interrupted_not_only_stopped` — a cancel gives `incomplete` through `interrupted`.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tuning/test_engine.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): tune a program under the rails`.

---

## Risks

- The spec needs best val in the row but does not say how to get it, and optimizer internals have no recorded proof, so this plan adds a paid pass. It is up to 24 calls at 2.5 per changed run: 360 more call-equivalents on 3,655 for one model, about 10%. The worst case in spec section 8 moves from $4.94 to about $5.26, under the $5.80 left. The run cap and the ledger stay the hard limits, and plan 11's estimate includes the pass.
- The riskiest step is Task 3's cancel path: a thread cannot be killed, so a call already inside the provider finishes (P17).
- `TQDM_DISABLE` and `logging.disable` are process-wide. Tuning runs are sequential, so nothing else runs under them.

## Proof

- `uv run --locked pytest packages/ijob-ai` passes.
- The five commands in `CLAUDE.md` exit 0.

## Review findings

One review over all three tasks. `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tuning -q`: 75 passed before the fix. No optimizer path escaped the rails: on fakes, guard calls equal engine plus tuner requests on all three (403, 50, 65), and a tripwire global LM got 0 hits.

Blocking, fixed:

- `railed.py:248`: a raising `on_call` threw away a paid answer and was counted nowhere. With `BrokenPipeError`, GEPA spent 397 calls and Bootstrap 85, each ending `complete, no change`. `_rail` now logs the callback's error and returns the answer. `test_a_raising_on_call_keeps_the_answer`, `test_a_raising_on_call_keeps_the_failure_counted` and `test_a_raising_progress_changes_no_result` failed first. `pytest` on `test_railed.py` and `tuning`: 99 passed.
- `test_engine.py:432`: `test_thirty_percent_failures_finish` recomputed the rule, so either status passed. It now asserts `incomplete`.

Left open:

- `optimizers.py:49` risk: `minibatch=False` does nothing under `auto`: `mipro_optimizer_v2.py:307` sets `minibatch = len(valset) > 50`. Plan 07's rule reaches that at about 128 keeps and 128 rejects. Fix: refuse a MIPROv2 val over 50 and correct the docstring.
- `engine.py:86` risk: examples with no `label` make the metric raise on every call, and the run reads `complete, no change` with clean counters after spending (GEPA 397, Bootstrap 85, MIPROv2 90 calls). Fix: check `label` in `__post_init__`.
- `engine.py:92` risk: a plain LM or another guard is accepted at run time. A plain LM made 11 unrailed calls and read `complete`. Fix: check both in `__post_init__`.
- `engine.py:309` risk: a direct `guard.stop()` mid-run gives `complete, no change`, though no caller does this today.
- `engine.py:423` risk: on a second Ctrl-C an in-flight call is cancelled before it is charged, so the budget can undercount, and the outcome is lost. Plan 11 must catch `KeyboardInterrupt` around `asyncio.run` and treat it as a stop.
- `test_package.py:81` risk: the optuna guard imports only metrics, so moving `import optuna` to the top of `optimizers.py` fails no test. `test_optimizers.py:138` risk: only Bootstrap's settings are pinned, and `auto` sets the spend. `test_mipro_uses_the_tuner_for_proposals` does not assert minibatch off.
- `engine.py:396` for plan 11: read `interrupted` first, because a cancelled MIPROv2 run reports a cap `compile_error` with `cap_reached=False`. Also shorten `compile_error` before showing or storing it, because `AdapterParseError` embeds the reply, so posting text can reach the terminal.
- `engine.py:334` nit: no `sort_keys`, so the same demos in another field order get another version.
- Nits: `engine.py:265` forwards `writelines` and `buffer`; `engine.py:345` nothing fails if `max_errors` leaves the val pass; `engine.py:367` reads `base` after compile; `metrics.py:24` positional bool; `metrics.py:105` "always truthy" holds only with no threshold; `optimizers.py:121` no `assert_never`; the cancel tests run Bootstrap only; `test_optimizers.py:185` the proposal text cannot be parsed by GEPA, so GEPA never rewrites there; `test_over_a_quarter_failed_is_incomplete` uses 2 of 7, under 30% though the plan says a higher rate.

## Deviations

- 2026-11-05: Plan claim: `test_thirty_percent_failures_finish` ends all three `complete`. The Change text and spec section 3 make over a quarter `incomplete`, and in P14 and spec section 17 "finish" means no run raises. Built the rule; the test asserts no compile error, at least one failure and `incomplete`. Measured shares: 0.303, 0.306, 0.265. No spec claim is disproved; only this plan's proof line was wrong.
- 2026-11-05: Plan claim: `TQDM_DISABLE` keeps the output quiet. tqdm reads it on first import and `import dspy` has already loaded it: a bar wrote 89 characters with it set. The variable stays, set and restored, and a thread-routed stderr drops writes from worker threads while the loop's progress still shows.
- 2026-11-05: Plan claim: `CountingJSONAdapter` counts in `parse`. An empty reply raises `AdapterParseError` without `parse` (`dspy/adapters/base.py:157-170`), so it counts in `__call__`.
- 2026-11-05: `build_optimizer` gained `task_lm`, because MIPROv2 raises without a default LM or `task_model` and runs its trials under `dspy.context(lm=self.task_model)`. `BuiltOptimizer` wraps the three compile shapes.
- 2026-11-05: Tasks 1 and 2 were written before their tests; mutation checks on the thresholds stood in for seeing them fail.
- 2026-11-05: `minibatch=False` is passed as planned, but only holds up to a val of 50 (see Review findings). Real label sets are far under that, so the spec row stands for now and no `disproves` is set.
- 2026-11-05: Tasks 1 and 2 ran before plan 08 finished, because they use no plan 08 code. Task 3 ran after plan 08 Task 2.

## Gate

Five commands in `CLAUDE.md` on the tree holding plans 06 to 10, 2026-11-05: format, check and mypy (300 files) clean; `4870 passed in 323.45s`. `test_cli_terminal.py` was not in plan 10's file list, but `test_every_command_is_checked_here` needs a check for each command, so it gained `test_tune_screen_without_a_baseline_gives_the_setting`.

## Risk fixes

2026-11-05, on the user's word to fix all open risks:

- `optimizers.py:49` a MIPROv2 val over 50 is refused before any call and reads `incomplete`: `test_mipro_refuses_a_val_over_the_limit_before_any_call`, `test_a_mipro_val_over_the_limit_is_incomplete_and_spends_nothing`, `test_mipro_keeps_minibatching_off_at_the_largest_val`. It first applies at about 128 keeps and 128 rejects; `minibatch=False` is still passed.
- `engine.py:86` `TuneRequest` refuses an example with no `label`: `test_a_request_refuses_an_example_with_no_label`.
- `engine.py:92` `TuneRequest` refuses a plain LM or a rail on another guard: `test_a_request_refuses_a_plain_lm`, `test_a_request_refuses_a_rail_on_another_guard`.
- `engine.py:309` a direct `guard.stop()` reads `incomplete`, with `interrupted` set: `test_a_direct_stop_makes_the_run_incomplete`.
- `engine.py:423` `test_two_real_ctrl_c_stop_spending_restore_output_and_end_the_worker`, with the caller's duty in the docstring and in plan 11's carry-over. A provider can still bill an in-flight call whose cost cannot be known, so that limit stays.
- `engine.py:396` `compile_error` keeps only the type for `AdapterParseError` and caps others at 200 characters, and a cancelled MIPROv2 run reads no cap error: `test_a_parse_error_keeps_only_its_type`, `test_a_long_error_message_is_cut`, `test_a_cancelled_mipro_run_reads_no_compile_error`.
- `engine.py:334` `sort_keys=True`: `test_demos_in_another_field_order_give_the_same_text`.
- `test_package.py:81`: `test_tuning_import_does_not_load_optuna`, 3 modules. `test_optimizers.py:138`: `test_gepa_settings`, `test_mipro_settings`.
