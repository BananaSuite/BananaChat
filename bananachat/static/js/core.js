// BananaChat core: shared helpers for every page (ES module).
//
// Pages import what they need:
//   import { api, t, toast, confirmDialog, el } from "./core.js";
// No inline scripts or styles are used anywhere (the CSP forbids them).

const bootElement = document.getElementById("bc-boot");
export const boot = bootElement ? JSON.parse(bootElement.textContent) : { strings: {}, csrf: "", lang: "en" };

// ----- translations -------------------------------------------------------
/** Translate a browser string (keys come from the "js.*" catalog entries). */
export function t(key, params = {}) {
  let text = boot.strings[key];
  if (text === undefined) return key;
  if (typeof text === "object") {
    text = (params.count === 1 ? text.one : text.other) ?? text.other ?? key;
  }
  return String(text).replace(/\{(\w+)\}/g, (match, name) => (name in params ? String(params[name]) : match));
}

// ----- DOM helpers --------------------------------------------------------
/** Build an element without innerHTML: el("button", {class: "btn", onclick: fn}, "Label"). */
export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name.startsWith("on") && typeof value === "function") node.addEventListener(name.slice(2), value);
    else if (name === "dataset") Object.assign(node.dataset, value);
    else if (name === "text") node.textContent = value;
    else node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Clone an inline SVG icon from a <template id="icon-NAME"> or return an empty span. */
export function icon(name) {
  const template = document.getElementById(`icon-${name}`);
  return template ? template.content.firstElementChild.cloneNode(true) : el("span");
}

export function pageData(id = "page-data") {
  const node = document.getElementById(id);
  return node ? JSON.parse(node.textContent) : {};
}

// ----- HTTP ---------------------------------------------------------------
export class ApiError extends Error {
  constructor(message, status, code, data) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

function errorMessage(data, status) {
  const error = data && data.error;
  if (typeof error === "string") return error;
  if (error && typeof error.message === "string") return error.message;
  if (status === 413) return t("error_too_large");
  if (status === 429) return t("error_rate_limited");
  if (status >= 500) return t("error_server");
  return t("error_request");
}

/** Headers for a same-origin request authenticated by the session cookie. */
export function requestHeaders(extra = {}) {
  return { "X-CSRF-Token": boot.csrf, "X-Requested-With": "fetch", Accept: "application/json", ...extra };
}

/**
 * Call a JSON endpoint. Options: method, json (object body), form (FormData), signal, raw (return Response).
 * Throws ApiError with a human-readable message on failure; a 401 sends the user to sign in.
 */
export async function api(url, { method, json, form, signal, raw = false, headers = {} } = {}) {
  const init = { method: method || (json || form ? "POST" : "GET"), headers: requestHeaders(headers), signal, credentials: "same-origin" };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form) {
    init.body = form;
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(t("error_network"), 0, "network");
  }
  if (response.status === 401) {
    window.location.assign(`${boot.urls.login}?next=${encodeURIComponent(location.pathname + location.search)}`);
    throw new ApiError(t("error_signed_out"), 401, "auth_required");
  }
  if (raw && response.ok) return response;
  const type = response.headers.get("Content-Type") || "";
  const data = type.includes("application/json") ? await response.json().catch(() => null) : null;
  if (!response.ok) {
    throw new ApiError(errorMessage(data, response.status), response.status, data?.error?.code || "error", data);
  }
  return raw ? response : data;
}

/**
 * Read a text/event-stream response, calling onEvent(object) for each "data:" JSON line.
 * Returns when the stream ends. Comments (heartbeats) are ignored.
 */
export async function readEventStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
      if (!data || data === "[DONE]") continue;
      try {
        onEvent(JSON.parse(data));
      } catch (error) {
        if (error instanceof SyntaxError) continue;
        throw error;
      }
    }
  }
}

// ----- feedback -----------------------------------------------------------
const toastRegion = () => document.getElementById("toasts");

