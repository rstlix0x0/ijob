---
status: approved
created: 2026-11-02
depends-on: [02, 03, 04]
built-on: 6ee2c2bd4580
---

# Recipe Sources at Search Time Implementation Plan

**Goal:** Read every stored recipe source at search time with no model call.

**Architecture:**
- `ijob_core.model.SourceKind` gains `RECIPE = "recipe"`. The kind lives in the ref text (spec
  §10). Its registry and description arms land in the same task, so every exhaustive `match` stays
  closed.
- `ijob_sources/recipe_source.py` holds `RecipeSource`, a `Source`. It owns pagination and the
  search-time failure wording of spec §4.3, §6 and §9.
  - `RecipeFailedError(SourcesError)` carries those exact messages. It lives in
    `ijob_sources/errors.py`.
- `build_source(row, *, known=…, recipe: str | None = None)` builds a `RecipeSource` from the row
  and the stored recipe text. The recipe is parsed inside `fetch`, not at build time, so an
  unsupported version reaches the failure line (spec §4.3).
- `details.describe` gains a `RECIPE` arm. It runs plan 03's detail XPath on the job page that
  `raw["url"]` names, when `raw` holds an XPath (spec §9). The description lookup is the on-demand
  half of reading a recipe source.
- `Service` attaches plan 02's guard to every client it uses for recipe sources:
  - `_collect` runs the recipe sources through a second, guarded client, concurrently with the
    rest. It merges the two `CollectResult`s.
  - `_describe` uses a guarded client for a recipe sighting.
  - `Service` and `open_service` take a keyword `resolver: HostResolver = system_resolver`. Tests
    pass a fake one, so no test resolves DNS.
- Search loads no LLM code. `recipe_source.py` imports only `ijob_sources` and `ijob_core`.

**Tech Stack:** Python 3.14, lxml and jmespath through plan 03, httpx `MockTransport`, pytest.

---

## Files that change

```
packages/ijob-core/src/ijob_core/model.py                                   — [modify] SourceKind.RECIPE
packages/ijob-core/tests/ijob_core_tests/test_model.py                      — [modify] recipe ref round trip (covered by list(SourceKind))
packages/ijob-sources/src/ijob_sources/errors.py                            — [modify] RecipeFailedError
packages/ijob-sources/src/ijob_sources/recipe.py                            — [modify] read_detail_description
packages/ijob-sources/src/ijob_sources/recipe_source.py                     — [create] RecipeSource
packages/ijob-sources/src/ijob_sources/registry.py                          — [modify] RECIPE arm, recipe parameter
packages/ijob-sources/src/ijob_sources/details.py                           — [modify] RECIPE arm
packages/ijob-sources/tests/ijob_sources_tests/test_errors.py               — [modify] RecipeFailedError messages
packages/ijob-sources/tests/ijob_sources_tests/test_recipe_source.py        — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_registry.py             — [modify] RECIPE builds only with a recipe
packages/ijob-sources/tests/ijob_sources_tests/test_details.py              — [modify] RECIPE description
packages/ijob-sources/tests/ijob_sources_tests/fixtures/recipe/paged_1.html — [create] list page linking to paged_2
packages/ijob-sources/tests/ijob_sources_tests/fixtures/recipe/paged_2.html — [create] list page linking back to paged_1 (a loop)
packages/ijob-sources/tests/ijob_sources_tests/fixtures/recipe/detail.html  — [create] a job page for detail_description
packages/ijob-app/src/ijob_app/service.py                                   — [modify] resolver, guarded clients, recipe rows in _build_sources
packages/ijob-app/tests/ijob_app_tests/test_service.py                      — [modify] recipe search, refusal and describe tests
```

## Order of work

### Task 1 — The recipe kind and its source

**Files:**
- Modify `model.py`, `test_model.py`, `errors.py`, `test_errors.py`, `recipe.py`, `registry.py`,
  `details.py`, `test_registry.py` and `test_details.py`.
- Create `recipe_source.py`, `test_recipe_source.py` and the three fixtures.

