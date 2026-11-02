---
status: approved
created: 2026-11-01
milestone: ijob-v1/04
---

# Intent: LLM layer

## Problem

ijob finds and filters postings, but it cannot tell the user which ones fit them. Milestone
ijob-v1/04 needs the first LLM features, and nothing for them exists yet. `ijob-ai` is an empty
package. In detail:

- **No LLM runtime.** There is no way to call a model, no routing of a task to a configured model,
  and no provider for either the user's Claude Code login or OpenRouter.
- **The CV is stored but never read.** M3 keeps the user's PDF CV, but nothing turns it into a
  profile a model can compare postings against.
- **Some preferences cannot be expressed.** The filters cover role, date, company, salary and
  country. Rules such as "no crypto", "remote from Indonesia, not US-only", "no heavy on-call" or
  "visa sponsorship to Japan" live only in the posting's text, and no filter can read them.
- **No fit signal.** Nothing says how well a posting matches the CV, or why.
- **No visibility into LLM use.** Without tracing, there is no record of which model ran, what it
  cost, how many tokens it used or how long it took. Milestones 05 and 06 (tuning, benchmarks,
  agents) build on that record.
- **Unbounded cost.** A broad search could send hundreds of postings to a model.

## Affected systems

- `ijob-ai`: the model runtime, the providers and the LLM tasks (CV profile, instruction parsing,
  judge, scorer).
- `ijob-core`: storage for the CV profile, judge verdicts, scores and the trace span store, through
  migrations.
- `ijob-app`: the CLI and the TUI gain the CV profile, custom instructions, judge results, on-demand
  scoring and `ijob traces`. The configuration gains per-task model routing and the spending cap.
- The root `pyproject.toml` and `uv.lock`, for the new dependencies.

## Desired outcome

- **CV to profile.** The stored PDF CV becomes a structured profile that the user can see and
  refresh from both the CLI and the TUI.
- **Custom instructions.** The user writes free-text instructions. ijob turns the parts it can into
  ordinary filters, and the rest into rules for the judge.
- **Judge, only when instructions are set.** With custom instructions, every search runs the judge on
  the postings that pass the filters. Each posting gets keep or drop plus a one-line reason. Dropped
  postings are hidden but can be viewed together with their reasons. With no instructions, the judge
  does not run, and search makes no LLM call and costs nothing, exactly as today.
- **Scorer, only on demand.** The user asks for a score on one posting or on the postings they
  select, never automatically. Each gets a 0–100 fit score against the CV profile, with reasons.
- Judge verdicts and scores appear in both the CLI and the TUI.
- **Cached.** A verdict or score is stored per posting, together with the CV profile or instructions
  and the task version it was made with. Repeating a search or a score reuses it at no cost.
- **Spending cap per command.** A configurable USD cap applies to each command run, with a default
  of $0.50. When the cap is reached the command stops cleanly and shows what it completed.
- **Tracing always on.** Every LLM call emits OpenTelemetry spans with model, tokens, cost and
  latency. Spans are always kept in a local SQLite span store that `ijob traces` shows, and are also
  exported to a local Langfuse when an endpoint is configured.
- The gate passes, and every LLM path is tested with fakes and no real model call.

## Constraints

Decisions already made. The spec honours them and does not reopen them.

- **Runtime.** Every LLM step runs through DSPy (`dspy.LM`), as a typed DSPy module. A provider
  registry routes each task to a model taken from configuration. No model is hard-coded; shipped
  defaults are only defaults.
- **Providers.** Two providers:
  - `claude-sdk`: a custom DSPy engine over claude-agent-sdk.
    - It uses the user's own Claude Code login and never bills Anthropic API credit.
    - It is isolated from the user's Claude Code settings, tools and MCP servers.
    - It runs the `claude` CLI already installed on the user's machine: the path comes from
      configuration, or else from `PATH`. The SDK's bundled copy is never installed. This decision
      is final.
  - `openrouter`: uses `OPENROUTER_API_KEY`.
  - DSPy's vendored `claude_code` provider is not used.
- **Prompt-injection defence.** Posting text, titles, notes and every other source field are
  untrusted third-party text.
  - Untrusted text goes into a delimited data block that the prompt names as data. It never takes
    instruction position.
  - The judge, the scorer, the CV profile and instruction parsing get no tools and no network.
  - Model output is parsed into a typed, validated schema. It is never executed, opened as a URL or
    rendered as markup.
  - Only the M3 normalizer's output is sent as posting text.
- **Observability.** ijob's code uses only the OpenTelemetry SDK. Tracing cannot be switched off.
  Langfuse is optional and local.
- **Cost.** Order of work: deterministic filters, then the judge, then the scorer, which runs on
  demand only. Caching and the per-command cap apply to every LLM task.
- **Testing.** Tests and CI never call an LLM or the network.
- **Data.** The CV, its profile, instructions, verdicts, scores and spans are personal data. They live
  in the ijob home and are never committed. Fixtures use a made-up CV and public postings.
- **Boundaries.** `ijob-core` imports no LLM library and no network library. LLM provider code does
  not import agent or tuning code.
- **Gate.** The python guideline gate in CLAUDE.md must pass.
- **Shape.** One chain, with several plans.

## Non-goals

- Tuning (GEPA), evals (DeepEval) and `ijob bench`, which belong to ijob-v1/05.
- Agents of any kind, which belong to ijob-v1/06 and ijob-v1/07.
- Automatic scoring of search results.
- CV revision or coaching. The CV is read, never edited.
- OCR for scanned PDFs.
- Running Langfuse for the user: no Docker stack is shipped or started.
- A daily or monthly spending total; only the per-command cap.
