---
status: done
created: 2026-11-01
depends-on: [01, 04, 05, 06, 07]
built-on: cae3bcdf3b23
---

# LLM Service Implementation Plan

**Goal:** Give the CLI and the TUI one async service API for the profile, instructions, judged
search, on-demand scoring and traces.

**Architecture:**
- **Lazy LLM module.** New module `ijob_app.llm_ops` holds the LLM operations. It imports `ijob_ai`
  LLM modules, and plan 04's `claude_engines`, only inside its functions, never at module level or
  in default arguments (spec §1.6).
- **How a `Service` gets its LLM inputs.** `open_service` (`service.py:754`) gains the keyword
  `llm: LlmOptions | None`. `LlmOptions` is a frozen dataclass holding:
  - `config_path: Path`;
  - `engines: Callable[[ModelRef, Path], tuple[Engine, AsyncEngine]] | None`, where None means plan
    04's factory, imported lazily;
  - `session: requests.Session | None`, for the Langfuse export;
  - `cli_which: Callable[[str], str | None] | None`.

  When `llm` is None, it defaults to `LlmOptions(config_path=home.config)`. `Service.__init__`
  (`service.py:287`) stores it. Plans 09 and 10 pass it, so `--config` reaches `MissingModelError`'s
  message.
- **`LlmRuntime`** is built on the first LLM operation and kept by the `Service`. It holds
  `LlmSettings`, the CLI path, and the `Registry`.
  - The CLI path is resolved and version-checked once, before the first claude-sdk task.
  - The registry factory is `lambda ref: engines(ref, cli_path)`, so the path the version check ran
    is the one the engine uses (spec §3.3).
- **Each operation:**
  - calls `start_tracing` (plan 05) for its own provider;
  - creates a `Budget`;
  - runs inside `operation_trace`, whose root span opens only at the first model call;
  - runs items in an `asyncio.TaskGroup` under `asyncio.Semaphore(concurrency)`, checking
    `budget.try_start()` before each call;
  - reads and writes plan 01's cache;
  - finishes tracing when it ends.
- `Service` (`service.py:280`) gains thin methods that delegate to `llm_ops` (spec §7.1). `store_cv`
  is unchanged.

**Tech Stack:** Python 3.14, asyncio (`TaskGroup`, `Semaphore`), pytest with fake engines.

---

## Files that change

```
packages/ijob-app/src/ijob_app/llm_ops.py                     — [create] LlmOptions, LlmRuntime, statuses, result types, run_items, operations
packages/ijob-app/src/ijob_app/service.py                     — [modify] open_service/Service take LlmOptions; new delegating methods
packages/ijob-app/tests/ijob_app_tests/llm_fakes.py           — [create] fake engine factory, fake CLI script writer, seeded stores for app tests
packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py        — [create]
packages/ijob-app/tests/ijob_app_tests/test_service.py        — [modify] delegation and import-light tests
```

## Order of work

### Task 1 — Options, runtime and the item runner

**Files:**
- Create `llm_ops.py` and the test helper `llm_fakes.py`.
- Modify `service.py`, only `open_service` and `Service.__init__` for `LlmOptions`.
- Test `test_llm_ops.py` and `test_service.py`.

**Change:**
- `LlmOptions` (above).
- `LlmRuntime.create(home, options)`: `prepare_environment()`, then `read_llm_settings`.
- `async LlmRuntime.registry_for(task)`:
  - when the task's model is claude-sdk and the CLI is not yet checked, it runs `resolve_cli` and
    `check_version` (plan 04);
  - it then returns the shared `Registry`.
- `JudgeStatus` is a StrEnum with spec §7.1's members: `not_run`, `cached`, `judged`,
  `no_description`, `failed`, `cap_reached`, `stopped`, `provider_error`. `no_description` is the
  status of a fresh verdict made without a description.
- `ScoreStatus` has the same members minus `no_description`.
- `STATUS_TEXT` holds the user texts per spec §4.4 and §9:

  | Status | Judge text | Score text |
  |---|---|---|
  | `failed` | `judge failed` | `score failed` |
  | `cap_reached` | `cap reached` | `cap reached` |
  | `stopped` | `stopped` | `stopped` |
  | `provider_error` | `provider error` | `provider error` |

- `OperationReport` holds `done`, `cached`, `dropped`, `not_done`, `spent`, `cap`, `stopped_by` (None,
  `cap`, `provider` or `cost_unknown`), `warnings` and `dropped_spans`.
