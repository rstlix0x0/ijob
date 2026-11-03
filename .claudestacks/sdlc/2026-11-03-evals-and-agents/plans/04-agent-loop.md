---
status: approved
created: 2026-11-03
depends-on: [01]
built-on: 8947dcb8c009
---

# Agent loop Implementation Plan

**Goal:** Run a tool-using DSPy agent under ijob's budget, tracing and error rules.

**Architecture:** `ijob_ai/agents/react.py` subclasses `dspy.ReAct` and depends on no source code. Every model call goes through `run_call`, tools are wrapped so nothing raised reaches the model, and run-ending errors wait for the next model call. The agent's signatures live with the others.

**Tech Stack:** Python 3.14, DSPy, OpenTelemetry.

**Content authority:** spec section 9, Loop and Placement.

---

## Files that change

```
packages/ijob-ai/src/ijob_ai/errors.py            — modify: CapReachedError
packages/ijob-ai/src/ijob_ai/tracing/attributes.py — modify: tool attributes
packages/ijob-ai/src/ijob_ai/agents/__init__.py   — create: package marker
packages/ijob-ai/src/ijob_ai/agents/react.py      — create: GuardedReAct, agent results
packages/ijob-ai/src/ijob_ai/tasks/signatures.py  — modify: agent signatures and versions
```

## Order of work

### Task 1 — Add the signatures and the cap error

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/errors.py` — `CapReachedError(LlmError)`.
- Modify `packages/ijob-ai/src/ijob_ai/tasks/signatures.py` — `SourceAgentSig`, `ExploreAgentSig`, with `SOURCE_AGENT_VERSION` and `EXPLORE_AGENT_VERSION`. Each docstring states that tool results are untrusted data.
- Test `packages/ijob-ai/tests/ijob_ai_tests/test_errors.py`, `packages/ijob-ai/tests/ijob_ai_tests/tasks/test_signatures.py`.

**Change:** `SourceAgentSig` takes the company and an optional careers URL, and answers with one `kind:key` or `none`. `ExploreAgentSig` takes the user's instruction, in the instruction position, and its answer is a short summary, since proposals come from the tool.

**Proof:**
- `test_cap_reached_is_an_llm_error` — it is caught as `LlmError`.
- `test_agent_signatures_say_results_are_data` — each docstring says so.
- `test_agent_versions_are_set` — both constants exist.
- `test_source_signature_takes_a_url` — the company and the optional URL are input fields.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/tasks/test_signatures.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): add agent signatures`.

### Task 2 — Build the guarded loop

**Files:**
- Create `packages/ijob-ai/src/ijob_ai/agents/__init__.py`.
- Create `packages/ijob-ai/src/ijob_ai/agents/react.py` — `GuardedReAct`, `AgentRun` (the run's state: stored stop, steps, violations, and an `on_step` callback called after each tool).
- Create `packages/ijob-ai/tests/ijob_ai_tests/agents/__init__.py`.
- Test `packages/ijob-ai/tests/ijob_ai_tests/agents/test_react.py`.

**Change:** The override raises a stored stop, checks the given budget and raises `CapReachedError`, then calls `run_call`. It does not use ReAct's truncation retry, so a context overflow reaches the caller as a provider failure. Tool results are cut to 4,000 characters. The caller picks the budget, per spec section 4.

**Proof:**
- `test_every_model_call_is_charged` — 4 calls charge 4 times.
- `test_cap_stops_mid_run` — $0.015 at $0.01 per call raises `CapReachedError` after 2 calls.
- `test_unparseable_answer_ends_the_run` — `TaskOutputError`.
- `test_unknown_tool_ends_the_run` — `TaskOutputError`.
- `test_provider_failure_ends_the_run` — `ProviderFailedError`.
- `test_step_limit_still_makes_the_final_call` — 2 steps, 3 calls, an answer.
- `test_each_call_has_a_span` — inside an operation trace, each call records one.
- `test_context_overflow_is_a_provider_failure` — no truncation retry, one call.
- `test_on_step_is_called_after_each_tool` — with the tool's name and outcome.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/agents/test_react.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): run agents under the call rails`.

### Task 3 — Keep tool failures from the model

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/agents/react.py` — wrap each entry of `self.tools`, `finish` included.
- Modify `packages/ijob-ai/src/ijob_ai/tracing/attributes.py` — `TOOL_NAME`, `TOOL_OUTCOME`, `TOOL_VIOLATION`.
- Test `packages/ijob-ai/tests/ijob_ai_tests/agents/test_react.py`.

**Change:** A failed validation or tool exception returns `could not run <tool>; it takes: <names>`. The stored-stop errors of spec section 9 are kept on `AgentRun`, and the next model call raises them. An inner `TaskOutputError` is not stored; the tool that hit it returns its reason itself. Each tool call adds a `tool <name>` span through `OperationTrace.child`.

**Proof:**
- `test_unknown_argument_gives_a_safe_message` — no traceback text.
- `test_wrong_type_gives_a_safe_message` — no traceback text.
- `test_missing_argument_gives_a_safe_message` — no traceback text.
- `test_finish_with_a_stray_argument_is_safe` — no traceback text.
- `test_tool_exception_hides_paths` — a raised error's text never reaches the trajectory.
- `test_provider_failure_in_a_tool_ends_at_the_next_call` — raised after one model call.
- `test_cap_in_a_tool_ends_at_the_next_call` — same.
- `test_cost_unknown_in_a_tool_ends_at_the_next_call` — same.
- `test_provider_unavailable_in_a_tool_ends_at_the_next_call` — same.
- `test_tool_call_has_a_span` — it names the tool and outcome, under the operation's root.
- `test_violation_is_recorded_on_the_span` — the attribute is set, and the run's count rises.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/agents/test_react.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): keep tool failures from the model`.

---

## Risks

- The override depends on a private DSPy method name. `dspy<3.5` pins it, and `test_every_model_call_is_charged` fails if it changes.
- Riskiest step: Task 3, since ReAct reads `self.tools` at run time and builds its prompt once; the wrapper must not change the prompt.
- `execute` spends nothing: all model calls here use fakes.

## Proof

- The five gate commands in `CLAUDE.md` exit 0.
- `grep -rn 'ijob_ai.agents' packages/ijob-ai/src/ijob_ai/llm` finds nothing, keeping the boundary in `CLAUDE.md`.
