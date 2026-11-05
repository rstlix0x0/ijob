---
status: approved
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