/** Show a transient message. kind: info | success | warning | error. */
export function toast(message, kind = "info", { timeout } = {}) {
  const region = toastRegion();
  if (!region) return;
  const close = el("button", { type: "button", class: "icon-btn", "aria-label": t("dismiss"), text: "×" });
  const node = el("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : null }, el("div", { class: "grow", text: message }), close);
  close.addEventListener("click", () => node.remove());
  region.append(node);
  const delay = timeout ?? (kind === "error" ? 9000 : 5000);
  if (delay > 0) setTimeout(() => node.remove(), delay);
  return node;
}

function makeDialog({ title, message, body, actions, wide = false }) {
  const dialog = el("dialog", { class: wide ? "wide" : null, "aria-labelledby": "dialog-title" });
  const content = el("div", { class: "dialog-body" }, el("h2", { id: "dialog-title", text: title }));
  if (message) content.append(el("p", { class: "muted", text: message }));
  if (body) content.append(body);
  dialog.append(content, el("div", { class: "dialog-actions" }, ...actions));
  document.body.append(dialog);
  dialog.addEventListener("close", () => setTimeout(() => dialog.remove(), 0));
  return dialog;
}

/** Ask for confirmation; resolves true or false. The safe choice has focus. */
export function confirmDialog({ title, message = "", confirmLabel = t("confirm"), cancelLabel = t("cancel"), danger = false }) {
  return new Promise((resolve) => {
    let result = false;
    const cancel = el("button", { type: "button", class: "btn btn-ghost", text: cancelLabel });
    const confirm = el("button", { type: "button", class: `btn ${danger ? "btn-danger" : "btn-primary"}`, text: confirmLabel });
    const dialog = makeDialog({ title, message, actions: [cancel, confirm] });
    cancel.addEventListener("click", () => dialog.close());
    confirm.addEventListener("click", () => { result = true; dialog.close(); });
    dialog.addEventListener("close", () => resolve(result));
    dialog.showModal();
    cancel.focus();
  });
}

/** Ask for a line of text; resolves the string or null when cancelled. */
export function promptDialog({ title, label, value = "", maxLength = 200, confirmLabel = t("save") }) {
  return new Promise((resolve) => {
    let result = null;
    const input = el("input", { type: "text", id: "dialog-input", value, maxlength: maxLength });
    const field = el("div", { class: "field" }, el("label", { for: "dialog-input", text: label }), input);
    const form = el("form", { method: "dialog" }, field);
    const cancel = el("button", { type: "button", class: "btn btn-ghost", text: t("cancel") });
    const confirm = el("button", { type: "button", class: "btn btn-primary", text: confirmLabel });
    const dialog = makeDialog({ title, body: form, actions: [cancel, confirm] });
    const accept = (event) => { event.preventDefault(); result = input.value; dialog.close(); };
    form.addEventListener("submit", accept);
    confirm.addEventListener("click", accept);
    cancel.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => resolve(result));
    dialog.showModal();
    input.focus();
    input.select();
  });
}

/** Show a value that is displayed only once (tokens, share links) with a copy button. */
export function secretDialog({ title, message, secret, note }) {
  const input = el("input", { type: "text", readonly: true, value: secret, "aria-label": title });
  const copy = el("button", { type: "button", class: "btn", text: t("copy") });
  copy.addEventListener("click", async () => { if (await copyText(secret)) copy.textContent = t("copied"); });
  const body = el("div", { class: "stack" }, el("div", { class: "secret-box" }, input, copy));
  if (note) body.append(el("p", { class: "hint", text: note }));
  const done = el("button", { type: "button", class: "btn btn-primary", text: t("done") });
  const dialog = makeDialog({ title, message, body, actions: [done], wide: true });
  done.addEventListener("click", () => dialog.close());
  dialog.showModal();
  input.select();
  return dialog;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = el("textarea", { class: "visually-hidden" });
    area.value = text;
    document.body.append(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

export function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle("is-loading", busy);
  button.setAttribute("aria-busy", busy ? "true" : "false");
}

export function safeStorage(kind = "local") {
  try {
    const storage = kind === "session" ? window.sessionStorage : window.localStorage;
    const probe = "__bc";
    storage.setItem(probe, "1");
    storage.removeItem(probe);
    return storage;
  } catch {
    return { getItem: () => null, setItem() {}, removeItem() {} };
  }
}

export function debounce(fn, delay) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}

// ----- page-wide behaviour ------------------------------------------------
function initMenus() {
  const menus = () => document.querySelectorAll("details[data-menu][open]");
  document.addEventListener("click", (event) => {
    for (const menu of menus()) if (!menu.contains(event.target)) menu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    for (const menu of menus()) {
      const restoreFocus = menu.contains(document.activeElement);
      menu.open = false;
      if (restoreFocus) menu.querySelector("summary")?.focus();
    }
  });
}

function initNavigation() {
  const toggle = document.querySelector("[data-nav-toggle]");
  const nav = document.getElementById("main-nav");
  if (!toggle || !nav) return;
  toggle.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", (event) => {
    if (!nav.classList.contains("open") || nav.contains(event.target) || toggle.contains(event.target)) return;
    nav.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  });
  // Escape closes the phone menu like the account menu, returning focus to its button.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !nav.classList.contains("open")) return;
    nav.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
    if (nav.contains(document.activeElement)) toggle.focus();
  });
}

