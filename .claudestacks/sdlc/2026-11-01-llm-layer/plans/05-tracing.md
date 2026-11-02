---
status: done
created: 2026-11-01
depends-on: [02, 03]
built-on: cae3bcdf3b23
---

# Tracing Implementation Plan

**Goal:** Record every model call as an OpenTelemetry span in `traces.db`, always on, with optional
local Langfuse export.

**Architecture:**
- **No process-global provider.** ijob never calls `opentelemetry.trace.set_tracer_provider`. That
  global can be set only once per process (opentelemetry-api 1.45.2 `trace/__init__.py:515,544-552`).
  - Each operation (spec §7.1) gets its own `TracerProvider`, built by `start_tracing` and shut down
    by `Tracing.finish()`. So the TUI's later operations and per-test providers never clash.
- **`OperationTrace`.** It is held in a `ContextVar` set by `operation_trace(tracing, name)` around
  the operation's `TaskGroup`. Tasks copy the context, so every call sees it.
  - `run_call` (plan 03) asks it for a child span.
  - The root span `ijob <name>` opens lazily, on the first child, so an operation that makes no model
    call writes no trace (spec §8).
  - Outside any `OperationTrace` (plain unit tests), `run_call` opens no span.
- **Modules.**
  - `ijob_ai.tracing.attributes` holds ijob's own attribute names (spec §8).
  - `ijob_ai.tracing.sqlite_exporter` converts `ReadableSpan` into plan 02's `SpanRow`. It catches
    `sqlite3.Error` itself, counting dropped spans, and never logs.
  - `ijob_ai.tracing.setup.start_tracing` handles `os.environ`, with `OTEL_SDK_DISABLED` removed
    with a warning and `ALWAYS_ON` pinned. It wires the processors, including optional local Langfuse
    export.

**Tech Stack:** Python 3.14, opentelemetry-sdk 1.45.2, opentelemetry-exporter-otlp-proto-http 1.45.2,
requests (session injection), pytest.

---

## Files that change

```
packages/ijob-ai/src/ijob_ai/tracing/__init__.py                       — [create] docstring only
packages/ijob-ai/src/ijob_ai/tracing/attributes.py                     — [create] attribute name constants
packages/ijob-ai/src/ijob_ai/tracing/sqlite_exporter.py                — [create] SqliteSpanExporter
packages/ijob-ai/src/ijob_ai/tracing/setup.py                          — [create] start_tracing, Tracing, OperationTrace, operation_trace
packages/ijob-ai/src/ijob_ai/llm/call.py                               — [modify] child span per call
packages/ijob-ai/tests/ijob_ai_tests/tracing/__init__.py               — [create] test package marker
packages/ijob-ai/tests/ijob_ai_tests/tracing/test_attributes.py        — [create]
packages/ijob-ai/tests/ijob_ai_tests/tracing/test_sqlite_exporter.py   — [create]
packages/ijob-ai/tests/ijob_ai_tests/tracing/test_setup.py             — [create]
packages/ijob-ai/tests/ijob_ai_tests/llm/test_call.py                  — [modify] span assertions
```

## Order of work

### Task 1 — Attribute names and the SQLite exporter

**Files:**
- Create `tracing/__init__.py`, `tracing/attributes.py` and `tracing/sqlite_exporter.py`.
- Test `tracing/test_attributes.py` and `tracing/test_sqlite_exporter.py`.

**Change:**
- Constants for spec §8's attributes:
  - `gen_ai.provider.name`, `gen_ai.operation.name`, `gen_ai.request.model`,
    `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
    `gen_ai.usage.cost`;
  - `ijob.task`, `ijob.task_version`, `ijob.posting_id`, `ijob.outcome`, `ijob.prompt`,
    `ijob.completion`;
  - `PROVIDER_NAMES`: `claude-sdk` → `anthropic`, `openrouter` → `openrouter`;
  - `Outcome` StrEnum: `ok`, `task_output_error`, `provider_failed`, `provider_unavailable`,
    `cost_unknown`, `timeout`.
- `SqliteSpanExporter(store: TraceStore)` implements `export`, `shutdown` and `force_flush`:
  - converts each span (hex ids, parent, name, ns times, status, attributes) and inserts it;
  - returns `SUCCESS`;
  - on `sqlite3.Error` adds the batch size to `dropped` and returns `FAILURE`, never raising or
    logging.

**Proof:** written first, seen failing, then passing:
- `test_attribute_names`: each constant equals its spec §8 string.
- `test_export_writes_tree`: a local `TracerProvider` with `SimpleSpanProcessor(SqliteSpanExporter)`
  and a nested span pair reads back from `TraceStore.tree` as parent and child.
- `test_export_sqlite_error_counts_dropped`: a store whose `insert` raises
  `sqlite3.OperationalError` gives `FAILURE` and `dropped == 1`, and `capsys` shows empty stderr.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tracing` passes and the
