import html
import re
from typing import Final

import pytest
from ijob_sources.description import MAX_DESCRIPTION_CHARS, TextForm, normalize


def test_plain_keeps_lines_and_paragraphs() -> None:
    assert normalize("Line one\nLine two\n\nPara", TextForm.PLAIN) == (
        "Line one\nLine two\n\nPara"
    )


def test_plain_escapes_markdown() -> None:
    assert normalize("*bold* [x](file:///etc)\n# h", TextForm.PLAIN) == (
        "\\*bold\\* \\[x\\](file:///etc)\n\\# h"
    )


def test_plain_escapes_every_special_character() -> None:
    assert normalize("a\\b_c`d<e", TextForm.PLAIN) == "a\\\\b\\_c\\`d\\<e"


def test_plain_leaves_inner_hash_alone() -> None:
    assert normalize("C# and issue #4", TextForm.PLAIN) == "C# and issue #4"


def test_plain_escapes_indented_line_start_hash() -> None:
    assert normalize("intro\n  ## deep", TextForm.PLAIN) == "intro\n  \\## deep"


def test_collapses_blank_lines() -> None:
    assert normalize("a\n\n\n\nb", TextForm.PLAIN) == "a\n\nb"


def test_strips_trailing_spaces_and_edge_blank_lines() -> None:
    assert normalize("\n\n  a   \nb\t\n\n", TextForm.PLAIN) == "  a\nb"


def test_normalizes_line_endings() -> None:
    assert normalize("a\r\nb\rc", TextForm.PLAIN) == "a\nb\nc"


def test_strips_terminal_control_characters() -> None:
    assert normalize("a\x1b[31mb\x00c\td", TextForm.PLAIN) == "a\\[31mbc\td"


@pytest.mark.parametrize("form", list(TextForm))
def test_strips_lone_surrogates(form: TextForm) -> None:
    result = normalize("<p>a\ud83db\udfffc</p>", form)
    assert result is not None
    assert "abc" in result
    result.encode()


def test_strips_a_lone_surrogate_from_a_link_target() -> None:
    result = normalize('<a href="https://x.example/\ud800">go</a>', TextForm.HTML)
    assert result == "[go](https://x.example/)"


def test_cuts_at_limit() -> None:
    result = normalize("a" * 60_000, TextForm.PLAIN)
    assert result is not None
    assert len(result) == MAX_DESCRIPTION_CHARS


@pytest.mark.parametrize("text", ["", "  \n\n ", "\r\n\t"])
def test_empty_gives_none(text: str) -> None:
    assert normalize(text, TextForm.PLAIN) is None


def _html(text: str) -> str | None:
    return normalize(text, TextForm.HTML)


def test_html_headings_paragraphs_breaks() -> None:
    assert _html("<h2>About</h2><p>One<br>Two</p>") == "## About\n\nOne\nTwo"


def test_html_all_heading_levels() -> None:
    source = "".join(f"<h{n}>t</h{n}>" for n in range(1, 7))
    expected = "\n\n".join(f"{'#' * n} t" for n in range(1, 7))
    assert _html(source) == expected


def test_html_paragraphs_and_divs_are_separated() -> None:
    assert _html("<div>A</div><p>B</p>C") == "A\n\nB\n\nC"


def test_html_self_closing_br() -> None:
    assert _html("a<br/>b") == "a\nb"


def test_html_lists() -> None:
    assert _html("<ul><li>Go</li><li>Rust</li></ul>") == "- Go\n- Rust"
    assert _html("<ol><li>Go</li><li>Rust</li></ol>") == "1. Go\n2. Rust"
    assert _html("<ul><li>Go<ul><li>Sub</li></ul></li><li>Rust</li></ul>") == (
        "- Go\n  - Sub\n- Rust"
    )
    assert _html("<ol><li>A<ul><li>B</li></ul></li></ol>") == "1. A\n   - B"


def test_html_list_item_paragraph_stays_on_item_line() -> None:
    assert _html("<ul><li><p>Go</p></li><li><p>Rust</p></li></ul>") == "- Go\n- Rust"


