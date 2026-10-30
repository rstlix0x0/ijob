# ijob: instructions for coding agents

## Gate

Run these five commands from the repository root. All must exit `0` before a change is done:

```bash
uv sync --locked --all-groups --all-packages
uv run --locked ruff format --check
uv run --locked ruff check --no-fix
uv run --locked mypy --strict .
uv run --locked pytest
```

## Layout

- The workspace has four members under `packages/`: `ijob-core`, `ijob-sources`, `ijob-ai`,
  `ijob-app`.
- Imports are flat: `ijob_core`, `ijob_sources`, `ijob_ai`, `ijob_app`.
- Tests live in `packages/<member>/tests/<import>_tests/`.
- Tool configuration lives only in the root `pyproject.toml`. Members carry no `[tool.*]` tables.
- The claudestacks python guideline governs style.

## Boundaries

- `ijob-core` imports no LLM library and no network library.
- LLM provider code does not import agent or tuning code.

## Testing

Tests and CI never call an LLM or the network. Use fakes and recorded fixtures.

## Data and secrets

Never commit any of these:
- secrets or `.env`
- personal tuned programs or labels
- personal data

## Sources

- Send ijob's own honest User-Agent.
- Follow robots.txt with longest-match semantics.
- No anti-bot bypass and no logged-in scraping.
- JobStreet is deep-link only.
- JapanDev is read only through its sitemap and JSON-LD.

## Models

Every model choice comes from configuration. Never hard-code one.

## Commits

Commit only when the user asks.
