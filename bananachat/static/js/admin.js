// Administrator pages (English-only by design). Progressive enhancement:
// every action also works as a plain form; scripts add live status and polish.
import {
  api, confirmDialog, el, fragmentTarget, keepInitialFragmentVisible, pageData,
  revealDisclosures, setBusy, toast,
} from "./core.js";

const data = pageData();

function formatBytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

// ----- overview: inference server -----------------------------------------------
async function initInferenceStatus() {
  const box = document.getElementById("inference-details");
  if (!box || !data.inference_url) return;
  const field = (name) => box.querySelector(`[data-inference="${name}"]`);
  try {
    const result = await api(data.inference_url);
    const status = field("status");
    status.replaceChildren(result.reachable
      ? el("span", { class: "badge badge-success", text: "Reachable" })
      : el("span", { class: "badge badge-danger", text: "Unreachable" }));
    if (!result.reachable && result.error) status.append(" ", el("span", { class: "small muted", text: result.error }));
    field("version").textContent = result.version || "—";
    const running = field("running");
    if (!result.reachable) running.textContent = "—";
    else if (!result.running.length) running.textContent = "None";
    else running.replaceChildren(...result.running.map((model, index) => el("span", {},
      index ? ", " : "", el("span", { class: "mono", text: model.name }),
      model.size_vram ? ` (${formatBytes(model.size_vram)} GPU)` : "")));
  } catch (error) {
    field("status").textContent = error.message;
  }
}

// ----- models: running models -----------------------------------------------------
function initRunningModels() {
  const container = document.getElementById("running-models");
  if (!container || !data.running_url) return;
  const refreshButton = document.querySelector("[data-running-refresh]");
  const unloadAllButton = document.querySelector("[data-unload-all]");

  async function unload(name, button) {
    setBusy(button, true);
    try {
      const result = await api(data.unload_url, { json: { name } });
      toast(result.message, "success");
      await load();
    } catch (error) {
      toast(error.message, "error");
      setBusy(button, false);
    }
  }

  function render(models) {
    if (!models.length) {
      container.replaceChildren(el("p", { class: "muted", text: "No models are loaded right now." }));
      if (unloadAllButton) unloadAllButton.disabled = true;
      return;
    }
    if (unloadAllButton) unloadAllButton.disabled = false;
    const rows = models.map((model) => {
      const button = el("button", { type: "button", class: "btn btn-sm", text: "Unload", "aria-label": `Unload ${model.name}` });
      button.addEventListener("click", () => unload(model.name, button));
      const expires = model.expires_at ? new Date(model.expires_at) : null;
      return el("tr", {},
        el("td", { class: "mono" }, model.name),
        el("td", {}, formatBytes(model.size)),
        el("td", {}, formatBytes(model.size_vram)),
        el("td", {}, expires && !Number.isNaN(expires.getTime()) ? expires.toLocaleTimeString() : "—"),
        el("td", { class: "actions" }, button));
    });
    const head = el("thead", {}, el("tr", {}, ...["Model", "Memory", "GPU memory", "Unloads at"].map((label) => el("th", { scope: "col", text: label })),
      el("th", { scope: "col" }, el("span", { class: "visually-hidden", text: "Actions" }))));
    container.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "table" }, head, el("tbody", {}, ...rows))));
  }

  async function load() {
    try {
      const result = await api(data.running_url);
      render(result.models);
    } catch (error) {
      container.replaceChildren(el("p", { class: "danger-text", text: error.message }));
    }
  }

  refreshButton?.addEventListener("click", async () => {
    setBusy(refreshButton, true);
    await load();
    setBusy(refreshButton, false);
  });
  unloadAllButton?.addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Unload all models?", message: "Every loaded model is removed from memory. Running answers may be interrupted.", confirmLabel: "Unload all", danger: true });
    if (!ok) return;
    setBusy(unloadAllButton, true);
    try {
      const result = await api(data.unload_all_url, { json: {} });
      toast(result.message, "success");
    } catch (error) {
      toast(error.message, "error");
    }
    setBusy(unloadAllButton, false);
    await load();
  });
  load();
}

