---
status: superseded
created: 2026-11-02
built-on: 7455820d41fe
---

# Spec: LLM-learned source recipes

ijob refuses any URL that is not a known hiring system, a single schema.org `JobPosting` page or an
RSS feed. This spec lets the user add such a URL anyway. A model studies a fetched sample, once,
and proposes a **recipe**: declarative data that says where the jobs are and which expression reads
each field. The user sees what the recipe reads from the page and confirms it. From then on, a
generic parser reads the source with the recipe and no model call, on every search and in the TUI.

Probe artifacts cited below live in the session scratchpad
`/private/tmp/claude-501/-Users-hiraq-Projects-rstlix0x0-ijob/414123fb-5882-46e9-9bbe-ce9eef8996d3/scratchpad/`
(written `<scratch>`): `probe_measure.py`, `probe_engines.py`, `probe_eval.py`, `probe_rules.py`,
and the pages in `probe-pages/`.

## 1. Premises (what exists today)

- **Unknown URLs are refused.** Probe:
  `IJOB_HOME=<scratch>/probe-home uv run --locked ijob sources add-url https://www.python.org/jobs/ --company "Python Software Foundation"`
  printed `error: cannot use https://www.python.org/jobs/: no hiring system, job posting or feed found`
  and exited 1.
- **The add path.**
  - CLI `sources add-url --company` (`ijob_app/cli.py:423-436`) calls `Service.add_url(url, company)`
    (`ijob_app/service.py:840-867`).
  - That calls `resolve_url` (`ijob_sources/detect.py:63-103`), which raises `UnsupportedUrlError`
    in two places: at `:98`, when the fetched page is neither a `JobPosting` page nor a feed, and at
    `:100`, when a feed parses with no items.
  - `UnsupportedUrlError` (`ijob_sources/errors.py:116`) is a `SourcesError`, which the CLI prints
    as `error: …`, exit 1 (`cli.py:340-356`).
  - The source is stored by `Store.add_source` (`ijob_core/store.py:1149-1170`). That is an upsert
    that only clears `removed`.
- **The JobPosting and feed refs.** These kinds use `manual_key(url)` (`ijob_sources/manual.py:45`).
  Probe (`<scratch>/probe_rules.py`): `https://www.python.org/jobs/ -> d4109ca35795779a`.
- **Storage.**
  - The `sources` table (`store.py:84-89`) has `ref TEXT PRIMARY KEY, company TEXT NOT NULL,
    target_url TEXT, origin, removed, last_fetched_at`.
  - The kind is the prefix of `ref`; there is no kind column.
  - The latest migration is v4 (`store.py:108-146`).
- **Source kinds.**
  - `SourceKind` is a `StrEnum` with 19 members (`ijob_core/model.py:61-79`, counted).
  - `build_source(row)` maps a `SourceRow` to its adapter (`ijob_sources/registry.py:47-111`).
  - A `Source` has `ref` and `async fetch(client) -> SourceBatch` (`ijob_sources/collect.py:49-70`).
  - `SourceBatch` holds `drafts` and `rejected` (`collect.py:33-46`).
- **A job draft.** `SightingDraft` (`model.py:189-227`) requires non-empty `external_id`, `title`,
  `company` and an http(s) `url`. Its other fields are `location`, a timezone-aware `posted_at`,
  `salaries: tuple[Money, ...]`, `country`, `remote`, `description` (Markdown) and `raw`.
- **Money.** `Money(min, max, currency, period)` (`ijob_core/money.py:28-66`) needs at least one
  bound, non-negative finite bounds, `min ≤ max`, and `currency` matching `[A-Z]{3}`
  (`money.py:15`). `Period` is `hour`, `month` or `year` (`money.py:19-24`). Salaries are built from
  structured fields per source (e.g. `greenhouse.py:319`). There is no free-text salary parser.
- **Description to Markdown.** `normalize(text, TextForm.HTML)` (`ijob_sources/description.py:538`)
  turns HTML into Markdown. Probe on the Working Nomads item 0 description gave
  `'[Lemon.io](https://lemon.io/for-developers/?utm_source=…) helps companies in the US'`.
