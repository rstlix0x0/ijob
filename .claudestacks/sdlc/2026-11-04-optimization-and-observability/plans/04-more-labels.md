---
status: done
created: 2026-11-04
depends-on: [03]
built-on: 671aa742d4fc
---

# More labels Implementation Plan

**Goal:** Grow the current label set by up to 100 postings at a time.

**Architecture:** The pure rules go in `eval_rules.py`: the share of a draw, the draw itself and the spot count. `prepare_label_run` gains `add`, draws from the pools less the set's cases and appends cases after the last position. Votes are then asked for as today. For labelled suites, `comparable_runs` also compares the case count, read from each run's stored `cases` metric (`eval_suites.py:970,1075`).

**Tech Stack:** Python 3.14, SQLite, Typer.

**Content authority:** spec section 6.

---

## Files that change

```
packages/ijob-core/src/ijob_core/eval_rules.py             — modify: add_split, draw_more, spot_per_task, spot_order
packages/ijob-app/src/ijob_app/eval_ops.py                 — modify: prepare_label_run add; review passes the count
packages/ijob-app/src/ijob_app/eval_report.py              — modify: comparable_runs compares case counts
packages/ijob-app/src/ijob_app/service.py                  — modify: eval_prepare add
packages/ijob-app/src/ijob_app/cli_eval.py                 — modify: label --add
packages/ijob-core/tests/ijob_core_tests/test_eval_rules.py — modify: the rules
packages/ijob-app/tests/ijob_app_tests/test_eval_ops.py    — modify: adding and refusing
packages/ijob-app/tests/ijob_app_tests/test_eval_report.py — modify: case counts
packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py    — modify: the option
```

## Order of work

### Task 1 — Write the draw and spot rules

**Files:**
- Modify `packages/ijob-core/src/ijob_core/eval_rules.py`.
- Test `packages/ijob-core/tests/ijob_core_tests/test_eval_rules.py`.

**Change:**
- `add_split(n, available)` — kept `floor(0.8n + 0.5)`, rejected `floor(0.1n + 0.5)`, unjudged the rest. A short pool gives all it has, and the remainder goes to unjudged, then kept, then rejected, as `limit_split` does. Outside 1 to 100 it raises `InvalidValueError`, with the same range text as `limit_split`.
- `draw_more(rules_sha256, profile_sha256, case_count, pools, n)` — samples the shares from pools the caller has already reduced, seeded by both hashes and `case_count`.
- `spot_per_task(cases)` — `max(5, cases // 20)`.
- `spot_order` — takes the per-task count, and its total becomes twice that. It replaces `_SPOT_TOTAL` and `_SPOT_PER_TASK`.

**Proof:**
- `test_add_split_rows` — each row of the section 6 table: 100 real gives 80, 10, 10; 5 gives 4, 1, 0; 2 gives 2, 0, 0; 1 gives 1, 0, 0; 100 of 30, 50, 500 gives 30, 10, 60; 100 of 30, 50, 5 gives 30, 50, 5.
- `test_add_split_range` — 0 and 101 raise `InvalidValueError`.
- `test_draw_more_is_seeded` — the same hashes and count give the same draw; another count gives another.
- `test_spot_per_task_counts` — 100 and 119 give 5, 120 gives 6, 200 gives 10.
- `test_spot_order_takes_more_for_a_larger_set` — with 10 per task and 5 already checked, 5 more per task are offered.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_eval_rules.py` passes and the Definition of Done is green. Commit `feat(ijob-core): add rules for growing a label set`.

### Task 2 — Add cases to the set

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_ops.py` — `prepare_label_run(add=)`, `LabelPlan.added`, `review_items` passes `spot_per_task` of the set's case count.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `eval_prepare(add=)`.
- Modify `packages/ijob-app/src/ijob_app/cli_eval.py` — `label --add`, with `min=1, max=100`; `_plan_lines` says how many were added.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_ops.py`, `test_cli_eval.py`.

**Change:** With `add`: refuse `--add` with `--limit`; refuse when no set is current, with the plain `eval label` advice; refuse while `items` for the existing cases is not empty, pointing at plain `ijob eval label`. A case skipped because its posting changed cannot be voted on, so it does not hold the refusal. Otherwise draw from the pools less the set's cases and append after the last position, with bucket and digest. Nothing to draw raises `TooFewCasesError`. A short draw says how many it added. Without `add` nothing changes.

**Proof:**
- `test_add_appends_after_the_last_position` — 5 more cases take the next positions, and the old ones are untouched.
- `test_add_asks_only_for_the_new_votes` — items hold only the new cases.
- `test_add_excludes_existing_cases` — no drawn posting is already in the set.
- `test_add_can_repeat` — a second `--add` after voting draws another group.
- `test_add_is_refused_while_votes_are_missing` — the error names plain `ijob eval label`, and no case is added.
- `test_add_ignores_a_case_whose_posting_changed` — such a case does not hold the refusal.
- `test_add_with_limit_is_refused` — `InvalidValueError`.
- `test_add_without_a_set_is_refused` — the error gives the plain `eval label` advice.
- `test_add_with_nothing_to_draw_raises` — `TooFewCasesError`.
- `test_short_draw_says_how_many` — 30 available of 100 reports 30.
- `test_cli_add_range` — `--add 0` and `--add 101` exit with the range error.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_ops.py packages/ijob-app/tests/ijob_app_tests/test_cli_eval.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add eval label --add`.

