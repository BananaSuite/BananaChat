"use strict";


//  Markdown renderer (lightweight, no external dependencies)

function buildCodeBlock(lang, code) {
  var escaped = code
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  var safeLang = (lang || "").replace(/[^a-z0-9_+#.\-]/gi, "");
  var langHtml = safeLang ? '<span class="code-block-lang">' + safeLang + "</span>" : "";
  return (
    '<div class="code-block" data-language="' + safeLang + '">' +
    '<div class="code-block-toolbar">' + langHtml + '<button type="button" class="code-download-btn">Download</button></div>' +
    '<pre><code>' + escaped + "</code></pre></div>"
  );
}

function inlineFormat(text) {
  var s = text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  s = s.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__(.+?)__/g, "<strong>$1</strong>");
  s = s.replace(/\*([^\*\n]+)\*/g, "<em>$1</em>");
  s = s.replace(/_([^_\n]+)_/g, "<em>$1</em>");
  return s;
}

function renderMarkdown(raw) {
  if (!raw) return "";
  var lines = raw.split("\n");
  var out = "";
  var i = 0;
  while (i < lines.length) {
    var line = lines[i];
    // Fenced code block
    if (/^```/.test(line)) {
      var lang = line.slice(3).trim();
      var codeLines = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      out += buildCodeBlock(lang, codeLines.join("\n"));
      i++;
      continue;
    }
    // ATX headers
    var hm = line.match(/^(#{1,3})\s+(.+)/);
    if (hm) {
      out += "<h" + hm[1].length + ' class="md-h">' + inlineFormat(hm[2]) + "</h" + hm[1].length + ">";
      i++;
      continue;
    }
    // Horizontal rule
    if (/^(---|\*\*\*|___)$/.test(line.trim())) {
      out += '<hr class="md-hr">';
      i++;
      continue;
    }
    // Unordered list block
    if (/^[-*+] /.test(line)) {
      out += '<ul class="md-list">';
      while (i < lines.length && /^[-*+] /.test(lines[i])) {
        out += "<li>" + inlineFormat(lines[i].slice(2)) + "</li>";
        i++;
      }
      out += "</ul>";
      continue;
    }
    // Ordered list block
    if (/^\d+\.\s/.test(line)) {
      out += '<ol class="md-list">';
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {
        out += "<li>" + inlineFormat(lines[i].replace(/^\d+\.\s/, "")) + "</li>";
        i++;
      }
      out += "</ol>";
      continue;
    }
    // Blank line
    if (line.trim() === "") { i++; continue; }
    // Paragraph: collect consecutive plain lines
    var paraLines = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^```/.test(lines[i]) &&
      !/^#{1,3}\s/.test(lines[i]) &&
      !/^[-*+] /.test(lines[i]) &&
      !/^\d+\.\s/.test(lines[i]) &&
      !/^(---|\*\*\*|___)$/.test(lines[i].trim())
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    if (paraLines.length) {
      out += '<p class="md-p">' + paraLines.map(inlineFormat).join("<br>") + "</p>";
    }
  }
  return out;
}


//  Reasoning / thinking block support (<think>…</think>)

function parseThinking(text) {
  if (!text) return { hasThinking: false, answer: text || "" };
  var openIdx = text.indexOf("<think>");
  if (openIdx === -1) return { hasThinking: false, answer: text };
  if (text.slice(0, openIdx).trim()) return { hasThinking: false, answer: text };
  var afterOpen = text.slice(openIdx + 7);
  var closeIdx = afterOpen.indexOf("</think>");
  if (closeIdx === -1) {
    return { hasThinking: true, thinking: afterOpen, answer: "", done: false };
  }
  return {
    hasThinking: true,
    thinking: afterOpen.slice(0, closeIdx),
    answer: afterOpen.slice(closeIdx + 8).replace(/^\s+/, ""),
    done: true,
  };
}

function renderWithThinking(text, isStreaming) {
  var t = parseThinking(text);
  if (!t.hasThinking) return renderMarkdown(text);

  var thinkHtml;
  if (t.done) {
    thinkHtml =
      '<details class="thinking-block">' +
        '<summary class="thinking-summary">' +
          '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" style="flex-shrink:0">' +
            '<circle cx="6" cy="6" r="5" stroke="currentColor" stroke-width="1.5"/>' +
            '<path d="M6 4v3M6 8.5v.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
          '</svg>' +
          ' Reasoning <span class="thinking-toggle-hint">(click to expand)</span>' +
        '</summary>' +
        '<div class="thinking-content">' + escapeHtml(t.thinking) + '</div>' +
      '</details>';
  } else {
    thinkHtml =
      '<div class="thinking-block thinking-in-progress">' +
        '<div class="thinking-summary">' +
          '<div class="typing-indicator" style="padding:0;display:inline-flex;gap:3px;">' +
            '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>' +
          '</div> Reasoning…' +
        '</div>' +
        (t.thinking.trim()
          ? '<div class="thinking-content">' + escapeHtml(t.thinking) + '</div>'
          : '') +
      '</div>';
  }

  return thinkHtml + (t.answer ? renderMarkdown(t.answer) : "");
}

function applyMarkdownToExistingMessages() {
  document.querySelectorAll(".message.assistant .message-bubble").forEach(function(el) {
    var raw = el.textContent;
    if (raw.trim() && !el.querySelector(".typing-indicator")) {
      el.innerHTML = renderWithThinking(raw, false);
      el.classList.add("md-rendered");
    }
  });
}

var CODE_EXTENSIONS = {
  python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts",
  jsx: "jsx", tsx: "tsx", html: "html", css: "css", json: "json", bash: "sh",
  shell: "sh", sh: "sh", sql: "sql", java: "java", c: "c", cpp: "cpp",
  "c++": "cpp", csharp: "cs", cs: "cs", go: "go", rust: "rs", ruby: "rb",
  php: "php", markdown: "md", md: "md", yaml: "yml", xml: "xml",
};

function enhanceCodeDownloads(root) {
  (root || document).querySelectorAll(".code-block:not([data-download-ready])").forEach(function(block) {
    block.dataset.downloadReady = "1";
    var button = block.querySelector(".code-download-btn");
    var code = block.querySelector("code");
    if (!button || !code) return;
    button.addEventListener("click", function() {
      var lang = (block.dataset.language || "").toLowerCase();
      var extension = CODE_EXTENSIONS[lang] || "txt";
      var blob = new Blob([code.textContent], { type: "text/plain;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = "generated-code." + extension;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function() { URL.revokeObjectURL(url); }, 0);
    });
  });
}

function speechTextForMessage(message) {
  var bubble = message.querySelector(".message-bubble");
  if (!bubble) return "";
  var clone = bubble.cloneNode(true);
  clone.querySelectorAll(".thinking-block,.code-block").forEach(function(el) { el.remove(); });
  return clone.textContent.replace(/\s+/g, " ").trim();
}

function enhanceAssistantTools(root) {
  enhanceCodeDownloads(root || document);
  var canSpeak = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
  (root || document).querySelectorAll(".message.assistant:not([data-tools-ready])").forEach(function(message) {
    if (message.querySelector(".typing-indicator")) return;
    message.dataset.toolsReady = "1";
    var actions = message.querySelector(".message-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "message-actions";
      message.appendChild(actions);
    }
    var messageId = message.dataset.messageId;
    var sessionInput = document.getElementById("chat-session-id");
    if (messageId && sessionInput && !actions.querySelector("[data-pdf-action]")) {
      var pdf = document.createElement("a");
      pdf.className = "message-action-btn";
      pdf.dataset.pdfAction = "1";
      pdf.href = "/chat/" + encodeURIComponent(sessionInput.value) + "/messages/" + encodeURIComponent(messageId) + "/pdf";
      pdf.textContent = "PDF";
      actions.appendChild(pdf);
    }
    if (!canSpeak || actions.querySelector(".speech-play-btn")) return;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "message-action-btn speech-play-btn";
    button.textContent = tr("read_aloud");
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", function() {
      if (button.getAttribute("aria-pressed") === "true") {
        window.speechSynthesis.cancel();
        button.setAttribute("aria-pressed", "false");
        button.textContent = tr("read_aloud");
        return;
      }
      var text = speechTextForMessage(message);
      if (!text) return;
      window.speechSynthesis.cancel();
      document.querySelectorAll(".speech-play-btn").forEach(function(item) {
        item.setAttribute("aria-pressed", "false"); item.textContent = tr("read_aloud");
      });
      var utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = window.BC_LANG === "it" ? "it-IT" : "en-US";
      utterance.onstart = function() { button.setAttribute("aria-pressed", "true"); button.textContent = tr("stop_reading"); };
      utterance.onend = utterance.onerror = function() { button.setAttribute("aria-pressed", "false"); button.textContent = tr("read_aloud"); };
      window.speechSynthesis.speak(utterance);
    });
    actions.appendChild(button);
  });
}

function initVoiceInput() {
  var button = document.getElementById("voice-input-btn");
  var input = document.getElementById("chat-input");
  var status = document.getElementById("voice-status");
  var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!button || !input || !Recognition) return;
  button.hidden = false;
  var recognition = null;
  var listening = false;
  var baseText = "";
  var recognitionFailed = false;

  function setListening(active, message) {
    listening = active;
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.classList.toggle("is-listening", active);
    button.title = active ? tr("voice_stop_listening") : tr("voice_input");
    button.setAttribute("aria-label", button.title);
    if (status) status.textContent = message || "";
  }

  function stopRecognition() {
    if (recognition && listening) recognition.stop();
  }
  window.BC_STOP_RECOGNITION = stopRecognition;

  button.addEventListener("click", function() {
    if (listening) { stopRecognition(); return; }
    window.speechSynthesis && window.speechSynthesis.cancel();
    recognition = new Recognition();
    recognitionFailed = false;
    recognition.lang = window.BC_LANG === "it" ? "it-IT" : "en-US";
    recognition.interimResults = true;
    recognition.continuous = false;
    baseText = input.value.trimEnd();
    recognition.onstart = function() { setListening(true, tr("voice_listening")); };
    recognition.onresult = function(event) {
      var transcript = "";
      for (var index = 0; index < event.results.length; index++) {
        transcript += event.results[index][0].transcript;
      }
      input.value = baseText + (baseText && transcript ? " " : "") + transcript;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    recognition.onerror = function(event) {
      var denied = event.error === "not-allowed" || event.error === "service-not-allowed";
      recognitionFailed = true;
      setListening(false, denied ? tr("voice_permission_denied") : tr("voice_error"));
    };
    recognition.onend = function() {
      if (!recognitionFailed) setListening(false, "");
      else { listening = false; button.setAttribute("aria-pressed", "false"); button.classList.remove("is-listening"); }
    };
    try { recognition.start(); } catch (error) { setListening(false, tr("voice_error")); }
  });
  window.addEventListener("pagehide", function() {
    stopRecognition();
    if (window.speechSynthesis) window.speechSynthesis.cancel();
  });
}
