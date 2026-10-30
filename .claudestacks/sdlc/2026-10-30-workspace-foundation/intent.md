---
status: done
created: 2026-10-30
spec: skipped
milestone: ijob-v1/01
---

# Intent: Workspace foundation

skip: scaffolding only; every design choice was settled in the roadmap discussion.

## Problem

ijob has no code yet: the repository holds only SDLC artifacts. Every later milestone (sources,
TUI, LLM layer, quality loop, agents) needs a workspace that already builds, type-checks and tests
cleanly, so feature work starts from a green gate instead of fighting setup. The source research
done so far (job-board catalog, hiring-system endpoints, ~235-company seed list) lives only in a
temporary session scratchpad and will be lost unless it is persisted in the repository.

## Affected systems

- The whole repository root (project configuration, licence, README, CLAUDE.md, ignore rules).
- Four new workspace members: `ijob-core`, `ijob-sources`, `ijob-ai`, `ijob-app`.
- `docs/research/` (new), seeded from the session scratchpad research files.
- CI configuration (new).
- Local observability setup (Langfuse via Docker Compose, optional).

## Desired outcome

- `uv sync --locked --all-groups --all-packages`, `ruff format --check`, `ruff check --no-fix`,
  `mypy --strict .` and `pytest` all pass locally and in CI on Python 3.14.
- Each member imports as `ijob_core`, `ijob_sources`, `ijob_ai`, `ijob_app`, with at least one
  passing test each.
- An Apache-2.0 LICENSE, a portfolio-first README and a hand-written CLAUDE.md exist.
- `docs/research/` holds the source catalog, hiring-system endpoint notes and the company seed list.
- The reference-programs location in `ijob-ai` exists.
- `compose.yaml` starts a local Langfuse stack; `.env.example` documents the OTLP endpoint,
  Langfuse keys and `OPENROUTER_API_KEY`; ijob runs without Docker.

## Constraints

- Follow the claudestacks python guideline (strict quality baseline, pinned tool versions).
- CI never calls an LLM and never needs secrets.
- No secrets, personal data, tuned personal programs or labels are committed.
- Nothing is committed without the user's explicit request.

## Non-goals

- Any feature code (domain model, sources, TUI, LLM providers, evals, agents).
- Wiring OpenTelemetry into code; only the local Langfuse stack and its env documentation.
- Publishing packages.
