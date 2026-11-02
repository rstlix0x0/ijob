---
status: done
created: 2026-11-01
depends-on: [08]
built-on: cae3bcdf3b23
---

# LLM TUI Implementation Plan

**Goal:** Show judge verdicts, on-demand scores, the instructions flow and the CV profile in the
Textual app.

**Architecture:**
- **Search.** `IjobApp` (`tui/app.py:213`) always searches through `Service.search(layers, refs)`
  (plan 08). Stored instruction filters therefore apply even when there are no rules, and with no
  rules nothing LLM-related is imported or shown.
- **Filter layers from the inputs.** The inputs keep today's prefill from the merged config
  (`app.py:264,274`). The packaged and user layers come from `read_filter_layers` (plan 07). Only an
  input whose value differs from its prefill becomes part of the flags layer, so instruction filters
  are not shadowed by untouched prefills.
- **Workers.** Every LLM operation runs in an async Textual worker (`@work(exclusive=True, group=<operation>)`),
  never in a thread, because `Service` is bound to its thread (`service.py:283`).
  - A new filter edit cancels the in-flight judged search. Plan 08's runner keeps the verdicts that
    finished, so nothing is judged twice.
- **New keys.** `v` toggles dropped rows, `g` scores the highlighted posting, and `i` opens
  `InstructionsScreen`.
- **Status line.** It shows `$X of $Y` for whichever operation is running: search, `g`, the profile
  Refresh or instruction Submit. After it ends, a notification reports tracing warnings and
  `N spans not stored` (spec §8).
- **Model text.** It enters Markdown only through `_escape` (`tui/app.py:75-117`), and other widgets
  with `markup=False` (spec §4.1). Status texts come from plan 08's `STATUS_TEXT`.
- **Test wiring.** Tests build the `Service` through `tui/conftest.py`'s opener, with plan 08's
  `LlmOptions` carrying fake engines.

**Tech Stack:** Python 3.14, Textual 8.2.8 (`App.run_test`, `Pilot`, `work`), pytest with fake engines.

---

## Files that change

```
packages/ijob-app/src/ijob_app/tui/app.py                         — [modify] layered search, verdict column, v/g/i keys, workers, status/notifications, score in detail
packages/ijob-app/src/ijob_app/tui/instructions.py                — [create] InstructionsScreen (editor, proposal, Accept/Cancel)
packages/ijob-app/src/ijob_app/tui/cv.py                          — [modify] profile view and Refresh
packages/ijob-app/tests/ijob_app_tests/tui/conftest.py            — [modify] opener accepts LlmOptions with fake engines
packages/ijob-app/tests/ijob_app_tests/tui/test_app.py            — [modify]
packages/ijob-app/tests/ijob_app_tests/tui/test_instructions.py   — [create]
packages/ijob-app/tests/ijob_app_tests/tui/test_cv.py             — [modify]
```

## Order of work

### Task 1 — Layered search and verdicts in the results list

**Files:**
- Modify `tui/app.py` and `tui/conftest.py`.
- Test `tui/test_app.py`.

**Change:**
- `_apply_filters` (`app.py:504-514`) builds `FilterLayers` as described in the Architecture, then
  runs `Service.search` in the exclusive async worker. Each row's verdict cell is set as items
  arrive, with plan 09's cell texts: `✔`, `✘ <reason>`, `· <STATUS_TEXT>`, plus `(no description)`.
- The verdict column appears only when rules are stored. Without rules, the table is exactly as
  today.
- `v` toggles whether dropped rows are shown; they are hidden by default.
- The status line shows `$X of $Y` during the run and the report summary after. Warnings and dropped
  spans become notifications.
- The detail pane shows the verdict reason through `_escape`.

**Proof:** written first, seen failing, then passing, with `run_test` and fake engines:
- `test_results_without_rules_unchanged`: the existing `test_app.py` cases pass unmodified, and the
  fake engine records zero calls.
- `test_instruction_filters_apply_without_rules`: stored instructions with `countries=["SG"]` and no
  rules hide a JP posting.
- `test_untouched_prefill_does_not_shadow_instructions`: the prefilled country input left unchanged
  lets the instruction country apply. Editing it overrides the instruction.
- `test_verdicts_stream_into_rows`: after `Pilot.pause()`, two rows show `✔`, and the dropped one is
  hidden.
- `test_v_toggles_dropped_rows`: `v` shows the dropped row with `✘ <reason>`, and `v` again hides it.
- `test_filter_edit_cancels_running_search`: a slow fake engine and two quick filter edits make each
  posting's engine call happen at most once.
- `test_status_line_shows_budget`: during a slow run, the status text contains `of $0.50`.
- `test_dropped_spans_notified`: a trace store that refuses writes leads to a notification containing
  `spans not stored`.
- `test_reason_markup_shown_literally`: a reason `"[b]x[/b] **x** \x1b[31mred"` renders as
  `[b]x[/b] **x** [31mred` in the detail pane.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/tui/test_app.py` passes
and the CLAUDE.md gate is green. Commit `feat(tui): show judge verdicts`.

### Task 2 — Score the highlighted posting

**Files:** Modify `tui/app.py`; Test `tui/test_app.py`.

**Change:**
- `g` runs `Service.score([highlighted id])` in an async worker. The detail view shows `Score: N` and
  the reasons through `_escape`, or the `STATUS_TEXT` when the posting is not scored.
- Errors show as notifications with spec §9's text. `NoCvError` says to store a CV.
- The bindings at `app.py:230-238` gain `g`, `v` and `i`.

**Proof:** written first, seen failing, then passing:
- `test_g_scores_highlighted`: the detail view shows `Score: 87` and both fake reasons.
- `test_g_without_cv_notifies`: the notification text contains `ijob cv set`.
- `test_g_cached_second_time`: a second `g` on the same posting leaves the engine call count at 1.
- `test_g_status_line_shows_budget`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/tui/test_app.py` passes
and the gate is green. Commit `feat(tui): score a posting on demand`.

