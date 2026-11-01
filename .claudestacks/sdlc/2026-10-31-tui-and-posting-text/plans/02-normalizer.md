---
status: done
created: 2026-11-01
built-on: b175307f42cd
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

## Review findings

- security — MARKDOWN link/image filter bypassable (autolinks, reference links, nested/escaped brackets, titles, two-level parens, raw HTML, `[[x](ssh://a)](ssh://b)`) — `description.py` MARKDOWN path — fixed and verified: allow-list scanner; reviewer probe re-run `uv run --locked python -I …/scratchpad/probe.py | grep 'links=\[' | grep -v 'links=\[\]'` → only `https://ok`, `https://x`, `https://x/a`, `https://x/a%5Db`
- security — `<pre>` fence broke out after a list marker, heading marker or inside a held span — `description.py` pre handling — fixed and verified: fence on its own line, indented in list items, escaped-text fallback; parametrized hostile tests; `uv run --locked pytest -q packages/ijob-sources/tests/ijob_sources_tests/test_description.py` → `154 passed in 0.06s`
- security — control characters stripped after markup filtering, so stripping created markup — `description.py` finish step — fixed and verified: strip and line-ending unification moved to the input (and to decoded entities/hrefs); same test run
- spec — escape set of spec §3.1 could not stop `>`, list markers, setext, thematic breaks, indented code — `description.py` — fixed: line-start escaping widened; docstring states the exact guarantee
- correctness — adjacent/nested emphasis garbled (`**x****y**`) — `description.py` — fixed: same-kind spans merge (`<b>x</b><b>y</b>` → `**xy**`)
- docstring-discipline — module docstring overclaimed — fixed
- test-mandate — untested branches (stray `</iframe>`, inline-code pad, rstrip after cut) — fixed: tests added
- correctness — MARKDOWN escapes brackets inside code spans — accepted as cosmetic
- correctness — `&` not escaped, so PLAIN `AT&amp;T` renders as `AT&T` — declined: plan 03's `test_rss_list_item_not_unescaped_twice` asserts the literal `AT&amp;T pays 5 \< 6`
- open — Textual's gfm-like linkify may auto-link bare emails/`ftp://` text — carried to plan 07, whose spec §7 already requires opening only `http(s)` targets on `LinkClicked`

Full gate after the fix round (coder run): `161 files already formatted`; `All checks passed!`; `Success: no issues found in 107 source files`; `1686 passed`.

## Probe results

- Reviewer bypass corpus (`/private/tmp/claude-501/-Users-hiraq-Projects-rstlix0x0-ijob/414123fb-5882-46e9-9bbe-ce9eef8996d3/scratchpad/probe.py`, parses output with markdown-it-py from the reviewer's scratch env) — before fix: ssh/smb links and images survived in MARKDOWN, `<pre>` and control-char cases — AGAINST THE PLAN's MARKDOWN rule as implemented; after fix: only http(s) links survive (see above).
- Spec §3.1 entity claim (`convert_charrefs` default True) — exercised by `test_html_entities_unescaped` — passes.

## Deviations

- 2026-11-01 — Defect of the spec (§3.1): the listed escape set does not make "source text cannot form markup" true (block quotes, list markers, setext, thematic breaks, indented code), and a regex-level MARKDOWN filter cannot catch every non-http link form with the stdlib alone. Built instead: widened line-start escaping for HTML/PLAIN; MARKDOWN as an allow-list (keep `[text](http(s)://…)` without a title, replace other inline links by their text, drop images, escape every other `[`, `]`, `<`). Every stated requirement of §3.1 still holds (stdlib only, http(s)-only links, Markdown formatting passes through); the spec's wording should be updated by `design` at its next amendment. No `disproves` marker: no requirement the sibling plans depend on changed.
- 2026-11-01 — Additions not in the spec: control characters stripped on input; `\r\n`/`\r` unified; whitespace moved outside emphasis markers; code text emitted literally with a fence longer than any backtick run; hrefs percent-encoded for whitespace, parens, angle brackets, backslash; a titled http link in MARKDOWN becomes its text; `!` before a kept link is escaped.
- 2026-11-01 — Per-task commits not made (CLAUDE.md: commit only when the user asks).
