---
status: done
created: 2026-11-03
depends-on: [01]
built-on: 8947dcb8c009
---

# Jury labels Implementation Plan

**Goal:** Produce labels from the jury, leaving the user only the disagreements.

**Architecture:** The rules are pure functions in `ijob-core`. `eval_ops.py` runs the jury through one `run_items` call, storing each vote in the worker. `cli_eval.py` holds the `ijob eval` group, and `eval_view.py` draws the canvas screens with the helpers in `view.py`.

**Tech Stack:** Python 3.14, Typer, Rich, DSPy, SQLite.

**Content authority:** spec sections 6, 7 and 10, section 8's Paid runs and Estimate, and the Screens section.

---

## Files that change

```
packages/ijob-core/src/ijob_core/eval_rules.py     — create: sample, limit, label and spot check rules
packages/ijob-app/src/ijob_app/eval_estimate.py    — create: price list and cost estimates
packages/ijob-app/src/ijob_app/eval_ops.py         — create: label and review operations
packages/ijob-app/src/ijob_app/eval_view.py        — create: drawing for eval screens
packages/ijob-app/src/ijob_app/cli_eval.py         — create: the ijob eval group
packages/ijob-app/src/ijob_app/service.py          — modify: eval methods
packages/ijob-app/src/ijob_app/cli.py              — modify: register the group
```

## Order of work

### Task 1 — Write the label rules

**Files:**
- Create `packages/ijob-core/src/ijob_core/eval_rules.py` — `draw_cases`, `limit_split`, `judge_label`, `score_label`, `spot_order`.
- Test `packages/ijob-core/tests/ijob_core_tests/test_eval_rules.py`.

**Change:** Pure functions for spec section 6's draw, top-up and `--limit` split, its label table, and section 7's spot check order.

**Proof:**
- `test_draw_is_stable_per_hashes` — same hashes, same sample; other hashes, another.
- `test_draw_counts` — 40, 40 and 20 from large pools.
- `test_draw_tops_up_from_unjudged` — 12 kept gives kept 12, unjudged 48.
- `test_limit_split_table` — every N in spec section 6.
- `test_limit_short_bucket_uses_unjudged` — a bucket short of its share takes the rest from unjudged.
- `test_judge_label_table` — every judge row in spec section 6.
- `test_score_label_table` — every scorer row, spread 20 agreeing and 21 not.
- `test_spot_order_holds_as_labels_grow` — adding labels keeps the earlier relative order.
- `test_spot_order_prefers_five_per_task` — 5 per task when that many exist.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_eval_rules.py` passes and the Definition of Done is green. Commit `feat(ijob-core): add the eval label rules`.

### Task 2 — Estimate what a run costs

**Files:**
- Create `packages/ijob-app/src/ijob_app/eval_estimate.py` — `read_prices(client)`, `estimate(calls, prices)`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_estimate.py`.

**Change:** Applies spec section 8's token table to OpenRouter's public price list, read through `PoliteClient`. A claude-sdk model reads `subscription`. An unreadable list gives `unknown`. A model absent from a readable list raises `InvalidValueError` naming it.

**Proof:**
- `test_estimate_checked_example` — 100 postings at 6,132 characters and $2/$10 give about $1.81 per juror.
- `test_claude_sdk_reads_subscription` — no price lookup.
- `test_unreadable_list_reads_unknown` — a failed fetch gives `unknown` and raises nothing.
- `test_model_missing_from_list_is_refused` — it raises, naming the model.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_estimate.py` passes and the Definition of Done is green. Commit `feat(ijob-app): estimate eval costs`.

### Task 3 — Run the jury

**Files:**
- Create `packages/ijob-app/src/ijob_app/eval_ops.py` — `prepare_label_run`, `label_operation`, `derive_labels`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_ops.py`.

**Change:** Follows spec section 6 Voting: finds or creates the set, skips changed postings, requests only missing votes in one `run_items` call under `EvalSpend`, with a `jury` run row whose spend updates after each item through `on_outcome`, stores each vote in the worker, and derives labels after the run. Provider failures stop that juror alone; a stopped juror's later items make no call. The run never builds a profile.

