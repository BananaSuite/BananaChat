// BananaChat Markdown renderer (ES module, no dependencies, no DOM needed to render).
//
//   import { renderMessage, enhanceMarkdown } from "./markdown.js";
//   node.innerHTML = renderMessage(text, { labels });   // then, once per page:
//   enhanceMarkdown(document, labels);                   // copy/download buttons of code blocks
//
// Safety model: every piece of source text is HTML-escaped before it is
// emitted; the renderer only ever adds a fixed set of tags whose attributes
// are either constants or escaped values it validated itself (link targets
// must be http, https or mailto). No source HTML is ever passed through.
// Output belongs inside an element with the "prose" class.

const DEFAULT_LABELS = {
  reasoning: "Reasoning",
  reasoningLive: "Reasoning…",
  copy: "Copy",
  copied: "Copied",
  download: "Download",
  code: "Code",
};
const MAX_DEPTH = 8;

export function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ----- reasoning (<think>) ----------------------------------------------------

/** Split a leading <think>…</think> block: { reasoning, answer, open } (open = still streaming). */
export function splitReasoning(text) {
  const source = String(text ?? "");
  const match = /^\s*<think>/.exec(source);
  if (!match) return { reasoning: "", answer: source, open: false };
  const rest = source.slice(match[0].length);
  const end = rest.indexOf("</think>");
  if (end === -1) return { reasoning: rest.trim(), answer: "", open: true };
  return { reasoning: rest.slice(0, end).trim(), answer: rest.slice(end + 8).replace(/^\s+/, ""), open: false };
}

// ----- inline -------------------------------------------------------------------

