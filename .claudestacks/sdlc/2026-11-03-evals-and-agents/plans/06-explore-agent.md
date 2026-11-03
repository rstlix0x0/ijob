---
status: done
created: 2026-11-03
depends-on: [05]
built-on: 8947dcb8c009
---

# Explore agent Implementation Plan

**Goal:** Turn one instruction into verified source proposals the user picks from.

**Architecture:** The explore tools wrap the store and the source agent, sharing one budget. Proposals are recorded by the `propose` tool, so they survive any early stop. `ijob explore` is CLI-only.

**Tech Stack:** Python 3.14, DSPy, Typer, Rich, SQLite.

**Content authority:** spec section 9, Explore agent tools, How a run ends, Commands; the Screens section.

---

## Files that change

```
packages/ijob-core/src/ijob_core/store.py          — modify: companies matching a substring
packages/ijob-app/src/ijob_app/agent_tools.py      — modify: explore tools
packages/ijob-app/src/ijob_app/agent_ops.py        — modify: run the explore agent
packages/ijob-app/src/ijob_app/service.py          — modify: explore method
packages/ijob-app/src/ijob_app/agent_view.py       — modify: proposals panel
packages/ijob-app/src/ijob_app/cli_agents.py       — modify: explore command
packages/ijob-app/src/ijob_app/cli.py              — modify: register the command
```

## Order of work

### Task 1 — Build the explore tools

**Files:**
- Modify `packages/ijob-core/src/ijob_core/store.py` — `companies_matching(query, limit)`.
- Modify `packages/ijob-app/src/ijob_app/agent_tools.py` — `ExploreTools`, with `list_sources`, `companies_in_store`, `find_source`, `propose`, and its record of proposals, known sources and companies with no source. It receives the source agent runner as a callable, so `agent_tools.py` does not import `agent_ops.py`.
- Test `packages/ijob-core/tests/ijob_core_tests/test_store.py`, `packages/ijob-app/tests/ijob_app_tests/test_agent_tools.py`.

**Change:** The tool table and `propose` results of spec section 9. `find_source` calls the given `run_source_agent` on the shared budget, and its verified sources are what `propose` accepts. Violations in that inner run add to the explore run's count. A call that ends with no source counts toward "no source". An inner `TaskOutputError` returns a reason; other stops reach the wrapper from plan 04.

**Proof:**
- `test_companies_matching_is_distinct_and_limited` — distinct names, at most the limit.
- `test_companies_matching_escapes_the_query` — `%` and `_` match literally.
- `test_list_sources_names_refs_and_companies` — the user's sources.
- `test_companies_in_store_limit` — at most 50.
- `test_find_source_shares_the_budget` — its calls charge the run's budget.
- `test_find_source_unusable_answer_returns_a_reason` — no stop is stored.
- `test_find_source_provider_failure_stops_at_the_next_call` — stored as a stop.
- `test_propose_verified` — `proposed`, `why` cut to 200 characters.
- `test_propose_repeat` — `already proposed`, with no count.
- `test_propose_known_source` — `already one of your sources`, counted as known and not offered.
- `test_propose_unverified_is_a_violation` — `not verified in this run`.
- `test_inner_violation_adds_to_the_explore_count` — an off-site attempt inside `find_source` counts.
- `test_find_source_with_no_result_counts_no_source` — it appears in the header's count.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_agent_tools.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add the explore tools`.

### Task 2 — Run the explore agent

**Files:**
- Modify `packages/ijob-app/src/ijob_app/agent_ops.py` — `ExploreResult`, `explore_operation`.
- Modify `packages/ijob-app/src/ijob_app/service.py` — `explore`.
- Test `packages/ijob-app/tests/ijob_app_tests/test_agent_ops.py`.

**Change:** Runs the agent under `[llm].max_cost_usd`, with `--model` replacing the configured model, step limit 20, and an `on_step` callback. Proposals stand after every end in "How a run ends".

