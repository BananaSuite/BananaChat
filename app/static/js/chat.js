"use strict";

//  Chat: SSE streaming send + stop + background-task polling

function initChat() {
  var chatInput  = document.getElementById("chat-input");
  var sendBtn    = document.getElementById("chat-send-btn");
  var stopBtn    = document.getElementById("chat-stop-btn");
  var messagesEl = document.getElementById("chat-messages");
  var modelSelect = document.getElementById("model-select-chat");
  var personalitySelect = document.getElementById("personality-select-chat");
  var fileInput = document.getElementById("chat-file-input");
  var attachBtn = document.getElementById("chat-attach-btn");
  var fileList = document.getElementById("chat-file-list");
  var sessionId  = document.getElementById("chat-session-id");
  if (!chatInput || !messagesEl || !sessionId) return;

  var messagesCol = messagesEl.querySelector(".msgs") || messagesEl.querySelector(".messages-col") || messagesEl;
  var currentSid  = sessionId.value;
  var streaming   = false;
  var selectedFiles = [];

  if (personalitySelect) personalitySelect.addEventListener("change", function() {
    fetch("/chat/" + currentSid + "/personality", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify({ personality_id: personalitySelect.value }),
    }).then(function(response) {
      if (!response.ok) return response.json().then(function(data) { throw new Error(data.error || "Personality unavailable"); });
    }).catch(function(error) { showToast(error.message || "Could not change personality", "error"); });
  });

  function renderSelectedFiles() {
    if (!fileList) return;
    fileList.innerHTML = "";
    fileList.hidden = selectedFiles.length === 0;
    selectedFiles.forEach(function(file, index) {
      var chip = document.createElement("span");
      chip.className = "selected-file-chip";
      var name = document.createElement("span");
      name.textContent = file.name;
      var remove = document.createElement("button");
      remove.type = "button"; remove.textContent = "×"; remove.setAttribute("aria-label", "Remove " + file.name);
      remove.addEventListener("click", function() { selectedFiles.splice(index, 1); renderSelectedFiles(); });
      chip.appendChild(name); chip.appendChild(remove); fileList.appendChild(chip);
    });
  }
  if (attachBtn && fileInput) attachBtn.addEventListener("click", function() { fileInput.click(); });
  if (fileInput) fileInput.addEventListener("change", function() {
    var maximum = Number(window.BC_CHAT_MAX_FILES || 4);
    selectedFiles = Array.prototype.slice.call(fileInput.files || []).slice(0, maximum);
    renderSelectedFiles();
    fileInput.value = "";
  });

  // Current-generation state (shared between sendMessage, stop handler, poll)
  var currentReader     = null;
  var currentBubbleEl   = null;
  var currentMetaEl     = null;
  var currentAccumulated = "";
  var currentModelName  = "";

  // Auto-resize textarea
  chatInput.addEventListener("input", function() {
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + "px";
  });

  chatInput.addEventListener("keydown", function(e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  if (sendBtn) sendBtn.addEventListener("click", sendMessage);

  function scrollToBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  function appendUserMessage(content, files) {
    var div = document.createElement("div");
    div.className = "message user";
    div.innerHTML = '<div class="message-bubble">' + escapeHtml(content) + '</div>' +
      '<div class="message-meta">You</div>';
    messagesCol.appendChild(div);
    if (files && files.length) {
      var attachments = document.createElement("div");
      attachments.className = "message-attachments";
      files.forEach(function(file) { var chip = document.createElement("span"); chip.className = "attachment-chip"; chip.textContent = file.name; attachments.appendChild(chip); });
      div.insertBefore(attachments, div.querySelector(".message-meta"));
    }
    scrollToBottom();
    return div;
  }

  function appendAssistantPlaceholder() {
    var div = document.createElement("div");
    div.className = "message assistant";
    div.innerHTML =
      '<div class="message-bubble">' +
        '<div class="typing-indicator">' +
          '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>' +
        '</div>' +
      '</div>' +
      '<div class="message-meta">Waiting…</div>';
    messagesCol.appendChild(div);
    scrollToBottom();
    return div;
  }

  function getOrCreateStatusBar() {
    var bar = document.getElementById("chat-status-bar");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "chat-status-bar";
      bar.className = "chat-status-bar";
      var inputArea = document.querySelector(".chat-input-zone") || document.querySelector(".chat-input-area");
      if (inputArea) inputArea.insertAdjacentElement("beforebegin", bar);
    }
    return bar;
  }

  function clearStatusBar() {
    var bar = document.getElementById("chat-status-bar");
    if (bar) bar.remove();
  }

  function setStreaming(active) {
    streaming = active;
    if (sendBtn) {
      sendBtn.disabled = active;
      sendBtn.style.display = active ? "none" : "";
    }
    if (stopBtn) stopBtn.style.display = active ? "flex" : "none";
  }

  function finishStream() {
    setStreaming(false);
    clearStatusBar();
    currentReader = null;
    chatInput.focus();
    scrollToBottom();
  }

  // ── Stop button
  if (stopBtn) {
    stopBtn.addEventListener("click", function() {
      if (!streaming) return;
      stopBtn.disabled = true;
      if (currentMetaEl) currentMetaEl.textContent = tr("generation_stopping");
      fetch("/chat/" + currentSid + "/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: "{}",
      }).then(function(response) {
        if (!response.ok) throw new Error("Stop request failed");
        var reader = currentReader;
        currentReader = null;
        if (reader) reader.cancel().catch(function() {});
        checkPendingGeneration(true);
      }).catch(function() {
        if (currentMetaEl) currentMetaEl.textContent = "Could not stop generation. Please retry.";
      }).finally(function() { stopBtn.disabled = false; });
    });
  }

  // ── Send message
  function sendMessage() {
    if (streaming) return;
    if (window.BC_STOP_RECOGNITION) window.BC_STOP_RECOGNITION();
    var content = chatInput.value.trim();
    if (!content && !selectedFiles.length) return;
    var model = modelSelect ? modelSelect.value : "auto";

    chatInput.value = "";
    chatInput.style.height = "auto";

    var empty = messagesEl.querySelector(".chat-empty");
    if (empty) empty.remove();

    var filesToSend = selectedFiles.slice();
    selectedFiles = [];
    renderSelectedFiles();
    appendUserMessage(content || "Please analyze the attached files.", filesToSend);
    var assistantEl = appendAssistantPlaceholder();
    currentBubbleEl   = assistantEl.querySelector(".message-bubble");
    currentMetaEl     = assistantEl.querySelector(".message-meta");
    currentAccumulated = "";
    currentModelName  = "";

    setStreaming(true);

    var formData = new FormData();
    formData.append("content", content);
    formData.append("model", model);
    Object.keys(_chatParams).forEach(function(key) { formData.append(key, _chatParams[key]); });
    filesToSend.forEach(function(file) { formData.append("files", file, file.name); });
    fetch("/chat/" + currentSid + "/send", {
      method: "POST",
      headers: { "X-CSRFToken": getCsrfToken() },
      body: formData,
    }).then(function(resp) {
      if (!resp.ok) {
        return resp.json().then(function(data) {
          if (currentBubbleEl) currentBubbleEl.textContent = "Error: " + (data.error || resp.statusText);
          finishStream();
        }).catch(function() {
          if (currentBubbleEl) currentBubbleEl.textContent = "Error: " + resp.status + " " + resp.statusText;
          finishStream();
        });
      }
      var reader = resp.body.getReader();
      currentReader = reader;
      var decoder = new TextDecoder();
      var buffer = "";

      function read() {
        reader.read().then(function(result) {
          if (currentReader !== reader) return;
          if (result.done) {
            currentReader = null;
            if (streaming) checkPendingGeneration(true);
            return;
          }
          buffer += decoder.decode(result.value, { stream: true });
          var lines = buffer.split("\n");
          buffer = lines.pop();
          lines.forEach(function(line) {
            if (!line.startsWith("data: ")) return;
            try {
              var evt = JSON.parse(line.slice(6));
              if (evt.type === "queued") {
                var bar = getOrCreateStatusBar();
                bar.innerHTML = '<span class="status-item queued">⏳ Queue position: ' + escapeHtml(String(evt.position)) + "</span>";
                if (currentMetaEl) currentMetaEl.textContent = "Queued (#" + escapeHtml(String(evt.position)) + ")…";
              } else if (evt.type === "start") {
                currentModelName = evt.model || "";
                if (evt.notice) {
                  var modelNotice = assistantEl.querySelector(".chat-model-notice");
                  if (!modelNotice) {
                    modelNotice = document.createElement("p");
                    modelNotice.className = "chat-model-notice text-muted";
                    modelNotice.setAttribute("role", "status");
                    assistantEl.appendChild(modelNotice);
                  }
                  modelNotice.textContent = evt.notice;
                  setActiveModel("auto", "Auto");
                }
                clearStatusBar();
                var startLabel = evt.is_reasoning
                  ? (currentModelName ? currentModelName + " · reasoning…" : "Reasoning…")
                  : (currentModelName + " · generating…");
                if (currentMetaEl) currentMetaEl.textContent = startLabel;
              } else if (evt.type === "delta") {
                if (currentAccumulated === "" && currentBubbleEl.querySelector(".typing-indicator")) {
                  currentBubbleEl.innerHTML = "";
                  currentBubbleEl.classList.add("md-rendered");
                }
                currentAccumulated += evt.content;
                var pt = parseThinking(currentAccumulated);
                if (pt.hasThinking) {
                  currentBubbleEl.innerHTML = renderWithThinking(currentAccumulated, true);
                  if (pt.done && currentMetaEl && currentMetaEl.textContent.indexOf("reasoning") !== -1) {
                    currentMetaEl.textContent = currentModelName + " · generating…";
                  }
                } else {
                  currentBubbleEl.textContent = currentAccumulated;
                }
                scrollToBottom();
              } else if (evt.type === "done" || evt.type === "error") {
                clearStatusBar();
                if (currentAccumulated) {
                  currentBubbleEl.innerHTML = renderWithThinking(currentAccumulated, false);
                  currentBubbleEl.classList.add("md-rendered");
                } else if (evt.type === "error" && currentBubbleEl) {
                  currentBubbleEl.textContent = evt.message || tr("generation_failed");
                }
                var completedMessage = currentBubbleEl && currentBubbleEl.closest(".message");
                if (completedMessage && evt.message_id) completedMessage.dataset.messageId = evt.message_id;
                if (completedMessage) {
                  enhanceAssistantTools(completedMessage.parentNode || document);
                  showGenerationOutcome(completedMessage, evt.state || (evt.type === "error" ? "interrupted" : "completed"), evt.message);
                }
                var tokInfo = evt.tokens_out ? " · " + evt.tokens_out + (evt.tokens_out === 1 ? " token" : " tokens") : "";
                if (currentMetaEl) currentMetaEl.textContent = (currentModelName || "assistant") + tokInfo;
                if (evt.auto_title) {
                  var titleEl = document.getElementById("chat-title");
                  if (titleEl) titleEl.textContent = evt.auto_title;
                  var activeItem = document.querySelector(".session-item.active");
                  if (activeItem) {
                    var stEl = activeItem.querySelector(".session-title");
                    if (stEl) stEl.textContent = evt.auto_title;
                    activeItem.dataset.title = evt.auto_title;
                  }
                  document.title = evt.auto_title + "-- " + document.title.split(" --").slice(-1)[0];
                }
                finishStream();
              } else if (evt.type === "reconnect") {
                currentReader = null;
                reader.cancel().catch(function() {});
                checkPendingGeneration(true);
              }
            } catch(err) {}
          });
          if (!result.done) read();
        }).catch(function() {
          if (currentReader !== reader) return;
          currentReader = null;
          checkPendingGeneration(true);
        });
      }
      read();
    }).catch(function() {
      if (currentBubbleEl) currentBubbleEl.textContent = "Request failed.";
      finishStream();
    });
  }

  // ── Background-task polling (resume a chat left mid-generation)
  function checkPendingGeneration(resumeExisting) {
    var allMsgs = messagesCol.querySelectorAll(".message");
    if (!allMsgs.length) return;
    var lastMsg = allMsgs[allMsgs.length - 1];
    if (!resumeExisting && !lastMsg.classList.contains("user")) return;

    // Recover from a navigation or transport interruption using durable status.
    var assistantEl = resumeExisting && currentBubbleEl
      ? currentBubbleEl.closest(".message") : appendAssistantPlaceholder();
    currentBubbleEl   = assistantEl.querySelector(".message-bubble");
    currentMetaEl     = assistantEl.querySelector(".message-meta");
    currentAccumulated = "";
    currentModelName  = "";
    if (currentMetaEl) currentMetaEl.textContent = "Connecting…";
    setStreaming(true);

    var _pollErrors = 0;
    function poll() {
      if (!streaming) return;
      fetch("/chat/" + currentSid + "/status")
        .then(function(r) {
          if (!r.ok) throw new Error("status " + r.status);
          return r.json();
        })
        .then(function(data) {
          if (!streaming) return;
          _pollErrors = 0;

          // Show live partial content while generating
          if (data.generating && data.partial_content && currentBubbleEl) {
            currentBubbleEl.innerHTML = renderWithThinking(data.partial_content, true);
            currentBubbleEl.classList.add("md-rendered");
            currentAccumulated = data.partial_content;
            if (currentMetaEl) currentMetaEl.textContent = data.stopping ? tr("generation_stopping") : "Generating…";
            scrollToBottom();
          }

          if (data.last_role === "assistant") {
            var msg = data.last_message;
            if (msg && msg.content && currentBubbleEl) {
              currentBubbleEl.innerHTML = renderWithThinking(msg.content, false);
              currentBubbleEl.classList.add("md-rendered");
              var tokInfo = msg.tokens_out ? " · " + msg.tokens_out + (msg.tokens_out === 1 ? " token" : " tokens") : "";
              if (currentMetaEl) currentMetaEl.textContent = (msg.model_name || "assistant") + tokInfo;
              var recoveredMessage = currentBubbleEl.closest(".message");
              if (recoveredMessage && msg.id) recoveredMessage.dataset.messageId = msg.id;
              if (recoveredMessage) {
                enhanceAssistantTools(recoveredMessage.parentNode || document);
                showGenerationOutcome(recoveredMessage, msg.generation_state, data.error);
              }
            }
            finishStream();
          } else if (data.generating) {
            if (data.stopping && currentMetaEl) currentMetaEl.textContent = tr("generation_stopping");
            setTimeout(poll, 1000);
          } else {
            if (data.error && currentBubbleEl) {
              currentBubbleEl.textContent = data.error;
              showGenerationOutcome(assistantEl, data.state);
            } else {
              assistantEl.remove();
            }
            finishStream();
          }
        })
        .catch(function() {
          if (!streaming) return;
          _pollErrors += 1;
          if (_pollErrors >= 8) { assistantEl.remove(); finishStream(); return; }
          setTimeout(poll, 3000);
        });
    }
    poll();
  }

  checkPendingGeneration();
  scrollToBottom();
}