- **Polite fetching.**
  - `PoliteClient.get` (`ijob_sources/polite_http.py:166-224`) checks robots.txt, sends
    `USER_AGENT` (`polite_http.py:34-37`), retries 429/503, caches, and follows redirects
    (`polite_http.py:262-264`, `_send(… follow_redirects=True)`, up to five per `:283`).
  - robots.txt is fetched through the client's own send callable (`robots.py:51-60`, `:131-135`).
  - No response-size limit exists in `polite_http.py`: a grep for
    `limit|max_bytes|content-length` matched nothing. The same grep finds `robots.py:22`
    `_PARSE_LIMIT_BYTES` as the control.
- **On-demand description.** `details.describe(sighting, client)` (`ijob_sources/details.py:47-107`)
  reads the description from the stored `raw`, or fetches the job page (YC, Workable, Let's Get
  Rusty).
- **Pagination cap.** The paged sources stop at 5 pages (`arbeitnow.py:35`, `fourdayweek.py:38`).
- **LLM runtime (chain 2026-11-01-llm-layer).**
  - `Task` has `cv_profile`, `instructions`, `judge` and `scorer` (`ijob_ai/llm/settings.py:12-18`).
  - `[models]` keys are validated against `Task` (`ijob_app/llm_config.py:128-140`).
  - Tasks live in `ijob_ai/tasks/`, with pydantic outputs, signatures and wrappers that call
    `run_call`. Fakes are in `packages/ijob-ai/tests/ijob_ai_tests/llm/fakes.py`.
  - `ijob-ai` depends only on `ijob-core` (`packages/ijob-ai/pyproject.toml`).
- **Hosts the source rules name.**
  - JapanDev's host is `japan-dev.com` (`japandev.py:37`).
  - JobStreet appears in no code: `grep -rli jobstreet packages/` matched nothing. The same search
    finds `japandev.py`, `registry.py` and `details.py` for `japan-dev` as the control. It appears
    only in CLAUDE.md.
- **TUI.** `s` opens `SourcesScreen` (`tui/app.py:341`, `tui/sources.py:42`), which adds and removes
  sources. Its error line handles `InvalidValueError | SourcesError` (`tui/sources.py:139`).

## 2. Evidence from real pages

Each page was fetched once, after its robots.txt (saved as `probe-pages/robots-*.txt`).

| Page | robots.txt (`User-agent: *`) | Raw | Cleaned (§4.1) | Jobs |
|---|---|---|---|---|
| `www.python.org/jobs/` | `Disallow: /~guido/orlijn/`, `/webstats/` (`robots-python.txt:21-23`) | 59,322 chars | 43,398 | 19 |
| `weworkremotely.com/categories/remote-back-end-programming-jobs` | `Allow: /` (`robots-wwr.txt:3-4`) | 92,330 | 48,212 | 11 |
| `www.workingnomads.com/jobs` | `Disallow:` empty (`robots-wn.txt:1-2`) | 89,405 | 12,170 | 0 job links: rendered by JavaScript |
| `www.workingnomads.com/api/exposed_jobs/` (`Content-Type: application/json`) | as above | 168,188 | 1,880 (§4.1 JSON) | 35 |
| `remotive.com` | `Disallow: /jobs/*`, `/api/*` (`robots-remotive.txt:10,16`) | not fetched | — | — |

Notes:
- **Sizes.** "Raw" and the job counts come from `probe_measure.py` and `probe_eval.py`. "Cleaned"
  comes from `probe_eval.py`.
- **Working Nomads names its JSON URL** in a footer link: `probe_eval.py` printed
  `<a href="/api/exposed_jobs/" target="_self" rel="nofollow">API</a>`. None of the three HTML
  pages names a JSON or `/api/` URL inside `<script>` (`script url candidates []`).
- **The JSON shape.** `probe_eval.py` (JMESPath `[*]`) printed `items list 35`, and item 0 has keys
  `title, url, company_name, location, pub_date, description, tags, category_name`.
- **python.org's bare-text company** (`probe-pages/pyjobs.html:404-410`):
  `<span class="listing-company-name"><a href="/jobs/8183/">Senior Python Engineer</a><br/> webbyfox consultancy</span>`.
- **WWR's salary** sits in a category tag:
  `<p class="new-listing__categories__category"> $50,000 - $74,999 USD </p>`. Another reads
  `$100,000 or more USD`, and another `$20/hr+`.
