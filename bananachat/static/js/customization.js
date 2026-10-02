// Customize page: live preview of appearance preferences, saved automatically.
import {
  api, confirmDialog, fragmentTarget, keepInitialFragmentVisible, pageData,
  requestHeaders, revealDisclosures, t, toast,
} from "./core.js";

const data = pageData();
const form = document.getElementById("prefs-form");
const status = document.getElementById("save-status");
const root = document.documentElement;

// Deep links retain access to optional settings even when their group starts closed.
function revealSettings(hash = window.location.hash, scroll = true) {
  const target = fragmentTarget(hash);
  if (!target) return;
  const withinDisclosure = revealDisclosures(target, form);
  // The browser may have tried to scroll while the anchored control was hidden.
  if (scroll && withinDisclosure) requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  return target;
}

window.addEventListener("hashchange", () => revealSettings());
document.querySelector(".customize-toc")?.addEventListener("click", (event) => {
  const link = event.target.closest('a[href^="#"]');
  if (link) revealSettings(link.hash);
});
const initialSettingsTarget = revealSettings(window.location.hash, false);
if (initialSettingsTarget?.closest(".customize-extra")) keepInitialFragmentVisible(initialSettingsTarget);

const COLOUR_KEYS = ["custom_bg", "custom_text", "custom_primary", "custom_secondary", "custom_accent", "custom_sidebar"];
const PALETTE_NAME = { custom_bg: "bg", custom_text: "text", custom_primary: "primary", custom_secondary: "secondary", custom_accent: "accent", custom_sidebar: "sidebar" };
const INTEGER_KEYS = new Set(["contrast", "line_height", "letter_spacing", "sidebar_width", "semantic_bold", "semantic_italic", "semantic_code", "semantic_link", "semantic_heading"]);
const SEMANTIC = ["bold", "italic", "code", "link", "heading"];
const LINE_HEIGHTS = ["1.55", "1.8", "2.05"];
const LETTER_SPACINGS = ["normal", "0.03em", "0.07em"];

let prefs = { ...data.preferences };
let backgroundUrl = data.background_url || null;

// ----- preview ------------------------------------------------------------
function effectiveTheme(values = prefs) {
  return values.theme_mode === "dark" || values.theme_mode === "light" ? values.theme_mode : data.site_theme;
}

function effectivePalette(values = prefs) {
  const palette = { ...data.site_palettes[effectiveTheme(values)] };
  for (const key of COLOUR_KEYS) if (values[key]) palette[PALETTE_NAME[key]] = values[key];
  return palette;
}

function apply() {
  const theme = effectiveTheme();
  root.dataset.theme = theme;
  document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", theme);
  const palette = effectivePalette();
  for (const [name, value] of Object.entries(palette)) root.style.setProperty(`--palette-${name}`, value);
  root.style.setProperty("--palette-on-primary", primaryForeground(palette.primary));
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", palette.bg);
  root.style.setProperty("--font-scale", String(prefs.font_scale));
  root.style.setProperty("--line-height", LINE_HEIGHTS[prefs.line_height] || LINE_HEIGHTS[0]);
  root.style.setProperty("--letter-spacing", LETTER_SPACINGS[prefs.letter_spacing] || LETTER_SPACINGS[0]);
  root.style.setProperty("--sidebar-width", `${prefs.sidebar_width}px`);

  const body = document.body;
  for (const name of [...body.classList]) {
    if (/^contrast-\d$/.test(name) || /^sem-[a-z]+-\d$/.test(name)) body.classList.remove(name);
  }
  if (prefs.contrast) body.classList.add(`contrast-${prefs.contrast}`);
  body.classList.toggle("reduce-motion", Boolean(prefs.reduce_motion));
  for (const kind of SEMANTIC) {
    const level = prefs[`semantic_${kind}`];
    if (level) body.classList.add(`sem-${kind}-${level}`);
  }
  if (backgroundUrl) root.style.setProperty("--background-image", `url("${backgroundUrl}")`);
  else root.style.removeProperty("--background-image");
  body.classList.toggle("has-background", Boolean(backgroundUrl));
  updateColourFields(palette);
  paintPresets();
  checkContrast(palette);
}

// ----- contrast check -----------------------------------------------------
function luminance(hex) {
  const channels = [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16) / 255);
  const linear = channels.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

// Match formatting.primary_foreground for the initial server-rendered theme.
function primaryForeground(hex) {
  const value = luminance(hex);
  return (value + 0.05) / 0.05 >= 1.05 / (value + 0.05) ? "#000000" : "#ffffff";
}

