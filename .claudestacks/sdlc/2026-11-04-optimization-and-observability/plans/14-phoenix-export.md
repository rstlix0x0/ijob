---
status: approved
created: 2026-11-04
depends-on: [05, 11]
built-on: 671aa742d4fc
---

# Phoenix export Implementation Plan

**Goal:** Send traces, labels, runs and scores to Phoenix with `ijob traces export`.

**Architecture:** All mapping and HTTP stays in `ijob_ai/tracing/phoenix.py`, which imports `requests` and the OTLP exporter, so `ijob-core` stays free of network code. `ijob_app/trace_export.py` reads the stores and calls it in a fixed order: reachability, spans, labels, runs, scores. Reachability goes first because the span exporter returns only success or failure, so it cannot tell a stopped Phoenix from a refused batch. Every step can run again: Phoenix drops duplicate spans (P21), datasets are found by name, experiments are listed by name first, and evaluations are upserts (P22).

**Tech Stack:** Python 3.14, requests, OpenTelemetry OTLP over HTTP, Typer.

**Content authority:** spec section 12 (`ijob traces export`), section 15 (Phoenix rows) and section 16 (volume deleted).

---

## Files that change

```
packages/ijob-core/src/ijob_core/traces.py                 — modify: read all spans in batches and by time
packages/ijob-ai/src/ijob_ai/errors.py                     — modify: PhoenixError and its three kinds
packages/ijob-ai/tests/ijob_ai_tests/test_errors.py        — modify: their messages
packages/ijob-ai/src/ijob_ai/tracing/phoenix.py            — create: span mapping and the Phoenix client
packages/ijob-app/src/ijob_app/trace_export.py             — create: the export, step by step
packages/ijob-app/src/ijob_app/service.py                  — modify: traces_export
packages/ijob-app/src/ijob_app/cli_llm.py                  — modify: traces export
README.md                                                  — modify: the export step
packages/ijob-core/tests/ijob_core_tests/test_traces.py    — modify: reading spans
packages/ijob-ai/tests/ijob_ai_tests/tracing/test_phoenix.py — create: mapping and HTTP, fake session
packages/ijob-app/tests/ijob_app_tests/test_trace_export.py — create: the export, fake Phoenix
packages/ijob-app/tests/ijob_app_tests/test_cli_llm.py     — modify: the command
```

## Order of work

### Task 1 — Map spans and talk to Phoenix

**Files:**
- Modify `packages/ijob-core/src/ijob_core/traces.py` — `span_batches(size)`, ordered by start; `spans_between(start_ns, end_ns)`.
- Modify `packages/ijob-ai/src/ijob_ai/errors.py` — `PhoenixError(LlmError)`, with `PhoenixUnreachableError`, `PhoenixHttpError` and `SpanBatchRefusedError`, so `llm_failures` exits 1 on each (`cli_llm.py:106-122`).
- Create `packages/ijob-ai/src/ijob_ai/tracing/phoenix.py` — `to_readable_span`, `PhoenixClient`.
- Test `packages/ijob-core/tests/ijob_core_tests/test_traces.py`, `packages/ijob-ai/tests/ijob_ai_tests/tracing/test_phoenix.py`, `packages/ijob-ai/tests/ijob_ai_tests/test_errors.py`.

**Change:**
- `to_readable_span` keeps the stored trace and span ids, so a re-send is a duplicate (P21). `ijob.prompt` and `ijob.completion` become `input.value` and `output.value`. A span with no parent gets `openinference.span.kind` `CHAIN`. A span named `tool …` gets `TOOL` and `tool.name`. Any other gets `LLM`, `llm.model_name` from the response model or else the request model, `llm.token_count.prompt` and `llm.token_count.completion` (P19). The resource holds `service.name` and `openinference.project.name`, both `ijob`.
- `PhoenixClient(endpoint, session, exporter)` sends spans through `OTLPSpanExporter` to `/v1/traces` in batches of 100. No auth header is sent (P24). Its REST calls are those P20, P22 and P23 ran on 21.0.0:

  | Purpose | Call |
  |---|---|
  | Reachability | `GET /` |
  | Find a dataset by name | `GET /v1/datasets`, reading `data[].name` and `id` |
  | Create or append | `POST /v1/datasets/upload?sync=true`, with `action`, `name`, `inputs`, `outputs`, `metadata`; the reply has `data.dataset_id` |
  | Examples | `GET /v1/datasets/{id}/examples`, reading `data.examples` |
  | Find experiments | `GET /v1/datasets/{id}/experiments`, reading `data[].name` and `id` |
  | Create an experiment | `POST /v1/datasets/{id}/experiments`, with `name`, `metadata` |
  | Read runs | `GET /v1/experiments/{id}/runs`, reading `data` |
  | Post a run | `POST /v1/experiments/{id}/runs`, with `dataset_example_id`, `output`, `repetition_number` 1, `start_time`, `end_time`, `trace_id` |
  | Post a score | `POST /v1/experiment_evaluations`, with `experiment_run_id`, `name`, `annotator_kind` `CODE`, `start_time`, `end_time`, `result` holding `score` and `label` |

- Errors: a connection failure raises `PhoenixUnreachableError`; a REST reply of 400 or above raises `PhoenixHttpError`, with the status and first 200 characters; a refused span batch raises `SpanBatchRefusedError`, since the exporter returns only success or failure. Reads come before writes, so no 409 is expected, and one raises like any other.

