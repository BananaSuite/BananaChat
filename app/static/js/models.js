"use strict";


//  Model picker: shared state + core + initializers

var MODEL_PREF_KEY = "bc_model_pref";

// Registered callbacks: fn(value, label): called by setActiveModel
var _bcPickerLabelUpdaters = [];
// Active inference param overrides sent with each message
var _chatParams = {};

function _getInitialModel() {
  var saved = localStorage.getItem(MODEL_PREF_KEY) || "auto";
  var models = window.BC_MODELS || [];
  var found = null;
  for (var _i = 0; _i < models.length; _i++) {
    if (models[_i].value === saved) { found = models[_i]; break; }
  }
  if (!found) found = models[0];
  return found ? { value: found.value, label: found.label } : { value: "auto", label: "Auto" };
}

function setActiveModel(value, label) {
  var hidden = document.getElementById("model-select-chat");
  if (hidden) hidden.value = value;
  var nativeSel = document.getElementById("sidebar-model-select");
  if (nativeSel) nativeSel.value = value;
  localStorage.setItem(MODEL_PREF_KEY, value);
  _bcPickerLabelUpdaters.forEach(function(fn) { fn(value, label); });
}

// ── Hero picker (empty-state compact dropdown)
function initHeroModelPicker() {
  var mount = document.getElementById("hero-model-picker-mount");
  if (!mount) return;
  var models = window.BC_MODELS || [];
  var init = _getInitialModel();

  var btn = document.createElement("button");
  btn.type = "button";
  btn.className = "hero-model-btn";
  btn.innerHTML =
    '<span class="hm-prefix">Model:</span>' +
    '<span class="hm-label">' + escapeHtml(init.label) + '</span>' +
    '<svg width="10" height="6" viewBox="0 0 10 6" fill="none">' +
    '<path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var dropdown = document.createElement("div");
  dropdown.className = "hero-model-dropdown";

  models.forEach(function(m) {
    var item = document.createElement("div");
    item.className = "hero-model-item" +
      (m.value === "auto" ? " auto-item" : "") +
      (m.value === init.value ? " selected" : "");
    item.dataset.value = m.value;

    var reasoningBadge = m.is_reasoning
      ? ' <span class="mpcat mpcat-reasoning">reasoning</span>' : "";
    var catsHtml = (m.cats && m.cats.length)
      ? '<div class="picker-item-cats">' +
          m.cats.map(function(c) { return '<span class="mpcat">' + escapeHtml(c) + '</span>'; }).join("") +
        '</div>'
      : "";

    item.innerHTML =
      '<div class="hero-model-item-name">' + escapeHtml(m.label) + reasoningBadge + '</div>' +
      (m.desc ? '<div class="hero-model-item-desc">' + escapeHtml(m.desc) + '</div>' : '') +
      catsHtml;

    item.addEventListener("click", function() {
      setActiveModel(m.value, m.label);
      closeHeroPicker();
      var ci = document.getElementById("chat-input");
      if (ci) ci.focus();
    });
    dropdown.appendChild(item);
  });

  function openHeroPicker() {
    btn.classList.add("open");
    dropdown.classList.add("open");
    var sel = dropdown.querySelector(".hero-model-item.selected");
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }
  function closeHeroPicker() {
    btn.classList.remove("open");
    dropdown.classList.remove("open");
  }

  btn.addEventListener("click", function(e) {
    e.stopPropagation();
    dropdown.classList.contains("open") ? closeHeroPicker() : openHeroPicker();
  });
  document.addEventListener("click", function() { closeHeroPicker(); });
  dropdown.addEventListener("click", function(e) { e.stopPropagation(); });

  mount.appendChild(btn);
  mount.appendChild(dropdown);

  _bcPickerLabelUpdaters.push(function(value, label) {
    var labelEl = btn.querySelector(".hm-label");
    if (labelEl) labelEl.textContent = label;
    dropdown.querySelectorAll(".hero-model-item").forEach(function(el) {
      el.classList.toggle("selected", el.dataset.value === value);
    });
  });
}

