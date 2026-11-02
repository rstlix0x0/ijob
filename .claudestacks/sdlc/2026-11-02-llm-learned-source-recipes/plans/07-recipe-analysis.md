---
status: approved
created: 2026-11-02
depends-on: [05, 06]
built-on: 6ee2c2bd4580
---

# Recipe Analysis Implementation Plan

**Goal:** Turn an unrecognized page into a trialled recipe proposal that the service can store on confirmation.

**Architecture:**
- **Detection.** `resolve_url` raises the new `UnrecognizedPageError(UnsupportedUrlError)` at
  `detect.py:98`, with the same message. The empty-feed error at `detect.py:100` stays a plain
  `UnsupportedUrlError`. Callers start analysis only on the subclass (spec §7 step 1). `add_url`'s
  path is otherwise unchanged.
- **`ijob_app/recipe_ops.py`** holds the analysis. At module level it imports only `ijob_core`,
  `ijob_sources` and `ijob_app`. Every `ijob_ai` import is inside a function with
  `# noqa: PLC0415`, as in `llm_ops.py`, so `service.py` can import it at the top and search still
  loads no `ijob_ai` module.
  - `RecipeProposal` (frozen, slotted, keyword-only) has:
    - `ref`, `target_url` and `company`;
    - `recipe` (the canonical text from `recipe_text`), `recipe_format` and `recipe_url`;
    - `jobs: tuple[SightingDraft, ...]` and `invalid: int`;
    - `model: str` and `report: OperationReport`.
  - `analyze_operation(runtime, client, *, ref, target_url, company) -> RecipeProposal`.
  - `accept_recipe(store, proposal, *, at)`.
- **`ijob_app/recipe_errors.py`** holds the errors that must be `LlmError`s, so `run_items` records
  them as a failed item instead of an exception group (`llm_ops.py:369-380`). `recipe_ops.py` and
  `service.py` import `recipe_errors` only inside functions, because it imports `ijob_ai.errors` and
  search must load no `ijob_ai` module (`test_service.py:1046`, `:1056`). They are spec §11's:
  - `RecipeOutputError(TaskOutputError)` reads `the model's recipe is invalid: <reason>`;
  - `NoTrialJobsError(LlmError)` reads `the proposed recipe read no jobs from the page`;
  - `RecipeCapReachedError(LlmError)` reads
    `cost cap reached before the JSON recipe; raise [llm].max_cost_usd`.
- **Errors in `service.py`:**
  - `RecipeAlreadyAddedError(SourcesError)` reads
    `recipe:<key> is already added; run: ijob sources reanalyze recipe:<key>`. It exits 1 through
    the existing source-failure mapping.
  - `NoRecipeError(InvalidValueError)` has the reason `<ref> has no recipe`, and exits 2.
- **One operation, one budget.** Call 1 and call 2 run inside a single `_run_single(runtime,
  "sources add", …)` worker, so they share the operation's `Budget` and the trace `ijob sources add`
  (spec §7 step 4). The worker:
  - checks `budget.try_start()` itself before call 2;
  - returns a fetch failure of the JSON URL as a value, not an exception, because a non-`LlmError`
    from a worker becomes an `ExceptionGroup` (`llm_ops.py:425`, `:442-445`). The operation
    re-raises it after `_run_single` returns.
- **Fetching.** Every analysis fetch uses one client that has plan 02's guard (spec §6), opened by
  the service from its `ClientFactory`. The added URL is fetched again by the analysis, under the
  guard. `resolve_url`'s earlier fetch was unguarded.
- **Service methods:**
  - `learns_recipes() -> bool`;
  - `analyze_url(url, company) -> RecipeProposal`;
  - `analyze_source(ref) -> RecipeProposal`;
  - `accept_recipe(proposal) -> SourceRow`.

  The two `analyze_` names keep them apart from plan 06's `propose_recipe` wrapper.