**Change:**
- Add `SourceKind.RECIPE = "recipe"`.
- `RecipeFailedError(message)` carries one of these messages, each fixed by the spec:
  - `recipe for <ref> failed: <error>; run: ijob sources reanalyze <ref>`;
  - `recipe version N is not supported; run: ijob sources reanalyze <ref>`;
  - `recipe for <ref> expected <format>; run: ijob sources reanalyze <ref>`;
  - `recipe for <ref> found no jobs; if the site changed, run: ijob sources reanalyze <ref>`.

  It has one classmethod per message.
- `RecipeSource(key, *, company, recipe: str)`. Pagination starts at the recipe's `url`, so it takes
  no `target_url`:
  - `ref` is `SourceRef(SourceKind.RECIPE, key)`.
  - `fetch(client)` parses the recipe:
    - `UnsupportedRecipeVersionError` → the version message;
    - `InvalidRecipeError` → the `failed` message.
  - It then reads pages, starting at `recipe.url`, with plan 03's `fetch_document` and `read_page`.
  - Page 1:
    - a fetch or document error (any `SourcesError`) propagates;
    - a format mismatch → the `expected` message;
    - `RecipeEvaluationError` → the `failed` message;
    - zero valid drafts → the `found no jobs` message.
  - Pages 2–5:
    - a `SourcesError` from fetching or loading, or a format mismatch, stops pagination and keeps
      the jobs read;
    - `RecipeEvaluationError` fails the source with the `failed` message.
  - Pagination follows `next_url`, already resolved against the page's final URL. It stops when:
    - a page has `items == 0`;
    - `next_url` is absent;
    - `next_url` was fetched before;
    - or 5 pages were read.
  - A repeated `external_id` across pages keeps the first. `rejected` sums every page's.
  - It returns `SourceBatch(drafts, rejected)`.
- `read_detail_description(document, xpath) -> str | None` in `recipe.py` evaluates the XPath on the
  cleaned job page. It reads the result as HTML, as `description` is read (spec §4.3), and goes
  through `normalize`. An empty result is `None`. An engine error is `RecipeEvaluationError`.
- `build_source(row, *, known=frozenset(), recipe=None)`:
  - the `RECIPE` arm returns `RecipeSource(key, company=row.company, recipe=recipe)`;
  - it raises `InvalidValueError("recipe", "<ref> has no stored recipe")` when `recipe` is `None`.
  - Update the docstring's `Raises`.
- `describe` `RECIPE` arm:
  - when `raw` holds `detail_description`, it reads `raw["url"]` with `raw_object`, as the YC and
    Let's Get Rusty arms do (`details.py:158`, `:179`);
  - a missing or non-http(s) URL raises `ParseError`;
  - it fetches that URL with `fetch_document`, requires HTML (`UnsupportedUrlError` otherwise), and
    returns `read_detail_description`;
  - without `detail_description`, it returns `None` with no request.
- `details.py`'s module docstring, which counts kinds, is updated for `RECIPE`. The existing
  `test_describe_covers_every_source_kind` (`test_details.py:292-297`) gains `RECIPE` in its
  covered set.

**Proof:** written first, seen failing, then passing. Each test uses `httpx.MockTransport` and a
`PoliteClient` with no wait:
- `test_reads_one_page`: `list_bare_company.html` gives its drafts and `rejected`.
- `test_paginates_and_stops_on_loop`: `paged_1` → `paged_2` → back to `paged_1`. Exactly two pages
  are fetched, and the jobs of both are kept.
- `test_stops_at_five_pages`: six chained pages; the transport sees five.
- `test_stops_on_page_without_items`.
- `test_page_two_failure_keeps_page_one`: page 2 answers 500, robots-disallowed, wrong content type,
  or over 5 MiB. Each case returns page 1's jobs and no error.
- `test_page_one_failure_propagates`: parametrized over page-1 failures: a 500 raises
  `HttpStatusError`; a robots disallow raises `RobotsDisallowedError`; `text/plain` and a body over
  5 MiB raise `UnsupportedUrlError`.
- `test_no_jobs_message`: page 1 with zero valid drafts raises `RecipeFailedError`, and the message is
  exactly the `found no jobs` text with the ref.
