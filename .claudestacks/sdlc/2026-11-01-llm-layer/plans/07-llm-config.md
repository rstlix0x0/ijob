---
status: done
created: 2026-11-01
depends-on: [02, 03]
built-on: cae3bcdf3b23
disproves: cae3bcdf3b23
---

# LLM Configuration Implementation Plan

**Goal:** Read every LLM setting, filter layer and home path that the LLM features need from the
ijob home and its config file.

**Architecture:**
- New module `ijob_app.llm_config` reads the `[models]`, `[llm]`, `[providers.claude-sdk]` and
  `[tracing]` tables of the user's config file into plan 03's `LlmSettings` and `TracingSettings`
  (spec §6). It imports only plan 03's light modules.
- `ijob_app.config` gains:
  - a layered filter read that keeps the packaged spec and the user-file spec apart;
  - `effective_filters`, which applies spec §4.3's precedence and reports shadowed fields.
- `IjobHome` gains `traces`, and `setup_home` creates and reports `traces.db` (spec §8).
- The template documents the new tables.

**Tech Stack:** Python 3.14, tomllib, pytest.

---

## Files that change

```
packages/ijob-app/src/ijob_app/llm_config.py                       — [create] read_llm_settings, read_tracing_settings
packages/ijob-app/src/ijob_app/config.py                           — [modify] FilterLayers, read_filter_layers, effective_filters, ShadowedField
packages/ijob-app/src/ijob_app/data/config_template.toml           — [modify] new commented tables
packages/ijob-app/src/ijob_app/home.py                             — [modify] IjobHome.traces; setup creates traces.db
packages/ijob-app/tests/ijob_app_tests/test_llm_config.py          — [create]
packages/ijob-app/tests/ijob_app_tests/test_config.py              — [modify] layering and precedence tests
packages/ijob-app/tests/ijob_app_tests/test_home.py                — [modify] traces path and setup tests
packages/ijob-app/tests/ijob_app_tests/test_cli_home.py            — [modify] setup output gains the traces.db line
```

## Order of work

### Task 1 — LLM settings from the config file

**Files:** Create `llm_config.py`; Test `test_llm_config.py`.

**Change:** `read_llm_settings(config_path) -> LlmSettings` and `read_tracing_settings(config_path) -> TracingSettings`.
- An absent file or table gives the defaults.
- Every key follows spec §6's table: `[models].<task>` via `parse_model_ref`, plus the
  `max_cost_usd`, `concurrency`, `timeout_s`, `cli_path` (`~` expanded), `max_size_mb` and
  `langfuse_endpoint` rules.
- `langfuse_endpoint` must be an `http` or `https` URL whose host is `localhost`, `127.0.0.1` or
  `::1`; one trailing `/` is removed.
- Unknown keys in the new tables, unknown task names, and unknown `[providers]` sub-tables raise
  `InvalidValueError`, in the same way as `config.py:395-400`.
- `cli_path` existence is not checked here; plan 04's `resolve_cli` does that.

**Proof:** written first, seen failing, then passing:
- `test_defaults_when_tables_absent`: 0.50, 4, 120, None, 256, None.
- `test_models_parsed`: `judge = "openrouter/google/gemini-3.8-flash"` gives
  `ModelRef(openrouter, "google/gemini-3.8-flash")` for `Task.JUDGE`, and the other tasks are absent.
- `test_invalid_values_rejected`: parametrized over `max_cost_usd` 0 and 101, `concurrency` 0 and 17,
  `timeout_s` 9 and 601, `max_size_mb` 15, and a bad model reference. Each raises
  `InvalidValueError` naming the key.
- `test_langfuse_endpoint_local_only`: `http://localhost:3000/` gives `http://localhost:3000`;
  `https://cloud.langfuse.com` and `ftp://localhost` each raise.
- `test_unknown_keys_rejected`: `[llm].foo`, `[models].ranker` and `[providers.other]`.

**Done when:** those tests pass and the CLAUDE.md gate is green. Commit `feat(app): read LLM settings from config`.

### Task 2 — Filter layers and instruction precedence

**Files:** Modify `config.py`; Test `test_config.py`.

**Change:**
- `FilterLayers` holds three specs: `packaged`, `user` and `flags`.
- `read_filter_layers(config, flags, *, default)` builds each layer through `_build_spec`
  (`config.py:370-385`), from `_read_default` (`:344`), `_read_user` (`:350`) and the flags.
  - `read_filters` (`config.py:121-158`) keeps its behaviour, and is re-expressed as the merge of
    those layers with no instruction layer.
- `effective_filters(layers, instruction_table | None) -> tuple[FilterSpec, tuple[ShadowedField, ...]]`:
  - validates `instruction_table` through `_build_spec` under the key prefix `instructions.filters`,
    so a bad value raises `InvalidValueError`;
  - chooses each `FilterSpec` field in the order flag, user file, instruction, packaged, where "set"
    means differing from the default (`ijob_core/filters.py:20-46`);
  - returns a `ShadowedField(field, used_from, used_value, ignored_value)` for every instruction
    field that a flag or the user file overrides.

