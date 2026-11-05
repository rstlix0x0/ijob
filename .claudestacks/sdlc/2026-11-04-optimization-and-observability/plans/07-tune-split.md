---
status: approved
created: 2026-11-04
depends-on: [04]
built-on: 671aa742d4fc
---

# Tune split Implementation Plan

**Goal:** Freeze one train, val and held-out split per label set.

**Architecture:** A pure function in `eval_rules.py`, seeded by both hashes, divides postings that have both labels. A new `ijob_core/tune_records.py` stores the result in table `tune_splits`, reached as `Store.tunes`. `ijob_app/tune_ops.py` holds `freeze_split`, which applies the refusals. Only `tune screen` calls it (plan 10).

**Tech Stack:** Python 3.14, SQLite.

**Content authority:** spec section 7.

---

## Files that change

```
packages/ijob-core/src/ijob_core/tune_records.py             — create: SplitPart, TuneSplitRow, TuneRecords
packages/ijob-core/src/ijob_core/eval_rules.py               — modify: tune_split
packages/ijob-core/src/ijob_core/store.py                    — modify: the migration and Store.tunes
packages/ijob-app/src/ijob_app/tune_ops.py                   — create: freeze_split, TooFewKeepsError
packages/ijob-app/src/ijob_app/eval_suites.py                — modify: public unchanged_posting
packages/ijob-core/tests/ijob_core_tests/test_tune_records.py — create: storing a split; the migration
packages/ijob-core/tests/ijob_core_tests/test_eval_rules.py  — modify: the split rule
packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py      — create: freezing and refusing
```

## Order of work

### Task 1 — Write the split rule

**Files:**
- Create `packages/ijob-core/src/ijob_core/tune_records.py` — `SplitPart(StrEnum)` with `train`, `val`, `held_out`, `unused`.
- Modify `packages/ijob-core/src/ijob_core/eval_rules.py` — `tune_split(rules_sha256, profile_sha256, keeps, rejects)`.
- Test `packages/ijob-core/tests/ijob_core_tests/test_eval_rules.py`.

**Change:** Held-out keeps are `(k + 1) // 2`, and held-out rejects are `(k + r + 1) // 2` less those. From the rest, N is the smaller of the keeps and rejects left. Train takes `(6 * N + 5) // 10` of each, val takes the rest of N, and the others are `unused`. It returns a part per posting and N. Refusing below 20 is `freeze_split`'s job, so the rule stays pure. The draw is seeded by both hashes.

**Proof:**
- `test_tune_split_rows` — each row of the section 7 table gives its held-out, N, train, val and unused: 21 and 79; 39 and 161; 40 and 160; 59 and 141; 150 and 50.
- `test_tune_split_is_seeded` — the same hashes give the same parts; other hashes give others.
- `test_tune_split_balances_train_and_val` — equal keeps and rejects in each.
- `test_tune_split_puts_each_posting_in_one_part` — every posting appears exactly once.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_eval_rules.py` passes and the Definition of Done is green. Commit `feat(ijob-core): add the tuning split rule`.

### Task 2 — Store a split

**Files:**
- Modify `packages/ijob-core/src/ijob_core/tune_records.py` — `TuneRecords.put_split`, `split`, `has_split`.
- Modify `packages/ijob-core/src/ijob_core/store.py` — a new migration tuple (`store.py:62`) adds `tune_splits`, keyed by `set_id` and `posting_id`, with `part` checked against the four values; `Store.tunes`.
- Create `packages/ijob-core/tests/ijob_core_tests/test_tune_records.py`.

**Change:** `put_split` writes all rows in one transaction, and refuses a set that already has one, so a split stays frozen. There is no delete method, since there is no reset.

**Proof:**
- `test_split_round_trips` — rows written read back by posting.
- `test_a_frozen_split_cannot_be_replaced` — a second `put_split` raises and leaves the rows as they were.
- `test_a_new_set_gets_its_own_split` — two sets hold different rows.
- `test_tune_splits_migrates_an_older_schema` — a database at the previous `user_version` gains the table and keeps its rows.

**Done when:** `uv run --locked pytest packages/ijob-core/tests/ijob_core_tests/test_tune_records.py` passes and the Definition of Done is green. Commit `feat(ijob-core): store the frozen tuning split`.

### Task 3 — Freeze with its refusals

**Files:**
- Create `packages/ijob-app/src/ijob_app/tune_ops.py` — `TooFewKeepsError(LlmError)`, `SplitReport`, `freeze_split(store, eval_set)`, `split_report(store, eval_set)`.
- Modify `packages/ijob-app/src/ijob_app/eval_suites.py` — rename `_unchanged` to `unchanged_posting`, at its three uses (`eval_suites.py:446,506,641`).
- Create `packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py`.

**Change:** `freeze_split` returns the existing split when one is frozen. Otherwise it refuses, with nothing frozen, when `review_items` for the set has anything left, pointing at `eval review`, and raises `TooFewKeepsError` when N is under 20, naming the numbers and `eval label --add`. It takes postings with both a judge and a scorer label. A posting whose text changed is skipped and counted, by `unchanged_posting`. `split_report` reads the counts per part, the labels added after the freeze and the changed postings. It writes nothing.

**Proof:**
- `test_freeze_writes_the_split_once` — a second call returns the same rows.
- `test_too_few_keeps_freezes_nothing` — 21 keeps and 79 rejects, and 39 and 161, each raise, naming N (10 and 19) and `eval label --add`, with no rows.
- `test_labels_awaiting_review_freeze_nothing` — a split vote raises, naming `eval review`, with no rows.
- `test_a_spot_check_awaiting_review_freezes_nothing` — a pending spot check does the same.
- `test_only_postings_with_both_labels_count` — a posting with one label has no row.
- `test_changed_posting_is_skipped_and_counted` — it has no row, and the report counts it.
- `test_labels_added_later_are_left_out_and_counted` — after `--add`, the rows stay and the report counts the new ones.
- `test_split_report_writes_nothing` — before any freeze, the table stays empty.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py` passes and the Definition of Done is green. Commit `feat(ijob-app): freeze the tuning split`.

---

## Risks

- The migration runs on the user's real store the first time any command opens it. It only adds a table.
- A pending spot check blocks the freeze, because a check can change a label and the flow in spec section 2 decides them first.

## Proof

- `uv run --locked pytest packages/ijob-core packages/ijob-app/tests/ijob_app_tests/test_tune_ops.py` passes.
- The five commands in `CLAUDE.md` exit 0.