const SAFE_URL = /^(https?:\/\/|mailto:)[^\s<>"'`\u0000]+$/i;

function safeUrl(url) {
  const trimmed = String(url).trim();
  return SAFE_URL.test(trimmed) ? trimmed : null;
}

function linkHtml(url, labelHtml) {
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer nofollow">${labelHtml}</a>`;
}

/** Trim trailing punctuation that belongs to the sentence, not to a bare URL. */
function splitAutolink(url) {
  let end = url.length;
  while (end > 0 && /[.,;:!?'"*_~]/.test(url[end - 1])) end -= 1;
  let core = url.slice(0, end);
  while (core.endsWith(")") && (core.match(/\(/g) || []).length < (core.match(/\)/g) || []).length) {
    core = core.slice(0, -1);
  }
  return [core, url.slice(core.length)];
}

function delimitedEmphasis(html, delimiter, openTag, closeTag, wordBoundaries = false) {
  // Index usable closers once rather than retrying an unbounded regex at each
  // opener. Model output may contain long runs with no usable closing marker.
  const width = delimiter.length;
  const closes = [];
  for (let index = html.indexOf(delimiter); index !== -1; index = html.indexOf(delimiter, index + 1)) {
    if (index > 0 && !/\s/.test(html[index - 1])
        && (!wordBoundaries || !/\w/.test(html[index + width] || ""))) closes.push(index);
  }
  if (!closes.length) return html;
  const out = [];
  let cursor = 0, closing = 0;
  for (let open = html.indexOf(delimiter); open !== -1; open = html.indexOf(delimiter, open + 1)) {
    if ((wordBoundaries && open > 0 && (open <= cursor || /\w/.test(html[open - 1])))
        || open + width >= html.length || /\s/.test(html[open + width])) continue;
    while (closing < closes.length && closes[closing] < open + width + 1) closing += 1;
    if (closing === closes.length) break;
    const close = closes[closing];
    out.push(html.slice(cursor, open), openTag, html.slice(open + width, close), closeTag);
    cursor = close + width;
    open = cursor - 1;
  }
  out.push(html.slice(cursor));
  return out.join("");
}

function emphasis(html) {
  html = delimitedEmphasis(html, "***", "<strong><em>", "</em></strong>");
  html = delimitedEmphasis(html, "**", "<strong>", "</strong>");
  html = delimitedEmphasis(html, "__", "<strong>", "</strong>", true);
  html = html
    .replace(/(^|[^*\w])\*(?=[^\s*])([^*]*?[^\s*])\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/(^|[^\w])_(?=[^\s_])([^_]*?[^\s_])_(?!\w)/g, "$1<em>$2</em>");
  return delimitedEmphasis(html, "~~", "<del>", "</del>");
}

function codeSpans(source, hold) {
  // Index complete backtick runs and their next equal-length run once. A
  // backreference regex retries every suffix of a long unmatched run and can
  // block the browser for seconds on otherwise valid model output.
  const runs = [];
  const previous = new Map();
  for (const match of source.matchAll(/`+/g)) {
    const width = match[0].length;
    const prior = previous.get(width);
    if (prior !== undefined) runs[prior].next = runs.length;
    previous.set(width, runs.length);
    runs.push({ start: match.index, end: match.index + width, next: -1 });
  }
  const out = [];
  let cursor = 0;
  for (let index = 0; index < runs.length; index += 1) {
    const open = runs[index];
    if (open.next === -1) continue;
    const close = runs[open.next];
    const code = source.slice(open.end, close.start);
    out.push(source.slice(cursor, open.start),
      hold(`<code>${escapeHtml(code.replace(/^ (.*) $/, "$1"))}</code>`));
    cursor = close.end;
    index = open.next;
  }
  out.push(source.slice(cursor));
  return out.join("");
}

/**
 * Inline Markdown to HTML. Code spans and links are cut out first and kept as
 * placeholders, the rest is escaped, emphasis is applied to the escaped text,
 * then the placeholders are restored.
 */
export function renderInline(text, { links = true } = {}, shared = null) {
  // A link label is rendered with the caller's placeholder table (shared): it may hold code spans cut out already.
  const tokens = shared || [];
  const hold = (html) => `\u0000${tokens.push(html) - 1}\u0000`;
  let source = shared ? String(text) : String(text).replace(/\u0000/g, "�");

  source = codeSpans(source, hold);
  if (links) {
    // An image becomes a link to it, labelled with its alt text: remote images are never loaded.
    source = source.replace(/(!?)\[([^\[\]\n]{0,500})\]\(\s*<?((?:[^()\s<>]|\([^()\s<>]*\)){1,2000})>?(?:\s+"[^"\n]*")?\s*\)/g, (match, image, label, url) => {
      if (!image && !label) return match;
      const target = safeUrl(url);
      const labelHtml = label ? renderInline(label, { links: false }, tokens) : escapeHtml(target || url);
      return target ? hold(linkHtml(target, labelHtml)) : hold(`${labelHtml} (${escapeHtml(url)})`);
    });
    source = source.replace(/<((?:https?:\/\/|mailto:)[^\s<>]{1,2000})>/gi, (match, url) => {
      const target = safeUrl(url);
      return target ? hold(linkHtml(target, escapeHtml(target))) : match;
    });
    source = source.replace(/\bhttps?:\/\/[^\s<>"'`\u0000]{1,2000}/gi, (match) => {
      const [core, tail] = splitAutolink(match);
      const target = safeUrl(core);
      return target ? hold(linkHtml(target, escapeHtml(core))) + tail : match;
    });
  }
  // Backslash escapes (code spans and link addresses keep their backslashes): the character is literal.
  source = source.replace(/\\([!-/:-@[-`{-~])/g, (match, char) => hold(escapeHtml(char)));
  let html = emphasis(escapeHtml(source));
  // Placeholders may nest (a code span inside a link label), so restore until none remain.
  for (let pass = 0; pass < 3 && html.includes("\u0000"); pass += 1) {
    html = html.replace(/\u0000(\d+)\u0000/g, (match, index) => tokens[Number(index)] ?? "");
  }
  return html;
}

// ----- blocks --------------------------------------------------------------------

const FENCE_PREFIX = /^ {0,3}(`{3,}|~{3,})/;
const HEADING_PREFIX = /^ {0,3}(#{1,6})/;
const RULE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const SETEXT = /^ {0,3}(=+|-+)\s*$/;
const QUOTE = /^ {0,3}> ?(.*)$/;
const LIST_PREFIX = /^( *)([-*+]|\d{1,9}[.)])(\s+)/;
const LINE_BREAK = /[\r\n\u2028\u2029]/;

// Separate prefix recognition from whitespace/closing-marker trimming. An
// anchored regex with overlapping whitespace groups can retry long malformed
// lines quadratically, even before their inline content is rendered.
function readFence(line) {
  const prefix = FENCE_PREFIX.exec(line);
  if (!prefix) return null;
  const rest = line.slice(prefix[0].length);
  if (rest.includes("`")) return null;
  const info = rest.trimStart();
  const space = info.search(/\s/);
  return { marker: prefix[1], language: space === -1 ? info : info.slice(0, space) };
}

function readHeading(line) {
  const prefix = HEADING_PREFIX.exec(line);
  if (!prefix) return null;
  const rest = line.slice(prefix[0].length);
  if (rest && !/^\s/.test(rest)) {
    // A line made only of hashes is an empty heading, capped at level six.
    if (!/^#*\s*$/.test(rest)) return null;
    return { level: prefix[1].length, content: "" };
  }
  let content = rest.trim();
  let end = content.length;
  while (end > 0 && content[end - 1] === "#") end -= 1;
  content = content.slice(0, end).trimEnd();
  if (LINE_BREAK.test(content)) return null;
  return { level: prefix[1].length, content };
}

function readListItem(line) {
  const prefix = LIST_PREFIX.exec(line);
  if (!prefix) return null;
  const content = line.slice(prefix[0].length);
  if (LINE_BREAK.test(content)) return null;
  return { indent: prefix[1], marker: prefix[2], spacing: prefix[3], content };
}

function isTableDivider(line) {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|")) row = row.slice(0, -1);
  return row.split("|").every((cell) => /^:?-+:?$/.test(cell.trim()));
}

function indentOf(line) {
  return line.length - line.replace(/^ +/, "").length;
}

function expandTabs(line) {
  return line.replace(/\t/g, "    ");
}

function codeBlock(language, code, labels) {
  const lang = String(language || "").replace(/[^A-Za-z0-9_+#.-]/g, "").slice(0, 30);
  const label = escapeHtml(lang || labels.code);
  const langAttribute = lang ? ` data-lang="${escapeHtml(lang)}"` : "";
  return `<div class="code-block"><header><span class="code-lang">${label}</span><span class="code-actions">`
    + `<button type="button" class="btn btn-ghost btn-sm" data-code-copy>${escapeHtml(labels.copy)}</button>`
    + `<button type="button" class="btn btn-ghost btn-sm" data-code-download${langAttribute}>${escapeHtml(labels.download)}</button>`
    + `</span></header><pre><code${lang ? ` class="language-${escapeHtml(lang)}"` : ""}>${escapeHtml(code)}</code></pre></div>`;
}

function splitCells(line) {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
  const cells = [];
  let current = "";
  for (let index = 0; index < row.length; index += 1) {
    const char = row[index];
    if (char === "\\" && row[index + 1] === "|") { current += "|"; index += 1; }
    else if (char === "|") { cells.push(current.trim()); current = ""; }
    else current += char;
  }
  cells.push(current.trim());
  return cells;
}

function table(header, divider, rows) {
  const aligns = splitCells(divider).map((cell) => {
    const left = cell.startsWith(":"), right = cell.endsWith(":");
    return left && right ? "center" : right ? "right" : left ? "left" : "";
  });
  const cell = (tag, text, index) => {
    const align = aligns[index] ? ` class="align-${aligns[index]}"` : "";
    return `<${tag}${align}>${renderInline(text)}</${tag}>`;
  };
  const headCells = splitCells(header);
  const head = `<tr>${headCells.map((text, index) => cell("th", text, index)).join("")}</tr>`;
  const body = rows.map((line) => {
    const cells = splitCells(line);
    return `<tr>${headCells.map((_, index) => cell("td", cells[index] ?? "", index)).join("")}</tr>`;
  }).join("");
  return `<div class="table-scroll"><table><thead>${head}</thead>${body ? `<tbody>${body}</tbody>` : ""}</table></div>`;
}

function startsBlock(line, next) {
  return readFence(line) || readHeading(line) || RULE.test(line) || QUOTE.test(line) || readListItem(line)
    || (line.includes("|") && next !== undefined && isTableDivider(next) && next.includes("-"));
}

function list(lines, start, labels, depth) {
  const first = readListItem(lines[start]);
  const base = first.indent.length;
  const ordered = /\d/.test(first.marker);
  const items = [];
  let index = start;
  while (index < lines.length) {
    if (index > start && lines[index].trim() === "") {
      // Blank lines between items make a loose list, still one list.
      let ahead = index;
      while (ahead < lines.length && lines[ahead].trim() === "") ahead += 1;
      const again = ahead < lines.length ? readListItem(lines[ahead]) : null;
      if (!again || again.indent.length !== base || /\d/.test(again.marker) !== ordered) break;
      index = ahead;
    }
    const match = readListItem(lines[index]);
    if (!match || match.indent.length !== base || /\d/.test(match.marker) !== ordered) break;
    const offset = base + match.marker.length + Math.min(match.spacing.length, 4);
    const body = [match.content];
    index += 1;
    while (index < lines.length) {
      const line = lines[index];
      if (line.trim() === "") {
        const following = lines[index + 1];
        if (following !== undefined && following.trim() !== "" && indentOf(following) > base) {
          body.push("");
          index += 1;
          continue;
        }
        break;
      }
      const nested = readListItem(line);
      if (nested && nested.indent.length <= base) break;
      if (indentOf(line) <= base && startsBlock(line, lines[index + 1])) break;
      body.push(line.slice(Math.min(indentOf(line), offset)));
      index += 1;
    }
    let html = renderBlocks(body, labels, depth + 1);
    const single = /^<p>([\s\S]*)<\/p>$/.exec(html);
    if (single && !single[1].includes("<p>")) html = single[1];
    else html = html.replace(/^<p>([\s\S]*?)<\/p>(?=<(?:ul|ol)[ >])/, "$1");
    items.push(`<li>${html}</li>`);
  }
  const number = parseInt(first.marker, 10);
  const open = ordered ? (number !== 1 && Number.isFinite(number) ? `<ol start="${number}">` : "<ol>") : "<ul>";
  return { html: `${open}${items.join("")}${ordered ? "</ol>" : "</ul>"}`, next: index };
}

function renderBlocks(lines, labels, depth) {
  if (depth > MAX_DEPTH) return `<p>${renderInline(lines.join("\n")).replace(/\n/g, "<br>")}</p>`;
  const out = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim() === "") { index += 1; continue; }

    const fence = readFence(line);
    if (fence) {
      const marker = fence.marker;
      const code = [];
      index += 1;
      while (index < lines.length) {
        const closing = /^ {0,3}(`{3,}|~{3,})\s*$/.exec(lines[index]);
        if (closing && closing[1][0] === marker[0] && closing[1].length >= marker.length) break;
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      out.push(codeBlock(fence.language, code.join("\n"), labels));
      continue;
    }
    const heading = readHeading(line);
    if (heading) {
      out.push(`<h${heading.level}>${renderInline(heading.content)}</h${heading.level}>`);
      index += 1;
      continue;
    }
    if (RULE.test(line)) { out.push("<hr>"); index += 1; continue; }
    if (QUOTE.test(line)) {
      const quoted = [];
      while (index < lines.length && lines[index].trim() !== "" && (QUOTE.test(lines[index]) || quoted.length)) {
        const match = QUOTE.exec(lines[index]);
        if (!match && startsBlock(lines[index], lines[index + 1])) break;
        quoted.push(match ? match[1] : lines[index]);
        index += 1;
      }
      out.push(`<blockquote>${renderBlocks(quoted, labels, depth + 1)}</blockquote>`);
      continue;
    }
    if (readListItem(line)) {
      const result = list(lines, index, labels, depth);
      out.push(result.html);
      index = result.next;
      continue;
    }
    if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1]) && lines[index + 1].includes("-")) {
      const rows = [];
      let cursor = index + 2;
      while (cursor < lines.length && lines[cursor].trim() !== "" && lines[cursor].includes("|")) {
        rows.push(lines[cursor]);
        cursor += 1;
      }
      out.push(table(line, lines[index + 1], rows));
      index = cursor;
      continue;
    }
    const paragraph = [];
    let level = 0;
    while (index < lines.length && lines[index].trim() !== "") {
      if (paragraph.length) {
        const underline = SETEXT.exec(lines[index]);
        if (underline) {
          // A paragraph underlined with === or --- is a heading.
          level = underline[1][0] === "=" ? 1 : 2;
          index += 1;
          break;
        }
        if (startsBlock(lines[index], lines[index + 1])) break;
      }
      paragraph.push(lines[index].trim());
      index += 1;
    }
    if (level) out.push(`<h${level}>${paragraph.map((part) => renderInline(part)).join(" ")}</h${level}>`);
    else out.push(`<p>${paragraph.map((part) => renderInline(part)).join("<br>")}</p>`);
  }
  return out.join("");
}

/** Markdown to HTML (no reasoning handling). */
export function renderMarkdown(text, { labels = {} } = {}) {
  const merged = { ...DEFAULT_LABELS, ...labels };
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n").map(expandTabs);
  return renderBlocks(lines, merged, 0);
}

/** An answer: a leading <think> block becomes a collapsed "Reasoning" section, the rest is Markdown. */
export function renderMessage(text, { labels = {} } = {}) {
  const merged = { ...DEFAULT_LABELS, ...labels };
  const { reasoning, answer, open } = splitReasoning(text);
  let html = "";
  if (reasoning || open) {
    const summary = escapeHtml(open ? merged.reasoningLive : merged.reasoning);
    html += `<details class="reasoning${open ? " is-live" : ""}"><summary>${summary}</summary>`
      + `<div class="reasoning-body">${renderMarkdown(reasoning, { labels: merged })}</div></details>`;
  }
  return html + renderMarkdown(answer, { labels: merged });
}

// ----- behaviour (browser only) ----------------------------------------------------------

const EXTENSIONS = {
  python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts", jsx: "jsx", tsx: "tsx",
  html: "html", css: "css", scss: "scss", json: "json", bash: "sh", shell: "sh", sh: "sh", zsh: "sh",
  sql: "sql", java: "java", kotlin: "kt", c: "c", cpp: "cpp", "c++": "cpp", csharp: "cs", cs: "cs", go: "go",
  rust: "rs", ruby: "rb", php: "php", markdown: "md", md: "md", yaml: "yml", yml: "yml", xml: "xml",
  toml: "toml", ini: "ini", swift: "swift", lua: "lua", r: "r", tex: "tex", latex: "tex", csv: "csv",
};

const enhanced = new WeakSet();

/** Wire the copy/download buttons of code blocks inside root (idempotent, event delegation). */
export function enhanceMarkdown(root = document, labels = {}) {
  if (enhanced.has(root)) return;
  enhanced.add(root);
  const merged = { ...DEFAULT_LABELS, ...labels };
  root.addEventListener("click", async (event) => {
    const button = event.target.closest?.("[data-code-copy], [data-code-download]");
    if (!button) return;
    const code = button.closest(".code-block")?.querySelector("code");
    if (!code) return;
    if (button.hasAttribute("data-code-copy")) {
      try {
        await navigator.clipboard.writeText(code.textContent);
        button.textContent = merged.copied;
        setTimeout(() => { button.textContent = merged.copy; }, 1500);
      } catch { /* clipboard unavailable: nothing to do */ }
      return;
    }
    const extension = EXTENSIONS[(button.dataset.lang || "").toLowerCase()] || "txt";
    const url = URL.createObjectURL(new Blob([code.textContent], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `code.${extension}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  });
}

/** Plain text of rendered content for reading aloud (reasoning and code left out). */
export function speakableText(element) {
  const clone = element.cloneNode(true);
  for (const node of clone.querySelectorAll(".reasoning, .code-block")) node.remove();
  return clone.textContent.replace(/\s+/g, " ").trim();
}