// ── Input toolbar model picker (compact button, upward dropdown)
function initInputModelPicker() {
  var mount = document.getElementById("input-model-picker-mount");
  if (!mount) return;
  var models = window.BC_MODELS || [];
  var init = _getInitialModel();

  var btn = document.createElement("button");
  btn.type = "button";
  btn.className = "input-model-btn";
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  btn.setAttribute("aria-label", tr("model", "Model") + ": " + init.label);
  btn.innerHTML =
    '<span class="model-status-dot" aria-hidden="true"></span>' +
    '<span class="input-model-prefix">' + escapeHtml(tr("model", "Model")) + '</span>' +
    '<span class="input-model-label">' + escapeHtml(init.label) + '</span>' +
    '<svg class="model-picker-chevron" width="9" height="5" viewBox="0 0 10 6" fill="none" style="flex-shrink:0">' +
    '<path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var dropdown = document.createElement("div");
  dropdown.className = "input-model-dropdown";
  dropdown.setAttribute("role", "listbox");

  var header = document.createElement("div");
  header.className = "model-picker-header";
  header.innerHTML = '<strong>' + escapeHtml(tr("models", "Models")) + '</strong>' +
    '<span>' + models.length + '</span>';
  var searchWrap = document.createElement("label");
  searchWrap.className = "model-picker-search";
  searchWrap.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
  var search = document.createElement("input");
  search.type = "search";
  search.placeholder = tr("model_search", "Search models…");
  search.setAttribute("aria-label", search.placeholder);
  searchWrap.appendChild(search);
  var list = document.createElement("div");
  list.className = "model-picker-list";
  var empty = document.createElement("p");
  empty.className = "model-picker-empty";
  empty.textContent = tr("no_models", "No matching models");
  empty.hidden = true;
  dropdown.appendChild(header);
  dropdown.appendChild(searchWrap);
  dropdown.appendChild(list);
  dropdown.appendChild(empty);

  models.forEach(function(m) {
    var item = document.createElement("div");
    item.className = "input-model-item" +
      (m.value === "auto" ? " auto-item" : "") +
      (m.value === init.value ? " selected" : "");
    item.dataset.value = m.value;
    item.dataset.search = (m.label + " " + (m.desc || "") + " " + ((m.cats || []).join(" "))).toLowerCase();
    item.setAttribute("role", "option");
    item.setAttribute("tabindex", "0");
    item.setAttribute("aria-selected", m.value === init.value ? "true" : "false");

    var catsHtml = (m.cats && m.cats.length)
      ? '<div class="picker-item-cats">' +
          m.cats.map(function(c) { return '<span class="mpcat">' + escapeHtml(c) + '</span>'; }).join("") +
        '</div>'
      : "";
    var reasoningBadge = m.is_reasoning
      ? ' <span class="mpcat mpcat-reasoning">' + escapeHtml(tr("reasoning", "reasoning")) + '</span>' : "";

    item.innerHTML =
      '<div class="input-model-item-main"><div class="input-model-item-name">' + escapeHtml(m.label) + reasoningBadge + '</div>' +
      (m.desc ? '<div class="input-model-item-desc">' + escapeHtml(m.desc) + '</div>' : '') +
      catsHtml + '</div><span class="model-check" aria-hidden="true">✓</span>';

    item.addEventListener("click", function() { setActiveModel(m.value, m.label); closeInputPicker(); });
    item.addEventListener("keydown", function(e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); item.click(); } });
    list.appendChild(item);
  });

  search.addEventListener("input", function() {
    var query = search.value.trim().toLowerCase();
    var visible = 0;
    list.querySelectorAll(".input-model-item").forEach(function(item) {
      var show = !query || item.dataset.search.indexOf(query) !== -1;
      item.hidden = !show;
      if (show) visible += 1;
    });
    empty.hidden = visible !== 0;
  });

  function openInputPicker() {
    btn.classList.add("open");
    dropdown.classList.add("open");
    btn.setAttribute("aria-expanded", "true");
    var sel = dropdown.querySelector(".input-model-item.selected");
    if (sel) sel.scrollIntoView({ block: "nearest" });
    window.setTimeout(function() { search.focus(); }, 0);
  }
  function closeInputPicker() {
    btn.classList.remove("open");
    dropdown.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
    search.value = "";
    search.dispatchEvent(new Event("input"));
  }

  btn.addEventListener("click", function(e) {
    e.stopPropagation();
    dropdown.classList.contains("open") ? closeInputPicker() : openInputPicker();
  });
  document.addEventListener("click", function() { closeInputPicker(); });
  dropdown.addEventListener("click", function(e) { e.stopPropagation(); });

  mount.appendChild(btn);
  mount.appendChild(dropdown);

  _bcPickerLabelUpdaters.push(function(value, label) {
    var labelEl = btn.querySelector(".input-model-label");
    if (labelEl) labelEl.textContent = label;
    btn.setAttribute("aria-label", tr("model", "Model") + ": " + label);
    dropdown.querySelectorAll(".input-model-item").forEach(function(el) {
      el.classList.toggle("selected", el.dataset.value === value);
      el.setAttribute("aria-selected", el.dataset.value === value ? "true" : "false");
    });
    // Update empty state model indicator
    var emptyLbl = document.getElementById("empty-model-label");
    if (emptyLbl) emptyLbl.textContent = label;
  });
}