**Proof:**
- `test_needs_rules_and_profile` — each missing one refuses, naming its fix, with no call.
- `test_below_ten_cases_refuses` — too few postings refuse.
- `test_changed_rules_cv_or_jury_reports_what_changed` — the preparation names which changed and makes a new set.
- `test_rerun_requests_only_missing_votes` — a second run calls only the gaps.
- `test_changed_posting_gets_no_new_vote` — it is skipped and counted.
- `test_vote_is_stored_when_the_run_is_cancelled` — finished votes survive cancellation.
- `test_provider_failure_stops_one_juror` — the failed juror makes no further call; others finish; the report names it.
- `test_unusable_answer_leaves_no_vote` — a re-run retries it.
- `test_cost_unknown_stops_the_run` — remaining items do not start.
- `test_cap_stops_new_items` — votes stand and the report says cap.
- `test_subscription_cost_leaves_the_cap_alone` — a costly Opus fake does not stop the run.
- `test_labels_derived_after_run` — agreed cases get labels; split cases do not.
- `test_jury_run_row_keeps_spend_on_cancel` — a cancelled run's row holds what it spent.
- `test_jury_spend_reduces_the_next_cap` — a later command's cap reflects it.
- `test_run_does_not_build_a_profile` — a stale profile refuses, with no profile call.
- `test_later_task_version_leaves_votes_standing` — votes and labels stay; only gaps are requested.
- `test_posting_without_description_stays_in_its_pool` — also one marked low confidence.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_ops.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run the jury`.

### Task 4 — Add `ijob eval label`

**Files:**
- Create `packages/ijob-app/src/ijob_app/eval_view.py` — `jury_panel`, `jury_summary`.
- Create `packages/ijob-app/src/ijob_app/cli_eval.py` — `eval_app`, the `label` command.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `eval_prepare`, `eval_label`.
- Modify `packages/ijob-app/src/ijob_app/cli.py` — register the group.
- Test `packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py`, `packages/ijob-app/tests/ijob_app_tests/test_eval_view.py`.

**Change:** The label screen from the spec's Screens section: jurors, estimate, cap, the data notice of spec section 10, the question, live progress and the summary. Plain lines in a pipe.

**Proof:**
- `test_label_shows_estimate_and_data_notice` — the panel names jurors, cost, cap, and who receives the data.
- `test_label_declined_makes_no_call` — answering no spends nothing.
- `test_label_yes_skips_the_question` — `--yes` runs at once.
- `test_label_in_a_pipe_needs_yes` — exit 1 before any call.
- `test_label_limit_out_of_range_exits_2` — 0 and 101.
- `test_label_absent_jury_exits_2` — it names the fix.
- `test_label_provider_stop_exits_1_cap_exits_0` — the two stops' exit statuses.
- `test_label_cost_unknown_exits_1` — the run stops.
- `test_label_claude_only_jury_does_not_ask` — no question, and no data notice.
- `test_jury_panel_draws_every_row` — jurors, cost, estimate and cap, in `test_eval_view.py`.
- `test_jury_summary_counts` — agreed and to review per task, in `test_eval_view.py`.
- `test_label_no_money_left_exits_1` — the key is named, with no call.
- `test_label_changed_set_asks_first` — it says what changed and asks.
- `test_label_pipe_output_is_plain` — no box characters.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob eval label`.

### Task 5 — Add `ijob eval review`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_ops.py` — `review_items`, `record_review`.
- Modify `packages/ijob-app/src/ijob_app/eval_view.py` — `review_panel`.
- Modify `packages/ijob-app/src/ijob_app/cli_eval.py` — the `review` command.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `eval_review_items`, `eval_record_review`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_ops.py`, `packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py`, `packages/ijob-app/tests/ijob_app_tests/test_eval_view.py`.

**Change:** `review_items` first calls `derive_labels`, so votes kept through a stop reach review. Then the input table of spec section 7, then the spot check. Votes are shown as third-party text, drawn literally. `o` draws the whole posting with `posting_body`. With no rules, profile or jury to identify a set, it exits 1 with the fix.

**Proof:**
- `test_review_judge_keys` — `k`, `r`, and a rejection's rule from the rejecting votes.
- `test_review_score_input` — a number is stored; Enter takes `median_low`.
- `test_review_invalid_input_asks_again` — `101` and `abc`.
- `test_review_skip_stays_unlabelled` — `s` shows it next time.
- `test_review_quit_keeps_answers` — `q` and end of input exit 0 with answers stored.
- `test_review_open_shows_the_posting` — `o` shows it, then asks again.
- `test_review_in_a_pipe_exits_1` — "needs a terminal".
- `test_review_nothing_to_do_exits_0` — a done line.
- `test_review_skips_changed_and_removed_postings` — both skipped and counted.
- `test_review_missing_vote_is_offered` — a case with fewer votes than jurors appears; one with no vote does not.
- `test_spot_check_stops_at_ten` — 5 per task, and the mark persists.
- `test_spot_check_resumes_after_quit` — `q` then review continues where it stopped.
- `test_spot_check_different_answer_is_user_origin` — origin `user`, mark set.
- `test_spot_check_same_answer_stays_agreed` — origin `agreed`, mark set.
- `test_spot_check_with_few_labels_offers_the_rest_later` — fewer than 10 now, the rest once more exist.
- `test_review_derives_labels_first` — votes from a stopped run reach review.
- `test_review_enter_on_a_judge_case_asks_again` — no answer is stored.
- `test_review_keys_are_per_task` — a number on a judge case, and `k` on a scorer case, ask again.
- `test_review_without_a_set_exits_1` — no jury, rules or profile names its fix.
- `test_review_panel_draws_votes_literally` — markup in a vote's reason is not read, in `test_eval_view.py`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob eval review`.

