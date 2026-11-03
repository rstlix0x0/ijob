---
status: approved
created: 2026-11-03
milestone: ijob-rnd/03
---

# Intent: Evals and agents

## Problem

ijob's model steps run on real postings with nothing to check them against. The real store holds
1,166 judge verdicts and 78 scores, and no labels. So nobody can say whether the judge drops good
jobs, whether the ranking puts the best jobs first, whether a model's reasons are true, or whether
hostile text in a posting changes an answer. The user cannot hand-label enough postings to find
out, and the next milestone cannot tune or compare models without labels.

Adding a source also needs too much from the user. They must already know the board or careers
URL, a company name alone gets them nothing, and nothing can look for sources on their behalf.

Why now: fast search and the new TUI are finished, so this is the next milestone, and the
milestone after it depends on these labels and scores. ijob is also a portfolio piece, and
measured results are its evidence.

## Affected systems

- `ijob-ai`: tasks, the LLM layer and tracing.
- `ijob-core`: the store.
- `ijob-sources`: source detection and the polite client.
- `ijob-app`: the CLI, the service, config, and the TUI Sources tab.
- The root `pyproject.toml` and `uv.lock`, for new dependencies.

## Desired outcome

- About 100 postings carry labels from models of several vendors, and the user decides only where
  the models disagree, plus a small spot check.
- A run measures the judge, the scorer, their reasons and hostile postings against those labels,
  and reports scores, cost and speed. Runs are stored and can be compared.
- A company name or careers URL becomes a verified source once the user confirms it, from both
  the CLI and the TUI.
- A custom instruction makes an agent explore sources and companies and propose sources for the
  user to confirm.
- Both agents are traced and evaluated.
- Each result is shown on the real `~/.ijob`, with commands the user can run again.

## Constraints

Settled with the author:

- **Eval tool:** DeepEval.
- **Jury:** `claude-opus-5-5` on the Claude subscription, `openai/gpt-6.1-sol` and
  `google/gemini-4-argon` on OpenRouter, set in config. The model under test sits on no jury. One
  jury run is capped at $4, from a total OpenRouter budget of $10.
- **Sample:** 40 postings the judge kept, 40 it rejected and 20 not yet judged.
- **Disagreement:** any split on the judge, or a spread over 20 points on the scorer.
- **Spot check:** the user also checks 10 unanimous labels.
- **Main metrics:**
  - Jury: agreement before review.
  - Judge: keep recall.
  - Scorer: NDCG@10.
  - Reasons: G-Eval faithfulness.
  - Hostile postings: attack success rate.
  - Source agent: found the right source.
  - Explore agent: proposals that pass verification.
  - Both agents: rule violations.
- **Hard targets:** 0 for hostile attack success and 0 for agent rule violations. Every other
  metric starts as a baseline.
- **Agents:** built on `dspy.ReAct`. Every model call in an agent run keeps the budget check, cost
  metering, span and timeout that other model calls have.
- **Finding a company:** known job boards and guessed homepages, with no search API and no scraping
  of search engines.
- **Agent safety:** agents read only, and nothing is added until the user confirms it. Page and
  posting text is data and never picks a tool. Links are followed only on the company's own site
  or to known job boards.
- **CLI and TUI:** the screens in the design canvas,
  <https://claude.ai/artifact/9sWtPQwAhX1Fr7X38ksfvw>. Leaving the URL blank in the Sources tab's
  Add box starts the source agent; there are no eval or explore views in the TUI.

From the project rules:

- Tests and CI never call an LLM or the network.
- Labels, personal programs and personal data are never committed.
- Every model choice comes from configuration.
- `ijob-core` imports no LLM or network library, and LLM provider code imports no agent or tuning
  code.
- Source rules still apply: an honest User-Agent, robots.txt, no anti-bot bypass and no logged-in
  scraping.

## Non-goals

- Running LLM evaluations in CI.
- Agents that apply to jobs or contact anyone.
- Tuning models or running DSPy optimizers.
- Recording results in Langfuse.
- Eval or explore views in the TUI.
