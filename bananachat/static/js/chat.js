// Chat page: conversation, composer, streaming, sidebar and model picker.
//
// The streaming protocol is documented in bananachat/services/chat.py. The
// answer keeps being generated and saved on the server when this page goes
// away; if the stream breaks, the page polls /status until the run ends.
import {
  ApiError, api, boot, checkStatusSoon, confirmDialog, copyText, debounce, el, icon, onStatusChange, pageData,
  promptDialog, readEventStream, safeStorage, secretDialog, t, toast,
} from "./core.js";
import { initPersonas } from "./chat-personas.js";
import { enhanceMarkdown, renderMessage, speakableText, splitReasoning } from "./markdown.js";

const data = pageData();
const session = data.session;
const limits = data.limits;
const storage = safeStorage();
const $ = (id) => document.getElementById(id);
const MODEL_KEY = "bc-chat-model";
const PARAMS_KEY = "bc-chat-params";
const SIDEBAR_KEY = "bc-chat-sidebar-collapsed";
const POLL_INTERVAL = 1500;

const labels = {
  reasoning: t("chat_reasoning"),
  reasoningLive: t("chat_reasoning_live"),
  copy: t("copy"),
  copied: t("copied"),
  download: t("chat_download_code"),
  code: t("chat_code"),
};

const ui = {
  shell: $("chat-shell"),
  sidebar: $("chat-sidebar"),
  backdrop: $("chat-backdrop"),
  sidebarToggle: $("sidebar-toggle"),
  title: $("chat-title"),
  scroller: $("chat-scroll"),
  messages: $("chat-messages"),
  empty: $("chat-empty"),
  loadEarlier: $("load-earlier"),
  form: $("composer"),
  input: $("chat-input"),
  send: $("send-button"),
  stop: $("stop-button"),
  chips: $("file-chips"),
  fileInput: $("file-input"),
  attach: $("attach-button"),
  scrollBottom: $("scroll-bottom"),
  dropOverlay: $("drop-overlay"),
  announcer: $("chat-announcer"),
  menu: $("popup-menu"),
};

const state = {
  canSend: true,
  generating: false,
  files: [],
  hasMore: data.has_more,
  lastId: 0,
  oldestId: null,
  stickToBottom: true,
  sidebarNext: data.sidebar_next,
};

const chatUrl = (id, action = "") => `${data.urls.root}/${encodeURIComponent(id)}${action ? `/${action}` : ""}`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const dateFormat = new Intl.DateTimeFormat(boot.lang === "it" ? "it-IT" : "en-GB", { dateStyle: "medium", timeStyle: "short" });

