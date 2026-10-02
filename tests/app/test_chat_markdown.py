"""The shared Markdown renderer (static/js/markdown.js), run with Node when it is installed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "bananachat" / "static" / "js" / "markdown.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def render(*texts: str, inline: bool = False) -> list[str]:
    renderer = "renderInline" if inline else "renderMessage"
    script = (f"import {{ {renderer} }} from {json.dumps(MODULE.as_uri())};\n"
              f"const inputs = JSON.parse({json.dumps(json.dumps(list(texts)))});\n"
              f"console.log(JSON.stringify(inputs.map((text) => {renderer}(text))));\n")
    result = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30,
                            check=True)
    return json.loads(result.stdout)


ATTACKS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[click](javascript:alert(1))",
    "[click](JaVaScRiPt:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD4=)",
    '[x](https://ok.example/" onmouseover="alert(1))',
    "<https://ok.example/\"onmouseover=alert(1)>",
    "```\"><script>alert(1)</script>\n<b>code</b>\n```",
    "```js\" onclick=\"alert(1)\ncode\n```",
    "| <b>a</b> | b |\n|---|---|\n| <i>c</i> | d |",
    "<think><script>alert(1)</script></think>answer",
    "**<svg onload=alert(1)>**",
    "`<script>`",
    "[`x`](https://a.example/`<b>`)",
    "https://example.com/\"><script>alert(1)</script>",
    "\u0000script\u0000",
]


def test_rendering_never_emits_markup_from_the_source():
    for source, html in zip(ATTACKS, render(*ATTACKS), strict=True):
        assert "<script" not in html.lower(), source
        assert not re.search(r"<(img|svg|iframe|object|embed|b|i)\b", html), (source, html)
        tags = re.findall(r"<[^>]*>", html)  # text between tags is escaped and may mention anything
        for tag in tags:
            assert not re.search(r"\son\w+\s*=", tag), (source, tag)
            assert "javascript:" not in tag.lower() and "data:" not in tag.lower(), (source, tag)
            assert re.fullmatch(r'</?[a-z0-9]+(\s+[a-z-]+(="[^"<>]*")?)*\s*/?>', tag), (source, tag)
        for href in re.findall(r'href="([^"]*)"', html):
            assert href.startswith(("http://", "https://", "mailto:")), (source, href)


def test_markdown_features():
    heading, lists, code, table, link, reasoning, live = render(
        "# Title\n\nSome **bold**, *italic*, `code` and ~~gone~~.",
        "- one\n- two\n  - nested\n\n3. three\n4. four",
        "```python\nprint('<hi>')\n```",
        "| a | b |\n|:-:|--:|\n| 1 | 2 |",
        "See [the docs](https://example.com/docs) or https://example.org/path. Mail <mailto:a@example.com>",
        "<think>Consider **this**</think>\n\nThe answer",
        "<think>still thinking",
    )
    assert "<h1>Title</h1>" in heading and "<strong>bold</strong>" in heading and "<em>italic</em>" in heading
    assert "<code>code</code>" in heading and "<del>gone</del>" in heading
    assert "<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>" in lists and '<ol start="3">' in lists
    assert 'data-lang="python"' in code and "print(&#39;&lt;hi&gt;&#39;)" in code and "data-code-copy" in code
    assert 'class="align-center"' in table and 'class="align-right"' in table
    assert '<a href="https://example.com/docs" target="_blank" rel="noopener noreferrer nofollow">the docs</a>' in link
    assert 'href="https://example.org/path"' in link and "path</a>." in link and 'href="mailto:a@example.com"' in link
    assert reasoning.startswith('<details class="reasoning"><summary>Reasoning</summary>')
    assert "<strong>this</strong>" in reasoning and reasoning.endswith("<p>The answer</p>")
    assert 'class="reasoning is-live"' in live and "<details open" not in live


@pytest.mark.parametrize(("source", "expected"), [
    ("Normal `code` and ``a ` tick``.", "Normal <code>code</code> and <code>a ` tick</code>."),
    ("`x`` and ``y`", "<code>x`` and ``y</code>"),
    ("`x``", "`x``"),
    ("``x`", "``x`"),
    ("``a```b``", "<code>a```b</code>"),
    ("`  code  `", "<code> code </code>"),
    (r"\*literal\* and `<script>x</script>`", "*literal* and <code>&lt;script&gt;x&lt;/script&gt;</code>"),
    (r'`<b> & " \*x\*</b>`', r"<code>&lt;b&gt; &amp; &quot; \*x\*&lt;/b&gt;</code>"),
])
def test_code_spans_use_whole_matching_delimiters_and_escape_their_content(source, expected):
    assert render(source) == [f"<p>{expected}</p>"]


def test_inline_code_spans_preserve_multiline_content_and_nested_link_labels():
    multiline, linked = render("Inline `one\ntwo`.", "[`code`](https://example.com)", inline=True)
    assert multiline == "Inline <code>one\ntwo</code>."
    assert linked == ('<a href="https://example.com" target="_blank" rel="noopener noreferrer nofollow">'
                      '<code>code</code></a>')


def test_underscore_emphasis_preserves_word_boundaries_and_whitespace():
    sources = ("__bold__", "a__word__b", "__a__ __b__", "__one\ntwo__", "_____",
               "__ x__", "__x __", "__x__word")
    assert render(*sources, inline=True) == [
        "<strong>bold</strong>", "a__word__b", "<strong>a</strong> <strong>b</strong>",
        "<strong>one\ntwo</strong>", "<strong>_</strong>", "__ x__", "__x __", "__x__word",
    ]


def test_multiple_character_emphasis_preserves_formatting_and_unusable_closers():
    sources = ("***both***", "**bold**", "~~gone~~", "***<script>x</script>***",
               "**a **b ", "***a ***b ", "~~a ~~b ")
    assert render(*sources, inline=True) == [
        "<strong><em>both</em></strong>", "<strong>bold</strong>", "<del>gone</del>",
        "<strong><em>&lt;script&gt;x&lt;/script&gt;</em></strong>",
        "**a **b ", "<strong>*a *</strong>b ", "~~a ~~b ",
    ]


def test_heading_prefixes_closing_hashes_and_escaping():
    sources = ("# Title ###", "## Title###", "# text ## ##", "#######",
               "#no heading", "####### title", "## <script>x</script> ##")
    assert render(*sources) == [
        "<h1>Title</h1>", "<h2>Title</h2>", "<h1>text ##</h1>", "<h6></h6>",
        "<p>#no heading</p>", "<p>####### title</p>", "<h2>&lt;script&gt;x&lt;/script&gt;</h2>",
    ]


def test_fences_keep_the_first_language_token_and_require_matching_closers():
    normal, shorter, invalid = render("~~~ python extra\n<unsafe>\n~~~~",
                                      "````js\none\n```\ntwo\n````", "~~~ invalid`")
    assert 'data-lang="python"' in normal and "&lt;unsafe&gt;" in normal
    assert 'data-lang="js"' in shorter and "one\n```\ntwo" in shorter
    assert invalid == "<p>~~~ invalid`</p>"


def test_table_dividers_accept_outer_pipes_and_reject_empty_or_invalid_cells():
    table, empty_cell, invalid_cell = render("a|b\n| :--: | ---: |\n1|2",
                                            "a|b\n|--||--|", "a|b\n|--|x|")
    assert 'class="align-center"' in table and 'class="align-right"' in table
    assert "<tbody><tr>" in table
    assert empty_cell == "<p>a|b<br>|--||--|</p>"
    assert invalid_cell == "<p>a|b<br>|--|x|</p>"


def test_list_prefixes_preserve_numbering_indentation_and_unicode_spacing():
    ordered, nested, separator_in_content, separator_in_spacing = render(
        "9) first\n10) second", "- one\n  - nested", "- a\u2028x", "- \u2028a")
    assert ordered == '<ol start="9"><li>first</li><li>second</li></ol>'
    assert nested == "<ul><li>one<ul><li>nested</li></ul></li></ul>"
    assert separator_in_content == "<p>- a\u2028x</p>"
    assert separator_in_spacing == "<ul><li>a</li></ul>"


def test_long_unmatched_markdown_delimiters_render_promptly_without_losing_text_or_escaping():
    # A valid answer can contain 1 MiB of text. A code-span regex used to retry
    # each suffix of an unmatched run, freezing the browser even at 64 KiB.
    script = rf"""