CLAUDE.md gate is green. Commit `feat(ai): add trace attributes and SQLite span exporter`.

### Task 2 — Per-operation tracing and Langfuse export

**Files:** Create `tracing/setup.py`; Test `tracing/test_setup.py`.

**Change:** `start_tracing(traces_path, settings: TracingSettings, *, session=None) -> Tracing`
reads and edits `os.environ`.

1. If `OTEL_SDK_DISABLED` is present, it deletes it from `os.environ` and records the warning
   `warning: OTEL_SDK_DISABLED ignored; ijob tracing cannot be turned off`.
2. It opens `TraceStore` (plan 02) and builds a new, non-global `TracerProvider(sampler=ALWAYS_ON)`
   with `SimpleSpanProcessor(SqliteSpanExporter)`.
3. When `langfuse_endpoint` is set and both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` exist, it
   adds a `BatchSpanProcessor` around `OTLPSpanExporter` with:
   - `endpoint=<endpoint>/api/public/otel/v1/traces`;
   - headers `Authorization: Basic base64(pk:sk)` and `x-langfuse-ingestion-version: 4`;
   - `timeout=5`;
   - the given `session`.

   With an endpoint but a missing key, it records
   `warning: Langfuse export skipped; set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY`.

`Tracing` exposes:
- `tracer`, from that provider;
- `warnings`;
- `finish() -> TracingReport(dropped, pruned)`, which calls `force_flush(timeout_millis=5000)`, ends
  any open root span, shuts the provider down, runs `TraceStore.prune(max_size_mb × 2**20)` and
  closes the store.

`operation_trace(tracing, name)` is a context manager that sets the `OperationTrace` `ContextVar`.
`OperationTrace.child(name)` opens the root span `ijob <name>` on first use, then a child span under
it.

**Proof:** written first, seen failing, then passing, with `monkeypatch` on `os.environ`:
- `test_disabled_env_is_removed_and_spans_kept`: with `OTEL_SDK_DISABLED=true` and
  `OTEL_TRACES_SAMPLER=always_off` set in `os.environ`, a child span is stored, `os.environ` no
  longer holds `OTEL_SDK_DISABLED`, and the warning is present.
- `test_two_operations_in_one_process_both_traced`: two consecutive `start_tracing` → child →
  `finish` cycles store two traces.
- `test_operation_without_calls_writes_no_trace`: `operation_trace` with no `child` call leaves the
  store empty.
- `test_langfuse_export_headers_and_path`: a `requests.Session` with a recording adapter receives one
  POST to `http://localhost:3000/api/public/otel/v1/traces`, with the Basic header, the ingestion
  header and content type `application/x-protobuf`.
- `test_langfuse_missing_key_warns_and_skips`: the adapter records no request, and the warning is
  present.
- `test_finish_prunes_and_reports`: with `max_size_mb=16` and enough stored traces to exceed it,
  `pruned > 0`.