- **Working Nomads' pay text** in descriptions includes `$15.00–$17.00 per hour` and
  `$16–$24 per hour`.

## 3. Engines

A recipe is evaluated with XPath 1.0 through `lxml` for HTML, and JMESPath through `jmespath` for
JSON.

- **Availability.**
  - `uvx --python 3.14 --with lxml --with cssselect python -I -c …` printed
    `3.14.10 lxml 6.1.3 cssselect 1.6.0`.
  - `lxml` is not locked (`grep -c '^name = "lxml"' uv.lock` → `0`; the control `jmespath` → `1`).
    `jmespath` 1.1.0 is locked (`uv.lock:1037-1038`, via `botocore`, `uv.lock:254-255`).
  - `ijob-sources` declares both.
- **XPath on the probes** (`probe_engines.py`):
  - `xpath py items 19`;
  - `company webbyfox consultancy`, from
    `normalize-space(.//span[@class='listing-company-name']/text()[last()])`;
  - `posted 2026-10-23T22:34:52.166674+00:00`;
  - `xpath wwr items 11`.
- **JMESPath on the probe** (`probe_eval.py`): item 0 gives `title Senior Magento Developer`,
  `url https://www.workingnomads.com/job/go/1981017/`, `company Lemon.io` and
  `posted 2026-10-29T09:50:51-04:00`.
- **Behaviour the rules below rely on** (`probe_eval.py`):

  | Expression | Result |
  |---|---|
  | XPath `//li[` | `XPathSyntaxError` at compile |
  | XPath `foo()` | compiles, then `XPathEvalError: Unregistered function` on evaluation |
  | XPath `count(//li)` | `float 142.0` |
  | XPath `boolean(//li)` | `bool True` |
  | XPath `//li` | `list 142` |
  | XPath `string(//title)` | a string |
  | JMESPath `title[` | `IncompleteExpressionError` at compile |
  | JMESPath `foo(@)` | compiles, then `UnknownFunctionError` on evaluation |
  | JMESPath `@` on an item | a `dict` |
  | JMESPath `length(@)` | an `int` |
  | JMESPath `contains(…)` | a `bool` |
  | JMESPath missing key | `None` |

- **Alternatives rejected.**
  - CSS via `selectolax` (1.0.2 installs on 3.14) reads the bare text only through
    `span.text(deep=False)`, so the format would need extra flags.
  - A stdlib-only selector language would be new code that no model knows.
- **No code in a recipe.** ijob registers no XPath extension function and no JMESPath custom
  function, so an expression can only select and compute.

## 4. Recipe v1

### 4.1 The document a recipe reads

- **HTML** is parsed with `lxml.html`, then cleaned:
  - drop `script`, `style`, `svg`, `noscript`, `iframe` and `template` elements and comments;
  - drop every `style` and `on*` attribute;
  - keep the other attributes.

  The model's sample is that cleaned tree, serialized with whitespace collapsed and cut to 50,000
  characters plus `\n[truncated]`. The largest probe, WWR, is 48,212. The recipe is always
  evaluated on the same cleaned tree, never the raw page, so the model writes expressions against
  the structure they run on. `probe_eval.py` re-parsed the cleaned python.org page and read
  `re-parsed items 19 company webbyfox consultancy`.
- **JSON** is parsed as UTF-8. The model's sample has every array cut to its first 3 items and
  every string to 300 characters (Working Nomads: 168,188 → 1,880). The recipe is evaluated on the
  full document.
- **Which one.**
  - A `Content-Type` of `application/json` or any `+json` type means JSON.
  - `text/html` or `application/xhtml+xml` means HTML.
  - A missing header means the body's first non-space byte decides: `{` or `[` is JSON, `<` is
    HTML.
  - Anything else, unparseable JSON, or a body over 5 MiB is refused: `UnsupportedUrlError` at add,
    and the source's failure line at search (§11).

### 4.2 Keys

