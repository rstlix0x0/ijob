---
status: approved
created: 2026-11-01
built-on: cae3bcdf3b23
---

# LLM Runtime Implementation Plan

**Goal:** Run every model call through one metered, budgeted, timed, error-translating path over
`dspy.LM`.

**Architecture:**
- `ijob-ai` gains its dependencies (spec §2, plus `ijob-core`, which it imports) and the root tooling
  settings.
- Light modules have no heavy imports, so CLI startup stays cheap (spec §1.6):
  - `ijob_ai.errors`: the `LlmError` family;
  - `ijob_ai.llm.reference`: the model reference;
  - `ijob_ai.llm.settings`: `Task`, `LlmSettings`, `TracingSettings`;
  - `ijob_ai.llm.meter`: `CallMeter` in a `ContextVar`;
  - `ijob_ai.llm.budget`: `Budget`.
- Heavy modules import `dspy`, and are imported only on demand:
  - `ijob_ai.llm.registry` holds the process hygiene, which acts on `os.environ`, and builds one
    `dspy.LM` per task;
  - `ijob_ai.llm.call` holds `run_call`, the single error boundary (spec §3.5).
- **The engine factory.** The `Registry` takes it as `claude_engines: Callable[[ModelRef], tuple[Engine, AsyncEngine]]`
  and calls it once per claude-sdk task, when that task's LM is first built.
  - The caller (plan 08) binds the resolved, version-checked CLI path into the factory before any
    claude-sdk LM is built.
  - Plan 04 defines `claude_engines(ref, cli_path)`. Plan 08 passes
    `functools.partial(claude_engines, cli_path=<resolved>)`.

**Tech Stack:** Python 3.14, dspy 3.4.1 (lm15 native route, JSONAdapter), pytest, lm15 `FakeTransport`
(`dspy/_vendor/lm15/testing.py`).

---

## Files that change

```
pyproject.toml                                                — [modify] uv no-binary-package, mypy overrides, uv.sources ijob-ai
packages/ijob-ai/pyproject.toml                               — [modify] dependencies: ijob-core + spec §2 table
packages/ijob-app/pyproject.toml                              — [modify] depend on ijob-ai
uv.lock                                                       — [modify] relocked
packages/ijob-ai/src/ijob_ai/errors.py                        — [create] LlmError and subclasses
packages/ijob-ai/src/ijob_ai/llm/__init__.py                  — [create] docstring only
packages/ijob-ai/src/ijob_ai/llm/reference.py                 — [create] Provider, ModelRef, parse_model_ref
packages/ijob-ai/src/ijob_ai/llm/settings.py                  — [create] Task, LlmSettings, TracingSettings
packages/ijob-ai/src/ijob_ai/llm/meter.py                     — [create] CallMeter and its ContextVar
packages/ijob-ai/src/ijob_ai/llm/budget.py                    — [create] Budget
packages/ijob-ai/src/ijob_ai/llm/registry.py                  — [create] prepare_environment, Registry
packages/ijob-ai/src/ijob_ai/llm/call.py                      — [create] run_call
packages/ijob-ai/tests/ijob_ai_tests/test_package.py          — [modify] light-import and warning tests
packages/ijob-ai/tests/ijob_ai_tests/test_errors.py           — [create]
packages/ijob-ai/tests/ijob_ai_tests/llm/__init__.py          — [create] test package marker
packages/ijob-ai/tests/ijob_ai_tests/llm/fakes.py             — [create] scripted sync/async fake engines and recorded OpenRouter bodies
packages/ijob-ai/tests/ijob_ai_tests/llm/test_reference.py    — [create]
packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py     — [create]
packages/ijob-ai/tests/ijob_ai_tests/llm/test_meter.py        — [create]
packages/ijob-ai/tests/ijob_ai_tests/llm/test_budget.py       — [create]
packages/ijob-ai/tests/ijob_ai_tests/llm/test_registry.py     — [create]
packages/ijob-ai/tests/ijob_ai_tests/llm/test_call.py         — [create]
```

## Order of work

### Task 1 — Dependencies and tooling

**Files:**
- Modify the root `pyproject.toml`, `packages/ijob-ai/pyproject.toml`, `packages/ijob-app/pyproject.toml`
  and `uv.lock`.
- Test `packages/ijob-ai/tests/ijob_ai_tests/test_package.py`.