- `test_export_failure_does_not_raise`: an adapter raising `requests.ConnectionError` leaves
  `finish()` returning a report.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tracing` passes and the
gate is green. Commit `feat(ai): set up per-operation tracing with optional Langfuse export`.

### Task 3 — A span per model call

**Files:** Modify `llm/call.py`; Test `llm/test_call.py`.

**Change:**
- `run_call` gains `task: Task`, `task_version: str` and `posting_id: PostingId | None`.
- When an `OperationTrace` is current, it opens `child("chat <model>")`, where `<model>` is the
  reference's model part, also used as `gen_ai.request.model`.
- In `finally` it sets:
  - `gen_ai.provider.name` (from `PROVIDER_NAMES`), `gen_ai.operation.name` `chat`, the request
    model, and the meter's response model, tokens and cost;
  - `ijob.task`, `ijob.task_version` and `ijob.posting_id`;
  - `ijob.outcome`: the `Outcome` matching the translated error, or `ok`;
  - `ijob.prompt` and `ijob.completion`, each cut to 16 KB with `[truncated]`.

**Proof:** written first, seen failing, then passing, with a local `TracerProvider` and an in-memory
exporter wrapped in `operation_trace`:
- `test_call_span_name_and_genai_attributes`: a claude-sdk call to model `m` produces span `chat m`
  with provider `anthropic`, request model `m`, the fake's response model, tokens, and cost 0.004.
- `test_call_span_records_cost_and_outcome_on_parse_failure`: the outcome is `task_output_error`, and
  the cost is set.
- `test_call_span_provider_unavailable_outcome`: an engine raising `ProviderUnavailableError` gives
  the outcome `provider_unavailable`.
- `test_call_span_truncates_prompt`: a 20 KB prompt is stored at 16 KB ending in `[truncated]`.
- `test_call_span_nested_under_operation_root`: the child's parent is the root span `ijob search`.
- `test_call_without_operation_trace_opens_no_span`.

**Done when:** `uv run --locked pytest packages/ijob-ai` passes and the gate is green. Commit
`feat(ai): trace every model call`.

## Risks

- **Riskiest step:** Task 2's per-operation provider lifecycle. The two-operation test guards it.
- **Child-process environment.** `TRACEPARENT` reaches the claude child whenever a span is active
  (spec §3.3). This is accepted, and nothing removes it.
- **Not done:** traces for operations that make no model call (spec §8).

## Proof

- `uv run --locked pytest packages/ijob-ai` passes.
- The gate exits 0.

## Review findings

Reviewer verdict: blocking none. Full CLAUDE.md gate re-run from the worktree root: `uv sync` 0, `ruff format --check` 0, `ruff check --no-fix` 0, `mypy --strict .` 0 (161 files), `pytest` 0 (2330 passed, 40.7 s). Hard constraints checked: `set_tracer_provider` never called; `OTEL_SDK_DISABLED` popped before `TracerProvider(sampler=ALWAYS_ON)` is built; Langfuse export only with endpoint and both keys; tests use a recording adapter (no network); ijob-core imports no OTel or requests. All findings open (no blocking set, so no fix round):

- fixed 🟡 `call.py:53` OpenRouter spans now carry `gen_ai.response.model` and tokens: `_meter_openrouter` fills them from the history entry's `response_model` and `usage.prompt_tokens`/`completion_tokens`; an OpenRouter span test was added. Gate after fix: `uv run --locked pytest` → 2341 passed.
- fixed 🟡 `call.py:94` with no history entry, `ijob.prompt` falls back to the `inputs` mapping as sorted JSON, clipped to 16 KB; the provider_unavailable, timeout, provider_failed and cost_unknown span tests assert it.
- fixed 🟡 `setup.py:255` `start_tracing` gives the `opentelemetry.exporter.otlp`, `opentelemetry.sdk._shared_internal` and `opentelemetry.sdk.trace.export` loggers one `NullHandler` each (idempotent; records still propagate). `test_export_failure_prints_nothing_to_stderr` failed before the fix with 5 retry lines and 1 failure line on stderr; it passes after, also under `-p no:logging`.
- fixed 🟡 `setup.py:231` `test_global_tracer_provider_is_untouched` asserts a `ProxyTracerProvider` after `start_tracing` and `finish`.
- open 🔵 `setup.py:110` the root span starts from the ambient context; pass an empty `Context()` so it always starts a new trace.
- open 🔵 `setup.py:128` `finish()`/`start_tracing` can leak the open store if flush, shutdown or the exporter constructor raises.
- open 🔵 `setup.py:142` a second `operation_trace` on one `Tracing` silently reuses the first root and name.
- open 🔵 `attributes.py:12` two-way package dependency between `ijob_ai.tracing` and `ijob_ai.llm` (no module cycle).
- open 🔵 `sqlite_exporter.py:35` docstring says "full screen"; "full disk" was meant.
- fixed 🔵 `test_setup.py:236` a `_fast_retry` fixture patches the OTLP backoff to 1 ms, and the new stderr test checks stderr.
- answered ❓ Task 3: yes, OpenRouter fills the meter's response model and tokens (fixed above).
- open 🔵 follow-up review: `_translate`'s last `return` is unreachable (narrow to the caught union); the `InvalidValueError` for an unparseable `lm.model` has no test; the two `opentelemetry.sdk.*` logger names are not guarded by a test; the OpenRouter fixture body's model equals the request model; `suppress(ValueError)` in a test should be `pytest.raises`; `_fast_retry` patches a private OTel method; the idempotency test checks one of three loggers.
- open 🔵 additions beyond the Files list, all justified: `current_operation_trace()` getter; call-site arguments in `test_registry.py`/`test_claude_engine.py`; `requests>=2.34.2,<3` in ijob-ai's pyproject plus `uv.lock`.

## Deviations

- 2026-11-01: Tasks end in "Commit …", but the instruction to execute did not ask for commits (CLAUDE.md: commit only when the user asks). The work is left uncommitted.
- 2026-11-01: Plan Task 2 orders `finish()` as flush, then end the root. Built instead: end the root first, then `force_flush(timeout_millis=5000)`, then shutdown, prune, close — in the plan's order the root reaches Langfuse only at shutdown as a second POST. `test_langfuse_export_headers_and_path` (one request) guards it.
- 2026-11-01: `ijob_ai.tracing.setup` imports `requests` directly, but plan 03 Task 1 did not declare it (only transitive via the OTLP exporter). Added `requests>=2.34.2,<3` to `packages/ijob-ai/pyproject.toml` and relocked (no new download; 2.34.2 was already locked).
- 2026-11-01: Tasks 1 and 2 were built by one coder (Task 2 uses Task 1's exporter); Task 3 ran after plan 03's review. Task 3 also replaced plan 03's placeholder `TaskOutputError` task label with the real `task`.