//  Chat: inline rename (double-click title)

function initChatRename() {
  var titleEl = document.getElementById("chat-title");
  var sessionId = document.getElementById("chat-session-id");
  if (!titleEl || !sessionId) return;

  titleEl.addEventListener("click", function() {
    var current = titleEl.textContent.trim();
    var input = document.createElement("input");
    input.type = "text";
    input.value = current;
    input.style.cssText = "background:#1a1a24;border:1px solid #7e9ada;border-radius:6px;color:#c8ccd8;" +
      "padding:0.2rem 0.5rem;font-size:0.9rem;font-family:inherit;width:260px;max-width:100%;outline:none;";

    titleEl.replaceWith(input);
    input.focus();
    input.select();

    var saved = false;
    function save() {
      if (saved) return;
      saved = true;
      var newTitle = input.value.trim() || current;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.id = "chat-title";
      btn.className = "chat-title-btn";
      btn.title = "Click to rename";
      btn.textContent = newTitle;
      input.replaceWith(btn);
      initChatRename();
      // Sync sidebar active item optimistically
      var activeItem = document.querySelector(".session-item.active");
      if (activeItem) {
        var sidebarTitle = activeItem.querySelector(".session-title");
        if (sidebarTitle) sidebarTitle.textContent = newTitle;
        activeItem.dataset.title = newTitle;
      }
      document.title = newTitle + "-- " + document.title.split(" --").slice(-1)[0];
      fetch("/chat/" + sessionId.value + "/title", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ title: newTitle }),
      }).then(function(r) { return r.json(); }).then(function(d) {
        if (!d.ok) {
          // Revert optimistic update on failure
          btn.textContent = current;
          document.title = current + "-- " + document.title.split(" --").slice(-1)[0];
          if (activeItem) {
            var sidebarTitle = activeItem.querySelector(".session-title");
            if (sidebarTitle) sidebarTitle.textContent = current;
            activeItem.dataset.title = current;
          }
          showToast("Failed to rename chat.");
        }
      }).catch(function() {});
    }

    input.addEventListener("blur", save);
    input.addEventListener("keydown", function(e) {
      if (e.key === "Enter") { e.preventDefault(); save(); }
      if (e.key === "Escape") { input.value = current; save(); }
    });
  });
}