- `test_format_mismatch_message`: a `json` recipe served HTML.
- `test_evaluation_error_message`: XPath `foo()` as a field.
- `test_unsupported_version_message`: a stored `"version": 2`.
- `test_duplicate_ids_across_pages_keep_first`.
- `test_recipe_builds_only_with_recipe` in `test_registry.py`. Without `recipe`, the build raises
  `InvalidValueError`. `test_every_kind_builds` passes a recipe for `RECIPE`.
- `test_recipe_detail_description` in `test_details.py`: `raw` with `detail_description` and `url`
  fetches `detail.html` from that URL and returns its Markdown. `raw` without it returns `None`
  with no request.
- `test_recipe_detail_description_empty_is_none`: an XPath matching nothing gives `None`.
- `test_recipe_detail_description_bad_url`: a `raw` `url` of `mailto:x` raises `ParseError` with no
  request.
- `test_recipe_failed_messages` in `test_errors.py`.

**Done when:** `uv run --locked pytest packages/ijob-sources packages/ijob-core` passes and the
CLAUDE.md gate is green. Commit `feat(sources): read recipe sources`.

### Task 2 — Service wiring with guarded clients

**Files:**
- Modify `service.py` and `test_service.py`.

**Change:**
- `Service.__init__` and `open_service` gain a keyword `resolver: HostResolver = system_resolver`,
  documented in `Args`.
- `_build_sources` passes `recipe=self._store.recipe(row.ref).recipe` for a `RECIPE` row. It passes
  `None` when there is no row, which the registry refuses and `refused` already reports.
- `_collect(sources)` splits the sources by `ref.kind is SourceKind.RECIPE`:
  - the others go through the existing client;
  - the recipe sources go through a second client from the same factory, after
    `guard_requests(public_host_guard(self._resolver))`.
  - Both `collect` calls run in one `asyncio.TaskGroup`, and their batches and failures are merged.
  - With no recipe sources, no second client is opened.
- `_describe` opens a guarded client for a recipe sighting, the same way, and the existing client
  for the rest.

**Proof:** written first, seen failing, then passing. Each test uses the existing
`client_factory_for` fixture and a fake resolver:
- `test_search_reads_recipe_source`: a stored recipe source and recipe give postings, with no
  model configured.
- `test_recipe_source_private_host_fails_alone`: a recipe URL whose host resolves to `10.0.0.1`
  fails with `<origin>/robots.txt is not a public address`, the first request to that origin (spec
  §6). A greenhouse source in the same refresh still succeeds.
- `test_recipe_source_reserved_host_fails_alone`: a recipe URL on `www.japan-dev.com` fails with
  `www.japan-dev.com is read only by its own source kind`.
- `test_page_two_private_host_keeps_page_one`: page 1's `next_page` points to a host resolving to
  `10.0.0.1`. The refresh keeps page 1's postings and reports no failure.
- `test_non_recipe_sources_are_not_guarded`: a fake resolver that raises on any call is never
  called during a refresh without recipe sources.
- `test_recipe_row_without_recipe_is_refused`.
- `test_describe_recipe_sighting_is_guarded`: the job page host resolving to `127.0.0.1` gives the
  refusal as the description reason.
- `test_recipe_failure_carries_reanalyze_hint`: the `found no jobs` message reaches the
  `RefreshReport` failure.

**Done when:** `uv run --locked pytest packages/ijob-app/tests/ijob_app_tests/test_service.py`
passes and the CLAUDE.md gate is green. Commit `feat(app): read recipe sources through a guarded client`.

## Risks

- **Riskiest step:** Task 2's concurrent collect over two clients. Both clients must close on this
  loop, even when one `collect` raises. Nest both `async with` blocks around the task group.
- **Search reports.** The CLI and TUI failure lines print `str(error)`, so the spec's wording
  reaches the user unchanged. No renderer change is needed.
- **`sources add recipe:<key>`** is refused. `Service.add_source` builds without a recipe, so
  `build_source` raises `InvalidValueError`, which `_source_failures` (`cli.py:339-356`) maps to exit 2.
  Task 1's `test_recipe_builds_only_with_recipe` covers the build refusal.

## Proof

- `uv run --locked pytest packages/ijob-core packages/ijob-sources packages/ijob-app` passes.
- The gate exits 0.