def test_html_list_is_set_apart_from_text() -> None:
    assert _html("Intro<ul><li>Go</li></ul>After") == "Intro\n\n- Go\n\nAfter"


def test_html_stray_list_item() -> None:
    assert _html("<li>Go</li>") == "- Go"


def test_html_emphasis_and_code() -> None:
    assert _html("<strong>a</strong> <b>b</b> <em>c</em> <i>d</i>") == (
        "**a** **b** *c* *d*"
    )
    assert _html("Run <code>go test</code> now") == "Run `go test` now"
    assert _html("<pre>x = 1\n  y = 2</pre>") == "```\nx = 1\n  y = 2\n```"
    assert _html("<pre><code>x &lt; 1</code></pre>") == "```\nx < 1\n```"


def test_html_code_content_is_literal() -> None:
    assert _html("<code>*a* _b_</code>") == "`*a* _b_`"
    assert _html("<code>a`b</code>") == "``a`b``"
    assert _html("<pre>a\n```\nb</pre>") == "````\na\n```\nb\n````"


def test_html_emphasis_whitespace_moves_outside_the_markers() -> None:
    assert _html("<strong>Location: </strong>Central") == "**Location:** Central"
    assert _html("a<b> </b>b") == "a b"
    assert _html("<b></b>x") == "x"


def test_html_links_http_only() -> None:
    assert _html('<a href="https://x.test/a">x</a>') == "[x](https://x.test/a)"
    assert _html('<a href="HTTP://x.test">x</a>') == "[x](HTTP://x.test)"
    assert _html('<a href="javascript:alert(1)">y</a>') == "y"
    assert _html('<a href="file:///etc">z</a>') == "z"
    assert _html("<a>w</a>") == "w"


def test_html_link_target_cannot_break_out() -> None:
    assert (
        _html('<a href="https://x.test/a) b">x</a>') == "[x](https://x.test/a%29%20b)"
    )


def test_html_link_without_text_is_dropped() -> None:
    assert _html('a<a href="https://x.test"><img src="l.png"></a>b') == "ab"


def test_html_drops_script_style_img_iframe() -> None:
    source = (
        "<p>keep</p><script>alert('s')</script><style>p{c:red}</style>"
        '<img src="i.png" alt="pic"><iframe><p>frame</p></iframe>'
        '<iframe src="x"/><p>tail</p>'
    )
    assert _html(source) == "keep\n\ntail"


def test_html_entities_unescaped() -> None:
    assert _html("<p>R&amp;D caf&eacute;</p>") == "R&D café"


def test_html_text_is_escaped() -> None:
    assert _html("<p>*not bold*</p>") == "\\*not bold\\*"
    assert _html("<p>&lt;b&gt; [x](y) a_b</p>") == "\\<b> \\[x\\](y) a\\_b"


def test_html_line_start_hash_is_escaped() -> None:
    assert _html("<p>#hash</p><p>C# fine</p>") == "\\#hash\n\nC# fine"
    assert _html("<ul><li>#tag</li></ul>") == "- \\#tag"


def test_html_unknown_tag_keeps_text() -> None:
    assert _html('<span class="a">hi</span>') == "hi"
    assert _html("<table><tr><td>a</td></tr></table>") == "a"


def test_html_whitespace_collapses() -> None:
    assert _html("<p>  a \n\t b  </p>\n<p> c </p>") == "a b\n\nc"


def test_html_unclosed_tags() -> None:
    assert _html("<ul><li>Go") == "- Go"
    assert _html("<b>bold <i>both") == "**bold *both***"
    assert _html("<a href='https://x.test'>link") == "[link](https://x.test)"
    assert _html("<pre>code") == "```\ncode\n```"
    assert _html("<code>code") == "`code`"
    assert _html("</b></ul>stray") == "stray"


def test_html_misnested_tags_close_inner_first() -> None:
    assert _html("<b>a<i>b</b>c</i>") == "**a*b***c"


def test_html_empty_gives_none() -> None:
    assert _html("<p> </p><script>x</script>") is None


def test_escaped_html_after_one_unescape() -> None:
    raw = "&lt;p&gt;Trimmed job description.&lt;/p&gt;"
    assert _html(html.unescape(raw)) == "Trimmed job description."


