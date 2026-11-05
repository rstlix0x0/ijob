---
status: done
created: 2026-11-04
depends-on: [12, 13, 14]
built-on: 671aa742d4fc
---

# Tuning docs Implementation Plan

**Goal:** Explain the tuning commands and the report to someone who has not read the spec.

**Architecture:** A new `docs/tuning.md` carries the flow of spec section 2, and how to read the report. `docs/evals.md` gains the `--add` and `hostile` notes. `README.md` gains the commands and milestone 4's wording as built. It claims no results, since real runs come after this set.

**Tech Stack:** Markdown.

**Content authority:** spec section 14, and spec section 18 as the list that follows this set.

---

## Files that change

```
docs/tuning.md                                            — create: commands and how to read the report
docs/evals.md                                             — modify: --add and hostile notes
README.md                                                 — modify: commands; milestone 4 as built
packages/ijob-app/tests/ijob_app_tests/test_repo_files.py — modify: the docs name every command
```

## Order of work

### Task 1 — Write the docs

**Files:**
- Create `docs/tuning.md` — the ten steps of spec section 2 in order, the three optimizers, the cost limits (the run cap and the ledger), why cost comes from ijob and not Phoenix (P25), how to read `Choosing` and `Held-out`, and what `tune accept` checks.
- Modify `docs/evals.md` — `--add` with its draw and spot counts; a 200-case run is not compared with a 100-case one; `hostile` runs again on `judge-2`; a hostile row with a program stays out of `eval report`'s table.
- Modify `README.md` — the tune commands, a link to `docs/tuning.md`, and milestone 4's wording as built, with no result claimed.
- Modify `packages/ijob-app/tests/ijob_app_tests/test_repo_files.py`.

**Change:** No code. The docs use the commands as built, and contain no personal data, no prompt text and no model results.

**Proof:**
- `test_tuning_doc_names_every_tune_command` — `screen`, `run`, `report`, `list`, `accept` and `clear` each appear, and each is a command `ijob tune --help` lists.
- `test_evals_doc_names_add` — `eval label --add` appears.
- `test_readme_links_the_tuning_doc` — the link resolves to the file.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_repo_files.py` passes and the Definition of Done is green. Commit `docs(repo): explain tuning and its report`.

---

## Risks

- Spec section 18's list spends money, needs the user's votes in `eval review` and their pick of `[tune].models`, and starts Docker, so `execute` does not run it. It runs after this set, with the user.
- Real tuning results, costs and timing are therefore unknown when this plan ends, and the docs claim none.

## Proof

- `uv run --locked pytest` passes.
- The five commands in `CLAUDE.md` exit 0.
- After `execute`, the user and agent run the list in spec section 18 on the real `~/.ijob`, before calling the milestone done. Step 7 reads the UI in a browser. If fonts or layout break, `PHOENIX_ALLOW_EXTERNAL_RESOURCES` comes out of `compose.yaml` (spec section 12).

## Review findings

`docs/tuning.md` was checked against the source by the coder that aligned it, and by no reviewer. The README was reviewed with plan 14's command, and its three blocking lines were fixed, as recorded there.

## Deviations

- 2026-11-05: `docs/tuning.md` shows the built column names, which differ from the sample in spec section 10: the built `tune report` has more columns and names the baseline `[tune].baseline`.
- 2026-11-05: the doc's estimate on the `--add` line is left out, because it is a cost claim.
- 2026-11-05: the docs were corrected after the review fixes of plans 11 and 13 changed behaviour.

## Gate

Five commands in `CLAUDE.md` on the tree holding plans 11 to 15, 2026-11-05: format (355 files), check and mypy (311 files) clean; `5338 passed in 468.25s`. This covers all five plans; none ran the gate alone. No real model, real data, Docker or live Phoenix was used, so spec section 18 is still to do.
- 2026-11-05: `test_tuning_doc_names_every_tune_command` reads the commands from the Typer app, not from rendered `--help` text, which depends on terminal width.
