---
status: done
created: 2026-11-02
depends-on: [01, 02]
built-on: 6ee2c2bd4580
---

# Recipe Engine Implementation Plan

**Goal:** Read job drafts from one fetched page with a validated v1 recipe.

**Architecture:**
- `ijob-sources` owns `lxml` and `jmespath` (spec §3, §10).
- `ijob_sources/recipe_document.py` holds the §4.1 document a recipe reads:
  - `RecipeFormat` (`StrEnum`: `HTML = "html"`, `JSON = "json"`);
  - `HtmlDocument` (the cleaned `lxml` tree) and `JsonDocument` (the parsed value);
  - `load_document(body, content_type, url)`, `fetch_document(client, url)` and
    `sample_text(document)`.
- `ijob_sources/recipe.py` holds the v1 type, `parse_recipe(text) -> Recipe` with the §4.3 validity
  rules, `recipe_text(recipe)` (canonical JSON for storage), and evaluation:
  `read_page(recipe, document, *, page_url, ref, company) -> PageRead`. This is the placement spec
  §10 names. It imports `recipe_document`, never the reverse, so there is no import cycle.
- New errors in `ijob_sources/errors.py`, all `SourcesError`:
  - `InvalidRecipeError(reason)` reads `invalid recipe: <reason>`;
  - `UnsupportedRecipeVersionError(version)` reads `recipe version N is not supported`;
  - `RecipeEvaluationError(reason)` wraps any error either engine raises while evaluating.

  Plans 05 and 07 add the ref and `reanalyze` wording that spec §4.3 and §9 fix.
- No XPath extension function and no JMESPath custom function is registered (spec §3).

**Tech Stack:** Python 3.14, lxml 6.1.3 (`lxml.html`, `etree.XPath`), jmespath 1.1.0, httpx, pytest.

---

## Files that change

```
packages/ijob-sources/pyproject.toml                                        — [modify] add lxml>=6.1.3,<7 and jmespath>=1.1.0,<2
uv.lock                                                                     — [modify] uv lock
pyproject.toml                                                              — [modify] mypy override: ignore_missing_imports for lxml, lxml.*, jmespath, jmespath.*
packages/ijob-sources/src/ijob_sources/errors.py                            — [modify] InvalidRecipeError, UnsupportedRecipeVersionError, RecipeEvaluationError
packages/ijob-sources/src/ijob_sources/recipe_document.py                   — [create] §4.1 formats, cleaning, sampling, fetching
packages/ijob-sources/src/ijob_sources/recipe.py                            — [create] Recipe, parse_recipe, recipe_text, read_page, PageRead
packages/ijob-sources/tests/ijob_sources_tests/test_errors.py               — [modify] the three new messages
packages/ijob-sources/tests/ijob_sources_tests/test_recipe_document.py      — [create]
packages/ijob-sources/tests/ijob_sources_tests/test_recipe.py               — [create]
packages/ijob-sources/tests/ijob_sources_tests/fixtures/recipe/__init__.py  — [create] fixture package marker
packages/ijob-sources/tests/ijob_sources_tests/fixtures/recipe/list_bare_company.html — [create] python.org-style list, bare-text company, made-up jobs
packages/ijob-sources/tests/ijob_sources_tests/fixtures/recipe/list_category_salary.html — [create] WWR-style list, salary in a category tag
packages/ijob-sources/tests/ijob_sources_tests/fixtures/recipe/jobs.json   — [create] Working Nomads-style JSON list
```

The fixtures hold made-up companies, titles and URLs. They copy only the shapes quoted in spec §2, and no
personal data.

## Order of work

### Task 1 — Dependencies and the recipe type

**Files:**
- Modify `packages/ijob-sources/pyproject.toml`, the root `pyproject.toml` and `uv.lock`.
- Modify `errors.py` and `test_errors.py`.
- Create `recipe_document.py` (only `RecipeFormat` in this task) and `recipe.py`.
- Test `test_recipe.py`.

**Change:**
- Declare `lxml>=6.1.3,<7` and `jmespath>=1.1.0,<2` in ijob-sources, then run `uv lock`.
- Add one `[[tool.mypy.overrides]]` with `ignore_missing_imports = true` for
  `lxml`, `lxml.*`, `jmespath` and `jmespath.*`. It follows the existing `dspy` override, and adds no
  stub packages.
