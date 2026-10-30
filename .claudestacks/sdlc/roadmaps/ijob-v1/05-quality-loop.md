---
status: approved
created: 2026-10-30
---

# Milestone: Quality loop

## Goal

DeepEval suites (deterministic in CI, LLM judge on demand, with a custom judge model on ijob's
providers, routed through OpenRouter) cover the judge, scorer and CV features, and run without Docker.
`ijob bench` runs a suite across task models from several vendors (via OpenRouter) under a fixed
judge and records each run (model, scores, cost, tokens, latency, dataset and program version)
for side-by-side comparison. DeepEval scores and GEPA
runs are recorded to Langfuse when it is running. `ijob tune` runs GEPA from
labels with a cost estimate, a budget cap and a held-out test gate. Reference programs tuned on a
public demo dataset are committed with a report showing whether optimized cheap models
(BootstrapFewShot, GEPA) approach a frontier baseline on the relevance judge, using a ~200-pair
demo set labeled by a multi-vendor LLM jury, within a $10 OpenRouter budget.

## Non-goals

- Agents.
- LLM calls in CI.
- Weight fine-tuning.
