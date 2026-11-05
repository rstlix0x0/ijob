---
status: done
created: 2026-11-04
depends-on: [05]
built-on: 671aa742d4fc
---

# Tune settings Implementation Plan

**Goal:** Read every tuning choice from the user's configuration.

**Architecture:** `Task.TUNER` joins the enum, so `[models].tuner` parses like other tasks. A frozen `TuneSettings` holds candidates, baseline, models and the run cap. ijob ships no default model: these are template comments only.

**Tech Stack:** Python 3.14, TOML.

**Content authority:** spec section 13, the settings rows. Plans 10 and 11 hold the rows that a command refuses.

---

## Files that change

```
packages/ijob-ai/src/ijob_ai/llm/settings.py               — modify: Task.TUNER, TuneSettings
packages/ijob-app/src/ijob_app/llm_config.py               — modify: read_tune_settings, tuner check
packages/ijob-app/src/ijob_app/data/config_template.toml   — modify: tuner and [tune] comments
packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py  — modify: the enum and defaults
packages/ijob-app/tests/ijob_app_tests/test_llm_config.py  — modify: the table
```

## Order of work

### Task 1 — Read the tuner and the tune table

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/llm/settings.py` — `Task.TUNER = "tuner"`; `TuneSettings(candidates, baseline, models, max_cost_usd=2)`, with `baseline` `None` when absent and empty tuples for the lists.
- Modify `packages/ijob-app/src/ijob_app/llm_config.py` — `read_tune_settings`, `_TUNE_KEYS`, `_model_list`; `_models` refuses a `tuner` not on `claude-sdk`.
- Modify `packages/ijob-app/src/ijob_app/data/config_template.toml` — comment lines for `tuner`, `[tune]` and its six candidates, as in spec section 13.
- Test `packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py`, `packages/ijob-app/tests/ijob_app_tests/test_llm_config.py`.

**Change:** A `tuner` on another provider raises `InvalidValueError`, because its prompts carry rules and postings. Duplicates in a list are read once, in order. Unknown keys get the existing error. `max_cost_usd` uses `_cost`, so it is above 0 and at most 100. Each list entry goes through `parse_model_ref`.

**Proof:**
- `test_tuner_on_claude_sdk_reads` — `[models] tuner` reads as a `claude-sdk` reference.
- `test_tuner_on_openrouter_is_refused` — `InvalidValueError` naming `models.tuner`.
- `test_tuner_absent_raises_missing_model` — `model_for(Task.TUNER)` raises `MissingModelError`.
- `test_no_tune_table_gives_empty_settings` — no `baseline`, empty lists, cap 2.
- `test_empty_candidates_are_allowed` — `candidates = []` reads.
- `test_empty_models_are_allowed` — `models = []` reads.
- `test_duplicates_are_read_once` — a repeated entry appears once, order kept.
- `test_tune_unknown_key_is_refused` — another key raises the existing error.
- `test_tune_cost_bounds` — 0, 101 and `"x"` raise; 2 and 100 read.
- `test_bad_model_reference_is_refused` — a reference with no provider raises, naming the key.
- `test_template_stays_all_comments` — the template still parses to no settings.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py packages/ijob-app/tests/ijob_app_tests/test_llm_config.py` passes and the Definition of Done is green. Commit `feat(ijob-app): read the tuning settings`.

---

## Risks

- Model names in the template go stale. They are comments, and ijob calls none of them unless the user sets them.

## Proof

- `uv run --locked pytest packages/ijob-ai packages/ijob-app/tests/ijob_app_tests/test_llm_config.py` passes.
- The five commands in `CLAUDE.md` exit 0.

## Review findings

One review, scoped gate green (`pytest` on `test_settings.py` and `test_llm_config.py`: 91 passed). No blocking finding. Left open:

- `llm_config.py:181` risk: no test catches removal of the `baseline` type guard; without it `baseline = 1` raises `AttributeError`.
- `test_llm_config.py:318` risk: no test reads the `[tune]` template block, so deleting it or mistyping a candidate stays green.
- `settings.py:1`, `llm_config.py:1` nit: module docstrings omit tuning.
- `config_template.toml:64` nit: "written on one line each" reads as a reader rule, though a multi-line list parses.
- `test_llm_config.py:516` nit: `match=r"tune\."` does not prove each case names its own key.
- `test_llm_config.py:521` nit: the wrong-type test covers `candidates` only, not `models`.

## Deviations

- 2026-11-05: `test_tune_cost_bounds` is two tests, `test_tune_cost_out_of_bounds_is_refused` and `test_tune_cost_bounds_are_read`, with all five values covered. `test_bad_model_reference_is_refused` is parametrized over `baseline`, `candidates` and `models`.
- 2026-11-05: the template writes `candidates` on one line, because `test_template_new_settings_are_valid_once_uncommented` keeps only lines holding `=`.

## Gate

Five commands in `CLAUDE.md` on the tree holding plans 06 to 10, 2026-11-05: format, check and mypy (300 files) clean; `4870 passed in 323.45s`. `test_cli_terminal.py` was not in plan 10's file list, but `test_every_command_is_checked_here` needs a check for each command, so it gained `test_tune_screen_without_a_baseline_gives_the_setting`.

## Risk fixes

2026-11-05, on the user's word to fix all open risks, for plans 05 and 06:

- `README.md:91` now says `.env.example` lists the provider key, and names `IJOB_HOME` and `OTEL_SDK_DISABLED` in prose.
- `settings.py:81` `PhoenixSettings` validates at construction, through `check_local_endpoint`, which `llm_config.py` now shares: `test_phoenix_refuses_non_local_endpoints_at_construction`.
- `test_setup.py:161`: `test_export_failures_print_nothing_on_stderr` covers all three loggers, each caught by a mutant.
- `test_llm_config.py:327`: `test_template_tracing_and_phoenix_lines_parse_through_the_reader`.
- `test_repo_files.py:35`: `test_localhost_check_catches_a_widened_compose_file` catches a second `0.0.0.0` port and `network_mode: host`.
- `llm_config.py:181`: `test_tune_baseline_must_be_a_string`.
- `test_llm_config.py:318`: `test_template_tune_block_parses_once_uncommented`.
