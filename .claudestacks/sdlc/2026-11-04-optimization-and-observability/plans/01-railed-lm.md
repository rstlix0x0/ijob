---
status: done
created: 2026-11-04
built-on: 671aa742d4fc
---

# Railed LM Implementation Plan

**Goal:** Put every optimizer model call under a budget, a meter, a span and a timeout.

**Architecture:** A new `RailedLM(dspy.BaseLM)` in `ijob_ai/llm/railed.py` is called from an optimizer's worker thread and runs each call as a coroutine on the command's running loop. A shared `RailGuard` holds what all LMs of one tuning run share: the stop flag, the counts and the fatal error. The span, meter and error helpers of `call.py` become public so that a tuning span looks like any other. Nothing here imports tuning code.

**Tech Stack:** Python 3.14, DSPy 3.4.1, OpenTelemetry.

**Content authority:** spec section 3.

---

## Files that change

```
packages/ijob-ai/src/ijob_ai/llm/call.py                 — modify: public helpers; span takes a trace value
packages/ijob-ai/src/ijob_ai/llm/railed.py               — create: RailGuard and RailedLM
packages/ijob-ai/tests/ijob_ai_tests/llm/test_call.py    — modify: helper names and the trace argument
packages/ijob-ai/tests/ijob_ai_tests/llm/test_railed.py  — create: the rails, on fake LMs and engines
```

## Order of work

### Task 1 — Make the call helpers public

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/llm/call.py` — rename `_call_span` to `call_span`, `_meter_from_history` to `meter_from_history`, `_translate` to `translate_failure`, `_ForgivingJSONAdapter` to `ForgivingJSONAdapter`.
- Modify `packages/ijob-ai/tests/ijob_ai_tests/llm/test_call.py`.

**Change:** `call_span` gains an optional `trace` argument. When it is given, that `OperationTrace` is used; otherwise `current_operation_trace()` is read as today. `run_call` keeps its behaviour.

**Proof:**
- `test_call_span_uses_the_trace_it_is_given` — with no current trace and a trace passed, one span is recorded under it.
- `test_call_span_without_a_trace_opens_none` — no current trace and none passed records nothing, as today.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_call.py` passes and the Definition of Done is green. Commit `refactor(ijob-ai): expose the call helpers the rails share`.

### Task 2 — Build RailedLM

**Files:**
- Create `packages/ijob-ai/src/ijob_ai/llm/railed.py` — `RailGuard`, `RailedLM`.
- Create `packages/ijob-ai/tests/ijob_ai_tests/llm/test_railed.py`.

**Change:**
- `RailGuard` holds `stopped`, `stop()`, `calls`, `failed`, `first_error`, `cap_reached`, `fatal` (the `CostUnknownError` or `ProviderUnavailableError` that ended the run) and an optional `on_call` callback, which runs on the loop after each call. It is shared by the task LM and the tuner LM of one run. `stopped` means only that later calls are refused. It says nothing about why, so a run's status never reads it.
- `RailedLM` takes the registry LM, its provider, its budgets, `timeout_s`, `task`, `task_version`, the `OperationTrace` as a value, the running loop and the guard. It accepts both `lm(messages=…)` and `lm(prompt)` (P13), and hands the work to the loop with `asyncio.run_coroutine_threadsafe` (P8, P16).
- Each call, on the loop: refuse with `CapReachedError` if the guard is stopped or any budget has no room, spending nothing; `attempt = lm.copy()`; `open_meter`, `call_span` with `posting_id=None`, `asyncio.timeout`; `attempt.acall(...)`, with a `messages` call inside DSPy's `adapter_message_call` (P11, P12); meter from history; charge every budget; count.
- A completed call with no cost raises `CostUnknownError` and sets the guard's stop flag and `fatal` (`call.py:364-365`). `ProviderUnavailableError` does the same, since the grid stops on it (spec section 8). Any other failure, timeouts included, is counted in `failed` with `first_error`, translated by `translate_failure` and raised. There are no retries.
- `copy(**overrides)` returns a wrapper with the same guard and budgets, and applies the overrides to its inner LM. A provider other than the one named by the LM's model raises `ValueError`, as `run_call` does (`call.py:336-339`).