**Change:**
- `ijob-ai` dependencies: `ijob-core` (workspace, as `packages/ijob-sources/pyproject.toml:11` lists
  it) plus the five constraints in spec §2's table. `ijob-app` adds `ijob-ai`.
- The root config gets:
  - `[tool.uv] no-binary-package = ["claude-agent-sdk"]`;
  - `ijob-ai = { workspace = true }` under `[tool.uv.sources]`;
  - `[[tool.mypy.overrides]]` for `dspy` and `dspy.*` with `ignore_missing_imports = true`;
  - `[[tool.mypy.overrides]]` for `ijob_ai.tasks.signatures` with `disallow_subclassing_any = false`.
- Run `uv lock`.

**Proof:** written first, seen failing, then passing:
- `test_package_import_is_light`: a subprocess imports `ijob_ai`, `ijob_ai.errors`,
  `ijob_ai.llm.reference`, `ijob_ai.llm.settings`, `ijob_ai.llm.meter` and `ijob_ai.llm.budget`. It
  prints the sorted loaded module names whose top package is `dspy`, `claude_agent_sdk`, `pypdf` or
  `litellm`, and the test asserts the list is empty.
- `test_sdk_installed_without_bundled_cli`: the installed `claude_agent_sdk/_bundled` holds nothing
  but `.gitignore`.
- `test_heavy_imports_emit_no_warnings`: a subprocess runs
  `python -W error -c "import dspy, claude_agent_sdk, pypdf, opentelemetry.sdk.trace, opentelemetry.exporter.otlp.proto.http.trace_exporter"`
  and exits 0. This matches the gate's `filterwarnings = ["error"]` (`pyproject.toml:31`).
  - If it fails, the warning is recorded as a deviation, and the narrowest
    `filterwarnings = "ignore:<message>:<category>:<module>"` entry for that third-party module is
    added to the root config, with a reason comment.

**Done when:** `uv sync --locked --all-groups --all-packages` succeeds,
`uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/test_package.py` passes and the CLAUDE.md
gate is green. Commit `build(ai): add LLM dependencies and tooling`.

### Task 2 — Errors and model reference

**Files:**
- Create `errors.py`, `llm/__init__.py` and `llm/reference.py`.
- Test `test_errors.py` and `llm/test_reference.py`.

**Change:**
- `LlmError` is the root. Subclasses:
  - `MissingModelError(task, config_path)`, with spec §9's message;
  - `ProviderUnavailableError(reason)`;
  - `ProviderFailedError(reason)`;
  - `CostUnknownError(model)`;
  - `TaskOutputError(task, reason)`;
  - `NothingActionableError()`;
  - `NoCvError()`;
  - `CvTextError(reason)`.
- `Provider` is a StrEnum: `claude-sdk`, `openrouter`.
- `ModelRef(provider, model)` is frozen, with `__str__` giving `<provider>/<model>`.
- `parse_model_ref(value, key)` splits at the first `/`. It raises `InvalidValueError(key, …)` for no
  provider, an unknown provider, or a blank model.

**Proof:** written first, seen failing, then passing:
- `test_missing_model_message`: the message equals
  `no model for judge; set [models].judge in /x/config.toml`.
- `test_all_llm_errors_share_root`: each listed class subclasses `LlmError`, and none subclasses
  `InvalidValueError`.