**Tech Stack:** Python 3.14, plan 03's recipe engine, plan 06's `propose_recipe`, the `llm_ops`
runtime, pytest with `ijob_app_tests/llm_fakes.py` fake engines and `httpx.MockTransport`.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/errors.py                     — [modify] UnrecognizedPageError
packages/ijob-sources/src/ijob_sources/detect.py                     — [modify] raise UnrecognizedPageError at the "nothing found" branch
packages/ijob-sources/tests/ijob_sources_tests/test_detect.py        — [modify]
packages/ijob-app/src/ijob_app/recipe_errors.py                      — [create] RecipeOutputError, NoTrialJobsError, RecipeCapReachedError
packages/ijob-app/src/ijob_app/recipe_ops.py                         — [create] RecipeProposal, analyze_operation, accept_recipe
packages/ijob-app/src/ijob_app/service.py                            — [modify] learns_recipes, analyze_url, analyze_source, accept_recipe, RecipeAlreadyAddedError, NoRecipeError
packages/ijob-app/tests/ijob_app_tests/llm_fakes.py                  — [modify] recipe_answer, json_url_answer helpers
packages/ijob-app/tests/ijob_app_tests/recipe_pages.py               — [create] made-up page bodies: a list page, a JavaScript-only page with a footer JSON link, its JSON list
packages/ijob-app/tests/ijob_app_tests/test_recipe_errors.py         — [create]
packages/ijob-app/tests/ijob_app_tests/test_recipe_ops.py            — [create]
packages/ijob-app/tests/ijob_app_tests/test_service.py               — [modify] propose, reanalyze and accept through the service
```

## Order of work

### Task 1 — Tell an unrecognized page apart

**Files:**
- Modify `errors.py`, `detect.py` and `test_detect.py`.

**Change:**
- Add `UnrecognizedPageError(UnsupportedUrlError)`. Its constructor takes the URL and builds the
  same `cannot use <url>: no hiring system, job posting or feed found` message.
- `resolve_url` raises it in place of the `_NOTHING_FOUND` `UnsupportedUrlError`. Update the
  docstring's `Raises`.

**Proof:** written first, seen failing, then passing:
- `test_unrecognized_page_is_distinct`: a plain HTML page raises `UnrecognizedPageError`, which is
  also an `UnsupportedUrlError`, and its message is unchanged.
- `test_empty_feed_is_not_unrecognized`: an RSS feed with no items raises `UnsupportedUrlError`
  that is not an `UnrecognizedPageError`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_detect.py`
passes and the CLAUDE.md gate is green. Commit `feat(sources): tell an unrecognized page apart`.

### Task 2 — The analysis operation

**Files:**
- Create `recipe_errors.py`, `recipe_ops.py`, `recipe_pages.py`, `test_recipe_errors.py` and
  `test_recipe_ops.py`.
- Modify `llm_fakes.py`.

**Change:**
- `recipe_errors.py` defines the three errors. `RecipeOutputError` keeps `task` and `reason`, and
  its message is the spec's, not `TaskOutputError`'s.
- `analyze_operation(runtime, client, *, ref, target_url, company)`:
  1. It reads `runtime.settings.model_for(Task.SOURCE_RECIPE)` first. `MissingModelError`
     propagates before any fetch.
  2. It runs `fetch_document(client, target_url)`. Any `SourcesError` propagates: not a public
     address, refused host, over 5 MiB, or wrong content type (spec §11).
  3. Through `_run_single(runtime, "sources add", …)`, a worker:
     - calls `propose_recipe(final_url, format, sample_text(document), …)` with the
       `source_recipe` LM, provider and timeout;
     - on a `json_url` answer, resolves it with `urljoin(final_url, json_url)` and fetches it with
       the same client;
     - requires JSON there. HTML gives `UnsupportedUrlError(json_url, "expected JSON")`;
     - returns any `SourcesError` from that fetch as the worker's result;
     - otherwise, when `budget.try_start()` is false, raises `RecipeCapReachedError`;
     - otherwise makes call 2 on the JSON sample.

     There are at most two calls, and no retry (spec §7 step 5).
  4. After `_run_single`, a returned `SourcesError` is raised.
  5. Any `TaskOutputError` that `_run_single` raises, other than a `RecipeOutputError`, is re-raised
     as `RecipeOutputError(error.reason)`. Spec §11 gives every `TaskOutputError` of the analysis the
     wording `the model's recipe is invalid: …`. That covers an unparseable answer, a format
     mismatch, and a `json_url` for a JSON sample.
- The trial (spec §7 step 6):
  - Before parsing, the answer's JSON object has its `url` key set to the URL ijob fetched for the
    trial: the added URL, or the resolved JSON URL. That is the value spec §4.2 defines for `url`. A
    relative or empty `url` from the model therefore never fails the trial.
  - `parse_recipe` runs on that text. `InvalidRecipeError` and `UnsupportedRecipeVersionError`
    become `RecipeOutputError(reason)`.
  - `read_page(recipe, document, page_url=final_url, ref=ref, company=company)`, with no pagination
    and no detail fetch. `RecipeEvaluationError` becomes `RecipeOutputError`.
  - Zero drafts raise `NoTrialJobsError`.
- The result is a `RecipeProposal`:
  - `jobs` are the drafts and `invalid` is `rejected`;
  - `recipe` is `recipe_text` of the final recipe;
  - `model` is `str` of the model reference;
  - `report` is `_run_single`'s report.
- `accept_recipe(store, proposal, *, at)` calls plan 04's `set_recipe_source(ref, company=…,
  target_url=…, recipe=…, model=…, at=at)`.
- `recipe_pages.py` holds made-up bodies only:
  - `LIST_PAGE`, an HTML list of two jobs;
  - `JS_ONLY_PAGE`, with no job links and a footer `<a href="/api/jobs/">API</a>`;
  - `JOBS_JSON`.

**Proof:** written first, seen failing, then passing. Each test uses fake engines, a fake resolver
and `httpx.MockTransport`:
- `test_html_recipe_proposal`: one call. The proposal reads two jobs, `invalid` is 0, and the
  report has the fake call's cost. The trace root is `ijob sources add`.
- `test_json_url_takes_two_calls`: `JS_ONLY_PAGE` gives a `json_url` answer. `/api/jobs/` is fetched
  through the same client, and the second answer's `json` recipe reads `JOBS_JSON`. `recipe_url` is
  the resolved JSON URL.
- `test_cap_before_second_call`: a cap below the first call's cost raises `RecipeCapReachedError`,
  and the second fake answer is never requested.
- `test_json_url_serving_html_refused`: it raises `UnsupportedUrlError`, not an exception group.
- `test_json_url_private_host_refused`: it raises `NotPublicAddressError`.
- `test_invalid_recipe_is_recipe_output_error`: an answer with XPath `//li[` gives the message
  `the model's recipe is invalid: …`.