def _markdown(text: str) -> str | None:
    return normalize(text, TextForm.MARKDOWN)


def test_markdown_passes_through() -> None:
    source = (
        "#### Company Description\n\n**Remote** and _flexible_ [docs](https://x.test)"
    )
    assert _markdown(source) == source


def test_markdown_filters_non_http_links() -> None:
    assert _markdown("[a](https://x.test)") == "[a](https://x.test)"
    assert _markdown("[a](HTTP://x.test/p?q=1)") == "[a](HTTP://x.test/p?q=1)"
    assert _markdown('[a](http://x.test "t")') == "a"
    assert _markdown("[b](file:///etc)") == "b"
    assert _markdown("[c](javascript:alert(1))") == "c"
    assert _markdown("[d](<data:text/html,x>) and [e](/relative)") == "d and e"


def test_markdown_drops_images() -> None:
    assert _markdown("![logo](https://x.test/l.png)") is None
    assert _markdown("a ![logo](file:///l.png) b") == "a  b"
    assert _markdown("[![logo](https://x.test/l.png)](https://x.test)") is None


def test_markdown_applies_the_shared_rules() -> None:
    assert _markdown("a  \n\n\n\nb\r\n") == "a\n\nb"
    assert _markdown("  \n") is None


def test_detect_html() -> None:
    result = normalize("<p><strong>Location requirement</strong></p>", TextForm.DETECT)
    assert result == "**Location requirement**"


def test_detect_plain() -> None:
    text = "We are looking for a Senior AI Platform Engineer\nRemote 1 < 2"
    assert normalize(text, TextForm.DETECT) == (
        "We are looking for a Senior AI Platform Engineer\nRemote 1 \\< 2"
    )


_ESCAPED: Final = re.compile(r"\\[!-/:-@\[-`{-~]")
_CODE_SPAN: Final = re.compile(r"(`+).+?(?<!`)\1(?!`)", re.DOTALL)
_ALLOWED_LINK: Final = r"\[[^\[\]<\n]*\]\(https?://[^\s()<>\\]+\)"
_FENCE_OPEN: Final = re.compile(r"^( *(?:(?:[-+]|\d+[.)]) +)?)(`{3,})[^`]*$")


def _outside_fences(markdown: str) -> tuple[str, list[str]]:
    outside: list[str] = []
    problems: list[str] = []
    fence_length = 0
    column = 0
    for line in markdown.split("\n"):
        indent = len(line) - len(line.lstrip(" "))
        if not fence_length:
            opener = _FENCE_OPEN.match(line)
            if opener:
                fence_length = len(opener[2])
                column = len(opener[1])
            else:
                outside.append(line)
        elif line.strip() and indent < column:
            problems.append(f"fence content leaves its container: {line!r}")
            fence_length = 0
            outside.append(line)
        elif re.fullmatch(f"`{{{fence_length},}}", line.strip()):
            fence_length = 0
    if fence_length:
        problems.append("unclosed fence")
    return "\n".join(outside), problems


def _unsafe_markup(markdown: str) -> list[str]:
    outside, problems = _outside_fences(markdown)
    text = _CODE_SPAN.sub("", _ESCAPED.sub("", outside))
    if re.search(f"!{_ALLOWED_LINK}", text, re.IGNORECASE):
        problems.append("an image can form")
    text = re.sub(_ALLOWED_LINK, "", text, flags=re.IGNORECASE)
    problems.extend(f"unescaped {char!r}" for char in re.findall(r"[\[\]<]", text))
    return problems


def test_checker_accepts_allowed_links_and_closed_fences() -> None:
    assert _unsafe_markup("a [b](https://x.test/y) \\[c\\] `[d]`") == []
    assert _unsafe_markup("- ```\n  [x](ssh://e)\n  ```\n\ntail") == []


@pytest.mark.parametrize(
    "markdown",
    [
        "[x](ssh://e)",
        "<ssh://e>",
        "![x](https://x.test/l.png)",
        "\\\\[x]",
        "```\nopen",
        "- ```\na\n```",
        "[x](https://x.test/a b)",
    ],
)
def test_checker_flags_unsafe_markup(markdown: str) -> None:
    assert _unsafe_markup(markdown)


