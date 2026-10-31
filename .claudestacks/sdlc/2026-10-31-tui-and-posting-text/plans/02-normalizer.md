---
status: approved
created: 2026-11-01
built-on: a9902751b191
---

# Description Normalizer Implementation Plan

**Goal:** `ijob_sources.description.normalize` turns a source's description text of any `TextForm` into the spec's Markdown subset.

**Architecture:**
- One new module, `ijob_sources.description`, holding `TextForm` and `normalize` (spec §3.1). It sits in `ijob-sources` because HTML handling is a source concern and `ijob-core` stays free of it (spec §1.5).
- HTML is read with the standard library's `html.parser.HTMLParser` (already used by `jsonld.py:15,63` and `yc.py:14,76`) in a private subclass that emits Markdown; no new dependency.
- The module has no I/O; every rule is a pure function of its input.

**Tech Stack:** Python 3.14 stdlib (`html`, `html.parser`, `re`, `enum`), pytest.

---

## Files that change

```
packages/ijob-sources/src/ijob_sources/description.py               — [create] TextForm, normalize, the private HTML-to-Markdown parser
packages/ijob-sources/tests/ijob_sources_tests/test_description.py  — [create] table tests for every rule of spec §3.1
```

## Order of work

### Task 1 — `TextForm`, PLAIN and the shared rules

**Files:**
- Create `packages/ijob-sources/src/ijob_sources/description.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_description.py`

**Change:**
- `TextForm(StrEnum)`: `HTML`, `MARKDOWN`, `PLAIN`, `DETECT`.
- `normalize(text: str, form: TextForm) -> str | None`, with a `match` on `form` closed by `assert_never`. This task implements PLAIN plus the rules every form shares: Markdown-escaping of text (`\ * _ [ ] ` + "`" + ` <`, and `#` at a line start), whitespace collapsing (trailing spaces stripped, at most one blank line in a row, leading and trailing blank lines removed), the cut at 50,000 characters, and an empty result giving `None`. Until Tasks 2 and 3 land, the `HTML`, `MARKDOWN` and `DETECT` cases raise `NotImplementedError`. The cut length is a module constant `MAX_DESCRIPTION_CHARS = 50_000`.

**Proof:** written first, seen failing, then passing —
- `test_plain_keeps_lines_and_paragraphs` — `"Line one\nLine two\n\nPara"` keeps both line breaks and the paragraph break.
- `test_plain_escapes_markdown` — `"*bold* [x](file:///etc)\n# h"` comes back with `*`, `[`, `]` and the line-start `#` backslash-escaped, so no emphasis, link or heading can form; a `#` in the middle of a line is left as it is (spec §3.1).
- `test_collapses_blank_lines` — three blank lines in a row become one.
- `test_cuts_at_limit` — a 60,000-character input returns exactly `MAX_DESCRIPTION_CHARS` characters.
- `test_empty_gives_none` — `""` and `"  \n\n "` give `None`.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_description.py` passes and the gate in CLAUDE.md is green. Commit `feat(sources): add description normalizer with plain-text form`.

### Task 2 — HTML to Markdown

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/description.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_description.py`

**Change:** `normalize(…, TextForm.HTML)` feeds the text to a private `HTMLParser` subclass that maps tags as spec §3.1 lists: `h1–h6` → `#`…`######` headings; `p` and `div` → paragraphs; `br` → line break; `ul`/`ol`/`li` → `- ` and `1. ` items, nested lists indented; `strong`/`b` → `**…**`; `em`/`i` → `*…*`; `a` with an `http://` or `https://` `href` → `[text](href)`, any other `a` → its text; `pre` → fenced code block; `code` → inline code. `script`, `style`, `img` and `iframe` are dropped with their content; other tags keep their text; attributes other than `href` are ignored. Text nodes are Markdown-escaped (Task 1). Entities are unescaped by the parser, whose `convert_charrefs` defaults to `True` (spec §3.1 probe).