**Proof:**
- `test_explore_returns_proposals` — with counts of known and no source.
- `test_explore_cap_keeps_proposals` — with the stop reason.
- `test_explore_provider_failure_keeps_proposals` — with the error.
- `test_explore_unusable_answer_keeps_proposals` — with the error.
- `test_explore_trace_has_flat_tool_spans` — every span, inner runs included, sits directly under root `ijob explore`.
- `test_explore_steps_reach_the_callback` — one per tool call.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_agent_ops.py` passes and the Definition of Done is green. Commit `feat(ijob-app): run the explore agent`.

### Task 3 — Add `ijob explore`

**Files:**
- Modify `packages/ijob-app/src/ijob_app/agent_view.py` — `proposals_panel`.
- Modify `packages/ijob-app/src/ijob_app/cli_agents.py` — `explore_command`.
- Modify `packages/ijob-app/src/ijob_app/cli.py` — register it.
- Test `packages/ijob-app/tests/ijob_app_tests/test_cli_agents.py`, `packages/ijob-app/tests/ijob_app_tests/test_agent_view.py`.

**Change:** The spec's Screens section and spec section 9 Commands. Plain lines in a pipe.

**Proof:**
- `test_explore_unusable_answer_exits_1` — with proposals shown.
- `test_proposals_panel_draws_counts` — proposed, known and no source, in `test_agent_view.py`.
- `test_explore_blank_exits_2` — no call.
- `test_explore_over_limit_exits_2` — 4,001 characters, no call.
- `test_explore_missing_model_exits_1` — names `models.explore_agent`.
- `test_explore_bad_model_exits_2` — a bad `--model` reference.
- `test_explore_pipe_needs_yes` — exit 1 before any call.
- `test_explore_yes_adds_every_proposal` — no question.
- `test_explore_pick_numbers` — `1,2`.
- `test_explore_pick_all_and_none` — `all`, `none`, and Enter for none.
- `test_explore_invalid_pick_asks_again` — `9` and `abc`.
- `test_explore_end_of_input_means_none` — nothing is added.
- `test_explore_failed_add_continues_and_exits_1` — the rest are added.
- `test_explore_no_proposals_exits_0` — it says so.
- `test_explore_cap_exits_0_provider_failure_exits_1` — with proposals shown.
- `test_explore_draws_third_party_text_literally` — markup is not read.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_cli_agents.py` passes and the Definition of Done is green. Commit `feat(ijob-app): add ijob explore`.

---

## Risks

- A real run can reach the default $0.50 cap; the run then ends with its proposals, as spec section 9 states.
- Explore can only propose companies the model knows or that are in the store (spec section 9).
- `execute` spends nothing and calls no network: tests use fakes.

## Proof

- The five gate commands in `CLAUDE.md` exit 0.
- `uv run --locked ijob explore --help` lists the flags of the spec.

## Review findings

This plan shared its reviews with plan 05 and plan 07.

Fixed (`pytest test_agent_ops.py test_agent_tools.py test_agent_site.py test_service.py` → 364 passed):
- `agent_tools.py` whitespace in `propose` lost a verified source.
- `agent_tools.py` an inner unusable answer hid verified sources; the result lists the refs, then the reason.
- `agent_tools.py` a company counted as both "no source" and proposed; it leaves `no_source` once it has a proposal or a later success.
- `store.py` the backslash and limit branches of `companies_matching` gained tests.

Fixed, CLI (`pytest test_cli_agents.py test_agent_view.py test_tui.py test_cli_terminal.py test_recipe_imports.py` → 236 passed, also with `GITHUB_ACTIONS=1`):
- `cli_agents.py` a failed add could draw outside text as a `fix` command.
- `cli_agents.py` a blank instruction loaded DSPy.
- `cli_agents.py` a pick of 4,301 or more digits crashed.

Open:
- `store.py` nits: distinct names are exact, so `ACME` and `Acme` take two slots; the SQL hard-codes the escape beside `_LIKE_ESCAPE`; a 60,000-character query raises `sqlite3.OperationalError`.
- `cli_agents.py` nits: `٣` counts as a pick; Ctrl-C at a piped prompt reads as end of input; with stdout redirected, the question goes into the file.

## Deviations

- 2026-11-03: the run was lean, as in plan 05, and tasks were not committed one by one.
- 2026-11-03: `ExploreTools` receives a `find_source` closure, not `run_source_agent`, so `agent_tools.py` stays clear of `agent_ops.py` and the budget stays with the caller. `SourceAttempt` is the Protocol between them.
- 2026-11-03: `run_explore_agent` is a separate inner runner, so plan 07 can run it on an empty store and its own budget.
- 2026-11-03: inside explore, the source agent uses `[models].source_agent` if set, otherwise the explore model. The spec does not say which; a missing model inside a tool would leave the agent looping on a refusal.
- 2026-11-03: inner source-agent steps do not reach the outer `on_step`, so one `find_source` is one step. Inner runs get no URL, since explore has none to give.
- 2026-11-03: the explore header's count needed `list_sources` to open with `N sources:`.
- 2026-11-03: not run on real data. Spec section 11's list spends money and needs the user's OK at each step.
