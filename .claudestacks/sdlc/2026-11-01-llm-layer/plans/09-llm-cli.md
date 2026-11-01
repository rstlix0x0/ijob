---
status: approved
created: 2026-11-01
depends-on: [08]
built-on: cae3bcdf3b23
---

# LLM CLI Implementation Plan

**Goal:** Expose the profile, instructions, judged search, scoring and traces through `ijob`
commands with today's exit-code split.

**Architecture:**
- New module `ijob_app.cli_llm` builds:
  - the `instructions` and `traces` Typer groups;
  - the `score` command;
  - `_llm_failures()`, the LLM error-to-exit mapping;
  - `verdict_cell` and `score_line`, the shared text helpers that use plan 08's `STATUS_TEXT`.
- `make_app` (`cli.py:546`) gains two keywords:
  - `llm_options: Callable[[IjobHome, Path], LlmOptions] | None`, which tests use to inject fake
    engines;
  - `is_interactive: Callable[[], bool] = sys.stdin.isatty`, the TTY seam.

  It registers the new groups. `cv` and `search` gain their new behaviour in `cli.py`.
- Exit codes follow spec §7.2: 2 for `InvalidValueError`; 1 for refusals and failures, including
  `provider` and `cost_unknown` stops after printing partial results; 0 when finished or capped
  (`cli.py:113-114,247-283`).
- Model text is printed with `typer.echo`, already sanitized (spec §4.1). `ijob_app.cli_llm` imports
  `llm_ops` types only, which are light.

**Tech Stack:** Python 3.14, Typer 0.27.3, pytest with `CliRunner` and plan 08's `llm_fakes.py`.

---

## Files that change

```
packages/ijob-app/src/ijob_app/cli_llm.py                     — [create] instructions/traces groups, score, _llm_failures, verdict_cell, score_line
packages/ijob-app/src/ijob_app/cli.py                         — [modify] make_app keywords and registration; cv set/profile; search verdicts and --dropped
packages/ijob-app/tests/ijob_app_tests/test_cli_llm.py        — [create]
packages/ijob-app/tests/ijob_app_tests/test_cli.py            — [modify] search and cv cases
```

## Order of work

### Task 1 — Error mapping, injection seams and the `cv` commands

**Files:**
- Create `cli_llm.py`, holding only `_llm_failures`.
- Modify `cli.py`: `make_app` keywords, `cv set` (`cli.py:421`), and the new `cv profile`.
- Test `test_cli.py`.

**Change:**
- `_llm_failures()` maps `InvalidValueError` → `error: <reason>`, exit 2, and every `LlmError` →
  `error: <message>`, exit 1.
- `make_app` gains its keywords, and the `Service` is opened with
  `llm_options(home, config_path)` or `LlmOptions(config_path=…)`.
- `cv set` stores as today. Then:
  - if `[models].cv_profile` is set, it builds the profile and prints it;
  - otherwise it prints `note: profile not built; set [models].cv_profile` and exits 0;
  - a profile error prints `error: …` and exits 1, with the PDF kept.
- `cv profile [--refresh]` prints `key: value` lines (`summary`, `years_experience`, `seniority`,
  then each list comma-joined), building the profile when absent.

**Proof:** written first, seen failing, then passing:
- `test_cv_set_without_model_notes_and_exits_0`: stdout is today's `cv set` output plus the note
  line, with exit 0.
- `test_cv_set_builds_profile`: with `[models].cv_profile` set and a fake engine, stdout contains
  `seniority: senior`, from the fake profile.
- `test_cv_set_profile_error_keeps_pdf_exit_1`: a blank-text PDF prints "CV has no text layer; OCR is
  not supported" with exit 1, and `cv show` still lists the CV.
- `test_cv_profile_no_cv_exit_1`.
- `test_cv_profile_refresh_calls_again`: the fake engine records one call more with `--refresh` than
  without.
- `test_setup_and_cv_show_import_no_llm`: a subprocess running `ijob setup` and then `ijob cv show`
  in a temp home ends with neither `dspy` nor `claude_agent_sdk` in `sys.modules`, checked by an
  `atexit` print.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes and
the CLAUDE.md gate is green. Commit `feat(cli): build and show the CV profile`.

### Task 2 — The `instructions` commands

**Files:** Modify `cli_llm.py` and `cli.py` (registration); Test `test_cli_llm.py`.

**Change:**
- `instructions set TEXT [--yes]`:
  - prints the instruction filters, the rules, and each shadow warning as
    `warning: <field>: your config's <used> is used; the instruction's <ignored> is ignored`;
  - without `--yes`: if `is_interactive()` is false, prints `error: confirmation needed; pass --yes`
    and exits 1; otherwise asks `Store these? [y/N]`, where `y` or `yes` in any case stores, and
    anything else exits 1 without storing.
- Errors: blank or too long → exit 2; nothing actionable → exit 1.
- `instructions show` prints `text:`, `filters:` and `rules:` (numbered), or `no instructions set`
  with exit 0.
- `instructions clear` prints `cleared`.

**Proof:** written first, seen failing, then passing:
- `test_instructions_set_yes_stores`: after `--yes`, `instructions show` prints the rules.
- `test_instructions_set_prompt_y_and_n`: with `is_interactive=lambda: True`, `CliRunner` input
  `Y\n` stores, and input `n\n` exits 1 with nothing stored.
