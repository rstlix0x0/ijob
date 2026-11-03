# ijob

A tool for finding jobs in any field, and an R&D project and portfolio piece on building agentic
systems and apps with LLMs.

## Use it

```bash
uv run ijob setup                            # create ~/.ijob
uv run ijob search --title "product manager" # saved jobs at once, fetching beside them
uv run ijob cv set cv.pdf                    # read your CV
uv run ijob search --title golang --score    # best first, as each score arrives
uv run ijob tui                              # the terminal UI
uv run ijob sources find "Example Co"        # an agent finds where a company lists its jobs
uv run ijob explore "fintech in Tokyo"       # an agent proposes sources to add
```

To measure the models and the agents, label postings with a jury, then run the suites. A run that
calls an OpenRouter model asks first.

```bash
uv run ijob eval label --limit 10            # a jury of models votes on a sample of postings
uv run ijob eval review                      # settle the cases the jury split on
uv run ijob eval run judge                   # also scorer, reasons, hostile,
                                             # source-agent and explore-agent
uv run ijob eval report                      # the stored runs, side by side
```

## What it explores

- **Collecting at scale.** Job postings from hiring-system APIs, job boards and feeds, read politely
  and stored locally.
- **LLM tasks as DSPy signatures, behind a provider registry.** Reading a CV, turning instructions
  into filters and rules, judging and scoring postings, reading free-form posts as jobs, and
  learning how to read a new careers page. Two providers: the Claude Agent SDK, which uses the local
  Claude Code login, and OpenRouter. Each task picks its model from configuration, with a cost cap
  per operation.
- **Agents with hard rules.** A source agent turns a company name or careers URL into the one source
  that lists its jobs, and an explore agent turns an instruction into sources worth adding. Both
  are ReAct loops. Their tools stay on the company's site and its hiring board, and an answer counts
  only if a tool verified it.
- **Evals.** A jury of models labels a sample of postings and you settle the rest. Eval suites
  measure the judge, the scorer, the reasons they give, their resistance to injected text and the
  two agents. Committed cases hold no personal data; your labels stay in `~/.ijob`.
- **Tracing.** Every model call emits OpenTelemetry spans with model, tokens and cost, kept in a local
  store that `ijob traces` reads, with optional export to a local Langfuse.
- **Polite sourcing.** ijob sends its own User-Agent, follows robots.txt with longest-match
  semantics, and never bypasses anti-bot protection.

## Research milestones

1. Fast ranked search: results for any role in under a second, narrowed by title, then scored
   against a CV and ranked.
2. TUI rewrite: a new interface that opens at once and never freezes.
3. Evals and agents: a jury of models and your review label cases, DeepEval suites measure the
   judge, the scorer and their reasons, and a source agent (`ijob sources find`) and an explore
   agent (`ijob explore`) find sources and follow custom instructions, each with a suite of its
   own.
4. Optimization and observability: GEPA and other DSPy optimizers tuning cheap models against a
   frontier baseline, with the runs recorded in Langfuse.

## Workspace layout

A uv workspace with four members under `packages/`:

| Member | Import | Responsibility |
|---|---|---|
| `ijob-core` | `ijob_core` | Domain model, storage and the search pipeline |
| `ijob-sources` | `ijob_sources` | Job-source adapters and polite HTTP |
| `ijob-ai` | `ijob_ai` | LLM providers, DSPy modules, agents and tuning |
| `ijob-app` | `ijob_app` | CLI and TUI |

## Development

The only prerequisite is [uv](https://docs.astral.sh/uv/). It installs Python 3.14 and every tool.
From the repository root, all five commands must pass:

```bash
uv sync --locked --all-groups --all-packages
uv run --locked ruff format --check
uv run --locked ruff check --no-fix
uv run --locked mypy --strict .
uv run --locked pytest
```

CI runs the same commands. Tests never call an LLM or the network.

Tracing is optional. To run a local Langfuse stack, copy `.env.example` to `.env`, then run
`docker compose up -d`. The web UI is at <http://localhost:3000>. ijob runs without it.

## Sources policy

- LinkedIn, Indeed, Glassdoor and ZipRecruiter are excluded.
- JobStreet is only linked to, never fetched.
- No logged-in scraping and no anti-bot bypass.

The research behind these rules is in [`docs/research/`](docs/research/).

## License

Apache-2.0. See [`LICENSE`](LICENSE).
