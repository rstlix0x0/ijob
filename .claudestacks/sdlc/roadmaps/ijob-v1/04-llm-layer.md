---
status: approved
created: 2026-10-30
---

# Milestone: LLM layer

## Goal

Every LLM step runs through `dspy.LM` with a ProviderRegistry that routes each task to a model:
a custom `ClaudeEngine` (DSPy engine interface over claude-agent-sdk, isolated from user settings)
and OpenRouter. A PDF CV becomes a ResumeProfile. Custom instructions are parsed into filters plus a
judge. Postings are judged and scored against the CV with reasons, shown in both CLI and TUI. Every
LLM call emits OpenTelemetry spans (GenAI attributes: model, tokens, cost, latency). Tracing is
always on: spans are always kept in a local SQLite span store viewable with `ijob traces`, and also
exported to the local Langfuse when an endpoint is configured; ijob's code uses only the OTel SDK.

## Non-goals

- Tuning and evals.
- Agents.
