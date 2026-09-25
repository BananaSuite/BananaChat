"use strict";


//  Parameters panel (inference overrides)

function initParamsPanel() {
  var panel     = document.getElementById("chat-params-panel");
  var toggleBtn = document.getElementById("params-toggle-btn");
  var resetBtn  = document.getElementById("params-reset-btn");
  if (!panel || !toggleBtn) return;

  var PARAMS = [
    { name: "temperature",    cast: parseFloat, fmt: function(v) { return v.toFixed(2); } },
    { name: "top_p",          cast: parseFloat, fmt: function(v) { return v.toFixed(2); } },
    { name: "top_k",          cast: function(v) { return parseInt(v, 10); }, fmt: String },
    { name: "num_ctx",        cast: function(v) { return parseInt(v, 10); }, fmt: String },
  ];

  PARAMS.forEach(function(p) {
    var slider = document.getElementById("pslider-" + p.name);
    var badge  = document.getElementById("pbadge-" + p.name);
    if (!slider || !badge) return;
    slider.addEventListener("input", function() {
      var v = p.cast(slider.value);
      _chatParams[p.name] = v;
      badge.textContent = p.fmt(v);
      badge.classList.add("active");
      slider.classList.remove("inactive");
    });
  });

  function resetAll() {
    PARAMS.forEach(function(p) {
      delete _chatParams[p.name];
      var slider = document.getElementById("pslider-" + p.name);
      var badge  = document.getElementById("pbadge-" + p.name);
      if (slider) slider.classList.add("inactive");
      if (badge)  { badge.textContent = "auto"; badge.classList.remove("active"); }
    });
  }

  if (resetBtn) resetBtn.addEventListener("click", resetAll);

  toggleBtn.addEventListener("click", function() {
    var nowOpen = panel.hidden;
    panel.hidden = !nowOpen;
    toggleBtn.setAttribute("aria-pressed", nowOpen ? "true" : "false");
  });
}


//  Chat: new chat model picker (index page)

function initNewChatForm() {
  var incognitoBtn = document.getElementById("incognito-toggle");
  var incognitoInput = document.getElementById("incognito-input");
  var form = document.getElementById("new-chat-form");
  if (!incognitoBtn || !incognitoInput) return;

  incognitoBtn.addEventListener("click", function() {
    var current = incognitoBtn.getAttribute("aria-pressed") === "true";
    var next = !current;
    incognitoInput.value = next ? "1" : "0";
    if (form) form.submit();
  });
}


//  Sidebar: custom model selector (mounts to #sidebar-model-picker-mount)

function initSidebarModelPicker() {
  var mount = document.getElementById("sidebar-model-picker-mount");
  if (!mount) return;

  var models = (window.BC_MODELS && window.BC_MODELS.length)
    ? window.BC_MODELS
    : (function() {
        var sel = document.getElementById("sidebar-model-select");
        return sel ? Array.from(sel.options).map(function(o) {
          return { value: o.value, label: o.textContent.trim() };
        }) : [];
      })();

  var init = _getInitialModel();

  var wrapper = document.createElement("div");
  wrapper.className = "sidebar-model-picker";

  var btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sidebar-model-picker-btn";
  btn.innerHTML =
    '<span class="sidebar-model-picker-label">' + escapeHtml(init.label) + '</span>' +
    '<svg class="model-picker-chevron" width="10" height="6" viewBox="0 0 10 6" fill="none">' +
    '<path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var dropdown = document.createElement("div");
  dropdown.className = "sidebar-model-picker-dropdown";

  models.forEach(function(m) {
    var item = document.createElement("div");
    item.className = "sidebar-model-picker-item" +
      (m.value === "auto" ? " auto" : "") +
      (m.value === init.value ? " selected" : "");
    item.dataset.value = m.value;
    var nameHtml = '<span class="sidebar-picker-name">' + escapeHtml(m.label) + '</span>';
    if (m.is_reasoning) nameHtml += ' <span class="mpcat mpcat-reasoning">reasoning</span>';
    if (m.cats && m.cats.length) {
      nameHtml += '<div class="picker-item-cats">' +
        m.cats.map(function(c) { return '<span class="mpcat">' + escapeHtml(c) + '</span>'; }).join("") +
        '</div>';
    }
    item.innerHTML = nameHtml;
    item.addEventListener("click", function() { setActiveModel(m.value, m.label); closeSidePicker(); });
    dropdown.appendChild(item);
  });

  function openSidePicker() {
    btn.classList.add("open");
    dropdown.classList.add("open");
    var ctxMenu = document.getElementById("session-ctx-menu");
    if (ctxMenu) ctxMenu.classList.remove("open");
    var sel = dropdown.querySelector(".sidebar-model-picker-item.selected");
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }
  function closeSidePicker() {
    btn.classList.remove("open");
    dropdown.classList.remove("open");
  }

  btn.addEventListener("click", function(e) {
    e.stopPropagation();
    dropdown.classList.contains("open") ? closeSidePicker() : openSidePicker();
  });
  document.addEventListener("click", function() { closeSidePicker(); });
  dropdown.addEventListener("click", function(e) { e.stopPropagation(); });

  wrapper.appendChild(btn);
  wrapper.appendChild(dropdown);
  mount.appendChild(wrapper);

  _bcPickerLabelUpdaters.push(function(value, label) {
    var labelEl = btn.querySelector(".sidebar-model-picker-label");
    if (labelEl) labelEl.textContent = label;
    dropdown.querySelectorAll(".sidebar-model-picker-item").forEach(function(el) {
      el.classList.toggle("selected", el.dataset.value === value);
    });
  });
}