function contrastRatio(a, b) {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

function checkContrast(palette) {
  const warning = document.getElementById("contrast-warning");
  if (!warning) return;
  const low = contrastRatio(palette.text, palette.bg) < 4.5 || contrastRatio(palette.text, palette.secondary) < 4.5;
  warning.hidden = !low;
}

// ----- form <-> preferences -----------------------------------------------
function updateColourFields(palette) {
  for (const key of COLOUR_KEYS) {
    const input = form.elements[key];
    const field = form.querySelector(`[data-colour-field="${key}"]`);
    if (!input || !field) continue;
    const custom = Boolean(prefs[key]);
    input.dataset.custom = custom ? "1" : "0";
    if (document.activeElement !== input) input.value = prefs[key] || palette[PALETTE_NAME[key]];
    field.querySelector("[data-colour-state]").textContent = custom ? prefs[key] : t("customize_colour_theme");
    field.querySelector("[data-clear-colour]").hidden = !custom;
  }
}

function syncForm() {
  for (const element of form.elements) {
    const name = element.name;
    if (!name || !(name in prefs) || COLOUR_KEYS.includes(name)) continue;
    const value = prefs[name];
    if (element.type === "radio") element.checked = String(element.value) === String(value);
    else if (element.type === "checkbox") element.checked = Boolean(value);
    else if (name === "font_scale") {
      const match = [...element.options].find((option) => Math.abs(parseFloat(option.value) - value) < 0.001);
      if (match) element.value = match.value;
    } else element.value = String(value);
  }
  updateSidebarOutput();
}

function readElement(element) {
  const name = element.name;
  if (element.type === "checkbox") return element.checked;
  if (name === "font_scale") return parseFloat(element.value);
  if (INTEGER_KEYS.has(name)) return parseInt(element.value, 10);
  return element.value;
}

function updateSidebarOutput() {
  const output = document.getElementById("sidebar_width_value");
  if (output) output.textContent = `${prefs.sidebar_width} px`;
}

// ----- saving -------------------------------------------------------------
let revision = 0;
let savedRevision = 0;
let failedRevision = null;
let saveTimer;
let writes = Promise.resolve();
let pendingWrites = 0;
let resetting = false;
let resetGeneration = 0;
let reloadNeeded = false;
let allowReload = false;

// Preferences, reset and background changes all share the same ordering boundary.
// Discarding an old response alone cannot stop its request overwriting a newer save.
function queueWrite(operation) {
  pendingWrites += 1;
  const request = writes.then(operation).finally(() => {
    pendingWrites -= 1;
    if (!pendingWrites && !resetting && savedRevision === revision && reloadNeeded) {
      allowReload = true;
      window.location.reload();
    }
  });
  writes = request.catch(() => {});
  return request;
}

function cancelScheduledSave() {
  clearTimeout(saveTimer);
  saveTimer = undefined;
}

function showCurrentStatus() {
  if (failedRevision === revision) setStatus(t("customize_save_failed"), "is-error");
  else if (savedRevision === revision) setStatus(t("customize_saved"), "is-saved");
  else setStatus(t(saveTimer ? "customize_unsaved" : "customize_saving"));
}

function setStatus(text, kind = "") {
  status.textContent = text;
  status.className = `save-status mb-0 ${kind}`;
}

function clientPreferences() {
  const copy = { ...prefs };
  delete copy.background_image;
  return copy;
}

function save() {
  cancelScheduledSave();
  const ticket = revision;
  const changes = clientPreferences();
  failedRevision = null;
  setStatus(t("customize_saving"));
  return queueWrite(async () => {
    try {
      const result = await api(data.urls.save, { json: changes });
      reloadNeeded ||= Boolean(result.reload);
      if (ticket !== revision) return;
      prefs = { ...prefs, ...result.preferences };
      savedRevision = ticket;
      setStatus(t("customize_saved"), "is-saved");
    } catch (error) {
      if (ticket === revision) {
        failedRevision = ticket;
        setStatus(t("customize_save_failed"), "is-error");
      }
      toast(error.message, "error");
    }
  });
}

function change(name, value, { immediate = false } = {}) {
  if (resetting) return;
  revision += 1;
  failedRevision = null;
  prefs[name] = value;
  apply();
  setStatus(t("customize_unsaved"));
  cancelScheduledSave();
  if (immediate) save();
  else saveTimer = setTimeout(save, 450);
}

form.addEventListener("submit", (event) => event.preventDefault());
form.addEventListener("input", (event) => {
  const element = event.target;
  if (!element.name || !(element.name in prefs)) return;
  if (COLOUR_KEYS.includes(element.name)) {
    change(element.name, element.value.toLowerCase());
    return;
  }
  if (element.type === "range") {
    prefs.sidebar_width = readElement(element);
    updateSidebarOutput();
  }
  if (element.type !== "radio" && element.type !== "checkbox" && element.tagName !== "SELECT") change(element.name, readElement(element));
});
form.addEventListener("change", (event) => {
  const element = event.target;
  if (!element.name || !(element.name in prefs) || COLOUR_KEYS.includes(element.name)) return;
  if (element.type === "radio" && !element.checked) return;
  change(element.name, readElement(element), { immediate: element.name === "interface_language" });
});

form.addEventListener("click", (event) => {
  const clear = event.target.closest("[data-clear-colour]");
  if (clear) {
    change(clear.dataset.clearColour, "");
    form.elements[clear.dataset.clearColour]?.focus();
    return;
  }
  const preset = event.target.closest("[data-preset]");
  if (preset) {
    const values = data.presets[preset.dataset.preset][effectiveTheme()];
    Object.assign(prefs, values);
    change(COLOUR_KEYS[0], values[COLOUR_KEYS[0]]);
    toast(t("customize_preset_applied", { name: preset.textContent.trim() }), "success", { timeout: 2500 });
  }
});

document.getElementById("reset-colours")?.addEventListener("click", () => {
  for (const key of COLOUR_KEYS) prefs[key] = "";
  change(COLOUR_KEYS[0], "");
});

function paintPresets() {
  const theme = effectiveTheme();
  for (const button of form.querySelectorAll("[data-preset]")) {
    const values = data.presets[button.dataset.preset][theme];
    const swatches = button.querySelectorAll(".swatch");
    ["custom_bg", "custom_primary", "custom_accent", "custom_text"].forEach((key, index) => {
      if (swatches[index]) swatches[index].style.background = values[key];
    });
    const active = COLOUR_KEYS.every((key) => prefs[key] === values[key]);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }
}

// ----- reset --------------------------------------------------------------
const resetButton = document.getElementById("reset-all");
resetButton?.addEventListener("click", async () => {
  if (resetting) return;
  const ok = await confirmDialog({ title: t("customize_reset_title"), message: t("customize_reset_confirm"), confirmLabel: t("customize_reset_action"), danger: true });
  if (!ok || resetting) return;
  cancelScheduledSave();
  revision += 1;
  resetGeneration += 1;
  failedRevision = null;
  resetting = true;
  form.inert = true;
  form.setAttribute("aria-busy", "true");
  resetButton.disabled = true;
  setStatus(t("customize_saving"));
  await queueWrite(async () => {
    try {
      const result = await api(data.urls.reset, { method: "POST", json: {} });
      prefs = { ...result.preferences };
      backgroundUrl = null;
      savedRevision = revision;
      reloadNeeded ||= Boolean(result.reload);
      syncForm();
      apply();
      updateBackgroundControls();
      setStatus(t("customize_saved"), "is-saved");
    } catch (error) {
      // Earlier queued uploads/removals may have completed while reset hid
      // their results. If reset fails, show the background the server kept.
      try {
        const current = await api(data.urls.save);
        backgroundUrl = current.background_url;
        prefs.background_image = current.preferences.background_image;
        updateBackgroundControls();
        apply();
      } catch {
        // Keep the original reset error; the preview remains unchanged offline.
      }
      failedRevision = revision;
      setStatus(t("customize_save_failed"), "is-error");
      toast(error.message, "error");
    } finally {
      resetting = false;
      form.inert = false;
      form.removeAttribute("aria-busy");
      resetButton.disabled = false;
    }
  });
});

// ----- background image ---------------------------------------------------
const fileInput = document.getElementById("background-file");
const removeButton = document.getElementById("background-remove");
const preview = document.getElementById("background-preview");
const previewImage = document.getElementById("background-image");

function updateBackgroundControls() {
  preview.hidden = !backgroundUrl;
  removeButton.hidden = !backgroundUrl;
  if (backgroundUrl) previewImage.src = backgroundUrl;
  else previewImage.removeAttribute("src");
}

fileInput?.addEventListener("change", async () => {
  const file = fileInput.files[0];
  fileInput.value = "";
  if (!file || resetting) return;
  if (file.size > data.background_max_bytes) {
    toast(t("customize_background_too_large", { size: Math.round(data.background_max_bytes / 1048576) }), "error");
    return;
  }
  const body = new FormData();
  body.append("file", file);
  const generation = resetGeneration;
  setStatus(t("customize_uploading"));
  await queueWrite(async () => {
    try {
      const result = await api(data.urls.background, { form: body });
      if (generation !== resetGeneration) return;
      backgroundUrl = result.background_url;
      prefs.background_image = result.preferences.background_image;
      updateBackgroundControls();
      apply();
      showCurrentStatus();
    } catch (error) {
      if (generation === resetGeneration) setStatus(t("customize_save_failed"), "is-error");
      toast(error.message, "error");
    }
  });
});

removeButton?.addEventListener("click", async () => {
  if (resetting) return;
  const generation = resetGeneration;
  await queueWrite(async () => {
    try {
      await api(data.urls.background, { method: "DELETE" });
      if (generation !== resetGeneration) return;
      backgroundUrl = null;
      prefs.background_image = "";
      updateBackgroundControls();
      apply();
      showCurrentStatus();
      fileInput.focus();
    } catch (error) {
      toast(error.message, "error");
    }
  });
});

// Pending changes must not disappear during navigation. A final keepalive save
// is safe only when it cannot overtake an already running write.
window.addEventListener("beforeunload", (event) => {
  if (allowReload || (!pendingWrites && revision === savedRevision)) return;
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("pagehide", () => {
  cancelScheduledSave();
  if (resetting || pendingWrites || revision === savedRevision) return;
  const blob = new Blob([JSON.stringify(clientPreferences())], { type: "application/json" });
  fetch(data.urls.save, { method: "POST", body: blob, keepalive: true, credentials: "same-origin",
    headers: requestHeaders({ "Content-Type": "application/json" }) }).catch(() => {});
});

syncForm();
apply();
