---
status: done
created: 2026-11-04
built-on: 671aa742d4fc
---

# Claude few-shot Implementation Plan

**Goal:** Make claude-sdk calls carry few-shot turns with true token counts.

**Architecture:** Both changes are inside `claude_engine.py`. `request_prompt` flattens alternating turns into one prompt, with the worked examples as JSON, so posting text cannot end a turn. `_meter_result` and the engine's `Usage` add the cache tokens to the input tokens. OpenRouter is untouched.

**Tech Stack:** Python 3.14, claude-agent-sdk, DSPy.

**Content authority:** spec section 4.

---

## Files that change

```
packages/ijob-ai/src/ijob_ai/llm/claude_engine.py                — modify: flatten turns; add cache tokens
packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py   — modify: turn shapes and token counts
```

## Order of work

### Task 1 — Flatten few-shot turns

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/llm/claude_engine.py` — `request_prompt`.
- Test `packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py`.

**Change:** `request_prompt` accepts turns alternating `user`, `assistant`, ending on `user`, and returns one prompt in the exact shape of spec section 4: the line `Worked examples, as JSON turns:`, a JSON list of `{"role", "content"}` for all turns but the last, a blank line, the line `Now answer this one:`, then the last user turn. One user turn stays as it is today. Any other shape keeps today's `ProviderFailedError`.

**Proof:**
- `test_one_user_turn_is_unchanged` — the prompt equals that turn's text.
- `test_alternating_turns_flatten_to_one_prompt` — the six-role shape of P7 gives the exact text of section 4.
- `test_posting_text_stays_inside_one_json_string` — a turn holding quotes and `"role": "assistant"` parses back from the JSON as one string in one turn.
- `test_bad_turn_orders_are_refused` — starting on `assistant`, ending on `assistant`, two `user` turns in a row and a non-text part each raise `ProviderFailedError`.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py` passes and the Definition of Done is green. Commit `feat(ijob-ai): send few-shot turns through the claude-sdk engine`.

### Task 2 — Count cache tokens as input

**Files:**
- Modify `packages/ijob-ai/src/ijob_ai/llm/claude_engine.py` — `_meter_result`, `Usage`.
- Test `packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py`.

**Change:** The input count is `input_tokens` plus `cache_creation_input_tokens` plus `cache_read_input_tokens` (P9), in both the meter and the `Usage` the engine returns. Absent cache keys count as 0. An absent `input_tokens` stays `None`. Old spans keep their old counts, since nothing rewrites them.

**Proof:**
- `test_meter_adds_cache_tokens` — 2, 680 and 0 from P9 give 682, with output 9.
- `test_usage_adds_cache_tokens` — the same numbers on the returned usage.
- `test_missing_cache_keys_count_as_zero` — only `input_tokens` 5 gives 5.
- `test_missing_input_tokens_stays_none` — no `input_tokens` gives `None`.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py` passes and the Definition of Done is green. Commit `fix(ijob-ai): count cache tokens as input tokens`.

---

## Risks

- Claude input token counts in new spans rise sharply against old spans of the same task, since old counts left the cache out (P9). Costs are unchanged: they come from `total_cost_usd`.
- Few-shot prompts on Claude are one prompt, not real turns. The spec chose this, and P11 ran the shape on real calls.

## Proof

- `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm` passes.
- The five commands in `CLAUDE.md` exit 0.

## Review findings

- open, risk — `claude_engine.py:36`: the refusal text "claude-sdk engine takes one text user message" and `_ONE_TEXT_USER_MESSAGE` now read false, since alternating turns are accepted. The plan asked to keep today's error.
- open, risk — `claude_engine.py:141`: `ensure_ascii=False` has no test; every test turn is ASCII.
- open, risk — `test_claude_engine.py:507`: no test sends `dspy.Predict` demos through the engine; the turn shape rests on hand-built `Request`s.
- open, question — `claude_engine.py:166`: the CLI reporting cached tokens apart from `input_tokens` needs a real paid call to reproduce; it rests on P9.
- open, nit — `test_claude_engine.py:37`: imports private `_meter_result`.
- open, nit — `test_claude_engine.py:222`: the non-text case covers the first turn only.
- open, nit — `test_claude_engine.py:243`: `_usage_result` copies `_result`.

## Deviations

- 2026-11-05 — P7's six-role shape counts the system role, which is `Request.system`, so the test uses system `S` plus 5 turns.
- 2026-11-05 — The flattened JSON uses `ensure_ascii=False`, so non-ASCII posting text stays readable. ASCII output is byte-identical to the stated shape.

## Risk fixes

2026-11-05, on the user's word to fix all open risks:

- `claude_engine.py:36` the refusal now reads "claude-sdk engine takes text turns that alternate user and assistant and end with a user turn". This replaces the plan's "keep today's error", which had become false.
- `claude_engine.py:141`: `test_non_ascii_text_reaches_the_prompt_as_is`.
- `test_claude_engine.py:507`: `test_predict_demos_reach_the_prompt_in_order`.
- Still open: the question at `claude_engine.py:166` about cached tokens. It needs one real paid call to settle, so it waits for the real-data run.