/**
 * Wire up a custom model-picker dropdown.
 *
 * cfg fields:
 *   btn: toggle button element
 *   dropdown: dropdown container element
 *   labelEl: element whose textContent shows the selected name (optional)
 *   hidden: object/element with a writable .value property
 *   models: [{value, label, desc?}]
 *   prefKey: localStorage persistence key (default MODEL_PREF_KEY)
 *   itemBaseClass: CSS base class for items (default "model-picker-item")
 *   itemExtraCls: fn(model) -> extra class string (optional)
 *   itemHtml: fn(model) -> innerHTML string (optional; otherwise name+desc layout)
 */
function _buildCustomPicker(cfg) {
  var btn       = cfg.btn;
  var dropdown  = cfg.dropdown;
  var labelEl   = cfg.labelEl || null;
  var hidden    = cfg.hidden;
  var models    = cfg.models || [];
  var prefKey   = cfg.prefKey || MODEL_PREF_KEY;
  var baseCls   = cfg.itemBaseClass || "model-picker-item";

  dropdown.innerHTML = "";
  models.forEach(function(m) {
    var extra = cfg.itemExtraCls ? cfg.itemExtraCls(m) : (m.value === "auto" ? " auto-item" : "");
    var item = document.createElement("div");
    item.className = baseCls + extra;
    item.dataset.value = m.value;
    var catsHtml = (m.cats && m.cats.length)
      ? '<div class="picker-item-cats">' +
          m.cats.map(function(c) { return '<span class="mpcat">' + escapeHtml(c) + '</span>'; }).join("") +
        '</div>'
      : "";
    var reasoningBadge = m.is_reasoning
      ? '<span class="mpcat mpcat-reasoning">reasoning</span>'
      : "";
    item.innerHTML = cfg.itemHtml
      ? cfg.itemHtml(m)
      : '<div class="' + baseCls + '-name">' + escapeHtml(m.label) + reasoningBadge + '</div>' +
        (m.desc ? '<div class="' + baseCls + '-desc">' + escapeHtml(m.desc) + '</div>' : '') +
        catsHtml;
    item.addEventListener("click", function() { selectModel(m.value, m.label); close(); });
    dropdown.appendChild(item);
  });

  var saved = localStorage.getItem(prefKey) || "auto";
  var found  = models.find(function(m) { return m.value === saved; });
  if (!found) { found = models[0]; saved = found ? found.value : "auto"; }
  selectModel(saved, found ? found.label : "Auto");

  function selectModel(value, label) {
    hidden.value = value;
    if (labelEl) labelEl.textContent = label;
    localStorage.setItem(prefKey, value);
    dropdown.querySelectorAll("." + baseCls).forEach(function(el) {
      el.classList.toggle("selected", el.dataset.value === value);
    });
  }

  function open() {
    btn.classList.add("open");
    dropdown.classList.add("open");
    var sel = dropdown.querySelector("." + baseCls + ".selected");
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }

  function close() {
    btn.classList.remove("open");
    dropdown.classList.remove("open");
  }

  btn.addEventListener("click", function(e) {
    e.stopPropagation();
    dropdown.classList.contains("open") ? close() : open();
  });
  document.addEventListener("click", function() { close(); });
  dropdown.addEventListener("click", function(e) { e.stopPropagation(); });

  return { selectModel: selectModel, open: open, close: close };
}

