---
status: done
created: 2026-11-04
built-on: 671aa742d4fc
---

# Phoenix stack Implementation Plan

**Goal:** Replace Langfuse with one local Phoenix container.

**Architecture:** `compose.yaml` becomes the single service of spec section 12. Tracing writes only to `traces.db`, so a model call never waits on Phoenix. `[phoenix].endpoint` reuses the local-only check that `langfuse_endpoint` had. Export itself is plan 14.

**Tech Stack:** Docker Compose, Python 3.14, OpenTelemetry.

**Content authority:** spec sections 12 (Stack, Local tracing) and 14 (`.env.example`, `README.md`).

---

## Files that change

```
compose.yaml                                                      — modify: one Phoenix service
.env.example                                                      — modify: OPENROUTER_API_KEY and a note only
README.md                                                         — modify: Phoenix lines, auth warning, cost note
packages/ijob-ai/src/ijob_ai/tracing/setup.py                     — modify: local store only
packages/ijob-ai/src/ijob_ai/llm/settings.py                      — modify: drop the endpoint; add PhoenixSettings
packages/ijob-app/src/ijob_app/llm_config.py                      — modify: read_phoenix_settings; drop the key
packages/ijob-app/src/ijob_app/llm_ops.py                         — modify: drop the Langfuse session option
packages/ijob-app/src/ijob_app/data/config_template.toml          — modify: [phoenix] comments
packages/ijob-ai/tests/ijob_ai_tests/tracing/test_setup.py        — modify: drop the Langfuse tests
packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py         — modify: new settings
packages/ijob-app/tests/ijob_app_tests/test_llm_config.py         — modify: endpoint and unknown key
packages/ijob-app/tests/ijob_app_tests/test_repo_files.py         — create: compose content and the langfuse grep
```

## Order of work

### Task 1 — Drop the exporter

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/tracing/setup.py` — remove `_langfuse_exporter`, the key constants, `_MISSING_KEY_WARNING`, `_LANGFUSE_*`, the batch processor and the `session` argument of `start_tracing`, and correct the module docstring.
- Modify `packages/ijob-ai/src/ijob_ai/llm/settings.py` — remove `TracingSettings.langfuse_endpoint`; add `PhoenixSettings(endpoint="http://127.0.0.1:6006")`.
- Modify `packages/ijob-app/src/ijob_app/llm_config.py` — remove the key from `_TRACING_KEYS`; `_endpoint(value, key)` takes the key it names; add `read_phoenix_settings`, with `_PHOENIX_KEYS`.
- Modify `packages/ijob-app/src/ijob_app/llm_ops.py` — remove `LlmOptions.session` and `session=` in `run_items`, and correct the docstrings at lines 146, 237 and 256.
- Modify `packages/ijob-app/src/ijob_app/data/config_template.toml` — replace the Langfuse lines at 47-51 with commented `[phoenix]` lines and the local-only note.
- Test `packages/ijob-ai/tests/ijob_ai_tests/tracing/test_setup.py`, `packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py`, `packages/ijob-app/tests/ijob_app_tests/test_llm_config.py`.

**Change:** Every span goes only to the local store. `_silence_export_logs` stays, since the OTLP exporter is used by plan 14. An old `tracing.langfuse_endpoint` key gets the existing `unknown key` error (`llm_config.py:152-156`). The endpoint defaults to `http://127.0.0.1:6006`, and a non-local host is refused by the same check.

**Proof:**
- `test_spans_go_only_to_the_local_store` — the provider has one span processor, and spans arrive in `traces.db`.
- `test_old_langfuse_key_is_an_unknown_key` — `[tracing] langfuse_endpoint` raises `InvalidValueError` on `tracing.langfuse_endpoint`, with `unknown key`.
- `test_phoenix_endpoint_defaults_to_local` — no table reads `http://127.0.0.1:6006`.
- `test_phoenix_endpoint_local_only` — localhost, 127.0.0.1 and ::1 pass, with a trailing slash removed.
- `test_phoenix_endpoint_non_local_rejected` — another host raises `InvalidValueError` naming `phoenix.endpoint`.
- `test_phoenix_unknown_key_is_refused` — another key in `[phoenix]` raises the existing error.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tracing packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py packages/ijob-app/tests/ijob_app_tests/test_llm_config.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): keep traces local and drop the Langfuse exporter`.

### Task 2 — Replace the stack and its words

**Files:**
- Modify `compose.yaml` — the YAML of spec section 12 exactly: image `arizephoenix/phoenix:21.0.0`, `restart: "no"`, `mem_limit: 2g`, port `127.0.0.1:6006:6006`, the five environment variables and the `phoenix_data` volume.
- Modify `.env.example` — keeps `OPENROUTER_API_KEY`, and says nothing loads the file, so it has to be exported (P33).
- Modify `README.md` — replace lines 48, 62 and 90 (P32): local Phoenix, `docker compose up -d` and `down`, that auth is off because the port is on localhost only, that anything on the same Mac can read, change or delete its data while it runs, which holds the CV and rules, so stop it after use, and that cost comes from ijob, not Phoenix (P25).
- Create `packages/ijob-app/tests/ijob_app_tests/test_repo_files.py`.

**Change:** No code. The grep test covers `README.md`, `.env.example`, `compose.yaml`, `docs/` and `packages/*/src`, in any case. Test files are outside it, since one tests the `unknown key` error.

**Proof:**
- `test_langfuse_is_named_nowhere` — the listed paths hold no `langfuse`, in any case.
- `test_compose_binds_only_to_localhost` — `compose.yaml` holds `127.0.0.1:6006:6006` and the pinned image, with one service.
- `test_compose_turns_off_what_ijob_does_not_use` — telemetry, external resources, MCP and auth are each off, and `restart` is `"no"`.
- `test_env_example_says_nothing_loads_it` — it holds `OPENROUTER_API_KEY` and no `OTEL_`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_repo_files.py` and `docker compose config -q` pass and the Definition of Done is green. Commit `feat(repo): run a local Phoenix instead of Langfuse`.

---

## Risks

- The old Langfuse stack's Docker volumes on any machine that ran them are left behind. Nothing in this repo removes them.
- `README.md` names the steps before `ijob traces export` exists. Plan 14 adds that step.
- Another container already on port 6006 would clash with `docker compose up`. `execute` does not start the stack.

## Proof

- `uv run --locked pytest` passes.
- `git grep -il langfuse -- ':!.claudestacks' ':!uv.lock' ':!packages/*/tests'` prints nothing.
- The five commands in `CLAUDE.md` exit 0.