function announce(text) {
  ui.announcer.textContent = "";
  setTimeout(() => { ui.announcer.textContent = text; }, 50);
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function setTitle(title) {
  ui.title.textContent = title;
  document.title = `${title} · ${data.site_name}`;
  session.title = title;
}

// ----- messages -----------------------------------------------------------------

const STATE_KEYS = { stopped: "chat_state_stopped", failed: "chat_state_failed", interrupted: "chat_state_interrupted" };

function metaText(message) {
  const parts = [];
  if (message.role === "user") parts.push(t("chat_you"));
  else {
    parts.push(message.model_name || t("chat_assistant"));
    if (message.tokens_out) parts.push(t("chat_tokens", { count: message.tokens_out }));
  }
  if (message.created_at) parts.push(dateFormat.format(new Date(message.created_at)));
  return parts.join(" · ");
}

function iconButton(name, label, onclick, extra = {}) {
  return el("button", { type: "button", class: "icon-btn", "aria-label": label, title: label, onclick, ...extra }, icon(name));
}

const canSpeak = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
let speakingButton = null;

function stopSpeaking() {
  if (!canSpeak) return;
  window.speechSynthesis.cancel();
  if (speakingButton) {
    speakingButton.setAttribute("aria-pressed", "false");
    speakingButton.setAttribute("aria-label", t("chat_read_aloud"));
    speakingButton.title = t("chat_read_aloud");
    speakingButton = null;
  }
}

function speak(button, content) {
  if (speakingButton === button) { stopSpeaking(); return; }
  stopSpeaking();
  const text = speakableText(content);
  if (!text) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = boot.lang === "it" ? "it-IT" : "en-US";
  utterance.onend = utterance.onerror = () => { if (speakingButton === button) stopSpeaking(); };
  speakingButton = button;
  button.setAttribute("aria-pressed", "true");
  button.setAttribute("aria-label", t("chat_stop_reading"));
  button.title = t("chat_stop_reading");
  window.speechSynthesis.speak(utterance);
}

function messageActions(message, content) {
  const actions = el("div", { class: "message-actions" });
  const copy = iconButton("copy", t("chat_copy_message"), async () => {
    const text = message.role === "assistant" ? splitReasoning(message.content).answer : message.content;
    if (await copyText(text)) {
      copy.replaceChildren(icon("check"));
      announce(t("copied"));
      setTimeout(() => copy.replaceChildren(icon("copy")), 1500);
    }
  });
  actions.append(copy);
  if (message.role === "assistant") {
    if (canSpeak) {
      const button = iconButton("speaker", t("chat_read_aloud"), () => speak(button, content), { "aria-pressed": "false" });
      actions.append(button);
    }
    if (message.id) {
      actions.append(el("a", { class: "icon-btn", href: message.pdf_url || `${chatUrl(session.id, "messages")}/${message.id}/pdf`,
        "aria-label": t("chat_pdf"), title: t("chat_pdf"), download: "" }, icon("download")));
    }
  }
  return actions;
}

const KIND_KEYS = { image: "chat_kind_image", pdf: "chat_kind_pdf", text: "chat_kind_text", code: "chat_kind_code" };
/** The kind badge of a file ("IMAGE", "PDF"...), in the reader's language. */
function kindLabel(kind) {
  return (KIND_KEYS[kind] ? t(KIND_KEYS[kind]) : kind || "file").toUpperCase();
}

function attachmentList(items) {
  const list = el("ul", { class: "attachment-list" });
  for (const item of items) {
    if (item.kind === "image" && item.url) {
      list.append(el("li", { class: "attachment-image" },
        el("a", { href: item.url, target: "_blank", rel: "noopener" },
          el("img", { src: item.url, alt: item.filename, loading: "lazy" }))));
    } else {
      const name = el("span", { class: "truncate", text: item.filename });
      const body = item.url ? el("a", { href: item.url, class: "file-chip-link" }, name) : name;
      list.append(el("li", { class: "file-chip" }, el("span", { class: "file-chip-kind", text: kindLabel(item.kind) }), body));
    }
  }
  return list;
}

function stateNote(stateName, message) {
  if (!STATE_KEYS[stateName] && !message) return null;
  const text = message || t(STATE_KEYS[stateName]);
  return el("p", { class: `message-state state-${stateName}` }, icon(stateName === "stopped" ? "info" : "alert"), el("span", { text }));
}

/** A saved message as an <article>. */
function buildMessage(message) {
  const content = message.role === "assistant"
    ? el("div", { class: "prose message-content" })
    : el("div", { class: "message-content message-text", text: message.content });
  if (message.role === "assistant") content.innerHTML = renderMessage(message.content, { labels });
  const body = el("div", { class: "message-body" }, content);
  if (message.attachments?.length) body.append(attachmentList(message.attachments));
  const note = message.role === "assistant" ? stateNote(message.state) : null;
  if (note) body.append(note);
  const article = el("article", { class: `message message-${message.role}`, dataset: message.id ? { id: String(message.id) } : {} },
    body, el("p", { class: "message-meta", text: metaText(message) }), messageActions(message, content));
  article.setAttribute("aria-label", message.role === "user" ? t("chat_you") : (message.model_name || t("chat_assistant")));
  return article;
}

function track(message) {
  if (!message.id) return;
  state.lastId = Math.max(state.lastId, message.id);
  state.oldestId = state.oldestId === null ? message.id : Math.min(state.oldestId, message.id);
}

function appendMessage(message) {
  track(message);
  const node = buildMessage(message);
  ui.messages.append(node);
  updateEmpty();
  return node;
}

function updateEmpty() {
  ui.empty.hidden = ui.messages.childElementCount > 0 || state.generating;
}

/** Mark the local copy of the sent message with its saved id (so later syncs recognise it). */
function markSaved(node, id) {
  if (!node || !id) return;
  node.dataset.id = String(id);
  track({ id });
}

/**
 * Show saved messages in place of the local copies shown while sending (the sent message and
 * the pending answer), keeping the conversation order; messages already shown stay as they are.
 */
function replaceUnsaved(messages, { user = null, answer = null } = {}) {
  let anchor = null;
  for (const message of messages) {
    const shown = ui.messages.querySelector(`.message[data-id="${message.id}"]`);
    const local = message.role === "user" ? user : answer;
    if (shown && shown !== local) {
      anchor = shown;
      continue;
    }
    track(message);
    const node = buildMessage(message);
    if (local?.isConnected) local.replaceWith(node);
    else if (anchor) anchor.after(node);
    else ui.messages.append(node);
    if (local === user) user = null;
    else if (local === answer) answer = null;
    anchor = node;
  }
  updateEmpty();
  scrollToEnd();
}

// ----- scrolling --------------------------------------------------------------------

function nearBottom() {
  const { scrollTop, scrollHeight, clientHeight } = ui.scroller;
  return scrollHeight - scrollTop - clientHeight < 80;
}

function scrollToEnd(force = false) {
  if (force || state.stickToBottom) ui.scroller.scrollTop = ui.scroller.scrollHeight;
}

ui.scroller.addEventListener("scroll", () => {
  state.stickToBottom = nearBottom();
  ui.scrollBottom.hidden = state.stickToBottom;
}, { passive: true });

ui.scrollBottom.addEventListener("click", () => {
  state.stickToBottom = true;
  scrollToEnd(true);
  ui.scrollBottom.hidden = true;
  ui.input.focus();
});

// ----- the answer being generated ------------------------------------------------------

class PendingAnswer {
  constructor() {
    this.thinking = "";
    this.answer = "";
    this.modelName = "";
    this.status = el("p", { class: "message-status" }, el("span", { class: "typing", "aria-hidden": "true" }, el("span"), el("span"), el("span")),
      el("span", { text: t("chat_waiting") }));
    this.notice = el("p", { class: "message-notice", hidden: true });
    this.content = el("div", { class: "prose message-content" });
    this.body = el("div", { class: "message-body" }, this.notice, this.content, this.status);
    this.meta = el("p", { class: "message-meta", text: t("chat_assistant") });
    this.node = el("article", { class: "message message-assistant is-pending", "aria-busy": "true", "aria-label": t("chat_assistant") }, this.body, this.meta);
    this.frame = 0;
    this.lastRender = 0;
    ui.messages.append(this.node);
    updateEmpty();
    scrollToEnd(true);
  }

  setStatus(text) {
    this.status.lastElementChild.textContent = text;
  }

  started(event) {
    this.modelName = event.display_name || event.model;
    this.meta.textContent = this.modelName;
    this.setStatus(t("chat_writing"));
    if (event.notice) {
      this.notice.hidden = false;
      this.notice.replaceChildren(icon("info"), el("span", { text: event.notice }));
      announce(event.notice);
    }
  }

  get text() {
    if (!this.thinking.trim()) return this.answer;
    return this.answer ? `<think>${this.thinking.trim()}</think>\n\n${this.answer}` : `<think>${this.thinking.trim()}`;
  }

  append(text, thinking) {
    if (thinking) this.thinking += text;
    else this.answer += text;
    this.setStatus(thinking && !this.answer ? t("chat_thinking") : t("chat_writing"));
    this.schedule();
  }

  showPartial(text) {
    const parts = splitReasoning(text || "");
    this.thinking = parts.reasoning;
    this.answer = parts.answer;
    this.schedule();
  }

  schedule() {
    if (this.frame) return;
    const size = this.answer.length + this.thinking.length;
    const wait = Math.max(0, (size > 60000 ? 500 : size > 15000 ? 150 : 0) - (performance.now() - this.lastRender));
    this.frame = setTimeout(() => requestAnimationFrame(() => {
      this.frame = 0;
      this.render();
    }), wait);
  }

  render() {
    this.lastRender = performance.now();
    const open = this.content.querySelector("details.reasoning")?.open;
    this.content.innerHTML = renderMessage(this.text, { labels });
    if (open) this.content.querySelector("details.reasoning")?.setAttribute("open", "");
    scrollToEnd();
  }

  /** Replace with the saved message (or a note when nothing was saved). */
  finish(event) {
    clearTimeout(this.frame);
    this.frame = 0;
    const text = this.text;
    if (event.message_id && text) {
      const message = {
        id: event.message_id, role: "assistant", content: text, model: event.model,
        model_name: event.display_name || this.modelName, tokens_out: event.tokens_out,
        state: event.state, created_at: new Date().toISOString(), attachments: [],
      };
      const node = buildMessage(message);
      if (event.message && event.state !== "completed") {
        node.querySelector(".message-state")?.remove();
        node.querySelector(".message-body").append(stateNote(event.state, event.message));
      }
      this.node.replaceWith(node);
      track(message);
      return node;
    }
    return this.fail(event.state, event.message, text);
  }

  /** Show a final state without a saved message (e.g. failed before any output, or an unrecovered partial). */
  fail(stateName, message, text = this.text) {
    clearTimeout(this.frame);
    this.frame = 0;
    this.node.classList.remove("is-pending");
    this.node.removeAttribute("aria-busy");
    this.status.remove();
    if (text) this.content.innerHTML = renderMessage(text, { labels });
    else this.content.remove();
    const note = stateNote(stateName === "completed" ? "failed" : stateName, message);
    if (note) this.body.append(note);
    return this.node;
  }
}

// ----- sending --------------------------------------------------------------------------

function setGenerating(active) {
  state.generating = active;
  ui.send.hidden = active;
  ui.stop.hidden = !active;
  ui.stop.disabled = false;
  ui.form.setAttribute("aria-busy", active ? "true" : "false");
  updateEmpty();
}

function composeForm(text) {
  const form = new FormData();
  form.append("content", text);
  if (picker.selected !== "auto") form.append("model", picker.selected);
  if (effortPicker.selected) form.append("effort", effortPicker.selected);
  for (const input of document.querySelectorAll("[data-param]")) {
    if (input.value.trim() !== "") form.append(input.name, input.value.trim());
  }
  for (const file of state.files) form.append("files", file, file.name);
  return form;
}

async function send(text) {
  if (state.generating) return;
  if (!state.canSend) {
    toast(t("status_paused"), "warning");
    return;
  }
  const files = [...state.files];
  if (!text && !files.length) {
    ui.input.focus();
    return;
  }
  if (new TextEncoder().encode(text).length > limits.max_message_bytes) {
    toast(t("chat_too_long", { size: Math.floor(limits.max_message_bytes / 1024) }), "error");
    return;
  }
  const form = composeForm(text);
  const previousLastId = state.lastId;
  const userNode = appendMessage({
    role: "user", content: text || t("chat_files_only"), created_at: new Date().toISOString(),
    attachments: files.map((file) => ({ kind: kindOf(file.name), filename: file.name })),
  });
  ui.input.value = "";
  autoGrow();
  setFiles([]);
  setGenerating(true);
  const pending = new PendingAnswer();
  let terminal = null;
  let started = false;
  try {
    const response = await api(chatUrl(session.id, "send"), { form, raw: true, headers: { Accept: "text/event-stream" } });
    started = true;
    await readEventStream(response, (event) => {
      if (event.type === "queued") pending.setStatus(t("chat_queued", { position: event.position }));
      else if (event.type === "start") pending.started(event);
      else if (event.type === "delta") pending.append(event.text, event.thinking);
      else if (event.type === "done" || event.type === "error") terminal = event;
    });
  } catch (error) {
    if (!started) {
      // Refused before it ran: nothing was saved, so give the message back.
      userNode.remove();
      pending.node.remove();
      ui.input.value = text;
      autoGrow();
      setFiles(files);
      setGenerating(false);
      // Refusals for maintenance or an outage are worded for API clients; the page has its own translated text.
      const paused = error instanceof ApiError && error.status === 503 && ["maintenance", "outage"].includes(error.code);
      const message = paused ? t("status_paused") : error instanceof ApiError ? error.message : t("chat_error_network");
      if (paused) checkStatusSoon();
      toast(message, "error");
      announce(message);
      ui.input.focus();
      return;
    }
  }
  if (terminal) {
    finishRun(pending, terminal);
    if (files.length) await syncAfter(previousLastId, userNode, terminal.user_message_id);
    else markSaved(userNode, terminal.user_message_id);
  } else {
    await poll(pending, previousLastId, userNode);
  }
}

function finishRun(pending, event) {
  pending.finish(event);
  setGenerating(false);
  if (event.title && event.title !== session.title) setTitle(event.title);
  if (!session.incognito) upsertCurrentInSidebar();
  announce(event.state === "completed" ? t("chat_answer_ready") : (event.message || t(STATE_KEYS[event.state] || "chat_state_failed")));
  scrollToEnd();
}

/** Show the saved copy of the sent message (its attachments get links). */
async function syncAfter(afterId, userNode, userId) {
  try {
    const status = await api(`${chatUrl(session.id, "status")}?after=${afterId}`);
    replaceUnsaved(status.messages.filter((message) => message.id === userId), { user: userNode });
  } catch {
    markSaved(userNode, userId);  // the page keeps the local copy; a reload shows the saved one
  }
}

/** The stream broke: follow the run through /status until it ends. */
async function poll(pending, afterId, userNode = null) {
  let failures = 0;
  pending.setStatus(t("chat_reconnecting"));
  for (;;) {
    await sleep(POLL_INTERVAL * Math.min(4, 1 + failures));
    let status;
    try {
      status = await api(`${chatUrl(session.id, "status")}?after=${afterId}`);
      failures = 0;
    } catch (error) {
      failures += 1;
      if (error instanceof ApiError && error.status === 404) {
        window.location.assign(data.urls.root);
        return;
      }
      pending.setStatus(t("chat_reconnecting"));
      continue;
    }
    if (status.generating) {
      pending.setStatus(status.stopping ? t("chat_stopping") : t("chat_writing"));
      if (status.partial) pending.showPartial(status.partial);
      continue;
    }
    const saved = status.messages.filter((message) => message.role === "assistant");
    if (saved.length) {
      replaceUnsaved(status.messages, { user: userNode, answer: pending.node });
    } else {
      replaceUnsaved(status.messages, { user: userNode });
      pending.fail(status.state, status.error, status.partial || pending.text);
    }
    setGenerating(false);
    if (status.title && status.title !== session.title) setTitle(status.title);
    if (!session.incognito && status.messages.length) upsertCurrentInSidebar();
    announce(status.state === "completed" ? t("chat_answer_ready") : (status.error || t(STATE_KEYS[status.state] || "chat_state_failed")));
    return;
  }
}

ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  send(ui.input.value.trim());
});