- `async run_items(runtime, operation, items, worker)`:
  - runs workers under the semaphore and calls `budget.try_start()` before each call;
  - after a `ProviderFailedError` or `ProviderUnavailableError`, starts no new item, and marks the
    rest `provider_error`; after a `CostUnknownError`, marks the rest `stopped`; once `try_start` is
    false, marks the rest `cap_reached`;
  - catches only `LlmError` per item. Plan 06 guarantees that task errors are `LlmError`s;
  - on cancellation keeps finished results, then re-raises;
  - always finishes tracing.

**Proof:** written first, seen failing, then passing:
- `test_run_items_bounded_concurrency`: with concurrency 2 and six slow fake items, the recorded
  in-flight peak is exactly 2.
- `test_run_items_cap_marks_rest`: cap 0.25 at 0.1 per item over six items gives three done and three
  `cap_reached`, with `stopped_by="cap"`.
- `test_run_items_provider_failure_stops_rest`: the second item raises `ProviderFailedError`, later
  items are `provider_error`, and `stopped_by="provider"`.
- `test_run_items_cost_unknown_stops_rest`: the rest are `stopped`, with `stopped_by="cost_unknown"`.
- `test_run_items_cancel_keeps_finished`: cancelling after two finish leaves those two stored.
- `test_runtime_version_checked_once`: two judge operations run the fake CLI's `--version` once.
- `test_runtime_passes_checked_path_to_engines`: the fake factory receives the resolved fake-CLI
  path.
- `test_llm_ops_import_is_light`: a subprocess importing `ijob_app.service` and `ijob_app.llm_ops`
  has no `dspy` or `claude_agent_sdk` in `sys.modules`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py packages/ijob-app/tests/ijob_app_tests/test_service.py`
passes and the CLAUDE.md gate is green. Commit `feat(app): add LLM runtime and item runner`.

### Task 2 — Profile and instruction operations

**Files:**
- Modify `llm_ops.py` and `service.py`.
- Test `test_llm_ops.py` and `test_service.py`.

**Change:**
- `async profile_operation(runtime, store, cv_path, *, refresh) -> tuple[ResumeProfile, OperationReport]`:
  - raises `NoCvError` when no CV is stored;
  - reuses the stored profile when `cv_sha256`, the version and the model match, unless `refresh`;
  - otherwise reads the CV text, runs the task, and stores the result.
- `async propose_operation(runtime, store, layers, text) -> Proposal`, where
  `Proposal(text, instruction_filters: Mapping[str, object], rules, effective: FilterSpec, shadowed, from_cache)`:
  - `instruction_filters` is the instruction layer only;
  - it reuses the stored parse when the text hash, version and model match;
  - `effective` and `shadowed` come from `effective_filters(layers, instruction_filters)` (plan 07),
    which validates the layer.
- `accept_proposal(store, proposal, *, model, at)` stores `Instructions`, with `filters_json` being
  `instruction_filters` as sorted-key JSON (plan 01).
- `Service` methods: `build_profile(*, refresh)`, `cv_profile()`, `propose_instructions(layers, text)`,
  `accept_instructions(proposal)`, `instructions()`, `clear_instructions()`.

**Proof:** written first, seen failing, then passing, with fake engines:
- `test_profile_no_cv_raises`: `NoCvError`, with zero engine calls.
- `test_profile_cached_then_refresh`: the first call makes one engine call, a repeat makes zero, and
  `refresh=True` makes one.
- `test_propose_cache_reuse`: re-proposing the stored text gives `from_cache=True` and zero calls.
- `test_propose_reports_shadowed`: spec §4.3's example yields one shadowed `countries` field, and
  effective countries `{JP}`.
- `test_accept_roundtrip_instruction_layer`: after `accept`, the stored `filters_json` parses back to
  `instruction_filters`, not to the merged spec.
- `test_instructions_show_and_clear`: `instructions()` is None after `clear_instructions()`, and a
  second clear is a no-op.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py packages/ijob-app/tests/ijob_app_tests/test_service.py`
passes and the gate is green. Commit `feat(app): add profile and instruction operations`.

### Task 3 — Judged search

**Files:**
- Modify `llm_ops.py` and `service.py`.
- Test `test_llm_ops.py` and `test_service.py`.