// ----- models: download progress and the queue ------------------------------------------
function initDownloads() {
  const table = document.getElementById("download-table");
  if (!table || !data.downloads_url || !data.active_downloads) return;
  const notice = document.getElementById("downloads-notice");
  let timer = null;

  function typing() {
    const active = document.activeElement;
    return active && (active.matches("input, textarea, select") || active.closest("dialog, details[open]"));
  }

  async function poll() {
    timer = null;
    let result;
    try {
      result = await api(data.downloads_url);
    } catch {
      timer = setTimeout(poll, 10000);
      return;
    }
    let changed = false;
    for (const job of result.jobs) {
      const row = table.querySelector(`tr[data-job="${job.id}"]`);
      if (!row) { if (["queued", "pulling"].includes(job.status)) changed = true; continue; }
      if (row.dataset.status !== job.status || row.dataset.paused !== (job.paused ? "1" : "0")) { changed = true; continue; }
      const meter = row.querySelector("progress");
      if (meter) { meter.value = job.progress; meter.textContent = `${job.progress}%`; }
      const detail = row.querySelector("[data-job-detail]");
      if (detail) detail.textContent = job.detail;
    }
    const queue = result.queue;
    if (queue) {
      const meter = document.getElementById("queue-progress");
      if (meter) { meter.value = queue.percent; meter.textContent = `${queue.percent}%`; }
      const bytes = document.getElementById("queue-bytes");
      if (bytes) bytes.textContent = queue.bytes_total ? `${formatBytes(queue.bytes_done)} of ${formatBytes(queue.bytes_total)}` : `${queue.percent}%`;
      const count = document.getElementById("queue-active");
      if (count) count.textContent = String(queue.active);
    }
    const active = result.jobs.some((job) => ["queued", "pulling"].includes(job.status));
    if (changed) {
      if (!typing()) { window.location.reload(); return; }
      if (notice) notice.hidden = false;
    }
    if (active) timer = setTimeout(poll, 2000);
  }
  timer = setTimeout(poll, 1500);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && timer === null && notice?.hidden !== false) poll();
  });
}

// ----- settings: palettes ----------------------------------------------------------
function initPalettes() {
  const fields = document.querySelectorAll("input[data-palette]");
  if (!fields.length) return;
  const valid = (value) => /^#[0-9a-fA-F]{6}$/.test(value);

  function paint(field) {
    const preview = document.querySelector(`svg[data-preview="${field.dataset.palette}"]`);
    if (!preview || !valid(field.value)) return;
    for (const shape of preview.querySelectorAll(`[data-swatch="${field.dataset.key}"]`)) shape.setAttribute("fill", field.value);
  }

  for (const field of fields) {
    const picker = document.querySelector(`input[type="color"][data-color-for="${field.id}"]`);
    field.addEventListener("input", () => {
      field.setCustomValidity(valid(field.value.trim()) ? "" : "Use a colour like #1a2b3c.");
      if (picker && valid(field.value.trim())) picker.value = field.value.trim().toLowerCase();
      paint(field);
    });
    picker?.addEventListener("input", () => {
      field.value = picker.value;
      field.setCustomValidity("");
      paint(field);
    });
  }
}

// ----- small form behaviours -----------------------------------------------------------
function initToggles() {
  for (const select of document.querySelectorAll("select[data-toggle-target]")) {
    const target = document.getElementById(select.dataset.toggleTarget);
    if (!target) continue;
    const update = () => { target.hidden = select.value !== select.dataset.toggleValue; };
    select.addEventListener("change", update);
    update();
  }
}

function initPreviews() {
  for (const field of document.querySelectorAll("[data-preview-target]")) {
    const target = document.getElementById(field.dataset.previewTarget);
    if (!target) continue;
    const update = () => { target.textContent = field.value.trim() || target.dataset.empty || ""; };
    field.addEventListener("input", update);
  }
}

function initCounters() {
  for (const field of document.querySelectorAll("textarea[data-counter]")) {
    const output = document.getElementById(field.dataset.counter);
    if (!output) continue;
    const max = Number(field.getAttribute("maxlength")) || 0;
    const update = () => { output.textContent = `${field.value.length.toLocaleString()} of ${max.toLocaleString()} characters`; };
    field.addEventListener("input", update);
    update();
  }
}

// Section links reveal optional settings; core handles native form validation.
function initSettingsDisclosures() {
  const followHash = () => {
    const target = fragmentTarget();
    if (!target) return;
    revealDisclosures(target);
    target.scrollIntoView({ block: "start" });
    return target;
  };
  window.addEventListener("hashchange", followHash);
  keepInitialFragmentVisible(followHash());
}

initInferenceStatus();
initRunningModels();
initDownloads();
initPalettes();
initToggles();
initCounters();
initPreviews();
initSettingsDisclosures();
