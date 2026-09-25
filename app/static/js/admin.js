"use strict";


//  API Playground: SSE streaming

function initPlayground() {
  var sendBtn = document.getElementById("pg-send-btn");
  var responseEl = document.getElementById("pg-response");
  var modelSelect = document.getElementById("pg-model-select");
  var systemInput = document.getElementById("pg-system");
  var addUserBtn  = document.getElementById("pg-add-user");
  var addAsstBtn  = document.getElementById("pg-add-asst");
  var msgList     = document.getElementById("pg-messages");
  if (!sendBtn || !responseEl || !msgList) return;

  var streaming = false;

  function addMessage(role, content) {
    var idx = msgList.children.length;
    var div = document.createElement("div");
    div.className = "playground-msg";
    div.dataset.role = role;
    div.innerHTML =
      '<span class="playground-msg-role ' + role + '">' + escapeHtml(role) + '</span>' +
      '<textarea rows="2" placeholder="' + (role === "user" ? "User message" : "Assistant message") + '">' +
      escapeHtml(content || "") + '</textarea>' +
      '<button type="button" class="playground-msg-remove" title="Remove">&times;</button>';
    msgList.appendChild(div);
    div.querySelector("textarea").focus();
    div.querySelector(".playground-msg-remove").addEventListener("click", function() { div.remove(); });
    return div;
  }

  if (addUserBtn) addUserBtn.addEventListener("click", function() { addMessage("user", ""); });
  if (addAsstBtn) addAsstBtn.addEventListener("click", function() { addMessage("assistant", ""); });

  // Remove handlers for pre-existing messages
  document.querySelectorAll(".playground-msg-remove").forEach(function(btn) {
    btn.addEventListener("click", function() { btn.closest(".playground-msg").remove(); });
  });

  var stopBtn = document.getElementById("pg-stop-btn");
  var abortController = null;

  if (stopBtn) {
    stopBtn.addEventListener("click", function() {
      if (abortController) {
        abortController.abort();
        stopBtn.disabled = true;
        stopBtn.textContent = "Stopping…";
      }
    });
  }

  sendBtn.addEventListener("click", function() {
    if (streaming) return;
    var model = modelSelect ? modelSelect.value : "";
    var messages = [];
    var sys = systemInput ? systemInput.value.trim() : "";
    if (sys) messages.push({ role: "system", content: sys });

    msgList.querySelectorAll(".playground-msg").forEach(function(el) {
      var role = el.dataset.role;
      var content = el.querySelector("textarea").value.trim();
      if (content) messages.push({ role: role, content: content });
    });

    if (!messages.length) { showToast("Add at least one message."); return; }

    // Collect inference parameters (empty field = use model default)
    var payload = { model: model, messages: messages };
    var tempEl   = document.getElementById("pg-temperature");
    var topPEl   = document.getElementById("pg-top-p");
    var topKEl   = document.getElementById("pg-top-k");
    var numCtxEl = document.getElementById("pg-num-ctx");
    if (tempEl   && tempEl.value   !== "") payload.temperature = parseFloat(tempEl.value);
    if (topPEl   && topPEl.value   !== "") payload.top_p       = parseFloat(topPEl.value);
    if (topKEl   && topKEl.value   !== "") payload.top_k       = parseInt(topKEl.value, 10);
    if (numCtxEl && numCtxEl.value !== "") payload.num_ctx     = parseInt(numCtxEl.value, 10);

    streaming = true;
    sendBtn.disabled = true;
    sendBtn.textContent = "Sending…";
    if (stopBtn) { stopBtn.style.display = ""; stopBtn.disabled = false; stopBtn.textContent = "Stop"; }
    responseEl.textContent = "";
    responseEl.classList.remove("empty");

    var accumulated = "";
    abortController = new AbortController();

    fetch("/api/playground/send", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify(payload),
      signal: abortController.signal,
    }).then(function(resp) {
      if (!resp.ok) {
        return resp.json().then(function(d) {
          responseEl.textContent = "Error: " + (d.error || resp.statusText);
          done();
        });
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = "";

      function read() {
        reader.read().then(function(res) {
          if (res.done) { done(); return; }
          buf += decoder.decode(res.value, { stream: true });
          var lines = buf.split("\n");
          buf = lines.pop();
          lines.forEach(function(line) {
            if (!line.startsWith("data: ")) return;
            try {
              var evt = JSON.parse(line.slice(6));
              if (evt.type === "delta") {
                accumulated += evt.content;
                responseEl.textContent = accumulated;
              } else if (evt.type === "done") {
                var info = "\n\n[tokens in: " + evt.tokens_in + ", out: " + evt.tokens_out + "]";
                responseEl.textContent = accumulated + info;
                done();
              } else if (evt.type === "error") {
                responseEl.textContent = "Error: " + evt.message;
                done();
              }
            } catch(e) {}
          });
          if (!res.done) read();
        }).catch(function() { done(); });
      }
      read();
    }).catch(function(err) {
      if (err && err.name === "AbortError") {
        if (accumulated) responseEl.textContent = accumulated + "\n\n[stopped]";
        else responseEl.textContent = "[stopped]";
      } else {
        responseEl.textContent = "Request failed.";
      }
      done();
    });

    function done() {
      streaming = false;
      abortController = null;
      sendBtn.disabled = false;
      sendBtn.textContent = "Send";
      if (stopBtn) { stopBtn.style.display = "none"; stopBtn.disabled = false; stopBtn.textContent = "Stop"; }
    }
  });
}