_HOSTILE: Final = [
    (TextForm.MARKDOWN, "<file:///etc/passwd>"),
    (TextForm.MARKDOWN, "<ssh://evil/x>"),
    (TextForm.MARKDOWN, "[x][1]\n\n[1]: ssh://evil/x"),
    (TextForm.MARKDOWN, "[x]\n\n[x]: smb://evil/share"),
    (TextForm.MARKDOWN, "[x][]\n\n[x]: ssh://evil/x"),
    (TextForm.MARKDOWN, '<a href="ssh://evil">raw</a>'),
    (TextForm.MARKDOWN, "<img src=x onerror=y>"),
    (TextForm.MARKDOWN, "[a [b] c](ssh://evil)"),
    (TextForm.MARKDOWN, "[a\\]](ssh://evil)"),
    (TextForm.MARKDOWN, "[a](ssh://evil 'title')"),
    (TextForm.MARKDOWN, "[a](ssh://evil (title))"),
    (TextForm.MARKDOWN, '[a](ssh://evil "title")'),
    (TextForm.MARKDOWN, "[a](https://ok.test 'title')"),
    (TextForm.MARKDOWN, "[a](ssh://e((v)))"),
    (TextForm.MARKDOWN, "![a](ssh://evil 'title')"),
    (TextForm.MARKDOWN, "![a [b] c](https://x.test/l.png)"),
    (TextForm.MARKDOWN, "![a][1]\n\n[1]: https://x.test/l.png"),
    (TextForm.MARKDOWN, "![a](https://x.test/l.png)"),
    (TextForm.MARKDOWN, "[[x](ssh://a)](ssh://b)"),
    (TextForm.MARKDOWN, "[![a](https://x.test/l.png)](ssh://b)"),
    (TextForm.MARKDOWN, "[wow!](ssh://a)[b](https://ok.test)"),
    (TextForm.MARKDOWN, "!![x](y)[b](https://ok.test)"),
    (TextForm.MARKDOWN, "\\\\[x](ssh://evil)"),
    (TextForm.MARKDOWN, "`[a](ssh://e)` code"),
    (TextForm.MARKDOWN, "[a]\x00(ssh://evil)"),
    (TextForm.MARKDOWN, "!\x00[a](https://x.test/l.png)"),
    (TextForm.MARKDOWN, "<\x00ssh://evil>"),
    (TextForm.MARKDOWN, "[a]\r(ssh://evil)"),
    (TextForm.MARKDOWN, "[a](ssh://evil\n)"),
    (TextForm.PLAIN, "[a]\x00(ssh://evil)"),
    (TextForm.PLAIN, "<\x00ssh://evil>"),
    (TextForm.PLAIN, "\x1b[2K[x](ssh://evil)"),
    (TextForm.HTML, "<ul><li><pre>a\n[x](ssh://evil)\n</pre></li></ul>tail"),
    (TextForm.HTML, "<ol><li><pre>a\n<ssh://evil>\n</pre></li></ol>"),
    (TextForm.HTML, "<ul><li>item<pre>a\n[x](ssh://evil)</pre></li></ul>"),
    (TextForm.HTML, "<ul><li><b><pre>[x](ssh://evil)</pre></b></li></ul>"),
    (TextForm.HTML, "<ul><pre>[x](ssh://evil)</pre></ul>"),
    (TextForm.HTML, "<ul><li>a<ul><pre>[x](ssh://evil)</pre></ul></li></ul>"),
    (TextForm.HTML, "<li><pre>[x](ssh://evil)</pre></li>"),
    (TextForm.HTML, "<li><h2><pre>[x](ssh://evil)</pre></h2></li>"),
    (TextForm.HTML, "<h2><pre>[x](ssh://evil)</pre></h2>"),
    (TextForm.HTML, "<h2>a<pre>[x](ssh://evil)</pre></h2>"),
    (TextForm.HTML, '<a href="https://x.test"><pre>[y](ssh://e)</pre></a>'),
    (TextForm.HTML, "<b><pre>[y](ssh://e)</pre></b>"),
    (TextForm.HTML, "<em>a<pre>[y](ssh://e)</pre>b</em>"),
    (TextForm.HTML, "<pre>[y](ssh://e)\n```\n[z](ssh://e)\n````</pre>"),
    (TextForm.HTML, "<ul><li><pre>[y](ssh://e)\n```</pre></li></ul>"),
    (TextForm.HTML, "<pre>unclosed [y](ssh://e)"),
    (TextForm.HTML, "<ul><li><pre>unclosed [y](ssh://e)"),
    (TextForm.HTML, "<p>[x](ssh://evil) <ssh://evil> ![i](https://x.test/i.png)</p>"),
    (TextForm.HTML, '<a href="https://x.test/a]b">z</a>'),
    (TextForm.HTML, '<a href="https://x.test"><p>a</p><p>b</p></a>'),
    (TextForm.HTML, '<a href="https://x.test">1<a href="https://y.test">2</a>3</a>'),
    (TextForm.HTML, '<a href="ssh://e">a<a href="https://y.test">2</a></a>'),
    (TextForm.HTML, '<a href="https://x.test/\x01a">z</a>'),
    (TextForm.HTML, "<p>[a]&#x81;(ssh://evil)</p>"),
    (TextForm.HTML, "<p>&#91;a&#93;(ssh://evil)</p>"),
    (TextForm.DETECT, "[x](ssh://evil) <3 love"),
]


