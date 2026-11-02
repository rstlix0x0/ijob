---
status: approved
created: 2026-10-30
---

# Milestone: LLM layer and what follows

Merged on 2026-11-02 from milestones 04 LLM layer, 05 Quality loop, 06 Agents and 07 Reach.

## Goal

**LLM layer.** Every LLM step runs through `dspy.LM` with a ProviderRegistry that routes each task to
a model: a custom `ClaudeEngine` (DSPy engine interface over claude-agent-sdk, isolated from user
settings) and OpenRouter. A PDF CV becomes a ResumeProfile. Custom instructions are parsed into
filters plus a judge. Postings are judged and scored against the CV with reasons, shown in both CLI
and TUI. Every LLM call emits OpenTelemetry spans (GenAI attributes: model, tokens, cost, latency).
Tracing is always on: spans are always kept in a local SQLite span store viewable with `ijob traces`,
and also exported to the local Langfuse when an endpoint is configured; ijob's code uses only the
OTel SDK.

**Quality loop.** DeepEval suites (deterministic in CI, LLM judge on demand, with a custom judge model
on ijob's providers, routed through OpenRouter) cover the judge, scorer and CV features, and run
without Docker. `ijob bench` runs a suite across task models from several vendors (via OpenRouter)
under a fixed judge and records each run (model, scores, cost, tokens, latency, dataset and program
version) for side-by-side comparison. DeepEval scores and GEPA runs are recorded to Langfuse when it
is running. `ijob tune` runs GEPA from labels with a cost estimate, a budget cap and a held-out test
gate. Reference programs tuned on a public demo dataset are committed with a report showing whether
optimized cheap models (BootstrapFewShot, GEPA) approach a frontier baseline on the relevance judge,
using a ~200-pair demo set labeled by a multi-vendor LLM jury, within a $10 OpenRouter budget.

**Agents.** A source-discovery agent turns a company name or careers URL into a verified source
(detects the hiring system, probes it, checks identity, registers it). A custom-instruction ReAct
agent explores and proposes sources and companies. Both are evaluated with DeepEval agent metrics and
traced.

**Reach.** Tier C adapters (Workday, Workable with request spacing, HERP/HRMOS/Talentio), a headless
extractor agent for custom JavaScript career pages (Tier D), the interactive CV coach loop, and
JobStreet deep-links are in place.

## Non-goals

- LLM calls in CI.
- Weight fine-tuning.
- Applying to jobs automatically.
- Bypassing anti-bot protection.