function initModelPicker() {
  var btn      = document.getElementById("model-picker-btn");
  var dropdown = document.getElementById("model-picker-dropdown");
  var labelEl  = document.getElementById("model-picker-label");
  var hidden   = document.getElementById("model-select-chat");
  if (!btn || !dropdown || !hidden) return;
  _buildCustomPicker({ btn: btn, dropdown: dropdown, labelEl: labelEl, hidden: hidden,
    models: window.BC_MODELS || [] });
}


//  Chat: download button

function initChatDownload() {
  var btn = document.getElementById("chat-download-btn");
  var sessionId = document.getElementById("chat-session-id");
  if (!btn || !sessionId) return;
  btn.addEventListener("click", function() {
    window.location.href = "/chat/" + sessionId.value + "/download";
  });
}


//  Sidebar: search (AJAX for message content, local for title-only)

function initSidebarSearch() {
  var input = document.getElementById("sidebar-search");
  var list  = document.getElementById("sidebar-chat-list");
  if (!input || !list) return;

  var allItems = Array.from(list.querySelectorAll(".session-item"));
  var _timer = null;
  var _lastQ = "";

  function getOrCreateNoResults() {
    var el = list.querySelector(".sidebar-no-results");
    if (!el) {
      el = document.createElement("div");
      el.className = "sidebar-empty sidebar-no-results";
      el.textContent = "No matching chats";
      list.appendChild(el);
    }
    return el;
  }

  function showAll() {
    allItems.forEach(function(item) { item.style.display = ""; });
    var no = list.querySelector(".sidebar-no-results");
    if (no) no.remove();
  }

  function applyResults(visibleIds) {
    var any = false;
    allItems.forEach(function(item) {
      var vis = visibleIds === null || !!visibleIds[item.dataset.sessionId];
      item.style.display = vis ? "" : "none";
      if (vis) any = true;
    });
    if (!any) getOrCreateNoResults();
    else { var no = list.querySelector(".sidebar-no-results"); if (no) no.remove(); }
  }

  function localFilter(query) {
    var q = query.toLowerCase();
    var map = null;
    // local title filter (no server needed)
    var any = false;
    allItems.forEach(function(item) {
      var title = (item.querySelector(".session-title") || item).textContent.toLowerCase();
      if (title.includes(q)) any = true;
      item.style.display = title.includes(q) ? "" : "none";
    });
    if (!any) getOrCreateNoResults();
    else { var no = list.querySelector(".sidebar-no-results"); if (no) no.remove(); }
  }

  function ajaxSearch(query) {
    fetch("/chat/search?q=" + encodeURIComponent(query))
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!data.results) return;
        var ids = {};
        data.results.forEach(function(r) { ids[r.id] = true; });
        applyResults(ids);
      })
      .catch(function() { localFilter(query); });
  }

  input.addEventListener("input", function() {
    var query = input.value.trim();
    if (_timer) clearTimeout(_timer);
    if (!query) { showAll(); _lastQ = ""; return; }
    _lastQ = query;
    if (query.length < 2) { localFilter(query); return; }
    _timer = setTimeout(function() {
      if (_lastQ === query) ajaxSearch(query);
    }, 280);
  });
}