@pytest.mark.parametrize(("form", "text"), _HOSTILE)
def test_hostile_input_forms_no_unsafe_markup(form: TextForm, text: str) -> None:
    assert _unsafe_markup(normalize(text, form) or "") == []


def test_markdown_autolinks_and_raw_html_are_escaped() -> None:
    assert _markdown("<ssh://evil/x>") == "\\<ssh://evil/x>"
    assert _markdown('<a href="ssh://e">raw</a>') == '\\<a href="ssh://e">raw\\</a>'
    assert _markdown("see <b>x</b>") == "see \\<b>x\\</b>"


def test_markdown_reference_links_are_escaped() -> None:
    assert (
        _markdown("[x][1]\n\n[1]: ssh://e/x") == "\\[x\\]\\[1\\]\n\n\\[1\\]: ssh://e/x"
    )


def test_markdown_nested_links_are_not_rescanned() -> None:
    assert _markdown("[[x](ssh://a)](ssh://b)") == "\\[x\\](ssh://b)"


def test_markdown_unmatched_brackets_are_escaped() -> None:
    assert _markdown("[a [b] c](ssh://evil)") == "\\[a \\[b\\] c\\](ssh://evil)"
    assert _markdown("[a\\]](ssh://evil)") == "\\[a\\]\\](ssh://evil)"


def test_markdown_titles_and_nested_parentheses() -> None:
    assert _markdown("[a](ssh://evil 'title')") == "a"
    assert _markdown("[a](ssh://evil (title))") == "a"
    assert _markdown("[a](https://x.test 'title')") == "a"
    assert _markdown("[a](ssh://e((v)))") == "\\[a\\](ssh://e((v)))"


def test_markdown_existing_escapes_are_kept() -> None:
    assert _markdown("\\[a\\](ssh://e) \\\\ \\*x\\*") == "\\[a\\](ssh://e) \\\\ \\*x\\*"
    assert _markdown("\\\\[x](ssh://e)") == "\\\\x"


def test_markdown_image_forms_are_dropped_or_escaped() -> None:
    assert _markdown("![a](ssh://evil 'title')") is None
    assert _markdown("![a [b] c](https://x.test/l.png)") == (
        "!\\[a \\[b\\] c\\](https://x.test/l.png)"
    )
    assert _markdown("a ![logo](https://x.test/l.png) b") == "a  b"