// Enter sends and Shift+Enter adds a line; touch keyboards have no Shift+Enter, so there Enter adds a line and
// the send button sends.
const touchFirst = window.matchMedia("(pointer: coarse)");
const composerHint = $("composer-hint");
const keyboardHint = composerHint?.textContent;
const applyEnterKey = () => {
  ui.input.enterKeyHint = touchFirst.matches ? "enter" : "send";
  if (composerHint) composerHint.textContent = touchFirst.matches ? t("chat_composer_hint_touch") : keyboardHint;
};
applyEnterKey();
touchFirst.addEventListener("change", applyEnterKey);

ui.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !touchFirst.matches && !event.shiftKey && !event.isComposing && !event.altKey
      && !event.ctrlKey && !event.metaKey) {
    event.preventDefault();
    ui.form.requestSubmit();
  }
});

function autoGrow() {
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, window.innerHeight * 0.4)}px`;
}
ui.input.addEventListener("input", autoGrow);

ui.stop.addEventListener("click", async () => {
  ui.stop.disabled = true;
  try {
    await api(chatUrl(session.id, "stop"), { method: "POST" });
    announce(t("chat_stopping"));
  } catch (error) {
    ui.stop.disabled = false;
    toast(error.message, "error");
  }
});

// Maintenance or an AI-server outage pauses sending; drafts stay possible.
onStatusChange(({ canSend }) => {
  state.canSend = canSend;
  ui.send.disabled = !canSend;
  for (const control of [ui.attach, ui.fileInput, $("voice-button")]) if (control) control.disabled = !canSend;
  $("composer-paused").textContent = canSend ? "" : t("status_paused");
});

// ----- attachments ----------------------------------------------------------------------

const IMAGE_EXT = [".png", ".jpg", ".jpeg", ".webp"];
const accepted = new Set((limits.accept || "").split(","));

function extensionOf(name) {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot).toLowerCase();
}

function kindOf(name) {
  const extension = extensionOf(name);
  if (IMAGE_EXT.includes(extension)) return "image";
  return extension === ".pdf" ? "pdf" : "text";
}

function sizeLimit(name) {
  const kind = kindOf(name);
  return kind === "image" ? limits.max_image_bytes : kind === "pdf" ? limits.max_document_bytes : limits.max_text_bytes;
}

function setFiles(files) {
  state.files = files;
  ui.chips.replaceChildren(...files.map((file, index) => el("li", { class: "file-chip" },
    el("span", { class: "file-chip-kind", text: kindLabel(kindOf(file.name)) }),
    el("span", { class: "truncate", text: file.name }),
    el("span", { class: "faint", text: formatBytes(file.size) }),
    iconButton("close", t("chat_remove_file", { name: file.name }), () => {
      setFiles(state.files.filter((_, position) => position !== index));
      ui.input.focus();
    }))));
  ui.chips.hidden = files.length === 0;
}

function addFiles(list) {
  if (session.incognito) {
    toast(t("chat_no_history_files"), "warning");
    return;
  }
  const next = [...state.files];
  for (const file of list) {
    if (next.length >= limits.max_files) {
      toast(t("chat_file_count", { count: limits.max_files }), "error");
      break;
    }
    if (!accepted.has(extensionOf(file.name))) toast(t("chat_file_type", { name: file.name }), "error");
    else if (file.size > sizeLimit(file.name)) toast(t("chat_file_too_large", { name: file.name, size: formatBytes(sizeLimit(file.name)) }), "error");
    else next.push(file);
  }
  setFiles(next);
  if (next.length) announce(t("chat_files_attached", { count: next.length }));
}

if (ui.attach) {
  ui.attach.addEventListener("click", () => ui.fileInput.click());
  ui.fileInput.addEventListener("change", () => {
    addFiles(ui.fileInput.files);
    ui.fileInput.value = "";
    ui.input.focus();
  });
}

ui.input.addEventListener("paste", (event) => {
  const files = [...(event.clipboardData?.files || [])];
  if (!files.length) return;
  event.preventDefault();
  addFiles(files);
});

{
  const main = ui.form.closest(".chat-main");
  let depth = 0;
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  main.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth += 1;
    ui.dropOverlay.hidden = false;
  });
  main.addEventListener("dragover", (event) => { if (hasFiles(event)) event.preventDefault(); });
  main.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) ui.dropOverlay.hidden = true;
  });
  main.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    ui.dropOverlay.hidden = true;
    addFiles(event.dataTransfer.files);
  });
}

// ----- model picker -----------------------------------------------------------------------

const picker = {
  button: $("model-button"),
  label: $("model-button-label"),
  popover: $("model-popover"),
  search: $("model-search"),
  list: $("model-list"),
  models: [{ name: "auto", label: t("chat_model_auto"), description: t("chat_model_auto_hint"), categories: [], reasoning: false, vision: false },
    ...data.models],
  selected: "auto",
  visible: [],
  active: 0,

  init() {
    const known = (name) => name && this.models.some((model) => model.name === name);
    const remembered = storage.getItem(MODEL_KEY);
    this.selected = known(remembered) ? remembered : known(session.last_model) ? session.last_model : "auto";
    this.updateButton();
    this.button.addEventListener("click", () => (this.popover.hidden ? this.open() : this.close(true)));
    this.search.addEventListener("input", () => { this.active = 0; this.render(); });
    this.search.addEventListener("keydown", (event) => this.onKey(event));
    document.addEventListener("pointerdown", (event) => {
      if (!this.popover.hidden && !event.target.closest("#model-picker")) this.close(false);
    });
  },

  updateButton() {
    const model = this.models.find((item) => item.name === this.selected) || this.models[0];
    this.label.textContent = model.label;
    if (this.onChange) this.onChange(model);
  },
  onChange: null,

  open() {
    this.popover.hidden = false;
    this.button.setAttribute("aria-expanded", "true");
    this.search.value = "";
    this.active = Math.max(0, this.models.findIndex((model) => model.name === this.selected));
    this.render();
    this.search.focus();
  },

  close(focusButton) {
    this.popover.hidden = true;
    this.button.setAttribute("aria-expanded", "false");
    if (focusButton) this.button.focus();
  },

  render() {
    const query = this.search.value.trim().toLowerCase();
    this.visible = this.models.filter((model) => !query || [model.label, model.name, model.description, ...model.categories]
      .some((text) => (text || "").toLowerCase().includes(query)));
    this.active = Math.min(this.active, Math.max(0, this.visible.length - 1));
    if (!this.visible.length) {
      this.list.replaceChildren(el("li", { class: "model-empty", role: "presentation", text: t("chat_model_none") }));
      this.search.removeAttribute("aria-activedescendant");
      return;
    }
    this.list.replaceChildren(...this.visible.map((model, index) => {
      const badges = el("span", { class: "model-badges" },
        ...model.categories.map((name) => el("span", { class: "badge", text: name })),
        model.reasoning ? el("span", { class: "badge badge-info", text: t("chat_badge_reasoning") }) : null,
        model.vision ? el("span", { class: "badge badge-success", text: t("chat_badge_vision") }) : null,
        model.weight && model.weight !== 1
          ? el("span", { class: `badge${model.weight > 1 ? " badge-warning" : ""}`, text: t("chat_badge_weight", { factor: model.weight.toLocaleString(document.documentElement.lang || undefined) }) })
          : null);
      const option = el("li", {
        id: `model-option-${index}`, role: "option", class: `model-option${index === this.active ? " is-active" : ""}`,
        "aria-selected": model.name === this.selected ? "true" : "false",
      }, el("span", { class: "model-option-head" }, el("span", { class: "model-option-name", text: model.label }), badges),
      model.description ? el("span", { class: "model-option-description", text: model.description }) : null);
      option.addEventListener("pointerdown", (event) => event.preventDefault());
      option.addEventListener("click", () => this.choose(model));
      return option;
    }));
    this.search.setAttribute("aria-activedescendant", `model-option-${this.active}`);
    this.list.children[this.active]?.scrollIntoView({ block: "nearest" });
  },

  onKey(event) {
    const count = this.visible.length;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!count) return;
      this.active = (this.active + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
      this.render();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      this.active = event.key === "Home" ? 0 : Math.max(0, count - 1);
      this.render();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (this.visible[this.active]) this.choose(this.visible[this.active]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.close(true);
    } else if (event.key === "Tab") {
      this.close(false);
    }
  },

  choose(model) {
    this.selected = model.name;
    storage.setItem(MODEL_KEY, model.name);
    this.updateButton();
    this.close(false);
    announce(t("chat_model_selected", { name: model.label }));
    ui.input.focus();
  },

  // Used by the personality picker: select a model for this chat without remembering it for others.
  has(name) { return this.models.some((model) => model.name === name); },
  labelOf(name) { return (this.models.find((model) => model.name === name) || {}).label || name; },
  use(name) {
    if (!this.has(name)) return;
    this.selected = name;
    this.updateButton();
  },
};
picker.init();

// ----- reasoning effort -----------------------------------------------------------------------
// The chosen model's levels (lowest first). Locked levels show a lock; choosing one opens the
// account page's request form, pre-filled. The choice is remembered per model.

const EFFORT_KEY = "bc-chat-effort";
const effortPicker = {
  root: $("effort-picker"),
  button: $("effort-button"),
  label: $("effort-button-label"),
  popover: $("effort-popover"),
  list: $("effort-list"),
  effort: null,
  model: null,
  selected: null,
  active: 0,

  init() {
    if (!this.root) return;
    this.button.addEventListener("click", () => (this.popover.hidden ? this.open() : this.close(true)));
    this.list.addEventListener("keydown", (event) => this.onKey(event));
    document.addEventListener("pointerdown", (event) => {
      if (!this.popover.hidden && !event.target.closest("#effort-picker")) this.close(false);
    });
    this.update(picker.models.find((item) => item.name === picker.selected));
  },

  remembered() {
    try { return JSON.parse(storage.getItem(EFFORT_KEY) || "{}") || {}; } catch { return {}; }
  },

  /** The model changed: offer its levels (hidden for "auto" and models that do not reason). */
  update(model) {
    if (!this.root) return;
    this.close(false);
    this.model = model || null;
    this.effort = model?.effort || null;
    this.root.hidden = !this.effort;
    if (!this.effort) {
      this.selected = null;
      return;
    }
    const allowed = this.effort.levels.filter((level) => level.allowed).map((level) => level.value);
    const saved = this.remembered()[model.name];
    this.selected = allowed.includes(saved) ? saved : this.effort.default;
    this.updateButton();
  },

  levelOf(value) { return this.effort?.levels.find((level) => level.value === value); },

  updateButton() {
    const level = this.levelOf(this.selected);
    this.label.textContent = level ? level.label : "";
    this.button.setAttribute("aria-label", t("chat_effort_button", { level: level ? level.label : "" }));
    this.button.title = t("chat_effort_button", { level: level ? level.label : "" });
  },

  open() {
    this.popover.hidden = false;
    this.button.setAttribute("aria-expanded", "true");
    this.active = Math.max(0, this.effort.levels.findIndex((level) => level.value === this.selected));
    this.render();
    this.list.focus();
  },

  close(focusButton) {
    if (!this.popover || this.popover.hidden) return;
    this.popover.hidden = true;
    this.button.setAttribute("aria-expanded", "false");
    if (focusButton) this.button.focus();
  },

  render() {
    this.list.replaceChildren(...this.effort.levels.map((level, index) => {
      const locked = !level.allowed;
      const option = el("li", {
        id: `effort-option-${index}`, role: "option",
        class: `model-option effort-option${index === this.active ? " is-active" : ""}${locked ? " is-locked" : ""}`,
        // A locked level stays operable: choosing it opens the request form.
        "aria-selected": level.value === this.selected ? "true" : "false",
      }, el("span", { class: "model-option-name", text: level.label }),
      locked ? el("span", { class: "effort-lock" }, icon("lock"), el("span", { text: t("chat_effort_request") })) : null);
      option.addEventListener("pointerdown", (event) => event.preventDefault());
      option.addEventListener("click", () => this.choose(level));
      return option;
    }));
    this.list.setAttribute("aria-activedescendant", `effort-option-${this.active}`);
  },

  onKey(event) {
    const count = this.effort ? this.effort.levels.length : 0;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!count) return;
      this.active = (this.active + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
      this.render();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      this.active = event.key === "Home" ? 0 : Math.max(0, count - 1);
      this.render();
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (this.effort.levels[this.active]) this.choose(this.effort.levels[this.active]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.close(true);
    } else if (event.key === "Tab") {
      this.close(false);
    }
  },

  choose(level) {
    if (!level.allowed) {
      // A locked level: ask for it on the account page (the form opens pre-filled).
      window.location.href = level.request_url;
      return;
    }
    this.selected = level.value;
    const saved = this.remembered();
    saved[this.model.name] = level.value;
    storage.setItem(EFFORT_KEY, JSON.stringify(saved));
    this.updateButton();
    this.close(true);
    announce(t("chat_effort_selected", { level: level.label }));
  },
};
picker.onChange = (model) => effortPicker.update(model);
effortPicker.init();

// ----- parameters and personality ------------------------------------------------------------

{
  const panel = $("params-panel");
  const toggle = $("params-toggle");
  const inputs = [...document.querySelectorAll("[data-param]")];
  let saved = {};
  try { saved = JSON.parse(storage.getItem(PARAMS_KEY) || "{}") || {}; } catch { saved = {}; }
  for (const input of inputs) if (saved[input.name] !== undefined) input.value = saved[input.name];
  const persist = () => storage.setItem(PARAMS_KEY, JSON.stringify(Object.fromEntries(
    inputs.filter((input) => input.value.trim() !== "").map((input) => [input.name, input.value.trim()]))));
  for (const input of inputs) input.addEventListener("change", persist);
  toggle.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
    toggle.classList.toggle("is-active", !panel.hidden);
    if (!panel.hidden) inputs[0]?.focus();
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.classList.remove("is-active");
    toggle.focus();
  });
  $("params-reset").addEventListener("click", () => {
    for (const input of inputs) input.value = "";
    persist();
    announce(t("chat_params_reset_done"));
  });
}

// ----- voice input ------------------------------------------------------------------------

{
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const button = $("voice-button");
  if (Recognition && button) {
    button.hidden = false;
    let recognition = null;
    const setListening = (active) => {
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.classList.toggle("is-active", active);
      const label = active ? t("chat_voice_stop") : t("chat_voice_start");
      button.setAttribute("aria-label", label);
      button.title = label;
    };
    button.addEventListener("click", () => {
      if (recognition) { recognition.stop(); return; }
      stopSpeaking();
      recognition = new Recognition();
      recognition.lang = boot.lang === "it" ? "it-IT" : "en-US";
      recognition.interimResults = true;
      recognition.continuous = false;
      const base = ui.input.value.trimEnd();
      recognition.onstart = () => { setListening(true); announce(t("chat_voice_listening")); };
      recognition.onresult = (event) => {
        const transcript = [...event.results].map((result) => result[0].transcript).join("");
        ui.input.value = base + (base && transcript ? " " : "") + transcript;
        autoGrow();
      };
      recognition.onerror = (event) => {
        const denied = event.error === "not-allowed" || event.error === "service-not-allowed";
        toast(denied ? t("chat_voice_denied") : t("chat_voice_error"), "error");
      };
      recognition.onend = () => { recognition = null; setListening(false); ui.input.focus(); };
      try { recognition.start(); } catch { recognition = null; setListening(false); toast(t("chat_voice_error"), "error"); }
    });
    window.addEventListener("pagehide", () => recognition?.stop());
  }
}
window.addEventListener("pagehide", stopSpeaking);

// ----- popup menus (sidebar items and the chat header) -------------------------------------

const menu = {
  owner: null,

  open(button, items) {
    this.close(false);
    this.owner = button;
    ui.menu.replaceChildren(...items.map((item) => {
      const entry = el("button", { type: "button", role: "menuitem", tabindex: "-1", class: item.danger ? "danger" : null },
        icon(item.icon), el("span", { text: item.label }));
      entry.addEventListener("click", () => { this.close(true); item.action(); });
      return entry;
    }));
    ui.menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    const rect = button.getBoundingClientRect();
    const width = ui.menu.offsetWidth;
    const height = ui.menu.offsetHeight;
    const top = rect.bottom + 4 + height > window.innerHeight ? Math.max(8, rect.top - height - 4) : rect.bottom + 4;
    ui.menu.style.top = `${top}px`;
    ui.menu.style.left = `${Math.min(Math.max(8, rect.right - width), window.innerWidth - width - 8)}px`;
    ui.menu.firstElementChild?.focus();
  },

  close(focusOwner) {
    if (ui.menu.hidden) return;
    ui.menu.hidden = true;
    this.owner?.setAttribute("aria-expanded", "false");
    if (focusOwner) this.owner?.focus();
    this.owner = null;
  },
};

ui.menu.addEventListener("keydown", (event) => {
  const items = [...ui.menu.querySelectorAll("[role=menuitem]")];
  const index = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(index + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
  } else if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    items[event.key === "Home" ? 0 : items.length - 1]?.focus();
  } else if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    menu.close(true);
  } else if (event.key === "Tab") {
    menu.close(false);
  }
});
document.addEventListener("pointerdown", (event) => {
  if (!ui.menu.hidden && !ui.menu.contains(event.target) && !menu.owner?.contains(event.target)) menu.close(false);
});
window.addEventListener("resize", () => menu.close(false));
ui.scroller.addEventListener("scroll", () => menu.close(false), { passive: true });

// ----- chat actions ----------------------------------------------------------------------

function deleteMessage() {
  if (data.retention_days > 0) return t("chat_delete_message", { count: data.retention_days });
  return t("chat_delete_message_now");
}

async function renameChat(id, current) {
  const value = await promptDialog({ title: t("chat_rename_title"), label: t("chat_rename_label"), value: current, maxLength: 100 });
  if (value === null || !value.trim() || value.trim() === current) return;
  try {
    const result = await api(chatUrl(id, "title"), { json: { title: value } });
    if (id === session.id) setTitle(result.title);
    for (const item of sessionItems(id)) updateSidebarItem(item, result.title);
    announce(t("chat_renamed"));
  } catch (error) {
    toast(error.message, "error");
  }
}

async function shareChat(id) {
  try {
    const result = await api(chatUrl(id, "share"), { json: { action: "create" } });
    if (id === session.id) session.shared_url = result.url;
    for (const item of sessionItems(id)) item.dataset.shared = "1";
    secretDialog({ title: t("chat_share_title"), message: t("chat_share_message"), secret: result.url, note: t("chat_share_note") });
  } catch (error) {
    toast(error.message, "error");
  }
}

async function unshareChat(id) {
  const ok = await confirmDialog({ title: t("chat_unshare_title"), message: t("chat_unshare_message"), confirmLabel: t("chat_unshare"), danger: true });
  if (!ok) return;
  try {
    await api(chatUrl(id, "share"), { json: { action: "revoke" } });
    if (id === session.id) session.shared_url = null;
    for (const item of sessionItems(id)) item.dataset.shared = "0";
    toast(t("chat_unshared"), "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteChat(id, title) {
  const ok = await confirmDialog({ title: t("chat_delete_title", { title }), message: deleteMessage(), confirmLabel: t("chat_delete"), danger: true });
  if (!ok) return;
  try {
    await api(chatUrl(id, "delete"), { method: "POST" });
    if (id === session.id) {
      window.location.assign(data.urls.root);
      return;
    }
    for (const node of sessionItems(id)) node.remove();
    refreshSidebarEmpty();
    announce(t("chat_deleted"));
    ui.input.focus();
  } catch (error) {
    toast(error.message, "error");
  }
}

function chatMenuItems(id, title, shared) {
  const items = [{ icon: "edit", label: t("chat_rename"), action: () => renameChat(id, title) }];
  if (!(id === session.id && session.incognito)) {
    items.push({ icon: "share", label: shared ? t("chat_copy_share_link") : t("chat_share"), action: () => shareChat(id) });
    if (shared) items.push({ icon: "close", label: t("chat_unshare"), action: () => unshareChat(id) });
  }
  items.push({ icon: "download", label: t("chat_download"), action: () => window.location.assign(chatUrl(id, "download")) });
  if (id === session.id && session.incognito) {
    items.push({ icon: "trash", label: t("chat_end"), danger: true, action: () => document.querySelector(".chat-header form[data-confirm]")?.requestSubmit() });
  } else {
    items.push({ icon: "trash", label: t("chat_delete"), danger: true, action: () => deleteChat(id, title) });
  }
  return items;
}

$("chat-menu-button").addEventListener("click", (event) => {
  const button = event.currentTarget;
  if (menu.owner === button) { menu.close(true); return; }
  menu.open(button, chatMenuItems(session.id, session.title, Boolean(session.shared_url)));
});
$("share-button")?.addEventListener("click", () => {
  if (!ui.messages.querySelector(".message[data-id]")) {
    toast(t("chat_share_empty"), "warning");
    return;
  }
  shareChat(session.id);
});

// ----- sidebar -------------------------------------------------------------------------------

const sidebar = {
  list: $("session-list"),
  results: $("search-results"),
  searchEmpty: $("search-empty"),
  empty: $("sidebar-empty"),
  more: $("sidebar-more"),
  status: $("sidebar-status"),
  search: $("chat-search"),
};

function sidebarItem(id) {
  return sidebar.list.querySelector(`.session-item[data-session-id="${CSS.escape(id)}"]`);
}

/** Every entry of a chat: in the list and in the search results. */
function sessionItems(id) {
  return document.querySelectorAll(`.session-item[data-session-id="${CSS.escape(id)}"]`);
}

function updateSidebarItem(item, title) {
  item.dataset.title = title;
  item.querySelector(".session-link .truncate").textContent = title;
  item.querySelector(".session-menu-button").setAttribute("aria-label", t("chat_options_for", { title }));
}

function buildSidebarItem(entry) {
  const current = entry.id === session.id;
  const link = el("a", { class: "session-link", href: entry.url, "aria-current": current ? "page" : null },
    el("span", { class: "truncate", text: entry.title }));
  if (entry.snippet) link.append(el("span", { class: "session-snippet truncate", text: entry.snippet.replace(/\s+/g, " ").trim() }));
  return el("li", { class: `session-item${current ? " is-current" : ""}`, dataset: { sessionId: entry.id, title: entry.title, shared: entry.shared ? "1" : "0" } },
    link, el("button", { type: "button", class: "icon-btn session-menu-button", "aria-haspopup": "menu", "aria-expanded": "false",
      "aria-label": t("chat_options_for", { title: entry.title }) }, icon("more")));
}

function refreshSidebarEmpty() {
  sidebar.empty.hidden = sidebar.list.querySelector(".session-item") !== null;
}

function upsertCurrentInSidebar() {
  let item = sidebarItem(session.id);
  if (!item) item = buildSidebarItem({ id: session.id, title: session.title, url: window.location.pathname, shared: Boolean(session.shared_url) });
  else updateSidebarItem(item, session.title);
  sidebar.list.prepend(item);
  refreshSidebarEmpty();
}

for (const list of [sidebar.list, sidebar.results]) {
  list.addEventListener("click", (event) => {
    const button = event.target.closest(".session-menu-button");
    if (!button) return;
    if (menu.owner === button) { menu.close(true); return; }
    const item = button.closest(".session-item");
    menu.open(button, chatMenuItems(item.dataset.sessionId, item.dataset.title, item.dataset.shared === "1"));
  });
}

sidebar.more.hidden = !state.sidebarNext;
sidebar.more.addEventListener("click", async () => {
  sidebar.more.disabled = true;
  try {
    const result = await api(`${data.urls.sessions}?before=${encodeURIComponent(state.sidebarNext)}`);
    for (const entry of result.sessions) if (!sidebarItem(entry.id)) sidebar.list.append(buildSidebarItem(entry));
    state.sidebarNext = result.next;
    sidebar.more.hidden = !result.next;
    sidebar.status.textContent = t("chat_more_loaded", { count: result.sessions.length });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    sidebar.more.disabled = false;
  }
});

let searchSerial = 0;
const runSearch = debounce(async (query) => {
  const serial = ++searchSerial;
  if (query.length < 2) {
    sidebar.results.hidden = true;
    sidebar.searchEmpty.hidden = true;
    sidebar.list.hidden = false;
    refreshSidebarEmpty();
    sidebar.more.hidden = !state.sidebarNext;
    return;
  }
  try {
    const result = await api(`${data.urls.search}?q=${encodeURIComponent(query)}`);
    if (serial !== searchSerial) return;
    sidebar.results.replaceChildren(...result.results.map(buildSidebarItem));
    sidebar.results.hidden = false;
    sidebar.list.hidden = true;
    sidebar.empty.hidden = true;
    sidebar.more.hidden = true;
    sidebar.searchEmpty.hidden = result.results.length > 0;
    sidebar.status.textContent = t("chat_search_results", { count: result.results.length });
  } catch (error) {
    if (serial === searchSerial) toast(error.message, "error");
  }
}, 250);
sidebar.search.addEventListener("input", () => runSearch(sidebar.search.value.trim()));
sidebar.search.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && sidebar.search.value) {
    event.stopPropagation();
    sidebar.search.value = "";
    runSearch("");
  }
});

// Collapsible on wide screens, a drawer on narrow ones.
{
  const narrow = window.matchMedia("(max-width: 900px)");
  const main = ui.form.closest(".chat-main");
  const setExpanded = (expanded) => ui.sidebarToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  const closeDrawer = (focus) => {
    ui.shell.classList.remove("sidebar-open");
    ui.backdrop.hidden = true;
    main.inert = false;
    setExpanded(false);
    if (focus) ui.sidebarToggle.focus();
  };
  const apply = () => {
    if (narrow.matches) {
      closeDrawer(false);
    } else {
      main.inert = false;
      ui.backdrop.hidden = true;
      ui.shell.classList.remove("sidebar-open");
      setExpanded(!ui.shell.classList.contains("sidebar-collapsed"));
    }
  };
  if (storage.getItem(SIDEBAR_KEY) === "1") ui.shell.classList.add("sidebar-collapsed");
  apply();
  narrow.addEventListener("change", apply);
  ui.sidebarToggle.addEventListener("click", () => {
    if (narrow.matches) {
      if (ui.shell.classList.contains("sidebar-open")) { closeDrawer(true); return; }
      ui.shell.classList.add("sidebar-open");
      ui.backdrop.hidden = false;
      main.inert = true;
      setExpanded(true);
      sidebar.search.focus();
    } else {
      const collapsed = ui.shell.classList.toggle("sidebar-collapsed");
      storage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0");
      setExpanded(!collapsed);
    }
  });
  ui.backdrop.addEventListener("click", () => closeDrawer(true));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && narrow.matches && ui.shell.classList.contains("sidebar-open")) closeDrawer(true);
  });
}

// ----- history paging and suggestions ----------------------------------------------------------

ui.loadEarlier.addEventListener("click", async () => {
  ui.loadEarlier.disabled = true;
  try {
    const result = await api(`${data.urls.messages}?before=${state.oldestId}`);
    const height = ui.scroller.scrollHeight;
    const nodes = result.messages.map((message) => { track(message); return buildMessage(message); });
    ui.messages.prepend(...nodes);
    ui.scroller.scrollTop += ui.scroller.scrollHeight - height;
    state.hasMore = result.has_more;
    ui.loadEarlier.hidden = !result.has_more;
    nodes[nodes.length - 1]?.setAttribute("tabindex", "-1");
    announce(t("chat_earlier_loaded", { count: nodes.length }));
  } catch (error) {
    toast(error.message, "error");
  } finally {
    ui.loadEarlier.disabled = false;
  }
});

for (const button of document.querySelectorAll("[data-suggestion]")) {
  button.addEventListener("click", () => {
    ui.input.value = button.dataset.suggestion;
    autoGrow();
    ui.input.focus();
  });
}

// ----- start-up ------------------------------------------------------------------------------

enhanceMarkdown(ui.messages, labels);
for (const message of data.messages) appendMessage(message);
ui.loadEarlier.hidden = !state.hasMore;
// After the messages, so the preferred model is only chosen for a chat that has none.
initPersonas({
  personalities: data.personalities,
  session,
  chatUrl,
  models: picker,
  isEmpty: () => ui.messages.childElementCount === 0 && !state.generating && !data.run.generating,
  fillInput: (text) => { ui.input.value = text; autoGrow(); ui.input.focus(); },
  announce,
  manageUrl: data.urls.personalities,
});
updateEmpty();
scrollToEnd(true);
autoGrow();

if (data.run.generating) {
  // Reloaded while an answer is being written: show what is saved so far and follow it.
  setGenerating(true);
  const pending = new PendingAnswer();
  if (data.run.partial) pending.showPartial(data.run.partial);
  if (data.run.stopping) ui.stop.disabled = true;
  poll(pending, state.lastId);
}
ui.input.focus({ preventScroll: true });
