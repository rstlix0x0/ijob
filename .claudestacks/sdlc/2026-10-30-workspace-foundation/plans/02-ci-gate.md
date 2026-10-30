---
status: done
created: 2026-10-30
depends-on: [01]
built-on: cc0c09ea9127
---

# CI Gate Implementation Plan

**Goal:** Every push and pull request runs the python guideline gate in GitHub Actions without secrets or LLM calls.

**Content authority:** `intent.md` of this chain (spec skipped).

**Architecture:** One workflow file runs on `ubuntu-latest`, installs uv with the official
`astral-sh/setup-uv` action (pinned to a full commit SHA, uv version `0.13.6`), lets uv provide
CPython from `.python-version`, and runs the five gate commands exactly as the guideline states
them. The workflow declares read-only `contents` permission and references no secret.

**Tech Stack:** GitHub Actions, `actions/checkout`, `astral-sh/setup-uv`, uv 0.13.6.

---

## Files that change

```
.github/workflows/ci.yml — [create] the gate workflow
```

---

### Task 1 — Gate workflow

**Files:**
- Create `.github/workflows/ci.yml`

**Change:** a workflow named `ci`, triggered on `push` and `pull_request`, with
`permissions: contents: read` and a single job `gate` on `ubuntu-latest` that:
1. checks out the repository with `actions/checkout` (pinned to a full commit SHA);
2. installs uv `0.13.6` with `astral-sh/setup-uv` (pinned to a full commit SHA), with uv's cache enabled;
3. runs, as separate named steps, `uv sync --locked --all-groups --all-packages`,
   `uv run --locked ruff format --check`, `uv run --locked ruff check --no-fix`,
   `uv run --locked mypy --strict .`, `uv run --locked pytest`.

No step uses `secrets.*` and no `env` sets an API key. That tests never call an LLM is guaranteed by
the tests themselves (fakes and recorded fixtures), not by the workflow.

**Proof:** no Python behaviour changes, so no unit test. Checks:
- The YAML parses (syntax only; the first real run after a push proves GitHub accepts it):
  `uvx --from 'pyyaml==6.0.3' python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
  exits `0`.
- `grep -n 'secrets\.' .github/workflows/ci.yml` prints nothing.
- The five gate commands in the workflow are character-identical to `SKILL.md` § Definition of Done
  (workspace form).

**Done when:** the checks pass and the local gate is still green. Commit `ci: run the quality gate on push and pull request`.

---

## Risks

- **The workflow cannot run until the repository has a GitHub remote**; its first real run happens
  after the user pushes. Local checks above are the proof available now.
- **Action SHAs:** pin each action to the full SHA of its latest release at execution time and keep
  the release tag in a trailing comment; a tag-only reference is rejected.
- **Not done here:** release or publish workflows, dependency-update bots, LLM eval jobs (never in CI).

## Proof

The YAML parses, contains no secret reference, and its gate steps match the guideline's commands;
the local gate is green.

## Review findings

- security (🔵) — `actions/checkout` kept `persist-credentials: true` — `.github/workflows/ci.yml:16`. Fixed: `persist-credentials: false`. Verified: pyyaml 6.0.3 parses the file (`yaml-ok`).
- ci-hygiene (🔵) — no branch filter, `concurrency` group or `timeout-minutes` — `.github/workflows/ci.yml:3`. Not applied (YAGNI).
- unguarded (🔵) — nothing catches SHA pins reverting to tags or going stale — `.github/workflows/ci.yml:16,19`. Held by review.

## Probe results

- Latest releases and their commits — `gh api repos/<r>/releases/latest` then `gh api repos/<r>/commits/<tag>` — `actions/checkout v7.0.1 3d3c42e5aac5ba805825da76410c181273ba90b1`, `astral-sh/setup-uv v10.3.3 68559e99cc807c1eb7e9238fd43a15bba460eec2`.
- setup-uv inputs `version` and `enable-cache` exist at the pinned SHA — `gh api 'repos/astral-sh/setup-uv/contents/action.yml?ref=68559e99…'` — both listed.

## Deviations

- 2026-10-30 — added `persist-credentials: false` to the checkout step (review fix).
