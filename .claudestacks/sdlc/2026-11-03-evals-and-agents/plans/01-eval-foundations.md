---
status: approved
created: 2026-11-03
built-on: 8947dcb8c009
---

# Eval foundations Implementation Plan

**Goal:** Lay the shared base that every eval command stands on.

**Architecture:** DeepEval becomes a normal `ijob-ai` dependency that the test run does not load as a plugin. Config gains `[eval]` and three tasks. The store gains the eval tables behind one focused records class. A small spending module applies the caps. Nothing here adds a command.

**Tech Stack:** Python 3.14, uv, SQLite, DSPy, Typer.

**Content authority:** spec sections 3, 4 and 5, and section 6's pools.

---

## Files that change

```
pyproject.toml                                      — modify: pytest plugin flags in addopts
packages/ijob-ai/pyproject.toml                     — modify: deepeval dependency
uv.lock                                             — modify: the locked result
packages/ijob-ai/src/ijob_ai/llm/settings.py        — modify: new Task values, EvalSettings
packages/ijob-ai/src/ijob_ai/llm/registry.py        — modify: lm_for_ref
packages/ijob-app/src/ijob_app/llm_config.py        — modify: read [eval]
packages/ijob-app/src/ijob_app/data/config_template.toml — modify: document new keys
packages/ijob-app/src/ijob_app/llm_ops.py           — modify: run_items cap, provider check by reference
packages/ijob-app/src/ijob_app/eval_spend.py        — create: caps and which budget a call charges
packages/ijob-core/src/ijob_core/store.py           — modify: migration 8, evals accessor
packages/ijob-core/src/ijob_core/eval_records.py    — create: eval record types and their reads and writes
```

## Order of work

### Task 1 — Add DeepEval without loading its pytest plugins

**Files:**
- Modify `pyproject.toml` — add the plugin flags from spec 2.5 to `addopts`.
- Modify `packages/ijob-ai/pyproject.toml` — add `deepeval>=4.3.2,<5`.
- Modify `uv.lock` — relock.
- Test `packages/ijob-app/tests/ijob_app_tests/test_package.py`.

**Change:** The workspace installs deepeval, and `pytest` behaves as before. The lock moves the three packages named in spec 2.2.

**Proof:**
- `test_cli_import_leaves_deepeval_unloaded` — in a fresh interpreter, importing `ijob_app.cli` leaves `deepeval` out of `sys.modules`.

**Done when:** `uv run --locked pytest` passes and the Definition of Done is green. Commit `build(ijob-ai): add deepeval`.

### Task 2 — Read the eval settings

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/llm/settings.py` — `Task` gains `SOURCE_AGENT`, `EXPLORE_AGENT`, `EVAL_GRADER`; new frozen `EvalSettings` holding the jury, `max_cost_usd` and `total_cost_usd`.
- Modify `packages/ijob-app/src/ijob_app/llm_config.py` — `read_eval_settings(path)`.
- Modify `packages/ijob-app/src/ijob_app/data/config_template.toml` — document every new key, commented out; `jury` stays on one line, because `test_llm_config.py:248` keeps only lines holding `=` or a table header.
- Test `packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py`, `packages/ijob-app/tests/ijob_app_tests/test_llm_config.py`.

**Change:** `[eval]` follows spec section 4: only three keys, jury of 2 to 5 different references, costs above 0 and at most 100, defaults 4 and 10. An absent jury reads as none, for `eval label` to refuse.

**Proof:**
- `test_new_tasks_are_configurable` — `[models]` accepts `source_agent`, `explore_agent` and `eval_grader`.
- `test_eval_defaults` — no `[eval]` gives no jury, 4 and 10.
- `test_eval_unknown_key_is_refused` — a fourth key raises `InvalidValueError` naming it.
- `test_jury_size_and_duplicates_are_refused` — 1 and 6 entries and a duplicate raise, naming `eval.jury`.
- `test_jury_bad_reference_is_refused` — a reference with an unknown provider raises, naming `eval.jury`.
- `test_eval_cost_ranges` — 0, a negative and 101 raise for both cost keys.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_llm_config.py` passes and the Definition of Done is green. Commit `feat(ijob-app): read the eval settings`.

