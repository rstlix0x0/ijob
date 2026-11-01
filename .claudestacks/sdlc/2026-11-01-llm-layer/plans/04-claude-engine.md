---
status: approved
created: 2026-11-01
depends-on: [03]
built-on: cae3bcdf3b23
---

# Claude Engine Implementation Plan

**Goal:** Serve DSPy calls through the user's installed `claude` CLI, isolated and metered, on their
own login.

**Architecture:**
- `ijob_ai.llm.claude_cli` resolves and version-checks the CLI path (spec §3.3).
- `ijob_ai.llm.claude_engine` holds `ClaudeAsyncEngine` and `ClaudeSyncEngine`, plus
  `claude_engines(ref, cli_path)`.
  - Plan 08 binds the resolved path with `functools.partial` and hands the result to plan 03's
    `Registry`, whose factory type is `Callable[[ModelRef], tuple[Engine, AsyncEngine]]`.
- The engine:
  - builds the fixed isolation options;
  - runs one `claude_agent_sdk.query()` per request;
  - meters every `ResultMessage` into the current `CallMeter`, marking it `completed`;
  - maps SDK errors to `LlmError`s.
- `query` is injectable, for tests.

**Tech Stack:** Python 3.14, claude-agent-sdk 0.2.176 (sdist), dspy lm15 types, pytest, shell-script
fake CLIs.

---

## Files that change

```
packages/ijob-ai/src/ijob_ai/llm/claude_cli.py                 — [create] resolve_cli, check_version
packages/ijob-ai/src/ijob_ai/llm/claude_engine.py              — [create] options builder, engines, factory
packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_cli.py    — [create]
packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py — [create]
```

## Order of work

### Task 1 — Resolve and version-check the CLI

**Files:** Create `llm/claude_cli.py`; Test `llm/test_claude_cli.py`.

**Change:**
- `resolve_cli(configured: Path | None, which=shutil.which) -> Path`:
  - takes the configured path (with `~` expanded), or else `which("claude")`;
  - raises `ProviderUnavailableError` when the result is None or not an executable file;
  - never returns None.
- `async check_version(path, *, timeout_s=10) -> tuple[int, int, int]`:
  - runs `<path> --version` and parses the leading `<major>.<minor>.<patch>`;
  - raises `ProviderUnavailableError` on a non-zero exit, a timeout, unparsable output, or a version
    below `(2, 1, 248)`.

**Proof:** written first, seen failing, then passing. Fake CLIs are shell scripts in `tmp_path`.
- `test_resolve_uses_configured_path`: a configured executable script is returned even when the
  `which` stub would return another path.
- `test_resolve_falls_back_to_which`: with no configured path, the `which` stub's path is returned.
- `test_resolve_refuses_when_which_finds_nothing`: a `which` stub returning None raises
  `ProviderUnavailableError`.
- `test_resolve_refuses_non_executable`: a configured file without the execute bit raises
  `ProviderUnavailableError` naming the path.
- `test_check_version_parses`: `2.1.316 (Claude Code)` gives `(2, 1, 316)`.
- `test_check_version_too_old`: `2.1.247 (Claude Code)` raises, with `2.1.247` in the message.
- `test_check_version_unparsable`: `banana` raises.
- `test_check_version_nonzero_exit`: a script exiting 3 raises.
- `test_check_version_timeout`: a script sleeping 5 s, with `timeout_s=0.2`, raises in under 2 s.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_cli.py`
passes and the CLAUDE.md gate is green. Commit `feat(ai): resolve and check the claude CLI`.

### Task 2 — Isolation options and request mapping

**Files:** Create `llm/claude_engine.py`; Test `llm/test_claude_engine.py`.

**Change:**
- `build_options(*, cli_path, model, system_prompt, cwd) -> ClaudeAgentOptions` returns exactly spec
  §3.3's option set, including:
  - `env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}`;
  - `extra_args={"no-session-persistence": None}`;
  - `verbatim_prompts=True`;
  - no `skills`.
- `request_prompt(request) -> tuple[str, str]` returns the system text and the prompt text. It raises
  `ProviderFailedError("claude-sdk engine takes one text user message")` for zero or several
  messages, a non-user role, or a non-text part.

**Proof:** written first, seen failing, then passing:
- `test_build_options_argv`: the result of
  `SubprocessCLITransport(prompt="x", options=build_options(cli_path=Path("/nonexistent/claude"), model="claude-haiku-5-5", system_prompt="S", cwd=tmp_path))._build_command()`
  equals spec §3.3's argv list.
- `test_build_options_env_blanks_keys`: `options.env == {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}`.
- `test_request_prompt_single_user_text`: system `S` with one user text `P` gives `("S", "P")`.
- `test_request_prompt_rejects_other_shapes`: parametrized over two messages, an assistant role, and
  a non-text part. Each raises `ProviderFailedError`.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py`
