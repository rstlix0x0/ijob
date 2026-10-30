# ijob

An agentic job-hunting CLI and TUI for software engineers.

## What it does

ijob is in early development. None of the features below exist yet; they describe what is planned.

- Collect postings from hiring-system APIs (Greenhouse, Lever, Ashby and others), job boards and
  URLs the user adds.
- Filter by role, posting date, company, salary, country and free-form custom instructions.
- Rank postings against a PDF CV and explain each ranking.

## Engineering highlights

Planned:

- **DSPy modules behind a provider registry.** Two providers: the Claude Agent SDK, which uses the
  local Claude Code login, and OpenRouter, through `OPENROUTER_API_KEY`. Each task picks its model
  from configuration.
- **DeepEval evaluations** of every LLM step, run locally and never in CI.
- **GEPA optimization of cheap models**, measured against a frontier-model baseline on a labeled
  set, to show how close an optimized small model gets.
- **OpenTelemetry tracing** into a local Langfuse stack.
- **Polite sourcing.** ijob sends its own User-Agent, follows robots.txt with longest-match
  semantics, and never bypasses anti-bot protection.

## Status

Early development. The work is split into seven milestones:

1. Foundation: the workspace, the quality gate, CI and project docs.
2. Core and sources: the domain model, local storage, polite HTTP, and the first source adapters
   behind a search command that uses no LLM.
3. TUI: a terminal interface over search and results.
4. LLM layer: providers, CV profiling, judging and scoring.
5. Quality loop: evaluations, tracing and prompt optimization.
6. Agents: source discovery and agents that follow custom instructions.
7. Reach: more sources, headless page extraction and a CV coaching loop.

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
