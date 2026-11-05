---
status: approved
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