//  Token management: rename inline

function initTokenRename() {
  document.querySelectorAll(".token-rename-form").forEach(function(form) {
    var nameEl  = form.closest("tr").querySelector(".token-name");
    var editBtn = form.querySelector(".token-rename-btn");
    var input   = form.querySelector(".token-rename-input");
    if (!editBtn || !input || !nameEl) return;

    input.addEventListener("keydown", function(e) {
      if (e.key === "Enter") { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.submit(); }
      if (e.key === "Escape") { input.style.display = "none"; editBtn.textContent = "Rename"; }
    });

    editBtn.addEventListener("click", function(e) {
      e.preventDefault();
      if (input.style.display === "none" || input.style.display === "") {
        input.style.display = "inline-block";
        input.value = nameEl.textContent.trim() === "(unnamed)" ? "" : nameEl.textContent.trim();
        input.focus();
        editBtn.textContent = "Save";
      } else {
        form.requestSubmit ? form.requestSubmit() : form.submit();
      }
    });
  });
}


//  Sidebar: per-chat context menu (rename / share / delete)

function showShareLink(url) {
  var existing = document.getElementById("bc-share-overlay");
  if (existing) existing.remove();

  var overlay = document.createElement("div");
  overlay.id = "bc-share-overlay";
  overlay.style.cssText =
    "position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;" +
    "justify-content:center;z-index:9999;";

  var box = document.createElement("div");
  box.style.cssText =
    "background:#1e1e2c;border:1px solid #2d2d4a;border-radius:10px;padding:1.4rem 1.6rem;" +
    "width:min(480px,92vw);display:flex;flex-direction:column;gap:0.75rem;";

  var title = document.createElement("div");
  title.style.cssText = "font-weight:600;font-size:0.95rem;color:#c8ccd8;";
  title.textContent = "Share link";

  var row = document.createElement("div");
  row.style.cssText = "display:flex;gap:0.5rem;";

  var input = document.createElement("input");
  input.type = "text";
  input.value = url;
  input.readOnly = true;
  input.style.cssText =
    "flex:1;background:#16161f;border:1px solid #2d2d4a;border-radius:6px;" +
    "color:#c8ccd8;padding:0.4rem 0.6rem;font-size:0.82rem;font-family:monospace;outline:none;";

  var copyBtn = document.createElement("button");
  copyBtn.className = "btn btn-primary btn-sm";
  copyBtn.textContent = "Copy";
  copyBtn.addEventListener("click", function() {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(url).then(function() {
        copyBtn.textContent = "Copied!";
        setTimeout(function() { copyBtn.textContent = "Copy"; }, 1800);
      }).catch(function() { fallbackCopy(); });
    } else {
      fallbackCopy();
    }
    function fallbackCopy() {
      input.select();
      document.execCommand("copy");
      copyBtn.textContent = "Copied!";
      setTimeout(function() { copyBtn.textContent = "Copy"; }, 1800);
    }
  });

  var closeBtn = document.createElement("button");
  closeBtn.className = "btn btn-sm";
  closeBtn.textContent = "Close";
  closeBtn.addEventListener("click", function() { overlay.remove(); });

  var actions = document.createElement("div");
  actions.style.cssText = "display:flex;justify-content:flex-end;gap:0.4rem;";
  actions.appendChild(closeBtn);

  row.appendChild(input);
  row.appendChild(copyBtn);
  box.appendChild(title);
  box.appendChild(row);
  box.appendChild(actions);
  overlay.appendChild(box);
  document.body.appendChild(overlay);

  overlay.addEventListener("click", function(e) { if (e.target === overlay) overlay.remove(); });
  document.addEventListener("keydown", function onEsc(e) {
    if (e.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", onEsc); }
  });

  // Auto-select the URL text
  setTimeout(function() { input.select(); }, 50);
}