### Task 3 — Build a model by reference

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/llm/registry.py` — `lm_for_ref(ref)`, cached per reference; `lm_for` uses it.
- Modify `packages/ijob-app/src/ijob_app/llm_ops.py` — `LlmRuntime.registry_for_ref(ref)` does the `claude` CLI check per provider; `registry_for` calls it.
- Test `packages/ijob-ai/tests/ijob_ai_tests/llm/test_registry.py`, `packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py`.

**Change:** A juror, a grader or a `--model` value gets its LM without being any task's configured model.

**Proof:**
- `test_lm_for_ref_is_cached_per_reference` — the same reference gives the same LM; another gives another.
- `test_lm_for_still_uses_the_task_model` — existing behaviour holds.
- `test_registry_for_ref_checks_claude_once` — two claude-sdk references run the version check once.
- `test_registry_for_ref_skips_the_check_for_openrouter` — no CLI lookup for an OpenRouter reference.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_registry.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): build a model by reference`.

### Task 4 — Store eval records

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py` — migration 8 with the tables of spec section 5; `Store.evals` returns the records object on the same connection.
- Create `packages/ijob-core/src/ijob_core/eval_records.py` — frozen record types, plus `EvalRecords` with reads and writes.
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`, `packages/ijob-core/tests/ijob_core_tests/test_eval_records.py`.

**Change:** `EvalRecords` finds or creates a set; writes and reads cases, votes, labels, runs and results; `INSERT OR IGNORE` for votes and derived labels; records run spend after each item; sums OpenRouter spend, `running` rows included; shows any `running` row read by another command as `incomplete`; and reads pools by each posting's newest verdict under given rules, ties to kept (spec section 6).

**Proof:**
- `test_migration_8_upgrades_a_version_7_database` — a version 7 file gains the tables and keeps its rows.
- `test_set_is_unique_on_rules_profile_and_jury` — the same three find one set; a changed jury makes another.
- `test_vote_write_never_replaces` — a second vote for one key leaves the first.
- `test_derived_label_never_replaces_a_user_label` — a user label stands.
- `test_votes_do_not_reach_saved_scores` — a stored vote leaves `saved_scores` unchanged.
- `test_pools_use_the_newest_verdict` — a posting with both answers lands in one pool, by time.
- `test_pool_tie_goes_to_kept` — equal times give kept.
- `test_total_spend_sums_runs` — two runs' spend adds up.
- `test_running_row_reads_incomplete_to_others` — a `running` row reads `incomplete`, and its spend still counts.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_eval_records.py` passes and the Definition of Done is green. Commit `feat(ijob-core): store eval records`.

### Task 5 — Apply the eval caps

**Files:**
- Modify `packages/ijob-app/src/ijob_app/llm_ops.py` — `run_items` takes an optional `cap`.
- Create `packages/ijob-app/src/ijob_app/eval_spend.py` — `EvalSpend`, built per command.
- Test `packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py`, `packages/ijob-app/tests/ijob_app_tests/test_eval_spend.py`.

**Change:** `EvalSpend` works out the command's cap from `EvalSettings` and recorded spend, refuses at no money left with a new `EvalBudgetSpentError(LlmError)` naming `eval.total_cost_usd`, gives each call the budget to charge through `budget_for(ref)` (the operation's for OpenRouter, a separate uncapped one for claude-sdk), and owns the command's run row: `begin(suite, ...)` writes it, `record()` updates its spend, and `finish(...)` closes it.

**Proof:**
- `test_run_items_cap_replaces_the_llm_cap` — a given cap stops the run where `[llm].max_cost_usd` would not.
- `test_run_items_default_cap_is_unchanged` — no argument behaves as today.
- `test_cap_is_the_smaller_limit` — spent 7.50 of 10 with a cap of 4 gives 2.50; spent 1 gives 4.
- `test_no_money_left_refuses` — spent 10 raises, naming the key.
- `test_subscription_call_does_not_count` — a claude-sdk charge leaves the capped budget at 0, and shows as subscription spend.
- `test_openrouter_call_counts` — an OpenRouter charge counts toward the cap.
- `test_begin_writes_the_run_row` — a `running` row exists before any call.
- `test_record_updates_spend` — the row's OpenRouter and subscription spend follow each item.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_eval_spend.py` passes and the Definition of Done is green. Commit `feat(ijob-app): apply the eval spending caps`.

---

## Risks

- The lock moves rich, click and huggingface-hub (spec 2.2). A CLI or TUI test that pins rendering could change.
- Migration 8 runs on the user's real store the first time any command opens it. It only adds tables.
- Riskiest step: Task 1, since the flags must stay in step with the plugins deepeval ships.

## Proof

- The five gate commands in `CLAUDE.md` exit 0.
- No test calls an LLM or the network.
