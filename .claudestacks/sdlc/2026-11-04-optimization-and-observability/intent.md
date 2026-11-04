---
status: approved
created: 2026-11-04
milestone: ijob-rnd/04
---

# Intent: Optimization and observability

## Problem

ijob now measures its model steps, but it cannot improve them or show the evidence. On the real
labels Haiku's judge keeps 0.90 of the jobs it should and is right about 0.53 of the jobs it keeps,
and nothing can tune a model against those labels. No program can be saved, loaded or compared, so
a tuned model could not reach `ijob search` even if one existed.

The labels are also too few for a fair test. The 100 labelled postings hold 21 keeps, so a held-out
set would hold about 8, and one posting would move recall by about 0.12. The only frontier model
that sat on no jury is Sonnet, and its judge run predates the parser fix while its scorer has never
run, so there is no valid baseline yet.

The evidence is hard to see. Runs and traces sit in two SQLite files, and eval results are not
linked to their traces. The local Langfuse the project planned on is too heavy for the user's
machine, with 8 GiB of memory and six containers, and its export has never run. Optimizer calls
would also miss the budget check, cost meter, span and timeout that every other model call has.

Why now: milestone 3 produced the labels and scores this work needs, this is the last milestone,
and ijob is a portfolio piece whose measured results are its evidence.

## Affected systems

- `ijob-ai`: tasks, the LLM layer, tracing and a new tuning area.
- `ijob-core`: the store, for program, run and trace records.
- `ijob-app`: the CLI, the service, config, the eval suites and reports, and job search.
- The root `pyproject.toml` and `uv.lock`, for new dependencies.
- `compose.yaml`, `.env.example`, `README.md`, `docs/` and `packages/ijob-ai/src/ijob_ai/programs/`.

## Desired outcome

- About 100 more postings carry labels, adding about 38 keeps, with the user deciding only where
  the jury disagrees plus a spot check.
- Six cheap models from several vendors are measured untuned, so the best can be chosen on evidence.
- Each optimizer tunes each chosen cheap model for the judge and the scorer under a real spending
  cap.
- A committed report compares the six untuned models, and tuned and untuned chosen models with the
  Sonnet baseline, on postings no optimizer saw, with scores, margins, cost and speed. It holds no
  personal data.
- Once the user accepts a tuned program, `ijob search` uses it.
- Traces, eval scores and optimizer runs can be sent to a local Phoenix and compared there, including
  the runs already stored.
- Each result is shown on the real `~/.ijob`, with commands the user can run again.

## Constraints

Settled with the author:

- **Viewer:** Arize Phoenix in one Docker container through OrbStack, replacing Langfuse. It starts
  only to receive stored data and to show it, and never has to run during tuning. Local stores stay
  the source of truth, so ijob works without it.
- **Phoenix proof:** probed on the real data on 2026-11-04 with image 21.0.0: 616 MiB holding 45
  traces and 4,583 spans, with a dataset, experiments, scores and trace links through its REST API.
  Its port stays on localhost and its telemetry stays off. Its own cost total ($3.70 against ijob's
  $12.93) is not trusted; cost comes from ijob's records.
- **More labels:** about 100 more postings, mostly from those the judge kept, using the same jury
  and disagreement rules as milestone 3. About $2 of OpenRouter and about 50 decisions.
- **Optimizers:** GEPA, BootstrapFewShotWithRandomSearch and MIPROv2.
- **Models:** six cheap OpenRouter models, set in config, are first measured untuned on every
  labelled posting: `z-ai/glm-5.3-flash`, `openai/gpt-5-nano`, `deepseek/deepseek-v4.1-flash`,
  `qwen/qwen3.8-flash`, `mistralai/mistral-small-2603` and `openai/gpt-5.4-nano`. The two best on
  score for cost go on to tuning with Haiku, on the judge and the scorer. That is 18 tuning runs,
  against a Sonnet baseline. Jurors are not baselines on their own labels.
- **Budget:** the $10 OpenRouter total stands, with $2.20 spent. About $5.80 is left for models
  after the new labels: about $0.64 for the untuned measure and at most about $4.46 for tuning, by
  list prices. Every call an optimizer makes,
  reflection included, counts against a cap enforced on actual cost. The GEPA reflection model
  stays on the Claude subscription, because its prompts carry the user's rules and postings.
- **Evaluation:** tuned programs are scored only on postings held out from all tuning.
- **Search:** a tuned program is used only after the user accepts it and it has passed the hostile
  suite with 0 attacks. A program must never lose the user's rules or the statement that input is
  untrusted data.
- **Reference programs:** dropped. Tuned programs stay in the ijob home, and the promise of programs
  tuned on a public demo dataset comes out of the docs.

From the project rules:

- Tests and CI never call an LLM or the network.
- Labels, personal programs and personal data are never committed.
- Every model choice comes from configuration.
- `ijob-core` imports no LLM or network library, and LLM provider code imports no agent or tuning
  code.

## Non-goals

- Committing personal tuned programs or labels.
- A hosted observability service.
- Running Langfuse or keeping its stack in the repository.
- A public demo dataset or reference programs.
- Optimizers or tuning views in the TUI.
- Tuning tasks other than the judge and the scorer.