- `test_parse_model_ref_table`: parametrized over spec §3.1's five rows. `openrouter/google/gemini-3.8-flash`
  gives provider `openrouter` and model `google/gemini-3.8-flash`. The three error rows raise
  `InvalidValueError` whose `field` is the key passed in.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/test_errors.py packages/ijob-ai/tests/ijob_ai_tests/llm/test_reference.py`
passes and the gate is green. Commit `feat(ai): add LLM errors and model reference`.

### Task 3 — Settings, meter and budget

**Files:**
- Create `llm/settings.py`, `llm/meter.py` and `llm/budget.py`.
- Test `llm/test_settings.py`, `llm/test_meter.py` and `llm/test_budget.py`.

**Change:**
- `Task` is a StrEnum: `cv_profile`, `instructions`, `judge`, `scorer`.
- `LlmSettings` is frozen. It holds:
  - `models: Mapping[Task, ModelRef]`, where a task may be absent;
  - `max_cost_usd`, default 0.50;
  - `concurrency`, default 4;
  - `timeout_s`, default 120;
  - `cli_path: Path | None`;
  - `config_path: Path`.

  `model_for(task)` raises `MissingModelError` when the task has no model.
- `TracingSettings` is frozen: `max_size_mb`, default 256, and `langfuse_endpoint: str | None`.
- `CallMeter` is mutable: `cost_usd: float | None`, `input_tokens`, `output_tokens`, `model`, and
  `completed: bool`. `completed` is true once a provider answered: a claude-sdk `ResultMessage` was
  received, or an OpenRouter history entry exists.
  - `open_meter()` is a context manager that sets a fresh meter in a `ContextVar`.
  - `current_meter()` returns it, or raises `RuntimeError` outside one.
- `Budget(cap)` has `try_start() -> bool` (true while `spent < cap`), `charge(cost: float)`, `spent`,
  `cap` and `reached`. It never awaits, so concurrent asyncio tasks share it safely.

**Proof:** written first, seen failing, then passing:
- `test_model_for_missing_raises`: an absent task raises `MissingModelError` whose message names the
  task and the config path.
- `test_settings_defaults`: a default `LlmSettings` has 0.50, 4, 120 and None, and a default
  `TracingSettings` has 256 and None.
- `test_meter_is_per_context`: two concurrent tasks under `open_meter` set costs 0.1 and 0.2, and
  each reads back its own.
- `test_current_meter_outside_raises`.
- `test_budget_overshoot_bound`: cap 0.50, cost 0.002, four concurrent workers looping
  `try_start` → `await asyncio.sleep(0)` → `charge`. Starts stop at about call 250, and
  `0.50 ≤ spent ≤ 0.50 + 4×0.002` (spec §9 first example).

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_settings.py packages/ijob-ai/tests/ijob_ai_tests/llm/test_meter.py packages/ijob-ai/tests/ijob_ai_tests/llm/test_budget.py`
passes and the gate is green. Commit `feat(ai): add LLM settings, meter and budget`.

### Task 4 — Registry and process hygiene

**Files:**
- Create `llm/registry.py` and the test helper `llm/fakes.py`.
- Test `llm/test_registry.py`.

**Change:**
- `prepare_environment()` works on `os.environ`:
  - sets `os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"`;
  - raises `ProviderUnavailableError` naming the variable when `LITELLM_MODEL_COST_MAP_URL`,
    `OPENROUTER_API_BASE` or `OPENROUTER_BASE_URL` is set;
  - then imports `dspy` and calls `dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)`.
  - Later calls repeat the checks but not the configuration.
- `Registry(settings, claude_engines)`. `lm_for(task)` returns the task's `dspy.LM`, built once per
  task:
  - **openrouter:** first checks `OPENROUTER_API_KEY` in `os.environ`, raising
    `ProviderUnavailableError` when it is absent, then builds
    `dspy.LM("openrouter/<model>", cache=False, num_retries=0)`.
  - **claude-sdk:** calls the factory once, then builds
    `dspy.LM("claude-sdk/<model>", engine=…, async_engine=…, cache=False, num_retries=0)`.
  - **missing model:** `MissingModelError`.
- `fakes.py` provides:
  - a scripted `FakeAsyncEngine`, which records requests, answers texts from a list, can meter a
    cost or raise, and can sleep;
  - its sync companion;
  - recorded OpenRouter bodies, with and without `usage.cost`, served through lm15 `FakeTransport`.

**Proof:** written first, seen failing, then passing, with `monkeypatch` on `os.environ` and `HOME`
set to `tmp_path`:
- `test_prepare_environment_sets_cost_map_flag`: with `LITELLM_LOCAL_MODEL_COST_MAP=False`
  beforehand, `os.environ` holds `True` after.
- `test_prepare_environment_refuses_overrides`: parametrized over the three variables; each raises
  `ProviderUnavailableError` whose message names it.
- `test_prepare_environment_disables_dspy_cache`: after one fake-engine call through `run_call`
  (Task 5), `tmp_path/.dspy_cache` does not exist.
- `test_lm_for_openrouter_without_key_raises`: no `OPENROUTER_API_KEY` raises
  `ProviderUnavailableError` naming the variable, and no LM is built.
- `test_lm_for_claude_uses_injected_engines`: the factory is called once for two `lm_for(JUDGE)`
  calls. The LM's async engine is the factory's, and its `num_retries` is 0.