function initSidebarContextMenus() {
  var menu = document.createElement("div");
  menu.id = "session-ctx-menu";
  menu.className = "session-ctx-menu";
  menu.innerHTML =
    '<button class="session-ctx-item" data-action="rename">Rename</button>' +
    '<button class="session-ctx-item" data-action="share">Share</button>' +
    '<button class="session-ctx-item danger" data-action="delete">Delete</button>';
  document.body.appendChild(menu);

  var activeSid = null;
  var activeItem = null;

  function closeMenu() {
    menu.classList.remove("open");
    activeSid = null;
    activeItem = null;
  }

  function openMenu(btn, item) {
    var sid = item.dataset.sessionId;
    if (!sid) return;
    if (activeSid === sid) { closeMenu(); return; }
    activeSid = sid;
    activeItem = item;
    // Close sidebar model picker if open
    var smpDropdown = document.querySelector(".sidebar-model-picker-dropdown.open");
    if (smpDropdown) {
      smpDropdown.classList.remove("open");
      var smpBtn = document.querySelector(".sidebar-model-picker-btn.open");
      if (smpBtn) smpBtn.classList.remove("open");
    }
    var shareMenuItem = menu.querySelector('[data-action="share"]');
    if (item.dataset.incognito) {
      shareMenuItem.style.display = "none";
    } else {
      shareMenuItem.style.display = "";
      shareMenuItem.textContent = item.dataset.sharedToken ? "Revoke Link" : "Share";
    }
    var rect = btn.getBoundingClientRect();
    var menuW = 140;
    var left = rect.right - menuW;
    if (left < 4) left = 4;
    menu.style.top = (rect.bottom + 4) + "px";
    menu.style.left = left + "px";
    menu.classList.add("open");
  }

  document.addEventListener("keydown", function(e) {
    if ((e.key === "Enter" || e.key === " ") && e.target.classList.contains("session-menu-btn")) {
      e.preventDefault();
      e.stopPropagation();
      var item = e.target.closest(".session-item");
      if (item) openMenu(e.target, item);
    }
    if (e.key === "Escape") closeMenu();
  });

  document.addEventListener("click", function(e) {
    var btn = e.target.closest(".session-menu-btn");
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      var item = btn.closest(".session-item");
      if (item) openMenu(btn, item);
      return;
    }
    if (!menu.contains(e.target)) closeMenu();
  });

  menu.querySelectorAll(".session-ctx-item").forEach(function(btn) {
    btn.addEventListener("click", function(e) {
      e.stopPropagation();
      var action = btn.dataset.action;
      var sid = activeSid;
      var item = activeItem;
      closeMenu();
      if (!sid || !item) return;

      if (action === "rename") {
        var currentTitle = item.dataset.title || "New Chat";
        var titleSpan = item.querySelector(".session-title");
        if (!titleSpan) return;
        var renameInput = document.createElement("input");
        renameInput.type = "text";
        renameInput.value = currentTitle;
        renameInput.style.cssText =
          "width:100%;background:#1a1a24;border:1px solid #7e9ada;border-radius:4px;" +
          "color:#c8ccd8;padding:0.15rem 0.4rem;font-size:0.82rem;font-family:inherit;outline:none;";
        titleSpan.replaceWith(renameInput);
        renameInput.focus();
        renameInput.select();
        var renameSaved = false;
        function saveRename() {
          if (renameSaved) return;
          renameSaved = true;
          var newTitle = renameInput.value.trim() || currentTitle;
          var newSpan = document.createElement("span");
          newSpan.className = "session-title";
          newSpan.textContent = newTitle;
          renameInput.replaceWith(newSpan);
          item.dataset.title = newTitle;
          fetch("/chat/" + sid + "/title", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify({ title: newTitle }),
          }).then(function(r) { return r.json(); }).then(function(data) {
            if (!data.ok) { newSpan.textContent = currentTitle; item.dataset.title = currentTitle; }
            else if (item.classList.contains("active")) {
              var topbarTitle = document.getElementById("chat-title");
              if (topbarTitle) {
                topbarTitle.textContent = newTitle;
                document.title = newTitle + "-- " + document.title.split(" --").slice(-1)[0];
              }
            }
          });
        }
        renameInput.addEventListener("blur", saveRename);
        renameInput.addEventListener("keydown", function(e) {
          if (e.key === "Enter") { e.preventDefault(); saveRename(); }
          if (e.key === "Escape") { renameInput.value = currentTitle; saveRename(); }
        });

      } else if (action === "share") {
        var shareAction = item.dataset.sharedToken ? "revoke" : "create";
        var doSidebarShare = function(sid, item, shareAction) {
          fetch("/chat/" + sid + "/share", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify({ action: shareAction }),
          }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.error) { showToast("Error: " + data.error); return; }
            var isActive = item.classList.contains("active");
            var topbarShareBtn = isActive ? document.getElementById("chat-share-btn") : null;
            var topbarShareStatus = isActive ? document.getElementById("chat-share-status") : null;
            if (shareAction === "create" && data.token) {
              item.dataset.sharedToken = data.token;
              var url = window.location.origin + "/share/" + data.token;
              if (topbarShareBtn) {
                var svgHtml = topbarShareBtn.querySelector("svg") ? topbarShareBtn.querySelector("svg").outerHTML : "";
                topbarShareBtn.innerHTML = svgHtml + " Revoke";
                topbarShareBtn.setAttribute("data-action", "revoke");
                topbarShareBtn.title = "Revoke share link";
              }
              if (topbarShareStatus) {
                topbarShareStatus.innerHTML = '<a href="' + escapeHtml(url) + '" target="_blank">↗ View</a>';
              }
              showShareLink(url);
            } else {
              item.dataset.sharedToken = "";
              showToast("Share link revoked");
              if (topbarShareBtn) {
                var svgHtml = topbarShareBtn.querySelector("svg") ? topbarShareBtn.querySelector("svg").outerHTML : "";
                topbarShareBtn.innerHTML = svgHtml + " Share";
                topbarShareBtn.setAttribute("data-action", "create");
                topbarShareBtn.title = "Share this chat";
              }
              if (topbarShareStatus) topbarShareStatus.innerHTML = "";
            }
          }).catch(function() { showToast("Share request failed"); });
        };
        if (shareAction === "revoke") {
          showConfirm("Revoke the share link? Anyone with the link will lose access.", function() {
            doSidebarShare(sid, item, shareAction);
          });
        } else {
          doSidebarShare(sid, item, shareAction);
        }

      } else if (action === "delete") {
        showConfirm("Delete this chat? This cannot be undone.", function() {
          fetch("/chat/" + sid + "/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: "{}",
          }).then(function(r) { return r.json(); }).then(function(data) {
            if (!data.ok) return;
            if (item.classList.contains("active")) {
              window.location.href = "/chat";
            } else {
              item.remove();
            }
          });
        });
      }
    });
  });

  document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") closeMenu();
  });
}