**Proof:**
- `test_prompt_and_completion_become_input_and_output` — both are mapped, and neither `ijob.` key stays.
- `test_llm_span_kind_and_tokens` — `LLM`, the model name, prompt and completion counts.
- `test_tool_span_kind_and_name` — `TOOL` and `tool.name`.
- `test_root_span_is_a_chain` — no parent gives `CHAIN`.
- `test_stored_ids_are_kept` — trace and span ids match the row.
- `test_spans_go_in_batches_of_100` — 250 spans make three batches.
- `test_batch_refused_raises` — a failing exporter raises `SpanBatchRefusedError`.
- `test_unreachable_raises` — a connection error on `GET /` raises `PhoenixUnreachableError`.
- `test_phoenix_errors_exit_through_llm_failures` — each is an `LlmError`.
- `test_http_error_holds_status_and_200_characters` — a long body is cut.
- `test_span_batches_read_all_rows_in_order` — every row once, by start.
- `test_spans_between_read_a_window` — only those inside.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tracing/test_phoenix.py packages/ijob-core/tests/ijob_core_tests/test_traces.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): map spans and call Phoenix`.

### Task 2 — Export labels, runs and scores

**Files:**
- Create `packages/ijob-app/src/ijob_app/trace_export.py` — `export_all`, `ExportReport`.
- Create `packages/ijob-app/tests/ijob_app_tests/test_trace_export.py`, with a fake Phoenix holding the behaviours of P21 and P22.

**Change:**
- `export_all` checks reachability first, so nothing is sent to a stopped Phoenix.
- **Labels.** One dataset per set with labels, older sets included: `ijob-<task>-set-<id>`, with `posting_id` and title as input, the label as output, and bucket, origin and split part as metadata. Found by name, missing examples appended, existing examples not updated.
- **Runs.** Experiment `run-<id>-<model>` for full and held-out `judge` and `scorer` runs, with metadata, on the dataset of the run's own set. Full here means complete and not limited, at any size, so the runs already stored go too, such as runs 11 and 12. A held-out run is a complete, limited run of the task at a program's version, and its experiment also holds optimizer, tune run, val scores and spend from the `tune` row. Experiments are listed by name first, because Phoenix allows duplicates. Existing runs are read, and only missing ones posted.
- **Scores.** Evaluation `matches_label` for every run each time, since it is an upsert, so a stop between a run and its score heals. Each run's trace link comes from its time window, task and posting id (P27). A run with no span link is still posted, without a trace id.
- Tuning spans of `ijob tune-<task>` go with the rest. `ExportReport` counts what was sent, added and already there.

**Proof:**
- `test_dataset_is_created_with_split_parts` — name, examples and part in metadata.
- `test_dataset_is_found_and_appended` — a second export after `--add` adds only the new examples, with the same id.
- `test_experiment_is_found_by_name_not_duplicated` — a second export lists one experiment.
- `test_only_missing_runs_are_posted` — a partial experiment gets the rest, with no 409 raised.
- `test_a_stop_between_run_and_score_heals` — a run with no evaluation has one after the next export.
- `test_second_export_adds_nothing` — every count of new items is 0.
- `test_deleted_volume_refills` — an empty Phoenix gets everything again.
- `test_plain_limited_runs_are_left_out` — a `--limit` run makes no experiment.
- `test_a_stopped_phoenix_sends_nothing` — a session and an exporter that both fail on connection raise `PhoenixUnreachableError`, with no span sent.
- `test_stored_runs_of_any_size_are_exported` — a 100-case run on a 200-case set gets its experiment.
- `test_older_sets_get_their_own_dataset` — two sets give two datasets, and each run sits on its own.
- `test_held_out_experiment_holds_tuning_metadata` — optimizer, tune run, val scores and spend.
- `test_run_links_to_its_span` — by time window, task and posting id.
- `test_run_without_a_span_is_still_posted` — no trace id, and no error.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_trace_export.py` passes and the Definition of Done is green. Commit `feat(ijob-app): export labels, runs and scores to Phoenix`.

### Task 3 — Add the command

**Files:**
- Modify `packages/ijob-app/src/ijob_app/service.py` — `traces_export`, reading `read_phoenix_settings`.
- Modify `packages/ijob-app/src/ijob_app/cli_llm.py` — `traces export`, with `--db`, `--config`.
- Modify `README.md` — the `ijob traces export` step, and exporting after `tune screen`, since existing examples are not updated.
- Test `packages/ijob-app/tests/ijob_app_tests/test_cli_llm.py`.

**Change:** The command prints what was sent and added. It makes no model call and loads no model. Messages follow spec section 15: `Phoenix is not running at <url>; run docker compose up -d`; status and first 200 characters; `sending spans to <url> failed`, with `docker compose logs phoenix`. Each exits 1, and a rerun is safe.

**Proof:**
- `test_export_prints_its_counts` — sent, added and already there.
- `test_export_not_running_message` — the exact text, exit 1.
- `test_export_http_error_message` — status and body, exit 1.
- `test_export_span_failure_message` — both parts, exit 1.
- `test_export_non_local_endpoint_is_refused` — a non-local `[phoenix].endpoint` exits with the config error and sends nothing.
- `test_export_loads_no_llm_library` — no DSPy or Claude SDK in `sys.modules`, as `test_setup_and_cv_show_import_no_llm` checks (`test_cli.py:1838`).

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_llm.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob traces export`.

---

## Risks

- Phoenix's REST paths are those of image 21.0.0, which `compose.yaml` pins. A newer image may differ, and the fake Phoenix would not show it.
- Export holds the user's prompts, rules and CV in a volume readable by anything on the same Mac while the container runs. `README.md` says so (plan 05).
- A deleted volume refills only what is still in `traces.db`, since pruned traces are gone.

## Proof

- `uv run --locked pytest` passes.
- `uv run --locked python -c "import ijob_core.traces, sys; assert 'requests' not in sys.modules"` exits 0.
- The five commands in `CLAUDE.md` exit 0.