passes and the gate is green. Commit `feat(ai): build isolated claude options`.

### Task 3 — The engines and the factory

**Files:** Modify `llm/claude_engine.py`; Test `llm/test_claude_engine.py`.

**Change:**
- `ClaudeAsyncEngine(model_ref, cli_path, *, query=claude_agent_sdk.query)`:
  - `complete(request)` creates a `TemporaryDirectory` as `cwd`, removed when the call ends;
  - runs `query(prompt=…, options=build_options(…))`;
  - records the `AssistantMessage.model`;
  - on the `ResultMessage`, marks `current_meter()` completed and fills it with `total_cost_usd` and
    the tokens, whether `is_error` or not;
  - raises `CostUnknownError` when the cost is None, `ProviderFailedError` when `is_error`, and
    `TaskOutputError` when `result` is None;
  - otherwise returns an lm15 `Response`: text message, usage, assistant model, and
    `provider_data={"usage": {"cost": total_cost_usd}}`.
- SDK errors map per spec §3.3's table: `CLINotFoundError` → `ProviderUnavailableError`; the other
  four → `ProviderFailedError`.
- `stream` raises `NotImplementedError`. `aclose` is a no-op.
- `ClaudeSyncEngine.complete` raises `ProviderUnavailableError("claude-sdk runs async only")`.
- `claude_engines(ref, cli_path) -> tuple[ClaudeSyncEngine, ClaudeAsyncEngine]`.

**Proof:** written first, seen failing, then passing. Fakes are async generators yielding the SDK's
real `AssistantMessage` and `ResultMessage` dataclasses.
- `test_complete_success_meters_and_returns`: the response text, usage, model and
  `provider_data["usage"]["cost"]` match the fake, and the meter is completed with that cost.
- `test_complete_is_error_meters_then_raises`: the meter holds the cost, and `ProviderFailedError` is
  raised.
- `test_complete_none_cost_raises_cost_unknown`: `total_cost_usd=None` leaves the meter completed
  with cost None, and raises `CostUnknownError`.
- `test_complete_none_result_raises_task_output`: `result=None` with a cost raises
  `TaskOutputError`, with the cost metered.
- `test_sdk_errors_mapped`: parametrized over the five SDK error rows; each raises the mapped error.
- `test_cwd_temp_dir_removed`: the directory passed as `cwd` is gone after `complete`.
- `test_sync_engine_refuses`: `ClaudeSyncEngine().complete(request)` raises
  `ProviderUnavailableError`.
- `test_dspy_lm_accepts_engine_pair`: a `dspy.LM("claude-sdk/m", engine=sync, async_engine=async_, cache=False, num_retries=0)`
  with a fake query answers a `Predict.acall` through `run_call` (plan 03), and `budget.spent`
  equals the fake cost.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py`
passes and the gate is green. Commit `feat(ai): add claude-sdk engine`.

### Task 4 — Guard: cancellation kills the CLI child

**Files:** Test `llm/test_claude_engine.py`.

**Change:** no source change. This is a guard test for spec §3.3's timeout claim, which the
`cancel.py` probe already showed. It runs through the real SDK transport.

**Proof:**
- `test_timeout_kills_child`: a fake CLI script answers `-v`/`--version` with `2.1.316 (Claude Code)`,
  and otherwise writes its pid to a file and sleeps 60 s. `ClaudeAsyncEngine.complete` under
  `asyncio.timeout(1)` ends within 10 s, and `os.kill(pid, 0)` then raises `ProcessLookupError`.

**Done when:** `uv run --locked pytest packages/ijob-ai/tests/ijob_ai_tests/llm/test_claude_engine.py`
passes and the gate is green. Commit `test(ai): guard claude CLI cancellation`.

## Risks

- **Riskiest step:** Task 3's message dataclass construction. Fakes build the SDK's real
  `AssistantMessage` and `ResultMessage` (`types.py:1378,1695`), so a field change in the SDK breaks
  the tests instead of failing silently in production.
- **Slow test.** Task 4 takes about 7 s because of the SDK's graceful shutdown (spec §3.3 probe).
- **Not done:** any real model call in tests (CLAUDE.md).

## Proof

- `uv run --locked pytest packages/ijob-ai` passes.
- The gate exits 0.