//  Incognito: close session when user navigates away

function initIncognitoClose() {
  if (!window.BC_IS_INCOGNITO) return;
  var sessionIdEl = document.getElementById("chat-session-id");
  if (!sessionIdEl) return;
  var sid = sessionIdEl.value;
  window.addEventListener("pagehide", function() {
    fetch("/chat/" + sid + "/incognito-close", {
      method: "POST",
      keepalive: true,
      headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/json" },
      body: "{}",
    });
  });
}


//  Nav: hamburger toggle (mobile)

function initNavHamburger() {
  var btn  = document.getElementById("nav-hamburger");
  var menu = document.getElementById("nav-mobile-menu");
  if (!btn || !menu) return;

  function open() {
    menu.classList.add("open");
    btn.setAttribute("aria-expanded", "true");
  }
  function close() {
    menu.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
  }

  btn.addEventListener("click", function(e) {
    e.stopPropagation();
    menu.classList.contains("open") ? close() : open();
  });
  document.addEventListener("click", function() { close(); });
  menu.addEventListener("click", function(e) {
    if (e.target.tagName === "A") close();
    e.stopPropagation();
  });
  document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") close();
  });
}


//  Chat: sidebar drawer toggle (mobile)

function initSidebarDrawer() {
  var toggle   = document.getElementById("sidebar-toggle-btn");
  var sidebar  = document.querySelector(".chat-sidebar");
  var backdrop = document.getElementById("sidebar-backdrop");
  if (!toggle || !sidebar) return;

  function openDrawer() {
    sidebar.classList.add("open");
    if (backdrop) backdrop.classList.add("open");
    toggle.setAttribute("aria-expanded", "true");
  }
  function closeDrawer() {
    sidebar.classList.remove("open");
    if (backdrop) backdrop.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  }

  toggle.addEventListener("click", function() {
    sidebar.classList.contains("open") ? closeDrawer() : openDrawer();
  });
  if (backdrop) backdrop.addEventListener("click", closeDrawer);

  // Close when navigating to a session on mobile
  sidebar.querySelectorAll(".session-item").forEach(function(item) {
    item.addEventListener("click", function() {
      if (window.innerWidth <= 768) closeDrawer();
    });
  });

  document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") closeDrawer();
  });
}