**Proof:** written first, seen failing, then passing —
- `test_html_headings_paragraphs_breaks` — `<h2>About</h2><p>One<br>Two</p>` gives `## About`, a blank line, then `One` and `Two` on separate lines.
- `test_html_lists` — `<ul><li>Go</li><li>Rust</li></ul>` gives `- Go` / `- Rust`; an `<ol>` gives `1.` and `2.` items; a nested `<ul>` is indented.
- `test_html_emphasis_and_code` — `<strong>`, `<b>`, `<em>`, `<i>`, `<code>` and `<pre>` map to `**`, `**`, `*`, `*`, backticks and a fenced block.
- `test_html_links_http_only` — `<a href="https://x.test/a">x</a>` gives `[x](https://x.test/a)`; `<a href="javascript:alert(1)">y</a>` and `<a href="file:///etc">z</a>` give the plain text `y` and `z`.
- `test_html_drops_script_style_img_iframe` — their content does not appear in the output.
- `test_html_entities_unescaped` — `<p>R&amp;D caf&eacute;</p>` gives `R&D café`.
- `test_html_text_is_escaped` — `<p>*not bold*</p>` keeps the asterisks escaped.
- `test_html_unknown_tag_keeps_text` — `<span class="a">hi</span>` gives `hi`.
- `test_html_unclosed_tags` — `<ul><li>Go` with no closing tags gives `- Go` and raises nothing.
- `test_escaped_html_after_one_unescape` — `html.unescape("&lt;p&gt;Trimmed job description.&lt;/p&gt;")` normalized as HTML gives `Trimmed job description.` (the Greenhouse shape, spec §2.1).

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_description.py` passes and the gate is green. Commit `feat(sources): normalize HTML descriptions to Markdown`.

### Task 3 — MARKDOWN and DETECT

**Files:**
- Modify `packages/ijob-sources/src/ijob_sources/description.py`
- Test `packages/ijob-sources/tests/ijob_sources_tests/test_description.py`

**Change:**
- MARKDOWN: the text passes through unescaped, except that a link whose target is not `http(s)` is replaced by its text and every image (`![alt](target)`) is dropped; then the shared whitespace, cut and empty rules apply.
- DETECT: HTML when the text matches `<[a-zA-Z][^>]*>` (the regex the spec §2.1 probes used), else PLAIN.

**Proof:** written first, seen failing, then passing —
- `test_markdown_passes_through` — `#### Company Description\n\n**Remote**` is unchanged (the 4dayweek shape, `fourdayweek/page1.json:17`).
- `test_markdown_filters_non_http_links` — `[a](https://x.test)` stays; `[b](file:///etc)` becomes `b`.
- `test_markdown_drops_images` — `![logo](https://x.test/l.png)` disappears.
- `test_detect_html` — `<p><strong>Location requirement</strong></p>` is treated as HTML (the JapanDev shape).
- `test_detect_plain` — `We are looking for a Senior AI Platform Engineer` is treated as PLAIN (the Djinni shape) and keeps its lines.

**Done when:** `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_description.py` passes and the gate is green. Commit `feat(sources): add Markdown and detected description forms`.

---

## Risks

- **Over-escaping** is the most likely visible defect: PLAIN text with many `*` or `_` reads with backslashes in the CLI. Accepted by spec §3.1 so third-party text cannot form markup in the TUI.
- Malformed HTML: the parser subclass closes any open list, emphasis or code at the end of input; `test_html_unclosed_tags` holds it.
- Not done: tables (flattened to their cell text as unknown tags), and splitting the text into sections.

## Proof

- `uv run --locked pytest packages/ijob-sources/tests/ijob_sources_tests/test_description.py` passes.
- The five gate commands in CLAUDE.md exit `0`.
- `grep -n "^import\|^from" packages/ijob-sources/src/ijob_sources/description.py` shows only standard-library imports.