## Review findings

- fixed — `.env.example:1` claimed "ijob and docker compose read the environment only", which is false: compose reads a `.env` beside its file, and litellm calls `load_dotenv` on import. The clause is gone. `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_repo_files.py -q`: `10 passed in 0.17s`.
- open, risk — `README.md:91`: "`.env.example` lists the variables ijob reads" is false; `IJOB_HOME` and `OTEL_SDK_DISABLED` are not listed.
- open, risk — `settings.py:81`: the docstring says always local, but `PhoenixSettings` does not validate; only `read_phoenix_settings` checks. Plan 14 reads through that function.
- open, risk — `test_setup.py:161`: the stderr-silence test is gone; one logger of three is checked, and plan 14 does not name `_silence_export_logs`.
- open, risk — `test_llm_config.py:327`: the template's uncommented `[tracing]` lines are no longer validated.
- open, risk — `test_repo_files.py:35`: the localhost test stays green with a second `0.0.0.0` port or `network_mode: host`. The shipped `compose.yaml` is correct.
- open, nit — `README.md:48`: "A local Phoenix can show them" is not true until plan 14; this plan's Risks accept it.
- open, nit — `README.md:96`, `compose.yaml:9`: the cost mismatch and the variables' effects were not reproduced, since containers were barred.
- open, nit — `test_setup.py:34`: `_no_root_handlers` is dead.
- open, nit — `test_setup.py:127`: reads OpenTelemetry's private processor list.
- open, nit — `packages/ijob-app/pyproject.toml:18`: `requests` looks orphaned; recheck after plan 14.
- open, nit — `llm_config.py:266`: `removesuffix` leaves one `/` on `http://localhost:6006//`.
- open, nit — `llm_ops.py`: re-adding `LlmOptions.session` would fail no test.
- open, nit — `test_repo_files.py:27`: `except UnicodeDecodeError: continue` skips any undecodable file.
- open, nit — `test_repo_files.py:62`: asserts `export`, not the sentence.

## Deviations

- 2026-11-05 — This plan's Proof command `git grep -il langfuse -- ':!.claudestacks' ':!uv.lock' ':!packages/*/tests'` prints `packages/ijob-app/tests/ijob_app_tests/test_llm_config.py`, not nothing, since the pathspec does not match below `tests`. With `':!packages/*/tests/*'` it prints nothing. That file must hold the `unknown key` test, so the code stands.
- 2026-11-05 — `_endpoint` is `_endpoint(value, key, default)`, not `_endpoint(value, key)`, to carry the default.
- 2026-11-05 — One test beyond the plan: `test_phoenix_endpoint_must_be_a_string`, the only test of that branch.
- 2026-11-05 — `requests` stays in `packages/ijob-ai/pyproject.toml`, for plan 14.

## Risk fixes

2026-11-05, on the user's word to fix all open risks, for plans 05 and 06:

- `README.md:91` now says `.env.example` lists the provider key, and names `IJOB_HOME` and `OTEL_SDK_DISABLED` in prose.
- `settings.py:81` `PhoenixSettings` validates at construction, through `check_local_endpoint`, which `llm_config.py` now shares: `test_phoenix_refuses_non_local_endpoints_at_construction`.
- `test_setup.py:161`: `test_export_failures_print_nothing_on_stderr` covers all three loggers, each caught by a mutant.
- `test_llm_config.py:327`: `test_template_tracing_and_phoenix_lines_parse_through_the_reader`.
- `test_repo_files.py:35`: `test_localhost_check_catches_a_widened_compose_file` catches a second `0.0.0.0` port and `network_mode: host`.
- `llm_config.py:181`: `test_tune_baseline_must_be_a_string`.
- `test_llm_config.py:318`: `test_template_tune_block_parses_once_uncommented`.
