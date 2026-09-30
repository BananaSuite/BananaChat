// API playground: a multi-turn conversation streamed from /developer/playground/send.
//
// The request body uses the same format as POST /v1/chat/completions, so the
// "as an API request" panel shows exactly what a client would send.
import { ApiError, checkStatusSoon, confirmDialog, copyText, el, onStatusChange, pageData, readEventStream, requestHeaders, t, toast } from "./core.js";
import { enhanceMarkdown, renderMarkdown } from "./markdown.js";

const data = pageData();
const $ = (id) => document.getElementById(id);
const settings = $("pg-settings");
const log = $("pg-log");
const empty = $("pg-empty");
const statusLine = $("pg-status");
const composer = $("pg-composer");
const input = $("pg-input");
const roleSelect = $("pg-role");
const sendButton = $("pg-send");
const stopButton = $("pg-stop-button");
const addButton = $("pg-add");
const requestCode = $("pg-request");
const pausedNote = $("pg-paused");
const PAUSE_CODES = new Set(["maintenance", "outage"]);
let canSend = true;

const labels = { copy: t("copy"), copied: t("copied"), download: t("developer_download"), code: t("developer_code"),
  reasoning: t("developer_reasoning"), reasoningLive: t("developer_reasoning_live") };
const conversation = []; // { role, content, element }
let controller = null;

enhanceMarkdown(log, labels);

// ----- conversation ---------------------------------------------------------------
function roleLabel(role) {
  return role === "assistant" ? t("developer_role_assistant") : t("developer_role_user");
}

function renderBody(body, role, text) {
  if (role === "assistant") {
    body.className = "prose dev-msg-body";
    body.innerHTML = renderMarkdown(text, { labels }); // the renderer escapes all source text
  } else {
    body.className = "dev-msg-body dev-msg-plain";
    body.textContent = text;
  }
}

function addMessage(role, content) {
  const body = el("div");
  renderBody(body, role, content);
  const remove = el("button", { type: "button", class: "icon-btn dev-msg-remove", "aria-label": t("developer_remove_message", { role: roleLabel(role) }), text: "×" });
  const element = el("article", { class: `dev-msg dev-msg-${role}` },
    el("header", { class: "dev-msg-header" }, el("span", { class: "dev-msg-role", text: roleLabel(role) }), remove),
    body);
  const entry = { role, content, element, body };
  remove.addEventListener("click", () => {
    if (controller) return;
    const index = conversation.indexOf(entry);
    if (index >= 0) conversation.splice(index, 1);
    element.remove();
    refresh();
  });
  conversation.push(entry);
  log.append(element);
  refresh();
  element.scrollIntoView({ block: "end", behavior: "smooth" });
  return entry;
}

function refresh() {
  empty.hidden = conversation.length > 0;
  updateRequestPreview();
}

// ----- request ----------------------------------------------------------------------
function numberField(name) {
  const field = settings.elements[name];
  if (!field.value.trim()) return undefined;
  if (!field.checkValidity()) throw new FieldError(field);
  const value = Number(field.value);
  return Number.isFinite(value) ? value : undefined;
}

class FieldError extends Error {
  constructor(field) {
    super(t("developer_invalid_field", { name: field.labels?.[0]?.textContent || field.name }));
    this.field = field;
  }
}

function buildBody({ validate = true } = {}) {
  const body = { model: settings.elements.model.value, messages: [], stream: true };
  const system = settings.elements.system.value.trim();
  if (system) body.messages.push({ role: "system", content: system });
  for (const message of conversation) body.messages.push({ role: message.role, content: message.content });
  const read = (name) => {
    try { return numberField(name); } catch (error) { if (validate) throw error; return undefined; }
  };
  for (const name of ["temperature", "top_p", "max_tokens", "seed"]) {
    const value = read(name);
    if (value !== undefined) body[name] = value;
  }
  const stop = settings.elements.stop.value.split("\n").map((line) => line.trim()).filter(Boolean);
  if (stop.length > 4) {
    if (validate) throw new FieldError(settings.elements.stop);
  } else if (stop.length) {
    body.stop = stop;
  }
  if (settings.elements.reasoning_effort.value) body.reasoning_effort = settings.elements.reasoning_effort.value;
  return body;
}