function initToasts() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-dismiss-toast]");
    if (button) button.closest(".toast")?.remove();
  });
  for (const node of document.querySelectorAll("[data-flash]")) {
    if (!node.classList.contains("toast-error")) setTimeout(() => node.remove(), 7000);
  }
}

// ----- service status --------------------------------------------------------
// Maintenance mode or an unreachable AI server never close the site: a banner
// explains the situation and only sending is paused. The banner and every
// composer follow the live status (GET /status), so they recover by themselves.
const statusListeners = new Set();
export const serviceStatus = {
  canSend: boot.status ? boot.status.can_send !== false : true,
  kinds: boot.status ? boot.status.kinds || [] : [],
};

/** Call fn({canSend, kinds}) now and whenever the service status changes. Returns an unsubscribe function. */
export function onStatusChange(fn) {
  statusListeners.add(fn);
  fn({ ...serviceStatus });
  return () => statusListeners.delete(fn);
}

function applyDismissals(region) {
  const storage = safeStorage("session");
  for (const banner of region.querySelectorAll("[data-dismissible='1']")) {
    if (storage.getItem(`bc-status:${banner.dataset.dismissKey}`)) banner.remove();
  }
}

async function refreshStatus() {
  const region = document.getElementById("status-region");
  let data;
  try {
    data = await api(`${boot.urls.status}?banner=1`);
  } catch {
    return; // a failed poll changes nothing; the next one will try again
  }
  const canSend = data.can_send !== false;
  const kinds = (data.notices || []).map((notice) => notice.kind);
  const changed = canSend !== serviceStatus.canSend || kinds.join() !== serviceStatus.kinds.join();
  if (!changed) return;
  const restored = canSend && !serviceStatus.canSend;
  serviceStatus.canSend = canSend;
  serviceStatus.kinds = kinds;
  if (region && typeof data.banner_html === "string") {
    // Server-rendered, escaped markup of partials/status_banner.html.
    const fresh = new DOMParser().parseFromString(data.banner_html, "text/html").getElementById("status-region");
    if (fresh) {
      region.replaceChildren(...fresh.childNodes);
      applyDismissals(region);
    }
  }
  for (const fn of statusListeners) fn({ ...serviceStatus });
  document.dispatchEvent(new CustomEvent("bc:status", { detail: { ...serviceStatus } }));
  if (restored) toast(t("status_restored"), "success");
}

function initStatus() {
  const region = document.getElementById("status-region");
  if (region) {
    applyDismissals(region);
    region.addEventListener("click", (event) => {
      const button = event.target.closest("[data-dismiss-status]");
      if (!button) return;
      const banner = button.closest(".status-banner");
      safeStorage("session").setItem(`bc-status:${banner.dataset.dismissKey}`, "1");
      banner.remove();
    });
  }
  if (!boot.user || !boot.urls.status) return;
  let timer;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (document.visibilityState === "visible") await refreshStatus();
      schedule();
    }, serviceStatus.canSend ? 45000 : 15000);
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshStatus().then(schedule);
  });
  schedule();
}

/** Ask the server now (e.g. after a request was refused with status 503). */
export function checkStatusSoon() {
  setTimeout(refreshStatus, 250);
}

/** <form data-confirm="Question?" data-confirm-danger> asks before submitting. */
function initConfirmForms() {
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm || form.dataset.confirmed === "1") return;
    event.preventDefault();
    const submitter = event.submitter;
    const ok = await confirmDialog({
      title: form.dataset.confirmTitle || t("are_you_sure"),
      message: form.dataset.confirm,
      confirmLabel: form.dataset.confirmLabel || t("confirm"),
      danger: "confirmDanger" in form.dataset,
    });
    if (!ok) return;
    form.dataset.confirmed = "1";
    form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
    setTimeout(() => delete form.dataset.confirmed, 0);
  }, true);
}

/** <select data-autosubmit> submits its form when changed. */
function initAutoSubmit() {
  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.matches("[data-autosubmit]") && target.form) target.form.requestSubmit();
  });
}

/** <button data-copy="text"> copies text to the clipboard. */
function initCopyButtons() {
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    if (await copyText(button.dataset.copy)) toast(t("copied"), "success", { timeout: 2000 });
  });
}

/** Forms with data-busy show a spinner on their submit button while the next page loads. */
function initBusyForms() {
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form instanceof HTMLFormElement && "busy" in form.dataset && !event.defaultPrevented) {
      setTimeout(() => setBusy(event.submitter || form.querySelector("[type=submit]"), true), 0);
    }
  });
}

initMenus();
initNavigation();
initToasts();
initStatus();
initConfirmForms();
initAutoSubmit();
initCopyButtons();
initBusyForms();