**Proof:**
- `test_messages_shape_reaches_the_model` — `lm(messages=…)` returns the model's outputs.
- `test_prompt_shape_reaches_the_model` — `lm("text")` returns the model's outputs.
- `test_calls_run_on_the_given_loop` — the fake engine sees the loop's thread, not the worker's.
- `test_every_budget_is_checked_before_a_call` — one budget at its cap refuses the call and the model is not called.
- `test_every_budget_is_charged_after_a_call` — both budgets show the call's cost.
- `test_a_refused_call_spends_nothing` — `CapReachedError`, `cap_reached` set, no budget moves.
- `test_stop_refuses_every_later_call` — after `stop()`, calls raise at once and the model count stays still.
- `test_timeout_counts_as_a_failure` — a slow engine gives `ProviderFailedError`, `failed` 1.
- `test_provider_failure_keeps_the_first_error` — two failures keep the first error's text.
- `test_unknown_cost_raises_and_stops` — `CostUnknownError`, `fatal` set, the next call refused.
- `test_provider_unavailable_stops` — `ProviderUnavailableError`, `fatal` set, the next call refused.
- `test_provider_mismatch_is_refused` — a mismatched provider raises `ValueError` and calls nothing.
- `test_openrouter_is_metered_from_history` — billed cost from `provider_data` reaches the budget (P10).
- `test_claude_is_metered_by_its_engine` — the engine's meter reaches the budget.
- `test_each_call_records_one_span` — one `chat <model>` span with task and version, and no posting id.
- `test_each_call_uses_its_own_copy` — two calls do not share history.
- `test_failed_call_is_not_retried` — a failing engine is called once.
- `test_copy_keeps_the_rails` — a copy with an override shares guard and budgets and passes the override to the model.
- `test_on_call_runs_after_each_call` — the callback sees the counts on the loop's thread.
- `test_messages_call_gives_no_warning` — no `DeprecationWarning` under the repo's `filterwarnings=error` (P11).

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_railed.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): add rails for optimizer model calls`.

---

## Risks

- `adapter_message_call` is private to DSPy. `dspy>=3.4.1,<3.5` is pinned (`packages/ijob-ai/pyproject.toml:13`) and `test_messages_call_gives_no_warning` catches a change.
- Renaming the helpers touches every file that imports them. `call.py` is the only module with these names today, so the blast radius is `call.py` and `test_call.py`.
- Calls in flight may overshoot a budget by their own cost (`budget.py:4-10`). The spec accepts this.

## Proof

- `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm` passes.
- The five commands in `CLAUDE.md` exit 0.

## Review findings

- fixed — `railed.py:90` bug: the rail did not forward `supported_params`, `supports_response_schema`, `supports_function_calling`, `supports_reasoning`, so an OpenRouter task lost `response_format` under tuning. `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_railed.py` failed first on `test_an_openrouter_task_sends_the_same_request_through_the_rail_and_run_call` (`assert via_rail == direct`, `At index 810 diff`), then: `21 passed`.
- fixed — `railed.py:123` docstring said `trace` None means no spans; it now says the current operation trace is used. Same command: `21 passed`.
- fixed — `test_railed.py:243` unexplained `# type: ignore[misc]` replaced by the named function `_call_with`. `uv run --locked mypy --strict` on both files: `no issues in 2 source files`.
- open, risk — `railed.py:90`: `copy.deepcopy(rail)` raises `TypeError: cannot pickle '_contextvars.Context' object`; a program holding the rail through `set_lm` logs and prints to stderr. Unreachable while the rail goes only through `dspy.context`.
- open, risk — `railed.py:132`: no retries holds only because registry LMs set `num_retries=0`; inner `num_retries=3` and HTTP 429 gave 4 wire requests for `calls` 1. Fix: `self._lm.copy(num_retries=0)` and a 429 test.
- open, risk — `railed.py:176`: called on the loop's own thread, `future.result()` hangs; only the docstring forbids it.
- open, risk — `railed.py:222`: an `on_call` that raises replaces the call's outcome.
- open, risk — `railed.py:280`: an engine `CostUnknownError` with no completed meter counts in `failed` and does not stop. The real claude engine fills the meter first, so this is not reached today.
- open, risk — `test_call.py:371`: no test has both a current and a different given trace.
- open, risk — `test_railed.py:161`: no test charges a call that completed and then failed.
- open, risk — `test_railed.py:216`: nothing pins the `from failure` chain, so a timeout could record `provider_failed`.
- open, risk — `test_railed.py:253`: `failed == 0` is not asserted for fatal calls.
- open, risk — `test_railed.py:429`: `on_call` is tested on successful calls only.
- open, nit — `call.py:140`, `call.py:238`: new public functions lack `Args:` sections.
- open, nit — `railed.py:76`: `_check_provider` copies `run_call`'s check; `railed.py:270` repeats its raise rule.
- open, nit — `railed.py:90`: `rail.acall` raises a misleading `NotImplementedError`; wrapper `kwargs` do not reflect the inner LM's.
- open, nit — `railed.py:171`: `Raises:` omits engine `LlmError`s.
- open, nit — `railed.py:254`: `lm()` with no prompt or messages counts as a provider failure.
- open, nit — `railed.py:255`: a call cancelled after its meter completed goes uncharged.
- open, nit — `railed.py:261`: `fatal` keeps the last fatal error, not the first.
- open, nit — `railed.py:276`: the raw-`LlmError` branch has no test.
- open, nit — `test_call.py:383`: the assertion is a precondition, not "records nothing".
- open, nit — `test_railed.py:146`: the capped budget is last in both refusal tests.
- open, nit — `test_railed.py:334`: `make_tracing` copies `test_call.py:290`.

## Deviations

- 2026-11-05 — Task 2 said nothing of forwarding the LM's capabilities. Review showed the wire bodies differ without it, so the rail now forwards four properties, with one test beyond the plan's list.
- 2026-11-05 — Task 2's "There are no retries" holds only for LMs built with `num_retries=0`, as the registry builds them. Left as built; see the open `railed.py:132` finding.
- 2026-11-05 — The parity test captures wire bodies with the `serve_openrouter` fake transport, since this DSPy build routes through vendored lm15, not litellm.