**Change:**
- `Service.search(layers: FilterLayers, refs) -> AsyncIterator[SearchItem]`, where
  `SearchItem(posting, verdict: Verdict | None, status: JudgeStatus, no_description: bool)`:
  - computes the effective filters from `layers` and the stored instruction layer, if any, then runs
    `open_postings` (`service.py:349-367`);
  - `no_description` is computed from `posting.description is None`, whether the verdict is fresh or
    cached, so the marker survives caching;
  - with no stored rules, yields each posting with `status=not_run` and imports no `ijob_ai` LLM
    module (instruction filters still apply);
  - with rules, for each posting computes `input_sha256(posting_block(posting))`. It yields `cached`
    on a store hit; otherwise it judges, stores, and yields `judged` (`no_description` when the
    posting had none), or `failed` on `TaskOutputError`.
- Items are yielded in completion order. `Service.last_report` holds the `OperationReport` after
  iteration.

**Proof:** written first, seen failing, then passing:
- `test_search_without_rules_imports_no_llm_module`: a subprocess runs a no-rules search over a
  seeded store and prints `"dspy" in sys.modules or "claude_agent_sdk" in sys.modules`; the output
  is `False`.
- `test_search_instruction_filters_apply_without_rules`: stored instructions with
  `countries=["SG"]` and no rules filter out a JP posting, with zero engine calls.
- `test_search_with_rules_judges_and_caches`: the first run gives three `judged`; the second gives
  three `cached` and zero engine calls.
- `test_search_input_change_rejudges`: changing one posting's description re-judges only that one.
- `test_search_no_description_flag_survives_cache`: a lazy-kind posting has `no_description=True` on
  the first and the cached run.
