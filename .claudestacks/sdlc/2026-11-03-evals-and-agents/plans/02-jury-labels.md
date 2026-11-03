---
status: approved
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