### Task 3 — Instructions screen

**Files:**
- Create `tui/instructions.py`.
- Modify `tui/app.py` (push on `i`).
- Test `tui/test_instructions.py`.

**Change:** `InstructionsScreen` is `Screen[None]`.
- It shows a `TextArea` prefilled with the stored text, and a Submit button.
- Submit runs `propose_instructions` in an async worker, then shows the instruction filters, the
  rules and the shadow warnings, all `markup=False`, with Accept and Cancel.
- Accept stores the proposal and returns, and the results re-run the search. Cancel discards.
  `escape` goes back.
- Errors (blank, nothing actionable, provider) show inline with spec §9's text.

**Proof:** written first, seen failing, then passing:
- `test_i_opens_with_stored_text`: the text area holds the stored instruction text.
- `test_submit_shows_proposal_and_accept_stores`: after Accept, `Service.instructions()` returns the
  proposed rules.
- `test_cancel_stores_nothing`.
- `test_blank_text_shows_error`: the inline error mentions `instructions`, and the fake engine
  records zero calls.
- `test_shadow_warning_shown`: spec §4.3's example shows the countries warning.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/tui/test_instructions.py`
passes and the gate is green. Commit `feat(tui): edit and confirm instructions`.

### Task 4 — Profile on the CV screen

**Files:** Modify `tui/cv.py`; Test `tui/test_cv.py`.

**Change:**
- Under the stored CV, `CvScreen` shows the profile fields with `markup=False`, or `no profile yet`.
- A Refresh button rebuilds the profile in an async worker.
- Storing a new CV builds the profile when `[models].cv_profile` is set. Otherwise it shows the note
  text from spec §7.2.

**Proof:** written first, seen failing, then passing:
- `test_profile_shown_after_store`: the screen shows the fake profile's `seniority: senior`.
- `test_refresh_rebuilds`: the engine call count rises by one.
- `test_no_model_shows_note`: the note text `profile not built; set [models].cv_profile` is visible.
- `test_profile_text_literal`: a skill `[red]x[/red]` renders literally.

**Done when:** `uv run --locked pytest packages/ijob-app` passes and the gate is green. Commit
`feat(tui): show and refresh the CV profile`.

## Risks

- **Riskiest step:** Task 1's worker cancellation and streaming updates. The tests use
  `Pilot.pause()` to settle workers before asserting.
- **Multi-select.** It stays out of scope, by the author's decision (spec §11).
- **Not done:** a TUI view of traces. Traces stay in the CLI, `ijob traces`.

## Proof

- `uv run --locked pytest packages/ijob-app` passes.
- The gate exits 0.

## Review findings

One reviewer covered plans 09 and 10 together; see plan 09's Review findings for the gate runs. The TUI directory ran 3 times with 89 passed each time. A single `test_inputs_start_from_config` failure seen during concurrent edits did not reproduce. After the fix round the full gate was green: pytest 2664 passed.

- fixed 🟡 the TUI copies of the CLI text helpers (`_verdict_cell`, `_summary`, `_score_summary`, `_profile_text`, `_flat/_value/_shadow_line`) had drifted from the CLI. They now come from `ijob_app/llm_text.py`. The TUI keeps only a thin wrapper that blanks `NOT_RUN`, and cost is `.4f` everywhere.
- fixed 🟡 (blocking, via plan 09) `IjobApp` reads its cap and the `CvScreen` config path from the `--config` in force (`_llm_config`). `test_cv_key_gives_the_screen_the_config_in_force` guards the wiring and was mutation-checked.
- fixed 🔵 test helpers moved into `tui/conftest.py` under public names. The function-level `llm_config` import became a top-level import.
- open 🟡 Task 1 drift: rows arrive in arrival order (cached first, then by completion), not in filter order. With concurrency above 1 the table order is not deterministic, and it differs from the CLI's order.
- open 🟡 `CvScreen` store and Refresh show no cost and notify only `report.warnings`, not `dropped_spans`. `InstructionsScreen` shows no cost, because `propose_instructions` sets no report (plan 08's API).
- open 🔵 `app.py:774`: `_search` and `_score` read the shared `service.last_report`. A score that finishes during a no-rules search could show up in the search status.
- open 🔵 TUI notifications show tracing warnings with their own `warning: ` prefix.
- open 🔵 `_add_item` runs one `labels([id])` query per streamed row.

## Deviations

- 2026-11-01: Tasks end in "Commit …", but the instruction to execute did not ask for commits. The work is left uncommitted.
- 2026-11-01: Task 4 ran in parallel with Task 2, since their files are disjoint. Plans 09 and 10 had one review.
- 2026-11-01: The running status reads `judging… cost ? of $Y` and `scoring… cost ? of $Y`. Plan 08 has no mid-run spend hook, so the spend is unknown until the summary.
- 2026-11-01: The verdict column is the last column and exists only when rules are stored. `v` is disabled without it.
- 2026-11-01: Only an input that differs from its prefill goes into the flags layer, so an untouched prefill does not shadow stored instructions.
- 2026-11-01: `test_g_cached_second_time` asserts 2 engine calls after the first `g`, not 1, because the first score also builds the CV profile. `NoCvError` shows a TUI-specific hint naming `ijob cv set PATH` or `c`.
- 2026-11-01: `InstructionsScreen` is `Screen[None]`. The app re-runs the search on close only if the stored instructions changed.
- 2026-11-01: `CvScreen` gained `config_path`, so it checks `[models].cv_profile` without importing dspy.
