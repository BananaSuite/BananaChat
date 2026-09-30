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


def render(*texts: str) -> list[str]:
    script = (f"import {{ renderMessage }} from {json.dumps(MODULE.as_uri())};\n"
              f"const inputs = JSON.parse({json.dumps(json.dumps(list(texts)))});\n"
              "console.log(JSON.stringify(inputs.map((text) => renderMessage(text))));\n")
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
