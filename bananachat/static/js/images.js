// Images page: generate one image with progress reporting (server-sent events).
import { ApiError, checkStatusSoon, onStatusChange, pageData, readEventStream, requestHeaders, setBusy, t, toast } from "./core.js";

const data = pageData();
const $ = (id) => document.getElementById(id);
const form = $("img-form");
const prompt = $("img-prompt");
const generateButton = $("img-generate");
const cancelButton = $("img-cancel");
const statusLine = $("img-status");
const progress = $("img-progress");
const elapsed = $("img-elapsed");
const errorBox = $("img-error");
const figure = $("img-figure");
const output = $("img-output");
const caption = $("img-caption");
const actions = $("img-actions");
const download = $("img-download");

const pausedNote = $("img-paused");
const PAUSE_CODES = new Set(["maintenance", "outage"]);
let canSend = true;
let controller = null;
let ticker = 0;
let objectUrl = null;
let phase = { key: "", params: {}, since: 0 };

function setPhase(key, params = {}) {
  phase = { key, params, since: phase.key === key ? phase.since : Date.now() };
  statusLine.textContent = t(key, params);
  tick();
}

// Elapsed time lives outside the live region, so it is not re-announced every second.
function tick() {
  const seconds = phase.key ? Math.floor((Date.now() - phase.since) / 1000) : 0;
  elapsed.textContent = seconds >= 2 ? t("images_seconds", { count: seconds }) : "";
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  statusLine.textContent = t("images_failed");
}

function reset() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function base64ToBlob(b64, type) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Blob([bytes], { type });
}

function showImage(image, promptText) {
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(base64ToBlob(image.b64_json, image.mime_type));
  output.src = objectUrl;
  output.width = image.width;
  output.height = image.height;
  output.alt = t("images_alt", { prompt: promptText.slice(0, 200) });
  caption.textContent = t("images_caption", { model: image.model.name, width: image.width, height: image.height,
    seconds: Math.max(1, Math.round(image.duration_ms / 1000)), tokens: Math.round(image.tokens || 0).toLocaleString(document.documentElement.lang || undefined) });
  const extension = image.mime_type.split("/")[1] === "jpeg" ? "jpg" : image.mime_type.split("/")[1];
  download.href = objectUrl;
  download.download = `bananachat-${image.created}.${extension}`;
  figure.hidden = false;
  actions.hidden = false;
  statusLine.textContent = t("images_ready");
}

async function responseError(response) {
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

async function generate() {
  if (controller || !canSend) return;
  const text = prompt.value.trim();
  if (!text) {
    prompt.setAttribute("aria-invalid", "true");
    toast(t("images_prompt_required"), "error");
    prompt.focus();
    return;
  }
  prompt.removeAttribute("aria-invalid");
  reset();
  const body = { prompt: text, model: form.elements.model.value, size: form.elements.size.value };
  controller = new AbortController();
  setBusy(generateButton, true);
  cancelButton.hidden = false;
  progress.hidden = false;
  progress.removeAttribute("value");
  setPhase("images_sending");
  ticker = setInterval(tick, 1000);
  let finished = false;
  try {
    const response = await fetch(data.generateUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: requestHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) throw await responseError(response);
    await readEventStream(response, (event) => {
      if (event.type === "queued") {
        setPhase(event.position > 0 ? "images_queued" : "images_starting", { position: event.position });
      } else if (event.type === "started") {
        setPhase("images_generating");
      } else if (event.type === "progress") {
        if (event.state === "pending" && phase.key !== "images_waiting_gpu") setPhase("images_waiting_gpu");
        else if (event.state === "running" && phase.key !== "images_generating") setPhase("images_generating");
      } else if (event.type === "done") {
        finished = true;
        showImage(event.image, text);
      } else if (event.type === "error") {
        finished = true;
        showError(event.message);
      }
    });
    if (!finished) showError(t("images_interrupted"));
  } catch (error) {
    if (error.name === "AbortError") statusLine.textContent = t("images_cancelled");
    else showError(error instanceof ApiError ? error.message : t("error_network"));
  } finally {
    clearInterval(ticker);
    phase = { key: "", params: {}, since: 0 };
    tick();
    controller = null;
    progress.hidden = true;
    cancelButton.hidden = true;
    setBusy(generateButton, false);
    generateButton.disabled = !canSend;
    generateButton.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  generate();
});
prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    generate();
  }
});
cancelButton.addEventListener("click", () => controller?.abort());
// Maintenance or an AI-server outage pauses generation; the page follows the live status.
onStatusChange((state) => {
  canSend = state.canSend;
  if (!controller) generateButton.disabled = !canSend;
  pausedNote.textContent = canSend ? "" : t("status_paused");
});