- `test_instructions_set_non_tty_requires_yes`: with `is_interactive=lambda: False`, exit 1 with the
  message, and nothing stored.
- `test_instructions_set_blank_exit_2`.
- `test_instructions_set_nothing_actionable_exit_1`.
- `test_instructions_set_shows_shadow_warning`: spec §4.3's example prints the countries warning
  line.
- `test_instructions_show_empty_and_clear`: `no instructions set` with exit 0, and `cleared`.
- `test_instructions_show_imports_no_llm`: a subprocess `ijob instructions show` leaves no `dspy` in
  `sys.modules`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_llm.py` passes and
the gate is green. Commit `feat(cli): add instructions commands`.

### Task 3 — Judged `search`

**Files:** Modify `cli.py` (`search` at `cli.py:591`) and `cli_llm.py` (`verdict_cell`); Test `test_cli.py`.

**Change:**
- `search` builds `FilterLayers` with `read_filter_layers` (plan 07) and calls `Service.search`.
- With no stored rules, stdout is byte-for-byte today's, and stderr carries only today's refresh
  lines (`cli.py:368-387`).
- With rules:
  - it collects all items and prints them in filter order, as today's line plus one trailing
    tab-separated `verdict_cell`;
  - `verdict_cell` gives `✔`, `✘ <reason>`, or `· <STATUS_TEXT>`, and appends ` (no description)`
    when `no_description` is true;
  - dropped items print only with `--dropped`, and then kept and dropped are listed together;
  - one stderr summary line follows:
    `judged N, cached K, dropped M, not judged U, cost $X of $Y`, ending `; stopped: cap reached`,
    `; stopped: provider error` or `; stopped: cost unknown` when stopped;
  - tracing warnings and `warning: N spans not stored` go to stderr.
- Exit 1 after printing on `provider` or `cost_unknown`. Exit 0 otherwise.

**Proof:** written first, seen failing, then passing:
- `test_search_without_rules_output_unchanged`: the existing `test_cli.py` search cases pass
  unmodified, including their stderr expectations.
- `test_search_with_rules_marks_and_hides_dropped`: of three postings, the one the fake drops is
  absent, the two kept end in `\t✔`, and the summary reads `judged 3, cached 0, dropped 1, …`.
- `test_search_dropped_flag_lists_both`: all three print, and the dropped one ends in `\t✘ <reason>`.
- `test_search_no_description_marker`: a lazy-kind posting's cell ends with `(no description)`, also
  on the cached second run.
- `test_search_cap_reached_exit_0_with_note`: the unjudged cells read `· cap reached`, and the summary
  ends `; stopped: cap reached`.
- `test_search_provider_failure_prints_partial_exit_1`: the cells read `· provider error`, with exit
  1.
- `test_search_reason_printed_literally`: a fake reason `"[b]x[/b] **x** \x1b[31mred"` prints as
  `[b]x[/b] **x** [31mred`, with no `\x1b` byte.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli.py` passes and the
gate is green. Commit `feat(cli): show judge verdicts in search`.

### Task 4 — `score` and `traces`

**Files:** Modify `cli_llm.py`; Test `test_cli_llm.py`.

**Change:**
- `score ID...` prints in argument order:
  - a scored item as `<id>\t<score>\t<title>`, then each reason on its own line indented two spaces;
  - an unscored item as `<id>\t-\t<title>\t· <STATUS_TEXT>`, for example `score failed` or
    `cap reached`.
- It then prints one stderr summary line:
  `scored N, cached K, not scored U, cost $X of $Y`, with the same `; stopped: …` endings.
- Exit codes: an unknown id → 2; `NoCvError` → 1; a `provider` or `cost_unknown` stop → 1 after
  printing; otherwise 0.
- `traces [--limit N]`: N from 1 to 1,000, default 20. It prints
  `<trace id>\t<started ISO>\t<operation>\t<calls>\t$<cost>\t<duration ms>`.
- `traces show ID` prints the span tree, two spaces per depth level. Each call line holds the model,
  input and output tokens, `$cost` and duration. An unknown id exits 2.
- `traces prune` prints `pruned N traces, size X MB`.

**Proof:** written first, seen failing, then passing:
- `test_score_prints_in_argument_order`: `score 3 1` prints id 3's block before id 1's.
- `test_score_unscored_lines_and_summary`: a fake parse failure prints `· score failed`, and the
  summary reads `scored 1, cached 0, not scored 1, …`.
- `test_score_provider_failure_exit_1`.
- `test_score_unknown_id_exit_2`.
- `test_score_without_cv_exit_1`.
- `test_traces_list_after_search`: one row with operation `ijob search` and calls equal to the judged
  count.
- `test_traces_limit_bounds`: `--limit 0` and `--limit 1001` exit 2.
- `test_traces_show_tree_and_unknown`: the root line is followed by indented `chat <model>` lines,
  and an unknown id exits 2.
- `test_traces_prune_reports`: the output matches `pruned \d+ traces, size [\d.]+ MB`.

**Done when:** `uv run --locked pytest packages/ijob-app` passes and the gate is green. Commit
`feat(cli): add score and traces commands`.

## Risks

- **Riskiest step:** Task 3, which keeps today's output byte-identical. The unmodified existing
  tests guard it.
- **TTY handling.** `CliRunner` stdin is never a TTY, so the `is_interactive` seam is what makes the
  prompt testable.
- **Not done:** colour or Rich formatting. Output stays plain `typer.echo`.

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The gate exits 0.
