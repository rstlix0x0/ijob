---
status: done
created: 2026-11-04
depends-on: [12]
built-on: 671aa742d4fc
---

# Tune accept Implementation Plan

**Goal:** Let a program that passes hostile postings run in `ijob search`.

**Architecture:** `tune accept` runs the `hostile` suite with the program passed directly, for its task only, in a `limited` row under its version. Only 0 attacks and every pair answered mark it accepted. `judge_operation` and `score_operation` then read `program_in_force` and use its version for their saved answers, and `saved_scores` takes a version, so one list never mixes prompts.

**Tech Stack:** Python 3.14, DSPy, Typer, SQLite.

**Content authority:** spec section 11, and the rows of sections 9 and 15 that it uses.

---

## Files that change

```
packages/ijob-app/src/ijob_app/eval_suites.py            — modify: hostile with a program, one task
packages/ijob-app/src/ijob_app/eval_run.py               — modify: plan and estimate for that run
packages/ijob-app/src/ijob_app/tune_ops.py               — modify: prepare_accept, accept, clear
packages/ijob-app/src/ijob_app/llm_ops.py                — modify: search uses the program in force
packages/ijob-core/src/ijob_core/store.py                — modify: saved_scores version
packages/ijob-app/src/ijob_app/service.py                — modify: tune_accept, tune_clear, saved_scores
packages/ijob-app/src/ijob_app/tune_view.py              — modify: accept plan and result
packages/ijob-app/src/ijob_app/cli_tune.py               — modify: accept and clear
packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py — modify: hostile with a program
packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py  — modify: accepting
packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py   — modify: search with a program
packages/ijob-app/tests/ijob_app_tests/test_service.py   — modify: saved scores
packages/ijob-core/tests/ijob_core_tests/test_store.py   — modify: saved_scores
packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py  — modify: the commands
```

## Order of work

### Task 1 — Run hostile with a program

**Files:**
- Modify `packages/ijob-app/src/ijob_app/eval_suites.py` — `run_hostile_suite(program=None, task=None)`.
- Modify `packages/ijob-app/src/ijob_app/eval_run.py` — `_hostile_calls` and `plan_run` for one task.
- Test `packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py`.

**Change:** With a program: only that task's pairs run, every call uses the program, the row is `limited` and under the program's version, so it never becomes `eval report`'s current run. Without one nothing changes. The rules and profile are the suite's made-up ones, so the bound rule does not apply.

**Proof:**
- `test_hostile_with_a_program_runs_one_task` — only judge pairs run for a judge program.
- `test_hostile_with_a_program_uses_it` — the fake LM's prompt holds the instructions.
- `test_hostile_row_is_limited_under_the_version` — both fields.
- `test_hostile_leaves_the_report_table_unchanged` — `comparable_runs` keeps its current run.
- `test_hostile_without_a_program_is_unchanged` — both tasks, plain version.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_suites.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run hostile postings with a tuned program`.

### Task 2 — Accept or clear a program

**Files:**
- Modify `packages/ijob-app/src/ijob_app/tune_ops.py` — `prepare_accept`, `accept_program`, `AcceptReport`, `clear_program`.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `tune_prepare_accept`, `tune_accept`, `tune_clear`.
- Modify `packages/ijob-app/src/ijob_app/tune_view.py` — plan with the data line; result.
- Modify `packages/ijob-app/src/ijob_app/cli_tune.py` — `accept ID`, with `--yes`; `clear TASK`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py`, `test_cli_tune.py`.

**Change:**
- It refuses a program with no complete held-out run, or failing any condition of `program_status`, saying what to change. An unknown id is refused.
- It asks first when it can spend. For an OpenRouter model the prompt says demos and instructions may hold the user's rules and postings and go to OpenRouter and the vendor. A closed stdin without `--yes` refuses.
- It accepts only with 0 attacks and every pair of the task's data answered. The data holds five pairs per task (`evals/hostile.py:4`), and each runs clean and hostile, so this is the spec's 10 answers in 5 pairs. Otherwise nothing changes, and it points at `eval report RUN --failures`, with attacks, unanswered and the run number, exiting 1. An incomplete hostile run also accepts nothing.
- At most one program per task is accepted, and a new one replaces the old. Better scores are not required. `tune clear TASK` goes back to the base prompt, works on a malformed row by its task, and refuses an unknown task.

**Proof:**
- `test_accept_refuses_an_untested_program` — no held-out run: names `tune run`.
- `test_accept_refuses_each_out_of_force_condition` — model, bound and base version each say what to change.
- `test_accept_refuses_an_unknown_id` — `InvalidValueError`.
- `test_accept_passes_on_no_attacks_and_all_answered` — all 5 pairs of the task, 10 answers, are marked accepted.
- `test_accept_fails_on_an_attack` — nothing changes; names the run and `--failures`.
- `test_accept_fails_on_an_unanswered_pair` — nothing changes.
- `test_accept_fails_on_an_incomplete_run` — nothing changes.
- `test_attacks_on_the_other_task_do_not_count` — only the program's task is counted, since the other is not run.
- `test_openrouter_prompt_carries_the_data_line` — the line appears, and is absent for claude-sdk.
- `test_accept_closed_stdin_refuses` — nothing runs.
- `test_accept_replaces_the_old_program` — one accepted per task.
- `test_clear_returns_to_the_base_prompt` — none in force.
- `test_clear_recovers_a_malformed_row` — it clears, with no parse.
- `test_clear_unknown_task_is_refused` — the range of tasks is named.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob tune accept and clear`.

### Task 3 — Use the program in search

**Files:**
- Modify `packages/ijob-app/src/ijob_app/llm_ops.py` — `judge_operation` and `score_operation` at lines 877, 896, 1099 and 1114 take the program in force, passing it to the task and using its version for `store.verdict`, `put_verdict`, `store.score` and `put_score`.
- Modify `packages/ijob-core/src/ijob_core/store.py` — `saved_scores(profile_sha256, *, version=None)`: with a version, that version only; otherwise rows whose version has no `+`.
- Modify `packages/ijob-app/src/ijob_app/service.py` — pass the program in force from `search`, `judge`, `score` and `saved_scores`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py`, `test_service.py`, `packages/ijob-core/tests/ijob_core_tests/test_store.py`.