| Key | Required | Value |
|---|---|---|
| `version` | ✓ | the integer `1` |
| `format` | ✓ | `"html"` or `"json"` |
| `url` | ✓ | the http(s) URL to fetch: the added URL, or a JSON URL from the page (§7) |
| `items` | ✓ | an expression selecting each job: XPath for `html`, JMESPath for `json` |
| `fields.title`, `fields.url` | ✓ | an expression evaluated on each item |
| `fields.company`, `fields.location`, `fields.posted_at`, `fields.description`, `fields.salary`, `fields.external_id` | – | as above |
| `salary_defaults.currency` | – | three uppercase letters from §5's list |
| `salary_defaults.period` | – | `hour`, `month` or `year` |
| `next_page` | – | an expression on the whole page that gives the next page's URL |
| `detail_description` | – | `html` only: an XPath on a job's own page that gives its description |

### 4.3 Rules

- **Validity.** Every expression is a non-empty string of at most 500 characters that compiles in
  its engine. Unknown keys are rejected. A recipe that fails any of this is invalid.
- **`items`** must evaluate to a list or node set. Any other result means zero items.
- **Field text.** A field's value is the expression's result read as text:
  - a string as it is;
  - an integer, or a float with no fractional part, as its integer text (`142.0` → `142`);
  - another float as its decimal text;
  - a node as its text content;
  - a list or node set by its first entry, read by these same rules;
  - a boolean, a JSON object or `None` → absent.

  The text is then stripped and whitespace-collapsed. An empty result is absent.