//  Chat: share button

function initChatShare() {
  var shareBtn = document.getElementById("chat-share-btn");
  var shareStatus = document.getElementById("chat-share-status");
  var sessionId = document.getElementById("chat-session-id");
  if (!shareBtn || !sessionId) return;

  function doShare(action) {
    fetch("/chat/" + sessionId.value + "/share", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify({ action: action }),
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (data.error) { showToast("Error: " + data.error); return; }
      if (action === "create" && data.token) {
        var url = window.location.origin + "/share/" + data.token;
        if (shareStatus) {
          shareStatus.innerHTML = '<a href="' + escapeHtml(url) + '" target="_blank">↗ View</a>';
        }
        var svgHtml = shareBtn.querySelector("svg") ? shareBtn.querySelector("svg").outerHTML : "";
        shareBtn.innerHTML = svgHtml + " Revoke";
        shareBtn.setAttribute("data-action", "revoke");
        shareBtn.title = "Revoke share link";
        showShareLink(url);
      } else {
        if (shareStatus) shareStatus.innerHTML = "";
        var svgHtml = shareBtn.querySelector("svg") ? shareBtn.querySelector("svg").outerHTML : "";
        shareBtn.innerHTML = svgHtml + " Share";
        shareBtn.setAttribute("data-action", "create");
        shareBtn.title = "Share this chat";
        showToast("Share link revoked");
      }
    }).catch(function() { showToast("Share request failed"); });
  }

  shareBtn.addEventListener("click", function() {
    var action = shareBtn.getAttribute("data-action") || "create";
    if (action === "revoke") {
      showConfirm("Revoke the share link? Anyone with the link will lose access.", function() {
        doShare("revoke");
      });
    } else {
      doShare(action);
    }
  });
}


//  Chat: delete session

function initChatDelete() {
  var deleteBtn = document.getElementById("chat-delete-btn");
  var sessionId = document.getElementById("chat-session-id");
  if (!deleteBtn || !sessionId) return;

  deleteBtn.addEventListener("click", function() {
    showConfirm("Delete this chat? This cannot be undone.", function() {
      fetch("/chat/" + sessionId.value + "/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: "{}",
      }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) window.location.href = "/chat";
      });
    });
  });
}