//  Admin: queue status polling

function initAdminStatusPolling() {
  var badge = document.getElementById("queue-status-badge");
  if (!badge) return;

  function poll() {
    fetch("/admin/api/status").then(function(r) { return r.json(); }).then(function(data) {
      var q = data.queue || {};
      var running = data.running_models || [];
      var depth = q.depth || 0;
      var dot = badge.querySelector(".queue-dot");
      if (dot) {
        dot.className = "queue-dot " + (depth > 0 ? "busy" : (running.length > 0 ? "active" : ""));
      }
      badge.title = "Queue depth: " + depth + " | Running: " + (running.join(", ") || "none");
      var txt = badge.querySelector(".queue-text");
      if (txt) txt.textContent = depth > 0 ? "Queue: " + depth : (running.length > 0 ? "Running" : "Idle");
    }).catch(function() {});
  }

  poll();
  setInterval(poll, 10000);
}


//  Generic confirm for danger actions (uses site-native dialog)

function initConfirmForms() {
  // Forms: intercept submit, show confirm modal, re-submit on OK
  document.querySelectorAll("form[data-confirm]").forEach(function(form) {
    form.addEventListener("submit", function(e) {
      var msg = form.getAttribute("data-confirm");
      // If data-confirm was already removed (second fire after requestSubmit), let it through
      if (!msg) return;
      e.preventDefault();
      showConfirm(msg, function() {
        form.removeAttribute("data-confirm");
        if (form.requestSubmit) form.requestSubmit();
        else form.submit();
      });
    });
  });
  // Non-form elements with data-confirm (links, etc.)
  document.querySelectorAll("[data-confirm]:not(form)").forEach(function(el) {
    el.addEventListener("click", function(e) {
      e.preventDefault();
      var msg = el.getAttribute("data-confirm");
      showConfirm(msg, function() {
        el.removeAttribute("data-confirm");
        el.click();
      });
    });
  });
}


//  Admin: password reset modal

function showPasswordResetModal(username, onConfirm) {
  var overlay = document.createElement("div");
  overlay.style.cssText =
    "position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;" +
    "justify-content:center;z-index:9999;";

  var box = document.createElement("div");
  box.style.cssText =
    "background:#1e1e2c;border:1px solid #2d2d4a;border-radius:10px;" +
    "padding:1.4rem 1.6rem;width:min(380px,92vw);display:flex;flex-direction:column;gap:1rem;";

  var titleEl = document.createElement("p");
  titleEl.style.cssText = "margin:0;color:#c8ccd8;font-size:0.925rem;font-weight:600;";
  titleEl.textContent = "Reset password for " + username;

  var input = document.createElement("input");
  input.type = "password";
  input.placeholder = "New password (min 8 chars)";
  input.autocomplete = "new-password";
  input.minLength = 8;
  input.style.cssText =
    "background:#16161f;border:1px solid #2d2d4a;border-radius:6px;" +
    "color:#c8ccd8;padding:0.4rem 0.6rem;font-size:0.875rem;width:100%;box-sizing:border-box;outline:none;";

  var actions = document.createElement("div");
  actions.style.cssText = "display:flex;justify-content:flex-end;gap:0.5rem;";

  var cancelBtn = document.createElement("button");
  cancelBtn.className = "btn btn-sm";
  cancelBtn.textContent = "Cancel";

  var confirmBtn = document.createElement("button");
  confirmBtn.className = "btn btn-sm btn-danger";
  confirmBtn.textContent = "Reset Password";

  function close() {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  }

  function submit() {
    var val = input.value;
    if (val.length < 8) { input.focus(); input.style.borderColor = "#f87171"; return; }
    close();
    onConfirm(val);
  }

  function onKey(e) {
    if (e.key === "Escape") close();
    if (e.key === "Enter") submit();
  }

  cancelBtn.addEventListener("click", close);
  confirmBtn.addEventListener("click", submit);
  overlay.addEventListener("click", function(e) { if (e.target === overlay) close(); });
  document.addEventListener("keydown", onKey);
  input.addEventListener("input", function() { input.style.borderColor = ""; });

  actions.appendChild(cancelBtn);
  actions.appendChild(confirmBtn);
  box.appendChild(titleEl);
  box.appendChild(input);
  box.appendChild(actions);
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  setTimeout(function() { input.focus(); }, 50);
}

function initAdminResetPassword() {
  var form = document.getElementById("admin-reset-pw-form");
  if (!form) return;
  document.querySelectorAll(".admin-reset-pw-btn").forEach(function(btn) {
    btn.addEventListener("click", function() {
      showPasswordResetModal(btn.dataset.username, function(newPw) {
        form.action = "/admin/users/" + btn.dataset.userId + "/reset-password";
        form.querySelector("[name=new_password]").value = newPw;
        form.submit();
      });
    });
  });
}