import assert from "node:assert/strict";
import {{ renderInline, renderMessage }} from {json.dumps(MODULE.as_uri())};
const run = "`".repeat(1024 * 1024);
const differentRuns = Array.from({{ length: 512 }}, (_, index) => "`".repeat(index + 1)).join("x");
const underscores = "__a ".repeat(256 * 1024);
const spaces = " ".repeat(1024 * 1024);
const started = performance.now();
assert.equal(renderMessage("a" + run), "<p>a" + run + "</p>");
assert.equal(renderMessage("a" + run + "<script>x</script>"),
    "<p>a" + run + "&lt;script&gt;x&lt;/script&gt;</p>");
assert.equal(renderInline(run + "x" + run), "<code>x</code>");
assert.equal(renderInline(differentRuns), differentRuns);
assert.equal(renderMessage(underscores + "end"), "<p>" + underscores + "end</p>");
assert.equal(renderInline("__ok__ " + underscores), "<strong>ok</strong> " + underscores);
for (const pattern of ["**a ", "***a ", "~~a "]) {{
    const count = Math.floor(1024 * 1024 / pattern.length);
    const source = pattern.repeat(count);
    const rendered = renderInline(source);
    assert.equal((rendered.match(/a/g) || []).length, count);
    if (pattern !== "***a ") assert.equal(rendered, source);
}}
assert.equal(renderMessage("# a" + spaces + "x"), "<h1>a" + spaces + "x</h1>");
assert.equal(renderMessage("# a" + "#".repeat(1024 * 1024) + "x"),
    "<h1>a" + "#".repeat(1024 * 1024) + "x</h1>");
assert.equal(renderMessage("~~~" + spaces + "`"), "<p>~~~" + spaces + "`</p>");
assert.equal(renderMessage("a|b\n" + spaces + "x"), "<p>a|b<br>x</p>");
assert.equal(renderMessage("- " + spaces + "a\u2028x"), "<p>- " + spaces + "a\u2028x</p>");
console.log(JSON.stringify({{ elapsed: performance.now() - started }}));
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True,
                            timeout=10, check=True)
    assert json.loads(result.stdout)["elapsed"] < 5000