- `test_lm_for_missing_model_raises`: `MissingModelError` names the task.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_registry.py` passes
and the gate is green. Commit `feat(ai): add model registry and process hygiene`.

### Task 5 — `run_call`, the single error boundary

**Files:** Create `llm/call.py`; Test `llm/test_call.py`.

**Change:** `async run_call(predict, lm, *, inputs, provider, budget, timeout_s) -> Prediction`
follows spec §3.5:
1. Opens a meter, runs `predict.acall(**inputs)` on `lm.copy()` inside
   `dspy.context(lm=copy, adapter=dspy.JSONAdapter())`, under `asyncio.timeout(timeout_s)`.
2. In `finally`, for OpenRouter, when the copy's history has an entry, marks the meter `completed`
   and sets its cost from `history[-1]["response"].provider_data["usage"]["cost"]`, or else the
   entry's `cost`.
3. Charges the budget with `meter.cost_usd`, or 0 when the call never completed. A call that failed
   before any answer was not billed.
4. Translates errors, in this order:
   1. a `completed` call whose cost is still None → `CostUnknownError`, which wins over any other
      error, because it stops the run;
   2. `AdapterParseError` → `TaskOutputError`;
   3. a DSPy `LMError` whose `__cause__` is an `LlmError` → that cause;
   4. any other DSPy `LMError` → `ProviderFailedError`;
   5. timeout → `ProviderFailedError("timed out after N s")`.

`run_call` does not check `try_start`; the caller does.

**Proof:** written first, seen failing, then passing, with `fakes.py`:
- `test_run_call_success_charges_meter_cost`: a fake engine that meters 0.004 leaves
  `budget.spent == 0.004`.
- `test_run_call_parse_failure_is_charged`: an OpenRouter junk answer with `usage.cost=0.003` raises
  `TaskOutputError`, and `spent == 0.003`.
- `test_junk_engine_reaches_cap`: an always-junk fake engine at 0.1 per call, cap 0.25, and a loop of
  `try_start` + `run_call`. Every call raises `TaskOutputError`, `try_start` turns false after 3
  calls, and `spent == 0.3` (spec §9 second example).
- `test_run_call_unwraps_llm_error_cause`: an engine raising `ProviderUnavailableError` surfaces as
  `ProviderUnavailableError`, not `LMUnexpectedError`.
- `test_run_call_other_engine_error_is_provider_failed`: an engine raising `RuntimeError("x")`
  surfaces as `ProviderFailedError`, `spent` is 0, and the meter is not completed.
- `test_run_call_openrouter_billed_cost`: a body with `usage.cost=0.000123` leaves
  `spent == 0.000123`.
- `test_run_call_openrouter_estimate_fallback`: a body without `usage.cost` gives `spent` equal to the
  history entry's `cost`, which is above 0.
- `test_run_call_openrouter_429_single_attempt`: `FakeTransport` with 429 responses records exactly
  one request. The call raises `ProviderFailedError`, and `spent` is 0.
- `test_run_call_completed_without_cost_raises_cost_unknown`: a fake engine that completes the meter
  with cost None raises `CostUnknownError`.
- `test_run_call_timeout`: an engine sleeping 1 s with `timeout_s=0.1` raises a `ProviderFailedError`
  whose message contains `timed out after`.
- `test_run_call_makes_no_network_attempt`: sockets patched to record and refuse, then
  `prepare_environment` and an OpenRouter `FakeTransport` call record zero attempts.
- `test_concurrent_calls_isolated`: two concurrent calls on one LM, costing 0.001 and 0.002, give
  `spent == 0.003`, and each meter holds its own cost (spec §3.2 `p_concurrent.py`).

**Done when:** `uv run --locked pytest packages/ijob-ai` passes and the gate is green. Commit
`feat(ai): add metered model call boundary`.

## Risks

- **Riskiest step:** Task 1's relock. It needs network access during execution and pulls about
  210 MB of DSPy dependencies (spec §2).
- **Third-party warnings.** Warnings from DSPy, LiteLLM, pydantic or pypdf on 3.14 would fail the
  gate's `-W error` collection. Task 1's warning test surfaces any; the reviewer's re-run of
  `p_concurrent.py` passed under `-W error`.
- **Private test helper.** lm15's `FakeTransport` is private DSPy API, used only in tests. The `<3.5`
  pin bounds the drift.
- **Not done here:** spans. Plan 05 adds them to `run_call`.

## Proof

- `uv run --locked pytest packages/ijob-ai` passes.
- The five gate commands exit 0.
