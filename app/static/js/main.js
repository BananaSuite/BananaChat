/* BananaChat: main.js */
"use strict";


//  Utilities

function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function tr(key, fallback) {
  var strings = window.BC_I18N || {};
  return strings[key] || fallback || key;
}

function showGenerationOutcome(element, state, message) {
  if (!element || ["stopped", "failed", "interrupted"].indexOf(state) === -1) return;
  var note = element.querySelector(".chat-outcome");
  if (!note) {
    note = document.createElement("p");
    note.className = "chat-outcome text-muted";
    note.setAttribute("role", "status");
    element.appendChild(note);
  }
  note.textContent = tr("generation_" + state) + (message ? ". " + message : "");
}


//  Site-native confirm dialog (replaces window.confirm everywhere)

function showConfirm(msg, onConfirm) {
  var overlay = document.createElement("div");
  overlay.style.cssText =
    "position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;" +
    "justify-content:center;z-index:9999;";

  var box = document.createElement("div");
  box.style.cssText =
    "background:#1e1e2c;border:1px solid #2d2d4a;border-radius:10px;" +
    "padding:1.4rem 1.6rem;width:min(420px,92vw);display:flex;flex-direction:column;gap:1rem;";

  var msgEl = document.createElement("p");
  msgEl.style.cssText = "margin:0;color:#c8ccd8;font-size:0.925rem;line-height:1.5;";
  msgEl.textContent = msg;

  var actions = document.createElement("div");
  actions.style.cssText = "display:flex;justify-content:flex-end;gap:0.5rem;";

  var cancelBtn = document.createElement("button");
  cancelBtn.className = "btn btn-sm";
  cancelBtn.textContent = "Cancel";

  var confirmBtn = document.createElement("button");
  confirmBtn.className = "btn btn-sm btn-danger";
  confirmBtn.textContent = "Confirm";

  function close() {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  }

  function onKey(e) {
    if (e.key === "Escape") { close(); }
    if (e.key === "Enter" && box.contains(document.activeElement)) { close(); onConfirm(); }
  }

  cancelBtn.addEventListener("click", close);
  confirmBtn.addEventListener("click", function() { close(); onConfirm(); });
  overlay.addEventListener("click", function(e) { if (e.target === overlay) close(); });
  document.addEventListener("keydown", onKey);

  actions.appendChild(cancelBtn);
  actions.appendChild(confirmBtn);
  box.appendChild(msgEl);
  box.appendChild(actions);
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  confirmBtn.focus();
}


//  Token modal (shown after API key create / rotate)

function showTokenModal(label, tokenVal) {
  var overlay = document.createElement("div");
  overlay.style.cssText =
    "position:fixed;inset:0;background:rgba(0,0,0,.65);display:flex;align-items:center;" +
    "justify-content:center;z-index:9999;";

  var box = document.createElement("div");
  box.style.cssText =
    "background:#1e1e2c;border:1px solid #2d2d4a;border-radius:10px;" +
    "padding:1.4rem 1.6rem;width:min(520px,94vw);display:flex;flex-direction:column;gap:0.8rem;";

  var titleEl = document.createElement("div");
  titleEl.style.cssText = "font-weight:600;font-size:0.95rem;color:#c8ccd8;";
  titleEl.textContent = label;

  var warnEl = document.createElement("p");
  warnEl.style.cssText = "margin:0;font-size:0.8rem;color:#f87171;";
  warnEl.textContent = "Copy it now. This key will not be shown again.";

  var row = document.createElement("div");
  row.style.cssText = "display:flex;gap:0.5rem;";

  var input = document.createElement("input");
  input.type = "text";
  input.value = tokenVal;
  input.readOnly = true;
  input.style.cssText =
    "flex:1;background:#16161f;border:1px solid #2d2d4a;border-radius:6px;" +
    "color:#86efac;padding:0.4rem 0.6rem;font-size:0.82rem;font-family:monospace;outline:none;";

  var copyBtn = document.createElement("button");
  copyBtn.className = "btn btn-primary btn-sm";
  copyBtn.textContent = "Copy";
  copyBtn.addEventListener("click", function() {
    (navigator.clipboard ? navigator.clipboard.writeText(tokenVal) : Promise.reject())
      .then(function() {
        copyBtn.textContent = "Copied!";
        setTimeout(function() { copyBtn.textContent = "Copy"; }, 1800);
      })
      .catch(function() {
        try {
          input.select();
          var ok = document.execCommand("copy");
          copyBtn.textContent = ok ? "Copied!" : "Copy failed";
          setTimeout(function() { copyBtn.textContent = "Copy"; }, 1800);
        } catch (_) {
          copyBtn.textContent = "Copy failed";
          setTimeout(function() { copyBtn.textContent = "Copy"; }, 1800);
        }
      });
  });

  var actions = document.createElement("div");
  actions.style.cssText = "display:flex;justify-content:flex-end;";

  var doneBtn = document.createElement("button");
  doneBtn.className = "btn btn-sm";
  doneBtn.textContent = "Done";

  function close() {
    overlay.remove();
    document.removeEventListener("keydown", onEsc);
  }
  function onEsc(e) { if (e.key === "Escape") close(); }

  doneBtn.addEventListener("click", close);
  overlay.addEventListener("click", function(e) { if (e.target === overlay) close(); });
  document.addEventListener("keydown", onEsc);

  row.appendChild(input);
  row.appendChild(copyBtn);
  actions.appendChild(doneBtn);
  box.appendChild(titleEl);
  box.appendChild(warnEl);
  box.appendChild(row);
  box.appendChild(actions);
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  setTimeout(function() { input.select(); }, 50);
}


//  Warning banner: dismiss + persistence

function initWarningBanner() {
  var banner = document.getElementById("warning-banner");
  if (!banner) return;
  var dismissBtn = banner.querySelector(".warning-banner-dismiss");
  if (!dismissBtn) return;
  // Check if user already dismissed this session
  var dismissed = sessionStorage.getItem("warning_banner_dismissed");
  if (dismissed) { banner.style.display = "none"; return; }
  dismissBtn.addEventListener("click", function() {
    banner.style.display = "none";
    sessionStorage.setItem("warning_banner_dismissed", "1");
  });
}


//  Flash message: dismiss + token modal

function initFlashMessages() {
  // Dismiss buttons
  document.querySelectorAll(".flash-dismiss").forEach(function(btn) {
    btn.addEventListener("click", function() {
      var flash = btn.closest(".flash");
      if (flash) flash.remove();
    });
  });

  // Token flash: show as a modal overlay instead of inline
  document.querySelectorAll(".flash-token").forEach(function(el) {
    var msgEl = el.querySelector(".flash-msg");
    if (!msgEl) return;
    var rawText = msgEl.textContent.trim();
    var colonIdx = rawText.indexOf(": ");
    var label = colonIdx !== -1 ? rawText.slice(0, colonIdx).trim() : "API Token";
    var tokenVal = colonIdx !== -1 ? rawText.slice(colonIdx + 2).trim() : rawText;
    el.style.display = "none";
    showTokenModal(label, tokenVal);
  });
}