def test_markdown_bang_before_a_link_cannot_form_an_image() -> None:
    assert _markdown("[wow!](ssh://a)[b](https://ok.test)") == (
        "wow\\![b](https://ok.test)"
    )
    assert _markdown("!![x](y)[b](https://ok.test)") == "\\![b](https://ok.test)"
    assert _markdown("\\![b](https://ok.test)") == "\\![b](https://ok.test)"


def test_markdown_control_characters_cannot_assemble_markup() -> None:
    assert _markdown("[a]\x00(ssh://evil)") == "a"
    assert _markdown("!\x00[a](https://x.test/l.png)") is None
    assert _markdown("<\x00ssh://evil>") == "\\<ssh://evil>"
    assert _markdown("[a]\r(ssh://evil)") == "\\[a\\]\n(ssh://evil)"


def test_plain_control_characters_cannot_assemble_markup() -> None:
    assert normalize("\x00# heading", TextForm.PLAIN) == "\\# heading"
    assert normalize("[a]\x00(ssh://evil)", TextForm.PLAIN) == "\\[a\\](ssh://evil)"
    assert normalize("a\x85\x9f\x7fb\x0b\x0cc", TextForm.PLAIN) == "abc"


def test_html_control_characters_from_entities_are_stripped() -> None:
    assert _html("<p>a&#x81;b&#13;c</p>") == "ab c"
    assert _html("<pre>a&#13;b&#x81;c</pre>") == "```\na\nbc\n```"
    assert _html('<a href="https://x.test/&#x81;a">z</a>') == "[z](https://x.test/a)"


def test_html_pre_in_list_item_stays_in_the_item() -> None:
    assert _html("<ul><li><pre>a\n[x](ssh://evil)\n</pre></li></ul>tail") == (
        "- ```\n  a\n  [x](ssh://evil)\n  ```\n\ntail"
    )
    assert _html("<ul><li>item<pre>a</pre></li></ul>") == "- item\n  ```\n  a\n  ```"
    assert _html("<ol><li>x<pre>a</pre></li></ol>") == "1. x\n   ```\n   a\n   ```"
    assert _html("<ul><li>a<ul><li><pre>b</pre></li></ul></li></ul>") == (
        "- a\n  - ```\n    b\n    ```"
    )


def test_html_pre_in_list_item_fence_exceeds_content_backticks() -> None:
    assert _html("<ul><li><pre>a\n```\nb</pre></li></ul>") == (
        "- ````\n  a\n  ```\n  b\n  ````"
    )


def test_html_pre_closes_heading_and_spans_first() -> None:
    assert _html("<h2><pre>[x](ssh://e)</pre></h2>") == "```\n[x](ssh://e)\n```"
    assert _html("<h2>a<pre>x</pre></h2>") == "## a\n\n```\nx\n```"
    assert _html("<li><h2><pre>x</pre></h2></li>") == "- ```\n  x\n  ```"
    assert _html('<a href="https://x.test"><pre>x</pre></a>') == "```\nx\n```"
    assert _html("<b>a<pre>x</pre>b</b>") == "**a**\n\n```\nx\n```\n\nb"
    assert _html("<li><b><pre>x</pre></b></li>") == "- ```\n  x\n  ```"


def test_html_pre_without_item_context_falls_back_to_text() -> None:
    assert _html("<ul><pre>[x](ssh://e)</pre></ul>") == "\\[x\\](ssh://e)"
    assert _html("<ul><li>a</li><pre>b</pre></ul>") == "- a\nb"


def test_html_pre_never_leaves_a_fence_open() -> None:
    assert _html("<pre>code") == "```\ncode\n```"
    assert _html("<ul><li><pre>code") == "- ```\n  code\n  ```"
    assert _html("<h2><pre>code") == "```\ncode\n```"
    assert _html("<b><pre>code") == "```\ncode\n```"


def test_html_empty_pre_between_text_keeps_the_break() -> None:
    assert _html("x<pre> </pre>after") == "x\n\nafter"


def test_html_adjacent_emphasis_merges() -> None:
    assert _html("<b>x</b><b>y</b>") == "**xy**"
    assert _html("<i>a</i><i>b</i>") == "*ab*"
    assert _html("<strong>x</strong><b>y</b><b>z</b>") == "**xyz**"
    assert _html("<b>x</b><b>p<i>q</i></b>") == "**xp*q***"


