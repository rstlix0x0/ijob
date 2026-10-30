---
status: approved
created: 2026-10-30
built-on: cc0c09ea9127
---

# Project Docs Implementation Plan

**Goal:** The repository explains ijob, its rules and its source research to human readers and coding agents.

**Content authority:** `intent.md` of this chain (spec skipped). The engineering rules in Task 4 were
settled in the roadmap discussion and are recorded in the roadmap's vision and non-goals
(`roadmaps/ijob-v1/roadmap.md`) and milestone goals.

**Architecture:** Four documentation artefacts at fixed locations: `docs/research/` holding the source
research currently in the session scratchpad, an Apache-2.0 `LICENSE`, a portfolio-first `README.md`,
and a hand-written `CLAUDE.md` for coding agents. No code changes. This plan depends on no other plan,
so Task 1 can run first, before the session scratchpad disappears.

**Tech Stack:** Markdown, CSV.

Source of the research files (copy byte-for-byte; this path is session-temporary):
`/private/tmp/claude-501/-Users-hiraq-Projects-rstlix0x0-ijob/5f0a49ff-503b-4c75-8cf8-2a0883190512/scratchpad/research/`
containing `global_boards.md`, `asia_and_requested.md`, `ats_endpoints.md`, `companies.csv`
(header plus 235 rows).

---

## Files that change

```
docs/research/README.md              — [create] index of the research files and when they were gathered
docs/research/global_boards.md       — [create] copied: global/remote job boards catalog
docs/research/asia_and_requested.md  — [create] copied: Asia boards and user-requested sources
docs/research/ats_endpoints.md       — [create] copied: hiring-system (ATS) public endpoints
docs/research/companies.csv          — [create] copied: company seed list
LICENSE                              — [create] Apache License 2.0 full text
README.md                            — [create] portfolio-first project README
CLAUDE.md                            — [create] instructions for coding agents
```

---

### Task 1 — Persist the research

**Files:**
- Create `docs/research/global_boards.md`, `docs/research/asia_and_requested.md`,
  `docs/research/ats_endpoints.md`, `docs/research/companies.csv` (copies)
- Create `docs/research/README.md`

**Change:** copy the four files unchanged from the scratchpad path above. `docs/research/README.md`
lists each file with a one-line description, states the research date (2026-10-30), and says endpoint
and robots.txt findings may have changed since.

**Proof:** `for f in global_boards.md asia_and_requested.md ats_endpoints.md companies.csv; do cmp "<source>/$f" "docs/research/$f" || exit 1; done` exits `0`.

**Done when:** the copies are identical. Commit `docs: persist source research`.

### Task 2 — License

**Files:**
- Create `LICENSE`

**Change:** the unmodified Apache License 2.0 text from `https://www.apache.org/licenses/LICENSE-2.0.txt`.

**Proof:** `grep -q 'Version 2.0, January 2004' LICENSE && grep -q 'END OF TERMS AND CONDITIONS' LICENSE` exits `0`.

**Done when:** the check passes. Commit `docs: add Apache-2.0 license`.

### Task 3 — README

**Files:**
- Create `README.md`

**Change:** a portfolio-first README with exactly these headings, in order:
1. `# ijob` — followed by a one-line pitch: an agentic job-hunting CLI/TUI for software engineers.
2. `## What it does` — collects postings from hiring-system APIs, job boards and user-added URLs;
   filters by role, date, company, salary, country and custom instructions; ranks against a PDF CV
   with reasons.
3. `## Engineering highlights` — DSPy modules behind a provider registry (Claude Agent SDK using the
   local Claude Code login; OpenRouter via `OPENROUTER_API_KEY`); DeepEval evals; GEPA optimization
   of cheap models compared against a frontier baseline; OpenTelemetry tracing into a local Langfuse;
   polite sourcing (own User-Agent, longest-match robots.txt, no anti-bot bypass).
4. `## Status` — early development, with the seven milestones summarised in prose (foundation; core
   and sources; TUI; LLM layer; quality loop; agents; reach). No links to planning files.
5. `## Workspace layout` — the four members and one-line responsibility each.
6. `## Development` — prerequisite (uv), the five gate commands, optional `docker compose up` for Langfuse.
7. `## Sources policy` — excluded sites (LinkedIn, Indeed, Glassdoor, ZipRecruiter), JobStreet as
   deep-link only, link to `docs/research/`.
8. `## License` — Apache-2.0.

Planned features are marked as planned; nothing unbuilt is described in the present tense.

**Proof:** `for h in '# ijob' '## What it does' '## Engineering highlights' '## Status' '## Workspace layout' '## Development' '## Sources policy' '## License'; do grep -qx "$h" README.md || exit 1; done && ! grep -qi 'claudestacks' README.md` exits `0`.

**Done when:** the check passes. Commit `docs: add project README`.

### Task 4 — CLAUDE.md

**Files:**
- Create `CLAUDE.md`

**Change:** concise agent instructions under exactly these headings:
- `## Gate` — the five gate commands, character-identical to `SKILL.md` § Definition of Done in its
  workspace form (`uv sync --locked --all-groups --all-packages` first), run from the root, all must exit `0`.
- `## Layout` — the four members, flat imports `ijob_*`, tests in
  `packages/<member>/tests/<import>_tests/`, members carry no tool tables; the python guideline plugin governs style.
- `## Boundaries` — `ijob-core` imports no LLM or network library; LLM provider code does not import
  agent or tuning code.
- `## Testing` — tests and CI never call an LLM or the network; use fakes and recorded fixtures.
- `## Data and secrets` — never commit secrets, `.env`, personal tuned programs, labels or personal data.
- `## Sources` — honest User-Agent, longest-match robots.txt, no anti-bot bypass, no logged-in
  scraping, JobStreet deep-link only, JapanDev via sitemap + JSON-LD only.
- `## Models` — every model choice comes from configuration, never hard-coded.
- `## Commits` — only when the user asks.

**Proof:** `for h in '## Gate' '## Layout' '## Boundaries' '## Testing' '## Data and secrets' '## Sources' '## Models' '## Commits'; do grep -qx "$h" CLAUDE.md || exit 1; done && grep -qF 'uv sync --locked --all-groups --all-packages' CLAUDE.md` exits `0`.

**Done when:** the check passes. Commit `docs: add agent instructions`.

---

## Risks

- **Scratchpad loss:** the research source is session-temporary; Task 1 runs first and this plan has no dependency.
- **Overclaiming in the README:** Task 3's rule marks unbuilt features as planned.
- **Not done here:** architecture docs, ADRs, benchmark reports — they come with later milestones.

## Proof

All four research copies match their sources, the LICENSE is the Apache-2.0 text, and the README and
CLAUDE.md heading checks pass.