### Task 3 — Keep different case counts apart

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_report.py` — `comparable_runs`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_report.py`.

**Change:** For a suite in `LABELLED_SUITES` (`eval_report.py:47`), after the version filter, `comparable_runs` keeps only runs whose stored `cases` metric equals the newest run's. Any other suite compares as before, the agent suites included, though they also store `cases` (`eval_agent_suites.py:834,1067`). Left-out runs appear in `other_ids`, so `eval report` lists them under `not compared`.

**Proof:**
- `test_runs_of_different_case_counts_are_not_compared` — a 100-case and a 200-case `judge` run give one line, and the older id is in `other_ids`.
- `test_unlabelled_suites_still_compare` — two `hostile` runs and two `source-agent` runs of different `cases` all stay.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_report.py` passes and the Definition of Done is green. Commit `fix(ijob-app): compare only runs of the same size`.

---

## Risks

- Runs 11 and 12 fall under `not compared` as soon as a 200-case run exists. The spec intends this.
- `spot_order`'s callers change, and the old fixed five stays right only below 120 cases.

## Proof

- `uv run --locked pytest packages/ijob-core packages/ijob-app` passes.
- The five commands in `CLAUDE.md` exit 0.

## Review findings

- fixed — `eval_ops.py:545` bug: `--add` stored its cases during prepare, so a declined or budget-refused add grew the set for good and blocked every later `--add`. Cases now wait in `LabelPlan.pending` and `label_operation` stores them as the run starts. Failing first: `test_cli_budget_refused_add_leaves_the_set_alone` gave `assert 35 == 30`. After: `uv run --locked pytest` on `test_eval_ops.py` and `test_cli_eval.py`: `177 passed`.
- partly fixed — `cli_eval.py:215`: the `added` line is now asserted by `test_cli_accepted_add_stores_the_cases_and_asks_for_the_new_votes`. Open: the terminal note at `cli_eval.py:250` has no test.
- open, risk — `eval_ops.py:173`: the `adding=` message is unguarded; `test_add_with_nothing_to_draw_raises` checks only the type.
- open, risk — `eval_report.py:232`: the case-count check does nothing for `reasons`, which stores no `cases` metric, yet the docstring says labelled suites stand apart by size.
- open, nit — `eval_report.py:72`: `Comparison.others` omits the case count as a reason.
- open, nit — `eval_ops.py:522`: an add runs `_missing_votes` over the old cases twice.
- open, nit — `test_cli_eval.py:376`: `test_cli_add_range` asserts exit 2 only.
- open, nit — `eval_rules.py:218`: `_require_count` guards `--add` with `_LIMIT_MIN` and `_LIMIT_MAX`.
- open, nit — `eval_view.py:619` and `cli.py:145`, outside the file list: still explain left-out runs as `--limit` runs, and document `eval label` without `--add`.

## Deviations

- 2026-11-05 — The append moved from prepare to the run's start, so a declined or refused add stores nothing. `LabelPlan.pending` is new, and the `_add` test helper stores it after planning, standing in for the run's start.
- 2026-11-05 — Task 3 applies the case-count rule to all of `LABELLED_SUITES`, but only judge and scorer runs store `cases`, so `reasons` runs compare as before. Left as built; see the open `eval_report.py:232` finding.
- 2026-11-05 — A terminal `note: added N` line was added beyond the plan, to carry "says how many" on a terminal.
- 2026-11-05 — Tests beyond the plan: `test_add_out_of_range_is_refused`, `test_spot_check_grows_with_the_set`, `test_cli_add_with_limit_exits_2`, `test_cli_add_without_a_set_points_at_plain_label`, and three CLI tests for the declined, refused and accepted paths.

## Risk fixes

2026-11-05, on the user's word to fix all open risks:

- `cli_eval.py:250`: `test_cli_add_on_a_terminal_notes_the_added_count`.
- `eval_ops.py:173`: `test_add_with_nothing_to_draw_raises` now asserts the message.
- `eval_report.py:232`: `reasons` is a labelled suite, so its runs now store `cases`. `test_reasons_runs_of_different_case_counts_are_not_compared`, `test_reasons_runs_stored_without_cases_still_compare`. Once a new `reasons` run is newest, older rows stand apart; `eval_view.py:619` still explains that as a `--limit` run (open nit).