---

## Risks

- The worker writes to the store from concurrent tasks on one event loop, as `judge_operation` does today.
- Two concurrent `eval label` runs waste money; this is accepted in spec section 12.
- `execute` spends nothing: all model calls here use fakes.

## Proof

- The five gate commands in `CLAUDE.md` exit 0.
- `uv run --locked ijob eval label --help` and `uv run --locked ijob eval review --help` list the flags of the spec.

## Review findings

Fixed (`uv run --locked pytest .../test_cli_eval.py .../test_cli_terminal.py .../test_view.py` → 113 passed):
- `cli_eval.py:297`, `view.py:206` provider error text could print terminal escape codes or a made-up fix command. `fix_line` now cleans, and the juror stop line does not split a fix out of provider text.
- `cli_eval.py:227` the question went to stdout in a pipe; it now goes to stderr.
- `cli_eval.py:471` the skipped count was dropped when nothing was left to review.

Open:
- `cli_output.py` other `say_error` and `say_note` callers still split `; run:` out of their text. Their messages are ijob's own today.
- `cli_eval.py:263` the terminal Ctrl-C path has no test, and its exit status is undocumented.
- `eval_ops.py:999` no test answers `r` on an agreed rejection.
- `eval_ops.py:348` a limit that covers every case still marks the run limited.
- `eval_ops.py:507` a run can pass the cap by its calls in flight, up to 4 at the default concurrency; a cancelled in-flight call is unmetered.
- `eval_ops.py:520` provider failures and stopped-juror items count as `DONE` in `run_items`, so `operation.done` overcounts. `LabelReport.stored` and `failed` hold the true numbers.
- `eval_ops.py:531` no test covers `ProviderUnavailableError` stopping one juror; `eval_ops.py:538` none observes spend per item; `eval_ops.py:716` none covers a split case with a user label.
- `test_eval_ops.py:103` every test runs at concurrency 1, so stopped-juror and cap behaviour is unproven at the default of 4.
- `eval_rules.py:122` a short `unjudged` pool is not topped up, so `--limit 1` can select nothing.
- `eval_rules.py:195` no task check: scorer votes give `Agreed(keep=False)`, and judge votes crash `score_label`.
- `eval_rules.py:57`, `eval_estimate.py:62` `Agreed`, `Split`, `Price` and `PlannedCall` validate nothing at construction.
- `eval_estimate.py:142` a non-list or empty `data` reads as an empty price list, not `None`; `eval_estimate.py:167` a listed router model reads as "not in the list"; `test_eval_estimate.py:72` only HTTP 500 tests a failed fetch.
- `eval_estimate.py:138` whether OpenRouter's robots.txt allows the price list is unchecked here; `PoliteClient` reads it at run time.
- About 30 nits, in the session reports: naming, unused test helpers, docstring wording, duplicated test scaffolds.

## Deviations

- 2026-11-03: the build did not commit per task. CLAUDE.md says commit only when the user asks, and the run had no such instruction.
- 2026-11-03: Task 3 changed `eval_records.py`, outside its file list. Spec section 6's "label reject, no rule" could not be stored while `EvalLabel` refused a reject with no rule, so a label may now do so; a vote still needs a rule. It also added `EvalRecords.sets()`, since `prepare_label_run` must compare against earlier sets.
- 2026-11-03: Task 3 added `tests/ijob_app_tests/eval_fakes.py`, shared by the eval tests.
- 2026-11-03: Task 4 added `planned_calls`, `total_cost` and `cost_by_model` to `eval_estimate.py`, and terminal tests to `test_cli_terminal.py`, which `test_every_command_is_checked_here` requires.
- 2026-11-03: Task 4 imports eval modules inside `service.py` methods, since `test_search_without_rules_imports_no_llm_module` fails on a module-level import. The panel and data notice cover only jurors with votes left to cast.
- 2026-11-03: Task 5 added `EvalRecords.mark_spot_checked`, since no existing write sets the mark and keeps origin `agreed`. `eval_app` gained a `now` argument.
- 2026-11-03: Task 5 reads a missing `eval.jury` as exit 1 in review, per spec section 7; `eval label` stays at exit 2. Spec section 15 left open the spot check title suffix, the fewer-votes suffix, rule lines, juror names and the wrong-key line, and these were decided in code.
- 2026-11-03: Task 2's coder fetched OpenRouter's public price list once with curl to check its shape. It needed no key and cost nothing. No test uses the network, and later coders were told to run no such probe.
- 2026-11-03: Tasks 4 and 5 share files, so they were reviewed once, together.