- `test_evaluation_error_in_trial_is_recipe_output_error`: XPath `foo()`.
- `test_zero_jobs_in_trial`: it raises `NoTrialJobsError` with the spec message.
- `test_missing_model_before_fetch`: with no `[models].source_recipe`, `MissingModelError` is raised
  and the transport saw no request.
- `test_added_url_fetched_under_guard`: a target host resolving to `10.0.0.1` raises
  `NotPublicAddressError`, and no model call is made.
- `test_provider_errors_propagate`: parametrized. A fake `ProviderFailedError`,
  `ProviderUnavailableError` and `CostUnknownError` each surface as themselves, and nothing is
  stored.
- `test_unparseable_answer_is_recipe_output_error`: a fake answer that is not JSON gives
  `the model's recipe is invalid: …`.
- `test_model_url_replaced_before_parse`: an answer whose `url` is `/relative` is accepted, and
  `recipe_url` is the added URL.
- `test_added_url_refused_cases`: parametrized. The added URL on `www.jobstreet.com` raises
  `ReservedHostError`; a body over 5 MiB and `text/plain` raise `UnsupportedUrlError`. No model
  call is made in any case.
- `test_accept_recipe_stores_source_and_recipe`.
- `test_error_messages` in `test_recipe_errors.py`: the three spec messages, exactly.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_recipe_ops.py packages/ijob-app/tests/ijob_app_tests/test_recipe_errors.py`
passes and the CLAUDE.md gate is green. Commit `feat(app): analyze unrecognized pages into recipes`.

### Task 3 — Service operations

**Files:**
- Modify `service.py` and `test_service.py`.

**Change:**
- `learns_recipes()` is true when `[models].source_recipe` is set. It reads `self._runtime().settings`,
  and `LlmError`s from building the runtime propagate.
- `analyze_url(url, company)`:
  - The ref is `SourceRef(SourceKind.RECIPE, manual_key(url))` (spec §8).
  - A row that exists and is not removed raises `RecipeAlreadyAddedError`.
  - `_refuse_other_company(ref, company)` applies, as for every other added URL.
  - It opens a client from the factory with no cache, guards it with
    `public_host_guard(self._resolver)`, and runs `analyze_operation`.
- `analyze_source(ref)`:
  - It reads the row. Unknown or removed raises `UnknownSourceError(f"unknown source {ref}")`, the
    reason `remove_source` gives.
  - A kind other than `RECIPE` raises `NoRecipeError`.
  - Otherwise it runs the same analysis with the stored `target_url` and `company`.
  - The stored recipe is untouched until `accept_recipe` (spec §8).
- `accept_recipe(proposal)` calls `recipe_ops.accept_recipe` with `self._now()` and returns the row.
- Each method's docstring lists its `Raises`, in the style of `propose_instructions`.

**Proof:** written first, seen failing, then passing:
- `test_analyze_url_refuses_active_ref`: no fetch, and the exact message.
- `test_analyze_url_after_removal_restores_on_accept`: the source is not removed afterwards, and
  the recipe is replaced.
- `test_analyze_url_other_company_refused`.
- `test_reanalysis_keeps_old_recipe_until_accept`: after a proposal and before accept,
  `store.recipe(ref)` is still the old one; after accept it is the new one. The company is unchanged.
- `test_reanalysis_unknown_and_removed_ref`: each raises `UnknownSourceError`.
- `test_reanalysis_other_kind_has_no_recipe`: `greenhouse:x` raises `NoRecipeError` with
  `greenhouse:x has no recipe`.
- `test_learns_recipes_follows_config`.
- `test_search_still_loads_no_ijob_ai`: the existing subprocess guard test is unchanged and stays
  green with `recipe_ops` imported by `service.py`.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_service.py`
passes and the CLAUDE.md gate is green. Commit `feat(app): propose, reanalyze and accept recipes`.

## Risks

- **Riskiest step:** Task 2's worker shape. A `SourcesError` raised inside the worker would surface
  as an `ExceptionGroup`. The named tests `test_json_url_serving_html_refused` and
  `test_json_url_private_host_refused` pin that it does not.
- **Double fetch.** The added URL is fetched by `resolve_url`, then again under the guard. The
  polite client paces the second request.
- **Not done:** the CLI and TUI. Those are plans 08 and 09.

## Proof

- `uv run --locked pytest packages/ijob-sources packages/ijob-app` passes.
- The gate exits 0.