- `Recipe` and `RecipeFields` are frozen, slotted, keyword-only dataclasses. Their fields mirror
  spec §4.2: `version`, `format`, `url`, `items`, `fields` (title, url, company, location,
  posted_at, description, salary, external_id), `salary_defaults` (plan 01's `SalaryDefaults`),
  `next_page` and `detail_description`. The optional ones are `str | None`.
- `parse_recipe(text)` raises `InvalidRecipeError` when:
  - the text is not a JSON object;
  - any key is unknown, at the top level or inside `fields` or `salary_defaults`;
  - a required key is missing;
  - `format` is not `html` or `json`;
  - `url` is not an absolute http(s) URL;
  - any expression is not a non-empty string of at most 500 characters;
  - any expression does not compile: `etree.XPath` for `html`, `jmespath.compile` for `json`
    (catch `etree.XPathError` and `jmespath.exceptions.JMESPathError`);
  - `salary_defaults.currency` is not in plan 01's `KNOWN_CURRENCIES`;
  - `salary_defaults.period` is not a `Period`;
  - `detail_description` is given for `format: "json"`.
- `version` is checked first:
  - a value that is not a JSON integer (a string, a float, `true` or `false`) raises
    `InvalidRecipeError`;
  - an integer other than `1` raises `UnsupportedRecipeVersionError`.
- `recipe_text(recipe)` writes the recipe as sorted-key JSON. `parse_recipe(recipe_text(r)) == r`.

**Proof:** written first, seen failing, then passing:
- `test_valid_html_recipe_parses` and `test_valid_json_recipe_parses`: all keys, including the
  optional ones.
- `test_round_trip_through_recipe_text`.
- `test_unknown_key_rejected`: parametrized over the top level, `fields` and `salary_defaults`.
- `test_missing_required_key_rejected`: parametrized over `version`, `format`, `url`, `items`,
  `fields.title` and `fields.url`.
- `test_expression_limits`: an empty string, whitespace only, and 501 characters are rejected.
  Exactly 500 characters is accepted.
- `test_xpath_syntax_error_rejected`: `//li[` is rejected at parse.
- `test_jmespath_syntax_error_rejected`: `title[` is rejected at parse.
- `test_unknown_function_compiles`: XPath `foo()` and JMESPath `foo(@)` parse. They fail only at
  evaluation (Task 3).
- `test_version_two_unsupported`: `2` raises `UnsupportedRecipeVersionError`.
- `test_version_not_an_integer_invalid`: `true`, `"1"` and `1.0` each raise `InvalidRecipeError`.
- `test_bad_salary_defaults_rejected`: currency `usd`, currency `XXX`, and period `week`.
- `test_detail_description_only_for_html`.
- `test_non_object_and_bad_json_rejected`: `[]`, `""` and `{`.
- `test_error_messages` in `test_errors.py`: the three new messages.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_recipe.py packages/ijob-sources/tests/ijob_sources_tests/test_errors.py`
passes and the CLAUDE.md gate is green. Commit `feat(sources): add the recipe v1 type`.

### Task 2 — The document a recipe reads

**Files:**
- Modify `recipe_document.py`.
- Test `test_recipe_document.py`.

**Change:**
- `MAX_BODY_BYTES = 5 * 1024 * 1024`, `SAMPLE_LIMIT = 50_000`, `JSON_SAMPLE_ITEMS = 3` and
  `JSON_SAMPLE_CHARS = 300`.
- `load_document(body: bytes, content_type: str | None, url: str) -> HtmlDocument | JsonDocument`:
  - the format comes from the media type before any `;`, lowercased:
    - `application/json` or any `+json` type is JSON;
    - `text/html` or `application/xhtml+xml` is HTML;
    - a missing header means the first non-whitespace byte decides: `{` or `[` is JSON, `<` is HTML.
  - It raises `UnsupportedUrlError(url, reason)` (spec §4.1) for:
    - a body over `MAX_BODY_BYTES`;
    - any other media type;
    - an empty or undecidable body;
    - JSON that is not UTF-8 or does not parse;
    - HTML that `lxml` cannot parse.
- HTML cleaning, on the tree from `lxml.html.document_fromstring`:
  - drop `script`, `style`, `svg`, `noscript`, `iframe` and `template` with `drop_tree`, keeping tail
    text;
  - strip comments;
  - delete every `style` attribute and every attribute whose name starts with `on`;
  - keep every other attribute.
- `fetch_document(client: PoliteClient, url: str)` calls plan 02's `client.get(url,
  follow_redirects=True)`. It passes the response's body and `Content-Type` to `load_document`, and
  returns the document and the final URL (`str(response.url)`). URLs in the page resolve against
  that final URL (spec §6 "Redirects").
- `sample_text(document) -> str`:
  - HTML: the cleaned tree serialized as unicode, with every whitespace run collapsed to one space.
    Over `SAMPLE_LIMIT` characters, it is cut to `SAMPLE_LIMIT` plus `\n[truncated]`.
  - JSON: a copy with every array cut to its first `JSON_SAMPLE_ITEMS` items and every string to
    `JSON_SAMPLE_CHARS` characters, at any depth, serialized with `ensure_ascii=False`.

**Proof:** written first, seen failing, then passing:
- `test_format_from_content_type`: parametrized. `application/json; charset=utf-8`,
  `application/ld+json` and `application/vnd.api+json` are JSON; `text/html; charset=utf-8` and
  `application/xhtml+xml` are HTML.
- `test_format_sniffed_without_header`: `  [1]`, `{"a":1}` and `<html>` are sniffed.
- `test_other_types_refused`: `text/plain`, `application/xml`, and an empty body with no header each
  raise `UnsupportedUrlError`.
- `test_body_over_cap_refused`: `MAX_BODY_BYTES + 1` bytes is refused, and exactly `MAX_BODY_BYTES`
  is accepted.
- `test_bad_json_refused`: invalid UTF-8 and `{"a":` are refused.
- `test_cleaning_drops_scripts_styles_and_handlers`: no `script`, `style`, `svg`, `noscript`,
  `iframe` or `template` element, comment, `style` attribute or `onclick` attribute remains. `href`,
  `class`, `id` and `datetime` remain, and text after a dropped `<script>` remains.
- `test_html_sample_collapses_and_truncates`: a page whose cleaned form is over 50,000 characters
  gives exactly 50,000 characters plus `\n[truncated]`. A short page has no marker.
- `test_json_sample_cuts_arrays_and_strings`: a 35-item list of objects with 1,000-character
  strings gives 3 items, each string 300 characters, nested arrays cut too.
- `test_fetch_document_uses_final_url`: through `httpx.MockTransport`, a redirect to `/jobs/`
  gives the final URL `…/jobs/`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_recipe_document.py`
passes and the CLAUDE.md gate is green. Commit `feat(sources): load and sample recipe documents`.

### Task 3 — Evaluate a recipe on a page

**Files:**
- Modify `recipe.py`.
- Create the `fixtures/recipe/` package and its three fixtures.
- Test `test_recipe.py`.

**Change:**
- `PageRead` (frozen, slotted) has `items: int` (how many entries `items` selected),
  `drafts: list[SightingDraft]`, `rejected: int` and `next_url: str | None`. Plan 05 stops
  pagination on `items == 0`.
- `read_page(recipe, document, *, page_url, ref, company) -> PageRead`:
  - `items` is evaluated on the whole document. A result other than a list gives zero items (spec
    §4.3).
  - Each field expression is evaluated with the item as its context node (XPath) or input
    (JMESPath).
  - For `html`, an item that is not an element makes evaluation raise. That is an evaluation error,
    as for any other engine error.
- Field text follows spec §4.3:
  - a string as it is;
  - an integer, or a float with no fractional part, as integer text (`142.0` → `142`);
  - another float as its decimal text;
  - an element as its `text_content()`;
  - a list by its first entry, read by the same rules;
  - a bool, a dict or `None` is absent.
  - The text is then stripped and whitespace-collapsed. Empty is absent.
- `description` is read as HTML: an element's own markup through `lxml.html.tostring`, or the string.
  It goes through `normalize(…, TextForm.HTML)`.
- `url`, and `next_page`'s result, are resolved with `urljoin(page_url, …)`. A URL that is not
  http(s) after resolution is invalid.
- Missing values:
  - absent `external_id` → the resolved URL;
  - absent `company` → the `company` argument.
- `posted_at`: `datetime.fromisoformat`; a naive time is UTC, and a date alone is midnight UTC. Any
  other text is absent, and the job is kept.
- `salary`: plan 01's `parse_salary` with the recipe's `salary_defaults`. `None` gives no salaries.
- `raw` is sorted-key JSON holding:
  - the item's evaluated field texts;
  - the resolved job URL under `url`, which replaces the raw `url` text (spec §9);
  - when the recipe has `detail_description` and no `fields.description`, that XPath under
    `detail_description`.
- An item with no title or no valid URL, or one that `SightingDraft` refuses (`InvalidValueError`),
  is counted in `rejected`. A repeated `external_id` within the page keeps the first and is not
  counted.
- Any error either engine raises while evaluating becomes `RecipeEvaluationError` (spec §4.3). That
  covers `etree.LxmlError` (lxml's error base), `jmespath.exceptions.JMESPathError`, `TypeError` and
  `ValueError`.
  - The engine calls sit in one helper per engine. That helper also catches `Exception`, with a
    `# noqa: BLE001` that says it is the isolation point spec §4.3 asks for. So an engine error the
    list misses cannot escape `collect` as an `ExceptionGroup`.

**Proof:** written first, seen failing, then passing. Each test uses a fixture or a small inline
document:
- `test_reads_bare_text_company_list`: `list_bare_company.html` with
  `normalize-space(.//span[@class='listing-company-name']/text()[last()])` reads every job, with its
  bare-text company and its `datetime`-attribute `posted_at`.
- `test_reads_category_salary_list`: `list_category_salary.html` gives a job whose salary is
  50000–74999 USD year from `$50,000 - $74,999 USD` with defaults (USD, year). Its per-item company
  overrides the `company` argument.
- `test_reads_json_list`: `jobs.json` with `[*]` and keys `title`, `url`, `company_name` and
  `pub_date` gives all items, and `posted_at` keeps the `-04:00` offset.
- `test_field_text_rules`: parametrized over the §4.3 result types:
  - a string is read as it is;
  - XPath `count(…)` giving `142.0` reads as `142`;
  - JMESPath `length(@)` giving the integer `3` reads as `3`;
  - a float `1.5` reads as `1.5`;
  - an element reads as its text;
  - an XPath node set reads as its first entry;
  - a JMESPath list `["a", "b"]` reads as `a`;
  - XPath `boolean(…)`, a JSON object, a missing key and whitespace only are absent.
- `test_items_not_a_list_gives_zero`: XPath `count(//li)` and JMESPath `length(@)` as `items` give
  zero drafts.
- `test_unknown_function_raises_evaluation_error`: XPath `foo()` and JMESPath `foo(@)` raise
  `RecipeEvaluationError`.
- `test_relative_urls_resolve_against_page`: `/jobs/1/` on `https://example.org/list?page=2`.
- `test_missing_title_or_bad_url_rejected`: a `mailto:` URL and an empty title are each counted in
  `rejected`.
- `test_external_id_defaults_to_url` and `test_duplicate_external_id_keeps_first`.
- `test_posted_at_forms`: `2026-10-23T22:34:52.166674+00:00`, a naive time read as UTC, `2026-10-23`
  as midnight UTC, and `yesterday` as absent with the job kept.
- `test_description_markdown_from_node`: a node with `<p>a <a href="https://x.example/">b</a></p>`
  gives Markdown with the link.
- `test_raw_holds_fields_and_detail_xpath`: for a relative `/jobs/1/` on `https://example.org/`,
  `raw` parses as JSON with `url` equal to `https://example.org/jobs/1/`, the evaluated `title`
  text, and `detail_description` equal to the recipe's XPath. A recipe with `fields.description`
  has no `detail_description` key.
- `test_next_url_resolved` and `test_no_next_page_gives_none`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_recipe.py`
passes and the CLAUDE.md gate is green. Commit `feat(sources): evaluate recipes into job drafts`.

## Risks

- **Riskiest step:** Task 3's field-text rule for lxml string results. Attribute and text XPath
  results come back as `str` subclasses, and must go down the string arm before the element arm.
- **lxml typing.** With `ignore_missing_imports`, lxml values are `Any` to mypy. Keep them inside
  `recipe_document.py` and `recipe.py`, and convert at the boundary to `str`, `list` and the two
  document types.
- **Not done:** pagination, the `reanalyze` wording, and source wiring. Those are plan 05.

## Proof

- `uv run --locked pytest packages/ijob-sources` passes.
- The gate exits 0.

## Review findings

- fixed — `recipe_document.py:191` a `charset=` Python knows but libxml2 does not (`latin-1`, `utf-8-sig`, `rot13`, `base64`) raised `LookupError` out of `load_document`; the parser is now built inside the try and retries with `encoding=None`. `uv run --locked pytest packages/ijob-sources` → `1820 passed`.
- fixed — `recipe_document.py:223` JSON nested ≥1000 levels raised a bare `RecursionError` in `sample_text`; `load_document` now refuses JSON deeper than `MAX_JSON_DEPTH = 64` (measured iteratively) with `UnsupportedUrlError`. Same run as above.
- fixed — `recipe.py:670` `_read_result` recursed on a list's first entry outside the isolation; it now loops. Same run as above.
- fixed — `recipe.py:616` `detail_description` went into `raw` when the item's description was absent; it now goes in only when the recipe has no `fields.description`, as Task 3 says. Same run as above.
- fixed — `recipe.py:724` a `posted_at` outside the UTC range (`9999-12-31T23:59:59-05:00`) was kept and made `Store.record` raise `OverflowError`; it is now absent and the job kept. Same run as above; full `uv run --locked pytest` → `2950 passed`.
- open — `recipe_document.py:96` the 5 MiB cap is checked after `PoliteClient.get` buffers the whole body, so a huge or decompression-bomb response is held in memory before refusal. Needs a streamed byte limit in the client.
- open — nits: `_isolated` docstring says it is the one place engine errors are caught (compile helpers also catch); nothing pins the `except Exception` catch-all; `_resolve` keeps an out-of-range port; a json recipe's invalid `detail_description` reports "does not compile" before "html only"; `version: int` could be `Literal[1]`; UTF-8 JSON with a BOM is refused; `RecipeEvaluationError` docstring narrower than its uses.

## Deviations

- 2026-11-02 — The board row was not clean at start (`2026-11-01-llm-layer` reads `spec disproven by plan 07`); this chain's row was clean.
- 2026-11-02 — The dependency step ran on the main thread before the coders, so the parallel plan 04 coder never saw a stale lock. `jmespath` 1.1.0 was already in `uv.lock`; only `lxml` 6.1.3 was added. The mypy entry was merged into the existing `dspy` override (`module = ["dspy", "dspy.*", "jmespath", "jmespath.*", "lxml", "lxml.*"]`) instead of a separate one.
- 2026-11-02 — Task 3 asks for `# noqa: BLE001` on the engine wrapper's `except Exception`. `uv run --locked ruff check --no-fix` reports it RUF100 (unused), because BLE001 exempts a handler that re-raises with `from`. A plain comment marks the isolation point instead.
- 2026-11-02 — `etree.XPath("a\x00b")` raises a plain `ValueError`, not `XPathError`; `parse_recipe` also catches `ValueError`.
- 2026-11-02 — Decided where the plan is silent: a non-finite float field reads as absent; `recipe_text` and `raw` keep `ensure_ascii=True`; field text that cannot encode as UTF-8 is absent; `load_document` honours a `charset=` in `Content-Type`; an absent `salary_defaults` is `SalaryDefaults(None, None)` and is omitted from `recipe_text`; `fetch_document` is async and returns `(document, final_url)`; `str(RecipeEvaluationError)` is the engine's text with no prefix.
- 2026-11-02 — Beyond the plan, from review: JSON is refused past 64 levels of nesting (`MAX_JSON_DEPTH`).
- 2026-11-02 — The three tasks ran in one coder, sequentially, and plans 03 and 04 were reviewed by one reviewer over their combined diff. The task-briefer ledger and briefs were skipped; the coder read the plan's task sections directly.
- 2026-11-02 — Not committed: the instruction to execute did not ask for commits.