**Change:** `Service.saved_scores` reads the accepted scorer row first, and with none it returns the base rows before any config read, so a plain list stays store-only. With one, it reads `[models].scorer` from `self._llm.config_path` and the version from `ijob_ai.tasks.versions`, so it still loads no LLM library. Search reads the program in force for each task. Out of force, or none: the base prompt runs. A malformed stored program raises `InvalidValueError` naming the id, with the base prompt not silently used. Postings without a score under that version get scored as search reaches them. The TUI reads through the same `Service` methods.

**Proof:**
- `test_search_uses_the_judge_program_in_force` — the fake LM's prompt holds its instructions, and the verdict is stored under its version.
- `test_search_uses_the_scorer_program_in_force` — the same for scores.
- `test_search_span_shows_the_program_version` — `ijob.task_version` reads it.
- `test_saved_verdict_of_the_base_is_not_reused` — a base verdict does not answer for a program.
- `test_out_of_force_program_uses_the_base_prompt` — changed rules give the base prompt and version.
- `test_malformed_program_stops_search_with_its_id` — `InvalidValueError`.
- `test_saved_scores_with_a_version` — only that version's rows.
- `test_saved_scores_without_a_version_skip_programs` — a `scorer-1+abcd1234` row is left out, and a `scorer-1` row stays.
- `test_service_saved_scores_follow_the_program_in_force` — after `tune clear`, the base rows return.
- `test_saved_scores_without_a_program_read_no_config` — a broken config file does not fail the call.
- `test_saved_scores_with_a_program_load_no_llm_library` — no `dspy` in `sys.modules` afterwards, in a subprocess.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py packages/ijob-app/tests/ijob_app_tests/test_service.py packages/ijob-core/tests/ijob_core_tests/test_store.py` passes and the Definition of Done is green. Commit `feat(ijob-app): use the accepted program in search`.

---

## Risks

- Task 3 changes the main search path. Any bug there affects daily use, not only tuning, so with no accepted program every existing search test must pass unchanged.
- The accepted program's scores start empty, so a list may show fewer scores until search reaches those postings. The spec accepts this.

## Proof

- `uv run --locked pytest` passes.
- The five commands in `CLAUDE.md` exit 0.

## Review findings

One review: the gate holds, with no path accepting on an attack, an unanswered pair or an incomplete run, and the no-program search path is unchanged.

Blocking, fixed:

- `llm_ops.py`: with a replaced CV, `score_operation` rebuilt the profile and still ran the program. It now runs the base prompt under the base version: `test_replaced_cv_scores_with_the_base_prompt`.
- `tui/app.py`: `saved_scores` raised outside the `try`, so a config typo killed the open TUI. It now shows the error and stays open: `test_a_filter_change_with_a_broken_program_says_why_and_stays_open`.
- `cli.py`: `ijob search` gave a traceback on a malformed accepted row. It now exits 2 naming the id: `test_search_with_a_broken_program_names_it_and_exits_cleanly`.

Non-blocking, fixed: a hostile plan with a program counts as sending personal data; its estimate includes instructions and demos; it runs on the program's own model; `prepare_accept` refuses a program without the sentence before any call; a Service-level out-of-force test, which kills the mutant that survived all 318 search tests; `program_status` shows a sentence-less program as `accepted, not in use`; `tune_records` names the id on a bad row.

Left open:

- `tune list` stops on an unreadable row instead of listing it; `tune clear` gets out.
- An open TUI keeps a base `row.verdict` after a judge accept, since nothing reads saved verdicts back.
- 10 nits, in `.airsstack/cc/plugins/claudestacks/handoff/20261105-154610-9d4f/15-reviewer-plan-13.md`.

## Deviations

- 2026-11-05: `llm_ops` takes the resolved program; `Service` calls `program_in_force`.
- 2026-11-05: the replaced-CV check runs before the rebuild, because the version is chosen before the stream starts. If a rebuilt profile hashes the same as the old one, that one run still uses the base prompt.
- 2026-11-05: `run_hostile_suite`'s `task` defaults to the program's task and refuses a mismatch; `RunPlan` gained `program`.
- 2026-11-05: files outside the list: `tune_records.py`, `job_search.py`, one doc line in `eval_estimate.py`, `test_tune_records.py`, `test_job_search.py`, and a new `test_cli_tune_accept.py`, because `test_tune_ops.py` can't import the CLI helpers without a cycle.
- 2026-11-05: `Service.saved_scores` uses the runtime's settings once a model operation has run, so a listing and the next operation agree. `JobList.replace_saved_scores` keeps an open list right.
- 2026-11-05: accept asks only for an OpenRouter model, as `eval run` does. `tune clear` opens no config, so a broken config doesn't block it.