// ----- reasoning effort -------------------------------------------------------------
// The API's reasoning_effort values for each level ("on" models think at medium).
const EFFORT_VALUES = { off: "none", on: "medium", low: "low", medium: "medium", high: "high", max: "max" };

/** Offer the chosen model's effort levels; locked ones stay visible but cannot be picked. */
function syncEffort() {
  const select = settings.elements.reasoning_effort;
  if (!select) return;
  const model = (data.models || []).find((item) => item.id === settings.elements.model.value);
  const effort = model?.effort;
  const previous = select.value;
  const options = [el("option", { value: "", text: t("developer_effort_default") })];
  for (const level of effort?.levels || []) {
    options.push(el("option", { value: EFFORT_VALUES[level.value] || level.value, disabled: !level.allowed,
      text: level.allowed ? level.label : t("effort_locked_option", { level: level.label }) }));
  }
  select.replaceChildren(...options);
  select.disabled = !effort;
  if ([...select.options].some((option) => option.value === previous && !option.disabled)) select.value = previous;
}

function curlFor(body) {
  const payload = JSON.stringify({ ...body, stream: false }, null, 2).replace(/'/g, "'\\''");
  return `curl ${data.apiBase}/chat/completions \\\n  -H "Authorization: Bearer $BANANACHAT_API_KEY" \\\n`
    + `  -H "Content-Type: application/json" \\\n  -d '${payload}'`;
}

function updateRequestPreview() {
  requestCode.textContent = curlFor(buildBody({ validate: false }));
}

// ----- streaming --------------------------------------------------------------------
function setStreaming(active) {
  sendButton.hidden = active;
  sendButton.disabled = !canSend;
  addButton.disabled = active;
  stopButton.hidden = !active;
  log.setAttribute("aria-busy", active ? "true" : "false");
  if (active) stopButton.focus();
  else if (document.activeElement === stopButton || document.activeElement === document.body) input.focus();
}

function setStatus(text) {
  statusLine.textContent = text;
}

async function errorFrom(response) {
  let message = t("error_request");
  let code = "error";
  try {
    const payload = await response.json();
    message = payload?.error?.message || message;
    code = payload?.error?.code || code;
  } catch { /* not JSON */ }
  if (response.status === 401) window.location.reload();
  if (response.status === 503 && PAUSE_CODES.has(code)) {
    // Worded for API clients; the page has its own translated text.
    message = t("status_paused");
    checkStatusSoon();
  }
  return new ApiError(message, response.status, code);
}

async function send() {
  if (controller || !canSend) return;
  let body;
  try {
    body = buildBody();
  } catch (error) {
    if (error instanceof FieldError) {
      toast(error.message, "error");
      error.field.focus();
      return;
    }
    throw error;
  }
  const text = input.value.trim();
  if (text) {
    addMessage("user", text);
    body.messages.push({ role: "user", content: text });
    input.value = "";
  }
  if (!conversation.length || conversation[conversation.length - 1].role !== "user") {
    toast(t("developer_need_user_message"), "warning");
    input.focus();
    return;
  }

  controller = new AbortController();
  setStreaming(true);
  setStatus(t("developer_sending"));
  const answer = addMessage("assistant", "");
  answer.element.classList.add("is-streaming");
  const reasoning = { text: "", details: null, body: null };
  let frame = 0;
  let finished = false;
  const render = () => {
    frame = 0;
    renderBody(answer.body, "assistant", answer.content);
    if (reasoning.body) reasoning.body.innerHTML = renderMarkdown(reasoning.text, { labels });
    answer.element.scrollIntoView({ block: "end" });
  };
  const schedule = () => { if (!frame) frame = requestAnimationFrame(render); };

  try {
    const response = await fetch(data.sendUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: requestHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) throw await errorFrom(response);
    await readEventStream(response, (event) => {
      if (event.type === "queued") {
        setStatus(event.position > 0 ? t("developer_queued", { position: event.position }) : t("developer_starting"));
      } else if (event.type === "started") {
        setStatus(t("developer_answering", { model: event.model.name }));
        answer.element.querySelector(".dev-msg-role").textContent = `${roleLabel("assistant")} · ${event.model.name}`;
        if (event.notice) toast(event.notice, "info");
      } else if (event.type === "reasoning") {
        if (!reasoning.details) {
          reasoning.body = el("div", { class: "reasoning-body prose" });
          reasoning.details = el("details", { class: "reasoning dev-reasoning" }, el("summary", { text: labels.reasoning }), reasoning.body);
          answer.element.insertBefore(reasoning.details, answer.body);
        }
        reasoning.text += event.text;
        schedule();
      } else if (event.type === "delta") {
        answer.content += event.text;
        schedule();
      } else if (event.type === "done") {
        finished = true;
        const usage = event.usage;
        const parts = [t("developer_usage_line", { prompt: usage.prompt_tokens, completion: usage.completion_tokens, counted: Math.round(Number(event.tokens_counted || 0)) })];
        if (usage.estimated) parts.push(t("developer_usage_estimated"));
        if (event.finish_reason === "length") parts.push(t("developer_finish_length"));
        answer.element.append(el("p", { class: "hint dev-msg-meta", text: parts.join(" · ") }));
        setStatus(t("developer_done"));
      } else if (event.type === "error") {
        finished = true;
        answer.element.append(el("p", { class: "alert alert-error dev-msg-error", role: "alert", text: event.message }));
        setStatus(event.message);
      }
    });
    if (!finished) {
      answer.element.append(el("p", { class: "alert alert-warning dev-msg-error", text: t("developer_interrupted") }));
      setStatus(t("developer_interrupted"));
    }
  } catch (error) {
    if (error.name === "AbortError") {
      answer.element.append(el("p", { class: "hint dev-msg-meta", text: t("developer_stopped") }));
      setStatus(t("developer_stopped"));
    } else {
      const message = error instanceof ApiError ? error.message : t("error_network");
      answer.element.append(el("p", { class: "alert alert-error dev-msg-error", role: "alert", text: message }));
      setStatus(message);
    }
  } finally {
    if (frame) cancelAnimationFrame(frame);
    render();
    answer.element.classList.remove("is-streaming");
    if (!answer.content && !reasoning.text) {
      // Nothing to keep in the conversation; the error stays visible until the next send.
      conversation.splice(conversation.indexOf(answer), 1);
      answer.element.querySelector(".dev-msg-remove")?.remove();
    }
    controller = null;
    setStreaming(false);
    refresh();
  }
}

// ----- wiring -----------------------------------------------------------------------
composer.addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    send();
  }
});
addButton.addEventListener("click", () => {
  const text = input.value.trim();
  if (!text) {
    input.focus();
    return;
  }
  addMessage(roleSelect.value, text);
  input.value = "";
  input.focus();
});
stopButton.addEventListener("click", () => controller?.abort());
$("pg-clear").addEventListener("click", async () => {
  if (!conversation.length || controller) return;
  if (!(await confirmDialog({ title: t("developer_clear_title"), message: t("developer_clear_text"), confirmLabel: t("developer_clear"), danger: true }))) return;
  for (const message of conversation.splice(0)) message.element.remove();
  for (const leftover of log.querySelectorAll(".dev-msg")) leftover.remove();
  setStatus("");
  refresh();
  input.focus();
});
$("pg-copy-request").addEventListener("click", async () => {
  if (await copyText(requestCode.textContent)) toast(t("copied"), "success", { timeout: 2000 });
});
settings.addEventListener("input", updateRequestPreview);
settings.addEventListener("change", (event) => {
  if (event.target === settings.elements.model) syncEffort();
  updateRequestPreview();
});
syncEffort();
settings.addEventListener("submit", (event) => event.preventDefault());
// Maintenance or an AI-server outage pauses sending; the page follows the live status.
onStatusChange((state) => {
  canSend = state.canSend;
  sendButton.disabled = !canSend;
  pausedNote.textContent = canSend ? "" : t("status_paused");
});
refresh();