- `test_search_parse_failure_marks_failed_and_continues`.
- `test_search_hostile_posting_isolated`: with three postings, one hostile, the fake engine receives
  three separate requests, each holding exactly one posting block.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_llm_ops.py packages/ijob-app/tests/ijob_app_tests/test_service.py`
passes and the gate is green. Commit `feat(app): judge search results by instructions`.

### Task 4 — Scoring and traces

**Files:**
- Modify `llm_ops.py` and `service.py`.
- Test `test_llm_ops.py` and `test_service.py`.

**Change:**
- `Service.score(ids) -> AsyncIterator[ScoreItem]`, where
  `ScoreItem(posting, score: Score | None, status: ScoreStatus)`:
  - de-duplicates ids, keeping first order;
  - raises `UnknownPostingError` for any unknown id before any call (`service.py:108`);
  - raises `NoCvError` when no CV is stored;
  - builds the profile first, inside the same budget and root span, when it is absent;
  - caches by input hash, profile hash, version and model;
  - sets `last_report` after iteration.
- `Service.traces(limit=20)`, `Service.trace(trace_id)` and `Service.prune_traces()` use plan 02's
  store on `home.traces`, with `max_size_mb` from the settings.

**Proof:** written first, seen failing, then passing:
- `test_score_dedupes_ids`: `[3, 1, 3]` scores 3 then 1.
- `test_score_unknown_id_raises_before_calls`: zero engine calls.
- `test_score_without_cv_raises_no_cv`.
- `test_score_builds_missing_profile_in_budget`: one profile call plus one score call, and
  `last_report.spent` is their sum.
- `test_score_cached`: a second score of the same posting gives `cached` and zero calls.
- `test_traces_lists_operations_after_search`: after a judged search with two calls, `traces()` has
  one row with operation `ijob search` and calls 2.
- `test_cached_search_writes_no_trace`: a fully cached search adds no trace row.
- `test_trace_unknown_raises`: `UnknownTraceError`.

**Done when:** `uv run --locked pytest packages/ijob-app` passes and the gate is green. Commit
`feat(app): add on-demand scoring and trace queries`.

## Risks

- **Riskiest step:** Task 3's no-rules path, which must stay import-free. The subprocess tests guard
  it, and plan 09 guards the output.
- **Completion order.** Items are yielded in completion order, so the CLI reorders them for printing
  (plan 09).
- **Not done:** a TUI-specific runtime. The TUI reuses `Service` (plan 10).

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The gate exits 0.

## Review findings

Reviewer verdict: blocking none. The scoped DoD re-run was green: ruff format and check on the 5 files, mypy --strict clean, and `pytest test_llm_ops.py test_service.py` 137 passed. Before the review, the orchestrator's full gate was green: format 251, ruff ok, mypy 185 files, pytest 2560. One fix round followed.

- fixed 🟡 `llm_ops.py:441`: `tracing.finish()` (a flush of up to 5 s plus a prune) ran on the event loop. It now runs as `await asyncio.to_thread(tracing.finish)`. `TraceStore.open` gained `check_same_thread=False`, because callers never use the store from two threads at once. `uv run --locked --no-sync pytest test_traces.py test_llm_ops.py test_service.py ijob_ai_tests/tracing` → 180 passed.
- fixed ❓ `llm_ops.py:1034`: score used a stale profile after the CV was replaced. The orchestrator decided to rebuild in the same run when the stored `cv_sha256` differs. `test_score_rebuilds_a_profile_read_from_an_earlier_cv` covers it.
- fixed 🟡 `llm_ops.py:930`: the single-flight profile build was untested. `test_score_builds_one_profile_for_concurrent_items` runs at concurrency 4 and expects 1 profile and 4 score requests.
- fixed 🟡 `test_llm_ops.py:375`: the cancel test now asserts the root span row that only `finish` writes. It was mutation-checked.
- fixed 🟡 `test_llm_ops.py:1205`: the close-early test now sleeps and checks the request count. That exposed a real bug: `aclose()` did not close the inner `_stream_run`, so one extra call leaked. Fixed with `contextlib.aclosing`.
- fixed 🔵 `test_llm_ops.py:349`: a test now checks that verdicts made before a cancel stay stored.
- fixed 🟡 `pyproject.toml`: ijob-app declares `dspy`, `requests` and `pypdf`. `uv lock` added metadata only.
- fixed 🔵 the cache-hit `instruction_filters` is a `MappingProxyType`. The docstrings for search and score (`ProviderUnavailableError`) and for `accept_instructions` Raises were corrected. The bare `model_for` call has a comment, and `ItemStatus` has a docstring.
- open 🔵 `llm_ops.py:89`: `STATUS_TEXT` is keyed by `type[StrEnum]`. Splitting it into `JUDGE_STATUS_TEXT` and `SCORE_STATUS_TEXT` waits until the CLI and TUI are done.
- open 🔵 `llm_ops.py:299`: callers waiting on the lock after a failed CLI check re-run it, at most `concurrency` times.
- open 🔵 `llm_ops.py:656`: the instruction text checks are copied from `parse_instructions`.
- open 🔵 `llm_ops.py:1097`: the trace helpers live in `llm_ops`, which is otherwise about model calls.
- open 🟡 `service.py:304`: `Service` has 25 public methods. The plan prescribes this; split it later.
- open 🔵 tests read the private `service._llm`. `llm_fakes.write_cv_pdf` copies the private `PdfWriter._add_object` helper. The seeding helpers live in the test files, not in `llm_fakes.py`.

## Deviations

- 2026-11-01: Tasks end in "Commit …", but the instruction to execute did not ask for commits. The work is left uncommitted.
- 2026-11-01: Tasks 3 and 4 ran as one coder in sequence, because they share every file. The review DoD was scoped to plan 08's files, since plans 09 and 10 coders were editing CLI and TUI files at the same time. The orchestrator ran the full gate just before.
- 2026-11-01: Accepted coder deviations:
  - `Service.__init__` takes `home`.
  - `run_items(..., on_outcome=)` returns `RunResult`, and a worker's non-`LlmError` propagates as an `ExceptionGroup`.
  - New `ItemStatus` StrEnum and `StopReason` Literal. `STATUS_TEXT[JudgeStatus][status]` is nested to avoid a key collision.
  - `profile_operation` takes `at`. `Service.build_profile` returns `(ResumeProfile, OperationReport)`.
  - `propose_operation` checks blank or overlong text before any call.
  - `instructions_model(runtime)` was added.
  - `list_traces`, `read_trace` and `prune_traces` live in `llm_ops`, and their `Service` methods are sync.
  - `search` and `score` are async generators, so their errors raise on the first iteration. Cached items are yielded first.
  - With no rules, every item is `not_run` and `last_report` stays `None`, because building a report would import dspy.
  - When score builds a missing profile, the profile call and the first score share one `try_start`, so the cap can overshoot by at most one call.
- 2026-11-01: Changed outside plan 08's file list: `packages/ijob-core/src/ijob_core/traces.py` now opens with `check_same_thread=False`. This was needed to take `tracing.finish` off the event loop.
