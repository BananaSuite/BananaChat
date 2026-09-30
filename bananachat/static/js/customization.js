// Customize page: live preview of appearance preferences, saved automatically.
import { api, confirmDialog, debounce, pageData, requestHeaders, t, toast } from "./core.js";

const data = pageData();
const form = document.getElementById("prefs-form");
const status = document.getElementById("save-status");
const root = document.documentElement;

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
  const linear = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
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
let saving = 0;

function setStatus(text, kind = "") {
  status.textContent = text;
  status.className = `save-status mb-0 ${kind}`;
}

function clientPreferences() {
  const copy = { ...prefs };
  delete copy.background_image;
  return copy;
}

async function save() {
  saving += 1;
  const ticket = saving;
  setStatus(t("customize_saving"));
  try {
    const result = await api(data.urls.save, { json: clientPreferences() });
    if (ticket !== saving) return;
    const languageChanged = result.reload;
    prefs = { ...prefs, ...result.preferences };
    setStatus(t("customize_saved"), "is-saved");
    if (languageChanged) window.location.reload();
  } catch (error) {
    if (ticket === saving) setStatus(t("customize_save_failed"), "is-error");
    toast(error.message, "error");
  }
}

const scheduleSave = debounce(save, 450);

function change(name, value, { immediate = false } = {}) {
  prefs[name] = value;
  apply();
  setStatus(t("customize_unsaved"));
  if (immediate) save();
  else scheduleSave();
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
document.getElementById("reset-all")?.addEventListener("click", async () => {
  const ok = await confirmDialog({ title: t("customize_reset_title"), message: t("customize_reset_confirm"), confirmLabel: t("customize_reset_action"), danger: true });
  if (!ok) return;
  try {
    const result = await api(data.urls.reset, { method: "POST", json: {} });
    prefs = { ...result.preferences };
    backgroundUrl = null;
    syncForm();
    apply();
    updateBackgroundControls();
    setStatus(t("customize_saved"), "is-saved");
    if (result.reload) window.location.reload();
  } catch (error) {
    toast(error.message, "error");
  }
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
  if (!file) return;
  if (file.size > data.background_max_bytes) {
    toast(t("customize_background_too_large", { size: Math.round(data.background_max_bytes / 1048576) }), "error");
    return;
  }
  const body = new FormData();
  body.append("file", file);
  setStatus(t("customize_uploading"));
  try {
    const result = await api(data.urls.background, { form: body });
    backgroundUrl = result.background_url;
    prefs.background_image = result.preferences.background_image;
    updateBackgroundControls();
    apply();
    setStatus(t("customize_saved"), "is-saved");
  } catch (error) {
    setStatus(t("customize_save_failed"), "is-error");
    toast(error.message, "error");
  }
});

removeButton?.addEventListener("click", async () => {
  try {
    await api(data.urls.background, { method: "DELETE" });
    backgroundUrl = null;
    prefs.background_image = "";
    updateBackgroundControls();
    apply();
    setStatus(t("customize_saved"), "is-saved");
    fileInput.focus();
  } catch (error) {
    toast(error.message, "error");
  }
});

// Save before leaving when a change is still waiting for the debounce.
window.addEventListener("pagehide", () => {
  if (status.textContent !== t("customize_unsaved")) return;
  const blob = new Blob([JSON.stringify(clientPreferences())], { type: "application/json" });
  fetch(data.urls.save, { method: "POST", body: blob, keepalive: true, credentials: "same-origin",
    headers: requestHeaders({ "Content-Type": "application/json" }) }).catch(() => {});
});

syncForm();
apply();