def test_html_adjacent_emphasis_of_different_kinds_is_left_alone() -> None:
    assert _html("<b>x</b><i>y</i>") == "**x***y*"
    assert _html("<b>x</b> <b>y</b>") == "**x** **y**"


def test_html_nested_same_kind_emphasis_collapses() -> None:
    assert _html("<b>a<b>b</b></b>") == "**ab**"
    assert _html("<b>a<strong>b</strong>c</b>") == "**abc**"
    assert _html("<i>a<em>b</em></i>") == "*ab*"
    assert _html("<b>a<i>b</i></b>") == "**a*b***"


def test_html_nested_links_keep_the_outer_one() -> None:
    assert _html('<a href="https://x.test">1<a href="https://y.test">2</a>3</a>') == (
        "[123](https://x.test)"
    )
    assert _html('<a href="ssh://e">1<a href="https://y.test">2</a>3</a>') == (
        "1[2](https://y.test)3"
    )


def test_html_link_text_across_blocks_is_not_linked() -> None:
    assert _html('<a href="https://x.test"><p>a</p><p>b</p></a>') == "a\n\nb"


def test_html_stray_closing_dropped_tag_is_ignored() -> None:
    assert _html("</iframe>after") == "after"
    assert _html("</script>after<style>x</style>") == "after"


def test_html_inline_code_starting_with_a_backtick_is_padded() -> None:
    assert _html("<code>`a</code>") == "`` `a ``"
    assert _html("<code>a`</code>") == "`` a` ``"


def test_cut_drops_the_whitespace_it_ends_on() -> None:
    result = normalize("a" * 49_999 + " b", TextForm.PLAIN)
    assert result == "a" * 49_999


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("> quote", "\\> quote"),
        ("a\n> quote", "a\n\\> quote"),
        ("1. one\n2) two", "1\\. one\n2\\) two"),
        ("- a\n+ b\n-- c", "\\- a\n\\+ b\n\\-- c"),
        ("a\n---", "a\n\\---"),
        ("a\n===", "a\n\\==="),
        ("~~~\ncode\n~~~", "\\~~~\ncode\n\\~~~"),
        ("***\n___", "\\*\\*\\*\n\\_\\_\\_"),
        ("  > indented quote", "  \\> indented quote"),
        ("   1. three spaces", "   1\\. three spaces"),
        ("a - b > c = d + e 1. f", "a - b > c = d + e 1. f"),
        ("3.5 stars and 2020) done", "3.5 stars and 2020) done"),
        ("AT&amp;T pays 5 < 6", "AT&amp;T pays 5 \\< 6"),
    ],
)
def test_plain_line_start_block_markers_are_escaped(text: str, expected: str) -> None:
    assert normalize(text, TextForm.PLAIN) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("    four spaces", "four spaces"),
        ("a\n\n    code?", "a\n\ncode?"),
        ("\tone tab", "one tab"),
        ("  \t mixed", "mixed"),
        ("   three", "   three"),
    ],
)
def test_plain_indentation_cannot_form_code(text: str, expected: str) -> None:
    assert normalize(text, TextForm.PLAIN) == expected


def test_html_line_start_block_markers_are_escaped() -> None:
    assert _html("<p>&gt; quote</p>") == "\\> quote"
    assert _html("<p>1. one</p>") == "1\\. one"
    assert _html("<p>2) two</p>") == "2\\) two"
    assert _html("<p>- a</p><p>+ b</p><p>= c</p><p>~~~</p>") == (
        "\\- a\n\n\\+ b\n\n\\= c\n\n\\~~~"
    )
    assert _html("<p>a<br>---</p>") == "a\n\\---"
    assert _html("<ul><li>1. x</li><li>&gt; y</li></ul>") == "- 1\\. x\n- \\> y"
    assert _html("<p>a - b &gt; c 1. d</p>") == "a - b > c 1. d"


def test_html_indentation_cannot_form_code() -> None:
    assert _html("<p>    indented</p>") == "indented"
    assert _html("<p>a<br>      b</p>") == "a\nb"
