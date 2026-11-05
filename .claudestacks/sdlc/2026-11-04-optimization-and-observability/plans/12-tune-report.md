---
status: done
created: 2026-11-04
depends-on: [11]
built-on: 671aa742d4fc
---

# Tune report Implementation Plan

**Goal:** Compare untuned and tuned models on held-out postings in one report.

**Architecture:** `tune_report.py` reads stored rows and recomputes numbers per case from `eval_results`, using `judge_metrics` and `scorer_metrics`. It makes no call and writes nothing to the store. The same tables render to the terminal through `tune_view.py` and to Markdown for `--out`.

**Tech Stack:** Python 3.14, Rich, Typer.

**Content authority:** spec section 10, and the report rule of section 7.

---

## Files that change

```
packages/ijob-app/src/ijob_app/tune_report.py              — create: tables and Markdown
packages/ijob-app/src/ijob_app/tune_view.py                — modify: tables on the terminal
packages/ijob-app/src/ijob_app/service.py                  — modify: tune_report
packages/ijob-app/src/ijob_app/cli_tune.py                 — modify: report --out
packages/ijob-app/tests/ijob_app_tests/test_tune_report.py — create: the numbers and the markers
packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py    — modify: the command
```

## Order of work

### Task 1 — Build the tables

**Files:**
- Create `packages/ijob-app/src/ijob_app/tune_report.py` — `ChoosingRow`, `HeldOutRow`, `TuneReport`, `build_tune_report`.
- Create `packages/ijob-app/tests/ijob_app_tests/test_tune_report.py`.

**Change:**
- Before a split exists it says to run `tune screen`, and writes nothing. No `[tune].baseline` is refused, naming the key.
- `Choosing` holds every full run, limited to train and val keys, so held-out postings inform no choice: model, vendor, F1, recall, NDCG, error, cost per 100 postings.
- `Held-out` appears only once a tuned program has a complete held-out run. It then lists all untuned models and all programs: task, model, program, main number, margin, difference from the baseline, cost per 100, difference from the baseline, P50 and tune spend. The judge's main number is macro F1, with recall and precision beside it. The scorer's is NDCG@10.
- Untuned numbers come from stored per-case answers of full runs, limited to held-out keys. A missing key is left out and counted, with a `tune screen` hint.
- Cost comes from ijob's records. Subscription cost is labelled as metered value, not money spent. The report counts labels added after the freeze and changed postings, from `split_report`.

**Proof:**
- `test_report_before_a_split_says_to_screen` — it names `tune screen`, and the table stays empty.
- `test_report_without_baseline_is_refused` — names `tune.baseline`.
- `test_choosing_holds_no_held_out_key` — a held-out-only wrong answer does not move its numbers.
- `test_held_out_is_absent_before_a_tested_program` — `held_out` is empty until a complete held-out run exists.
- `test_held_out_lists_untuned_and_programs` — both appear, with the baseline's differences at 0.
- `test_untuned_numbers_are_recomputed_per_case` — macro F1 and NDCG@10 from stored answers match a checked example.
- `test_missing_key_is_counted` — a full run lacking one key shows the count and the hint.
- `test_unusable_answer_counts_as_wrong` — a stored `null` lowers recall.
- `test_subscription_cost_is_labelled` — a claude-sdk row says metered value.
- `test_cost_is_per_100_postings` — $1.00 for 200 cases gives $0.50.
- `test_report_counts_later_labels` — after `--add`, the count shows.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_report.py` passes and the Definition of Done is green. Commit `feat(ijob-app): build the tuning report`.

### Task 2 — Show and write the report

**Files:**
- Modify `packages/ijob-app/src/ijob_app/tune_report.py` — `report_markdown`.
- Modify `packages/ijob-app/src/ijob_app/tune_view.py`, `service.py`, `cli_tune.py` — `report`, with `--out PATH`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_tune_report.py`, `test_cli_tune.py`.

**Change:** `--out` writes the same tables as Markdown, overwriting that file, with model, optimizer and vendor names and numbers only. It holds no prompt text, rule, profile or posting. Both forms head the second table `Held-out (N postings, K keeps)`, and both say that cost comes from ijob, not Phoenix (P25). `tune report` never freezes a split.

**Proof:**
- `test_markdown_holds_no_personal_text` — markers seeded in a program's instructions, a rule, the profile and a posting are all absent.
- `test_markdown_holds_the_same_numbers` — values match the terminal's.
- `test_out_overwrites_the_file` — an old file is replaced.
- `test_report_writes_no_split` — run before a screen, the table stays empty.
- `test_cli_report_prints_both_sections` — once a tested program exists, `Choosing` and `Held-out (N postings, K keeps)` appear with real counts.
- `test_both_forms_say_where_cost_comes_from` — the line is on the terminal and in the Markdown.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_report.py packages/ijob-app/tests/ijob_app_tests/test_cli_tune.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob tune report`.

---

## Risks

- A report file goes in `docs/results/` only by the user's hand, after they review it (spec section 18). `execute` writes no report.
- With 30 keeps held out, margins are wide. The report shows the margin beside the number, as the spec asks.

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The five commands in `CLAUDE.md` exit 0.

## Review findings

Two reviews: one of `tune_report.py`, one of the command side. At first 30 of 33 mutants survived in the first.

Blocking, fixed:

- Choosing cost counted held-out spend. Decided, see Deviations.
- A changed posting stayed in untuned rows while the program skipped it, so differences compared different postings. It now counts as missing in every row.
- A program from an older set opened Held-out on the new set, graded by the new labels. Only programs of the report's set count.
- Tune spend was never labelled metered. Money and metered value are now shown apart.
- The Markdown Held-out table had no recall or precision.
- Tests for run selection, difference signs, missing keys, unusable answers and the metered label, each with a mutant seen dying.

Non-blocking, fixed: a judge with no answers for a labelled class no longer reads as unmeasured; an unusable count in Choosing; a note saying what `±` measures; a metered label on the cost difference; numbers no longer fold at 80 columns. `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_report.py`: 46 passed before the command side, which added more.

Left open: nits R3, R4, R7, R10, R12 to R15, R19, T8, T11 in `.airsstack/cc/plugins/claudestacks/handoff/20261105-154610-9d4f/06-reviewer-plan-12.md`, and F14 to F17 in `17-reviewer-plans-12-14-commands.md` beside it.

## Deviations

- 2026-11-05: Choosing's `$/100` is the whole run's rate, including held-out postings, against plan line 41, because `EvalResult` stores no per-case cost. Cost doesn't depend on labels, so no held-out label informs a choice. A note under the table says so in both forms.
- 2026-11-05: `±` stays after the main number as the plan orders it, but it is the 95% half-width of keep recall, not of macro F1, and the scorer has none. A note under the table says so.
- 2026-11-05: a program of the set with no complete held-out run is listed with dashes, once the table appears.
- 2026-11-05: `tune report` in a pipe prints the Markdown; `--out` also prints to stdout and notes the path on stderr.
- 2026-11-05: on a narrow terminal, a table that doesn't fit becomes one block per row, so no number folds.
- 2026-11-05: `tune_report.py` gained public `choosing_cells`, `held_out_cells`, `footer_lines`, so the terminal, pipe and file share one source.