- **`description`** is read as HTML (the matched node's own markup, or the string) and turned into
  Markdown with `normalize(…, TextForm.HTML)` (§1).
- **`url` and `next_page`** resolve against the URL of the page they were read from.
- **`external_id`** absent → the job's resolved URL is the id.
- **`company`** absent → the source's `company` (§7).
- **`posted_at`** is ISO 8601: a naive time is UTC, and a date alone is midnight UTC. Any other form
  is absent, and the job is kept.
- **Evaluation errors** (an `XPathEvalError`, a JMESPath `UnknownFunctionError`, or any other error
  either engine raises while evaluating):
  - during the trial, the recipe is invalid;
  - at search, the source fails with
    `recipe for <ref> failed: <error>; run: ijob sources reanalyze <ref>`.
- **An unsupported `version`** in a stored recipe makes that source fail at search with
  `recipe version N is not supported; run: ijob sources reanalyze <ref>`.

## 5. Salary text

`fields.salary` text becomes `salaries: (Money,)`. A sketch of these rules was run on the real
strings of §2 and on strings meant to break them (`probe_rules.py`). The table shows its real
output.

**Rules:**
1. **Currency.**
   - The first known code in the text wins: `USD EUR GBP CAD AUD NZD SGD HKD JPY CNY INR IDR MYR
     PHP THB VND KRW CHF SEK NOK DKK PLN CZK BRL MXN ZAR`, as a whole word.
   - Otherwise `€`→EUR, `£`→GBP, `₹`→INR.
   - Otherwise `$` or `¥` → `salary_defaults.currency`.
   - Otherwise there is no salary.
2. **Amounts** are digit runs. A `,`, `.` or space followed by exactly three digits is a thousands
   separator. A single `.` followed by other than three digits is the decimal point. Any other
   separator pattern means no salary. A following `k`/`K` multiplies by 1,000.
3. **Bounds.**
   - Two amounts → `min`, `max`.
   - One amount with `up to` → `max` only.
   - One amount with `or more`, a trailing `+`, or a leading `from` → `min` only.
   - One amount otherwise → `min = max`.
   - Three or more amounts → no salary.
4. **Period.**
   - `/hr`, `/hour`, `hourly`, `per hour`, `an hour` → hour.
   - `/mo`, `/month`, `monthly`, `per month` → month.
   - `/yr`, `/year`, `annually`, `per annum`, `per year`, `a year` → year.
   - Otherwise `salary_defaults.period`, otherwise none.
5. **The result** must satisfy `Money` (§1): `min > max` → no salary. No salary never rejects the
   job.

| Text | Defaults (currency, period) | Result |
|---|---|---|
| `$50,000 - $74,999 USD` | USD, year | 50000–74999 USD year |
| `$100,000 or more USD` | none | min 100000, USD, no period |
| `$15.00–$17.00 per hour` | USD | 15.00–17.00 USD hour |
| `$16–$24 per hour` | USD | 16–24 USD hour |
| `$20/hr+` | USD | min 20, USD hour |
| `$20/hr+` | none | no salary (`$` with no default) |
| `€60.000 - €80.000` | none | 60000–80000 EUR |
| `$120k` | USD, year | 120000–120000 USD year |
| `up to £45,000 a year` | none | max 45000 GBP year |
| `Competitive` | USD, year | no salary |
| `$80,000 - $60,000` | USD | no salary (min > max) |
| `$1,5k` | USD | no salary (ambiguous separator) |
| `$90k - $110k - $130k` | USD | no salary (three amounts) |
| `THE API pays 50000` | none | no salary (`THE` and `API` are not known codes) |

## 6. URLs, fetching and pages

- **Every fetch for a recipe source** goes through `PoliteClient`. That covers the added URL, a
  JSON `url`, `next_page` pages, job pages for `detail_description`, robots.txt and redirect hops.
  robots.txt is checked first, ijob's User-Agent is sent, and there is no login and no anti-bot
  bypass.
- **The public-address rule.**
  - It is enforced by an httpx `request` event hook on the client used for recipe sources. The hook
    resolves the request's host and refuses the request unless the scheme is http(s) and every
    resolved address `is_global`.
  - Probe: the hook fires on every redirect hop. A 302 to `http://10.0.0.1/b` gave the hook
    `['http://example.com/a', 'http://10.0.0.1/b']`. A raising hook stopped that hop before
    sending: the transport saw only `['http://example.com/a']`.
  - `is_global` was `False` for `127.0.0.1`, `10.1.2.3`, `192.168.1.5`, `172.16.0.1`,
    `169.254.169.254`, `::1`, `fc00::1` and `100.64.0.1`, and `True` for `8.8.8.8` and
    `2606:4700::1111`. `localhost` resolved to `['127.0.0.1', '::1']`.
  - A refusal reads `<url> is not a public address`. A host that does not resolve fails as today's
    fetch errors do.
- **Hosts never used by a recipe.** Any host with a label `jobstreet`, and `japan-dev.com` or its
  subdomains, are refused with `<host> is read only by its own source kind`. CLAUDE.md:
  "JobStreet is deep-link only", and "JapanDev is read only through its sitemap and JSON-LD".
- **Response size.** A body over 5 MiB is refused (§4.1).
- **Pagination.**
  - `next_page` is read from each page. Its URL is resolved against that page. It is followed while
    it gives a URL not fetched before, for at most 5 pages in all.
  - It stops at a page that reads no items, a repeated URL, an absent `next_page`, or 5 pages.
  - A failure fetching page 2–5 stops pagination and keeps the jobs already read. That failure
    includes robots, HTTP, the address rule, size or content type. The failure is counted in the
    existing per-source failure line only when page 1 itself fails.

## 7. Analysis: proposing a recipe

1. **When analysis starts.**
   - `Service.add_url` keeps its current path. Analysis starts only when `resolve_url` raises
     `UnsupportedUrlError` at `detect.py:98`: the page was fetched and is neither a `JobPosting`
     page nor a feed.
   - An empty feed (`detect.py:100`) keeps today's error.
   - Analysis needs `[models].source_recipe`. That is the new `Task.SOURCE_RECIPE =
     "source_recipe"`, validated like the other keys (`llm_config.py:128-140`). Without it, the CLI
     prints today's error, then `note: set [models].source_recipe to let ijob learn this page` on
     stderr, and exits 1.
2. **Company.** `--company` is required as today (`cli.py:423-436`), and is stored as the source's
   `company`. When the recipe has `fields.company`, each posting uses its own company instead
   (boards such as WWR).
3. **Confirmation is checked before any paid call.** A non-interactive run without `--yes` refuses
   before analysis with `error: confirmation needed; pass --yes`, exit 1, and nothing is spent.
4. **Call 1** (one `run_call`, in the operation's `Budget` capped by `[llm].max_cost_usd`, traced as
   `ijob sources add`):
   - Inputs: the URL, `html` or `json`, the sample (§4.1) and the field list of §4.2.
   - The sample is untrusted and goes only in an input field. The signature's docstring states that
     input fields are untrusted data and never instructions, as the existing signatures do.
   - Output: either a recipe, or, for HTML only, `json_url`: "the jobs are at this URL".
5. **Call 2**, only when call 1 gave a `json_url`:
   - ijob fetches that URL, resolved against the page and under §6's rules, and requires JSON.
   - It makes one more call with the JSON sample; that call must return a recipe with
     `format: "json"`.
   - If the budget cannot start call 2: `error: cost cap reached before the JSON recipe; raise
     [llm].max_cost_usd`, exit 1, nothing stored.
   - At most 2 calls in all. A bad recipe is not retried.
6. **Trial.** The recipe is evaluated on the fetched document (the page, or the JSON URL's full
   body), without pagination or detail fetches.
   - An invalid recipe or an evaluation error → `error: the model's recipe is invalid: <reason>`
     (`TaskOutputError`), exit 1, nothing stored.
   - Zero valid jobs → `error: the proposed recipe read no jobs from the page`, exit 1, nothing
     stored.
7. **Proposal**, printed to stdout:
   - `recipe: <format> at <url>`;
   - `read N jobs, M invalid`;
   - then up to 5 jobs, one per line: `<title>\t<company>\t<location or ->\t<salary or ->\t<url>`.

   The cost line `cost $X.XXXX of $Y.YY` goes to stderr.
8. **Confirm.**
   - Interactive: `Store this source? [y/N] `. `y` or `yes` in any case stores it. Anything else
     prints `not stored` and exits 1.
   - `--yes` stores it without asking.
   - Accept stores the source (kind `recipe`) and its recipe in one transaction, and prints
     `added recipe:<key>`.
9. **TUI.** On the sources screen, adding a URL that `resolve_url` refuses runs the same analysis in
   a worker (company still required). It shows the same proposal with Accept and Cancel, the cost on
   the screen's status line, and `LlmError`s on the existing error line.

## 8. Identity, re-adding and re-analyze

- **Ref.** A recipe source's ref is `recipe:<manual_key(added URL)>` (§1), and `target_url` is the
  added URL.
- **Re-adding.**
  - A URL whose recipe ref exists and is not removed is refused before analysis:
    `error: recipe:<key> is already added; run: ijob sources reanalyze recipe:<key>`, exit 1.
  - A removed one goes through analysis again. On accept, the source is restored and its recipe
    replaced.
- **`ijob sources reanalyze REF [--yes]`** fetches the stored `target_url` and runs §7 steps 3–8. It
  keeps the stored company, and the old recipe stays in force until the new one is accepted.
  - An unknown or removed ref → today's `UnknownSourceError`, exit 2.
  - A ref of another kind → `error: <ref> has no recipe`, exit 2.

## 9. Reading at search time

- **Wiring.** `build_source(row, *, recipe=None)` builds a `RecipeSource` for kind `recipe` from the
  row and the recipe the service reads from `source_recipes`. Search calls no model (intent: "No
  model at search time").
- **`RecipeSource.fetch`** fetches `url`, checks the content type against `format`, evaluates, and
  paginates (§6).
  - A `json` recipe that receives HTML, or the reverse, fails the source with
    `recipe for <ref> expected <format>; run: ijob sources reanalyze <ref>`.
- **Invalid items.** An item that has no title or valid URL, or that `SightingDraft` rejects, is
  skipped and counted in `rejected`. That feeds the existing `<ref>: skipped N invalid jobs` line.
  A repeated `external_id` in one fetch keeps the first.
- **Zero valid jobs** on a page 1 that fetched fine fails the source with
  `recipe for <ref> found no jobs; if the site changed, run: ijob sources reanalyze <ref>`. That
  goes through the existing per-source failure line, and the other sources carry on. A company
  with genuinely no openings gets the same message, which its wording allows for.
- **`raw`** holds the job's evaluated fields as JSON. When the recipe has `detail_description` and
  no `description`, `raw` also holds that XPath. `describe` (§1) can then fetch the job page under
  §6's rules and evaluate it on the cleaned page. An empty result means no description, which
  shows as `no description available`.

## 10. Placement and storage

- **`ijob-sources`** owns `lxml` and `jmespath`, and:
  - `recipe.py`: the v1 type, `parse_recipe(text) -> Recipe` (validity, §4.3) and evaluation;
  - `salary_text.py`: §5;
  - `public_host.py`: the §6 hook and host refusals;
  - the §4.1 cleaning and sampling;
  - `RecipeSource`;
  - `describe` for kind `recipe`.
- **`ijob-ai`** stays on `ijob-core` only. It has `Task.SOURCE_RECIPE`, a pydantic output mirroring
  v1 plus `json_url`, `SourceRecipeSig`, and a `propose_recipe` wrapper returning the output as JSON
  text. `ijob-app` passes that text to `parse_recipe`.
- **`ijob-core`.**
  - `SourceKind.RECIPE = "recipe"`; the kind lives in the ref text.
  - Migration 5 adds only the table
    `source_recipes(ref TEXT PRIMARY KEY REFERENCES sources(ref), recipe TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL) STRICT`,
    holding the recipe JSON, the model reference, and a UTC timezone-aware time.
  - A removed source keeps its row (`store.py:1172` only marks it removed).
  - `ijob-core` imports no LLM or network library.
- **`ijob-app`.** `Service` gains the analysis and accept operations, `add_url`'s fallback, and
  `reanalyze`. The CLI gains `--yes` on `sources add-url` and the `sources reanalyze` command. The
  TUI sources screen gains the proposal.

## 11. Errors

| Error | Where | User sees |
|---|---|---|
| `UnsupportedUrlError`, no `[models].source_recipe` | `add-url` | today's error, then the `note:` line; exit 1 |
| non-interactive, no `--yes` | `add-url`, `reanalyze` | `error: confirmation needed; pass --yes`; exit 1; no call |
| `MissingModelError` | `reanalyze` | `error: …`; exit 1 |
| `TaskOutputError` (invalid recipe, evaluation error in the trial) | analysis | `error: the model's recipe is invalid: …`; exit 1; nothing stored; cost charged |
| no jobs in the trial | analysis | `error: the proposed recipe read no jobs from the page`; exit 1 |
| cap before call 2 | analysis | `error: cost cap reached before the JSON recipe; raise [llm].max_cost_usd`; exit 1 |
| `ProviderFailedError`, `ProviderUnavailableError`, `CostUnknownError` | analysis | `error: …` as for the other LLM operations; exit 1; nothing stored |
| not a public address, refused host, over 5 MiB, wrong content type | analysis | `error: …`; exit 1 |
| the same, robots disallow, HTTP errors | search, page 1 | that source's failure line; the other sources carry on |
| the same | search, pages 2–5 | pagination stops; jobs read so far are kept (§6) |
| no jobs, evaluation error, format mismatch, unsupported version | search | that source's failure line, naming `reanalyze` |
| already added | `add-url` | `error: recipe:<key> is already added; …`; exit 1 |
| unknown or removed ref; ref of another kind | `reanalyze` | exit 2 (§8) |

## 12. Testing

- **No model and no network.** Model calls use the fake engines. HTTP uses `httpx.MockTransport`,
  as the existing source tests do (`test_polite_http.py`, `test_registry.py`).
- **Fixtures** are shaped like §2's pages and hold no personal data:
  - a python.org-style list with a bare-text company;
  - a WWR-style list with a category salary;
  - a Working Nomads-style JSON list;
  - a JavaScript-only page with a footer JSON link;
  - a paginated page with a loop.
- **Named cases:** every §5 example, every §4.3 result-type rule, every §6 address and host case,
  and every §11 row.
- **Import guard.** An import test proves that starting `search`, `tui` and `sources list` with
  recipe sources stored loads no `dspy` or `claude_agent_sdk`.

## 13. Risks

- **DNS between check and connect.** The hook resolves the host, then httpx resolves it again to
  connect. A host that changes its answer in between (DNS rebinding) could slip through. This is a
  single-user local tool fetching hosts the user chose or a page named.
- **XPath cost.** An expression can be expensive on a large page, and evaluation has no time limit.
  The 500-character cap, the 5 MiB body cap, and the user's look at the trial bound it.
- **Model accuracy.** The trial catches zero jobs and invalid expressions, but not a field read
  from the wrong element. The user's review of the five sample jobs is the check.

## 14. Non-goals

- Changing the known hiring systems, `JobPosting` pages or RSS feeds. They keep their parsers.
  Lenient parsing of their rejected samples is the sibling audit chain.
- Any model call at search time, or in the TUI's reading of results. Extracting fields from
  free-form prose at run time.
- Re-analyzing automatically.
- Pages that show jobs only after JavaScript runs, unless the page links to a JSON URL. JSON URLs
  named only inside `<script>`, since the sample has no scripts. Pages behind a login.
- Salary from anywhere but `fields.salary`.