**Proof:** written first, seen failing, then passing:
- `test_read_filters_unchanged`: the existing `test_config.py` cases stay green with no change.
- `test_effective_filters_checked_example`: spec §4.3's example. Packaged `role.include` engineering
  patterns, user `countries=["JP"]`, and instruction `role.include=["Rust"]`, `countries=["ID"]`,
  `remote=["worldwide","country"]` give countries `{JP}`, include `Rust` and remote
  `{worldwide, country}`. One `ShadowedField` names `countries`, with used value JP and ignored
  value ID.
- `test_flag_beats_instruction`.
- `test_instruction_filter_invalid_rejected`: `since = "banana"` raises `InvalidValueError` naming
  `instructions.filters.since`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_config.py` passes and
the gate is green. Commit `feat(app): layer filters with instruction precedence`.

### Task 3 — Template and home

**Files:**
- Modify `data/config_template.toml` and `home.py`.
- Test `test_home.py`, `test_llm_config.py` and `test_cli_home.py`. The expected `ijob setup` lines
  at `test_cli_home.py:48-51` and `:70-73` gain a fifth line for `home.traces`.

**Change:**
- The template gains spec §6's four tables, every key commented out.
- `IjobHome.traces` is `root/traces.db`.
- `setup_home` (`home.py:151-178`) also opens and migrates `TraceStore` (plan 02) and returns a fifth
  `SetupItem` for it, reporting `created` or `already there`.

**Proof:** written first, seen failing, then passing:
- `test_template_parses_and_yields_defaults`: `read_llm_settings` on a fresh template gives the
  defaults, and `tomllib` parses it.
- `test_template_mentions_every_new_key`: every key in spec §6's table appears in a comment.
- `test_traces_path`.
- `test_setup_creates_traces_db_idempotently`: the first run reports `created` and the second
  `already there`.

**Done when:** `uv run --locked pytest packages/ijob-app` passes and the gate is green. Commit `feat(app): add LLM config template and traces path`.

## Risks

- **Riskiest step:** Task 2's re-expression of `read_filters`. The existing config tests are the
  guard, and they must pass unchanged.
- **`ijob setup` output.** It gains a fifth line. Task 3 updates the CLI home tests in the same
  commit.
- **Not done:** reading `LANGFUSE_*` keys. That is environment, read by plan 05.

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The gate exits 0.

## Review findings

One reviewer covered plans 06 and 07 together. Its DoD re-run was green: sync 0, format 0, ruff check 0, mypy 0 issues in 182 files, pytest 2451 passed. Blocking was in plan 06 only.

- fixed 🟡 `config.py:256`: a flag or user-file value equal to the default counted as unset, so `--no-require-salary` lost to an instruction's `require_salary=true` and no shadow was reported. A field is now "set" when its layer names it explicitly, held in `FilterLayers.user_set` and `flags_set`. New tests cover `--no-require-salary`, the user file's `include = []`, and `effective_filters(layers, None) == read_filters(...)` over 6 cases. `uv run --locked pytest packages/ijob-app` → 413 passed.
- fixed 🟡 `test_config.py:488-504`: `test_read_filters_unchanged` now asserts full `FilterSpec` equality, `include` included.
- fixed 🟡 `config.py:456`: the `Money`, `bool`, `re.Pattern` and `RemoteScope` branches of `_value_text` are now tested.
- fixed 🔵 `ShadowedField.field` is now typed as the `FilterField` Literal.
- fixed 🟡 plan drift: recorded under Deviations below.
- open 🔵 `llm_config.py:18,121`: `_Table` and `_only_keys` duplicate helpers in `config.py`. They were kept apart on purpose while `config.py` was being changed.

## Deviations

- 2026-11-01: Tasks end in "Commit …", but the instruction to execute did not ask for commits. The work is left uncommitted.
- 2026-11-01: **Spec claim contradicted.** Spec §4.3 (spec.md:490), carried into Task 2, says "'Set' means differing from `FilterSpec`'s default".
  - That contradicts the spec's own precedence, "flag beats instruction". The reviewer reproduced it: `effective_filters` on `FilterFlags(require_salary=False)` (what `--no-require-salary` produces) plus an instruction `require_salary=true` gave `True` with shadowed `()`. A user file's `[filters.role] include = []` lost to the instruction's include the same way.
  - Built instead: a field is set when its layer explicitly gives it. `FilterLayers` carries `user_set` and `flags_set`, and `read_filter_layers` fills them in.
  - Frontmatter `disproves: cae3bcdf3b23`. The `design` skill clears it by amending spec §4.3.
- 2026-11-01: `read_filters` was not re-expressed as the layer merge. A merge keyed on "differs from default" could not represent a user file's `include = []`. `read_filters` keeps its table merge, and its flag step is shared with the layers through `_lay_flags`. With the explicit-set rule, `effective_filters(layers, None)` equals `read_filters(...)`, and a test guards that.
- 2026-11-01: Template table headers are commented out (`# [models]`), because every template line must be blank or start with `#` (an existing test). `test_template_new_settings_are_valid_once_uncommented` was added beyond the named tests.
- 2026-11-01: `llm_config` decisions:
  - an absent config file gives the defaults;
  - errors name the dotted key (`llm.max_cost_usd`);
  - `timeout_s` is an int in the file and a float in `LlmSettings`;
  - `langfuse_endpoint` is parsed with `urlsplit`, and IPv6 `[::1]` is accepted.
- 2026-11-01: The `cli.py` module docstring now lists `traces.db` among the home files.
