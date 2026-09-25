"use strict";


//  Synchronized UI customization

var _a11yPrefs = {};
var _a11ySaveTimer = null;

function getThemeConfig() {
  return window.BC_THEME_CONFIG || {
    default_mode: "dark",
    palettes: {
      dark: { primary: "#e6be32", secondary: "#1d1d1d", accent: "#cda624", text: "#ededed", sidebar: "#181818", bg: "#141414" },
      light: { primary: "#8a6500", secondary: "#ffffff", accent: "#6f5000", text: "#202124", sidebar: "#f4f1e8", bg: "#faf9f5" }
    }
  };
}

function getEffectiveThemeMode(prefs) {
  var mode = String((prefs && prefs.theme_mode) || "default").toLowerCase();
  if (mode === "dark" || mode === "light") return mode;
  return getThemeConfig().default_mode === "light" ? "light" : "dark";
}

function _backgroundUrl(filename) {
  filename = String(filename || "");
  return /^[0-9a-f]{32}\.jpe?g$/.test(filename)
    ? "/customization/background/" + filename
    : "";
}

function applyA11yPrefs(prefs) {
  var root = document.documentElement;
  var mode = getEffectiveThemeMode(prefs);
  var config = getThemeConfig();
  var palette = config.palettes[mode] || config.palettes.dark;
  var colors = {
    "--bg": prefs.custom_bg || palette.bg,
    "--text": prefs.custom_text || palette.text,
    "--accent": prefs.custom_primary || palette.primary,
    "--raised": prefs.custom_secondary || palette.secondary,
    "--accent-d": prefs.custom_accent || palette.accent,
    "--surf": prefs.custom_sidebar || palette.sidebar
  };
  Object.keys(colors).forEach(function(prop) { root.style.setProperty(prop, colors[prop]); });
  root.style.setProperty("--overlay", "color-mix(in srgb, var(--raised) 88%, var(--text))");
  root.style.setProperty("--border", "color-mix(in srgb, var(--text) 16%, var(--bg))");
  root.style.setProperty("--bdr-hi", "color-mix(in srgb, var(--text) 28%, var(--bg))");
  root.style.setProperty("--text-2", "color-mix(in srgb, var(--text) 72%, var(--bg))");
  root.style.setProperty("--text-3", "color-mix(in srgb, var(--text) 52%, var(--bg))");
  root.style.setProperty("--text-4", "color-mix(in srgb, var(--text) 34%, var(--bg))");
  root.style.setProperty("--a11y-font-scale", String(prefs.font_scale || 1));
  root.style.setProperty("--a11y-line-height-mul", ["1", "1.18", "1.36"][Number(prefs.line_height) || 0] || "1");
  root.style.setProperty("--a11y-letter-spacing", ["normal", "0.04em", "0.08em"][Number(prefs.letter_spacing) || 0] || "normal");
  root.style.setProperty("--chat-sidebar-width", String(prefs.sidebar_width || 260) + "px");
  var backgroundUrl = _backgroundUrl(prefs.background_image);
  root.style.setProperty("--bg-image", backgroundUrl ? 'url("' + backgroundUrl + '")' : "none");
  root.dataset.theme = mode;
  root.style.colorScheme = mode;
  var schemeMeta = document.querySelector('meta[name="color-scheme"]');
  if (schemeMeta) schemeMeta.content = mode;

  for (var contrast = 0; contrast <= 5; contrast++) {
    document.body.classList.remove("a11y-contrast-" + contrast);
  }
  if (Number(prefs.contrast) > 0) document.body.classList.add("a11y-contrast-" + prefs.contrast);
  document.body.classList.toggle("a11y-reduce-motion", !!prefs.reduce_motion);
  document.body.classList.toggle("a11y-has-bg", !!backgroundUrl);
  ["bold", "italic", "code", "link", "heading"].forEach(function(key) {
    document.body.classList.remove("a11y-sem-" + key + "-1", "a11y-sem-" + key + "-2");
    var level = Number(prefs["semantic_" + key]) || 0;
    if (level === 1 || level === 2) document.body.classList.add("a11y-sem-" + key + "-" + level);
  });
}

function _parseCustomizationResponse(response) {
  return response.json().catch(function() { return {}; }).then(function(data) {
    if (!response.ok) throw new Error(data.error || "Customization request failed");
    return data;
  });
}

function _postA11yPrefs(options) {
  options = options || {};
  return fetch("/api/accessibility", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
    body: JSON.stringify(_a11yPrefs),
    keepalive: !!options.keepalive
  }).then(_parseCustomizationResponse).catch(function(error) {
    if (!options.silent) showToast(error.message || "Could not save customization");
    throw error;
  });
}

function saveA11ySetting(key, value, immediate) {
  _a11yPrefs[key] = value;
  if (_a11ySaveTimer) clearTimeout(_a11ySaveTimer);
  if (immediate) return _postA11yPrefs({ keepalive: true });
  _a11ySaveTimer = setTimeout(function() {
    _a11ySaveTimer = null;
    _postA11yPrefs().catch(function() {});
  }, 450);
  return Promise.resolve();
}

function _rgbToHex(color) {
  if (!color) return null;
  color = color.trim().toLowerCase();
  if (/^#[0-9a-f]{6}$/.test(color)) return color;
  var match = color.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
  if (!match) return null;
  return "#" + [match[1], match[2], match[3]].map(function(value) {
    return ("0" + Number(value).toString(16)).slice(-2);
  }).join("");
}

function initAccessibility(prefs) {
  _a11yPrefs = Object.assign({
    theme_mode: "default", interface_language: "default", font_scale: 1,
    contrast: 0, sidebar_width: 260, line_height: 0, letter_spacing: 0,
    reduce_motion: 0, background_image: ""
  }, prefs || {});
  applyA11yPrefs(_a11yPrefs);

  var panel = document.getElementById("a11y-panel");
  var toggle = document.getElementById("a11y-toggle-btn");
  var mobileToggle = document.getElementById("mobile-a11y-toggle-btn");
  var closeButton = document.getElementById("a11y-close-btn");
  var overlay = document.getElementById("a11y-overlay");
  if (!panel || (!toggle && !mobileToggle)) return;

  function openPanel() {
    panel.classList.add("open");
    if (overlay) overlay.classList.add("open");
    syncPanel();
  }
  function closePanel() {
    panel.classList.remove("open");
    if (overlay) overlay.classList.remove("open");
  }
  if (toggle) toggle.addEventListener("click", openPanel);
  document.querySelectorAll("[data-open-customization]").forEach(function(button) {
    button.addEventListener("click", openPanel);
  });
  if (mobileToggle) mobileToggle.addEventListener("click", function() {
    var menu = document.getElementById("nav-mobile-menu");
    if (menu) menu.classList.remove("open");
    openPanel();
  });
  if (closeButton) closeButton.addEventListener("click", closePanel);
  if (overlay) overlay.addEventListener("click", closePanel);
  document.addEventListener("keydown", function(event) { if (event.key === "Escape") closePanel(); });

  panel.querySelectorAll("[data-theme-mode]").forEach(function(button) {
    button.addEventListener("click", function() {
      _a11yPrefs.theme_mode = button.dataset.themeMode;
      ["custom_bg", "custom_text", "custom_primary", "custom_secondary", "custom_accent", "custom_sidebar"].forEach(function(key) { _a11yPrefs[key] = ""; });
      applyA11yPrefs(_a11yPrefs);
      saveA11ySetting("theme_mode", _a11yPrefs.theme_mode);
      syncPanel();
    });
  });
  panel.querySelectorAll("[data-scale]").forEach(function(button) {
    button.addEventListener("click", function() {
      saveA11ySetting("font_scale", Number(button.dataset.scale));
      applyA11yPrefs(_a11yPrefs); syncPanel();
    });
  });
  panel.querySelectorAll("[data-contrast]").forEach(function(button) {
    button.addEventListener("click", function() {
      saveA11ySetting("contrast", Number(button.dataset.contrast));
      applyA11yPrefs(_a11yPrefs); syncPanel();
    });
  });
  panel.querySelectorAll("[data-line-height]").forEach(function(button) {
    button.addEventListener("click", function() {
      saveA11ySetting("line_height", Number(button.dataset.lineHeight));
      applyA11yPrefs(_a11yPrefs); syncPanel();
    });
  });
  panel.querySelectorAll("[data-letter-spacing]").forEach(function(button) {
    button.addEventListener("click", function() {
      saveA11ySetting("letter_spacing", Number(button.dataset.letterSpacing));
      applyA11yPrefs(_a11yPrefs); syncPanel();
    });
  });
  panel.querySelectorAll("[data-semantic]").forEach(function(button) {
    button.addEventListener("click", function() {
      var key = "semantic_" + button.dataset.semantic;
      saveA11ySetting(key, Number(button.dataset.level));
      applyA11yPrefs(_a11yPrefs); syncPanel();
    });
  });

  var motion = document.getElementById("a11y-motion-toggle");
  if (motion) motion.addEventListener("change", function() {
    saveA11ySetting("reduce_motion", motion.checked ? 1 : 0);
    applyA11yPrefs(_a11yPrefs);
  });
  var language = document.getElementById("a11y-language-select");
  if (language) language.addEventListener("change", function() {
    saveA11ySetting("interface_language", language.value, true).then(function() {
      window.location.reload();
    }).catch(function() {});
  });
  var sidebarWidth = document.getElementById("a11y-sidebar-width");
  if (sidebarWidth) sidebarWidth.addEventListener("input", function() {
    saveA11ySetting("sidebar_width", Number(sidebarWidth.value));
    applyA11yPrefs(_a11yPrefs); syncSidebarWidth();
  });

  var colorMap = {
    bg: ["custom_bg", "--bg"], text: ["custom_text", "--text"],
    primary: ["custom_primary", "--accent"], secondary: ["custom_secondary", "--raised"],
    accent: ["custom_accent", "--accent-d"], sidebar: ["custom_sidebar", "--surf"]
  };
  Object.keys(colorMap).forEach(function(name) {
    var input = document.getElementById("a11y-color-" + name);
    if (!input) return;
    input.addEventListener("input", function() {
      saveA11ySetting(colorMap[name][0], input.value);
      applyA11yPrefs(_a11yPrefs);
    });
  });
  panel.querySelectorAll("[data-clear-color]").forEach(function(button) {
    button.addEventListener("click", function() {
      saveA11ySetting("custom_" + button.dataset.clearColor, "");
      applyA11yPrefs(_a11yPrefs); syncColors();
    });
  });

  var presets = {
    ocean: {dark:["#0b1a2e","#c8ddf0","#5b9bd5","#112640","#a3c4f3","#091526"],light:["#f3f8fd","#17324a","#256fa8","#ffffff","#4f90c7","#dbeaf7"]},
    forest: {dark:["#0f1e12","#c8dcc8","#4caf50","#162a19","#8fbf9f","#0b180e"],light:["#f2f8f1","#1f3a24","#2f7d34","#ffffff","#5c9a68","#dcebdd"]},
    sunset: {dark:["#1f1017","#f0ddd0","#e76f51","#2a1520","#f4a261","#1a0d14"],light:["#fff4ed","#513026","#c45136","#ffffff","#de7d3c","#f5dfd4"]},
    lavender: {dark:["#1a1428","#d8d0e8","#9b7fd4","#221a34","#c4b5e0","#151020"],light:["#f7f3fc","#32274a","#7657b8","#ffffff","#9a7acb","#e7def4"]},
    midnight: {dark:["#0a0a12","#e0e0e8","#00d4ff","#10101c","#7ee8ff","#08080e"],light:["#f2f6fb","#202535","#157ea3","#ffffff","#2ca7c9","#dce5ef"]},
    copper: {dark:["#1c1410","#e8dcd0","#b87333","#241c16","#d4a574","#16100c"],light:["#fbf3ed","#453028","#9c5f29","#ffffff","#bd814c","#ecded4"]},
    rose: {dark:["#1a0e14","#f0d8e0","#e91e8c","#240a18","#f48fb1","#140a10"],light:["#fff1f6","#4c2636","#bf2f77","#ffffff","#df6796","#f4d9e4"]},
    slate: {dark:["#0e1220","#d0d8f0","#7c8fc4","#141826","#a8b8e0","#0a0e1a"],light:["#f4f6fb","#253044","#596b98","#ffffff","#7688b4","#e0e5f0"]},
    ember: {dark:["#1a0e08","#f0e0d0","#ff6b2b","#221208","#ffad7a","#140a04"],light:["#fff3ea","#4f2d20","#c94f1d","#ffffff","#e8793f","#f3dfd0"]}
  };
  var presetKeys = ["custom_bg", "custom_text", "custom_primary", "custom_secondary", "custom_accent", "custom_sidebar"];
  panel.querySelectorAll("[data-preset]").forEach(function(button) {
    button.addEventListener("click", function() {
      var values = presets[button.dataset.preset][getEffectiveThemeMode(_a11yPrefs)];
      presetKeys.forEach(function(key, index) { _a11yPrefs[key] = values[index]; });
      applyA11yPrefs(_a11yPrefs); syncPanel(); saveA11ySetting("custom_bg", values[0]);
      panel.querySelectorAll("[data-preset]").forEach(function(item) { item.classList.remove("active"); });
      button.classList.add("active");
    });
  });

  var backgroundInput = document.getElementById("a11y-background-input");
  var backgroundUpload = document.getElementById("a11y-background-upload");
  var backgroundClear = document.getElementById("a11y-background-clear");
  if (backgroundUpload && backgroundInput) {
    backgroundUpload.addEventListener("click", function() { backgroundInput.click(); });
    backgroundInput.addEventListener("change", function() {
      var file = backgroundInput.files && backgroundInput.files[0];
      if (!file) return;
      var body = new FormData(); body.append("file", file); backgroundUpload.disabled = true;
      fetch("/api/accessibility/background", { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body: body })
        .then(_parseCustomizationResponse).then(function(data) {
          _a11yPrefs.background_image = data.background_image || "";
          applyA11yPrefs(_a11yPrefs); syncBackground(); showToast("Background saved");
        }).catch(function(error) { showToast(error.message); }).finally(function() {
          backgroundUpload.disabled = false; backgroundInput.value = "";
        });
    });
  }
  if (backgroundClear) backgroundClear.addEventListener("click", function() {
    fetch("/api/accessibility/background", { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } })
      .then(_parseCustomizationResponse).then(function() {
        _a11yPrefs.background_image = ""; applyA11yPrefs(_a11yPrefs); syncBackground();
      }).catch(function(error) { showToast(error.message); });
  });

  var reset = document.getElementById("a11y-reset-btn");
  if (reset) reset.addEventListener("click", function() {
    showConfirm("Reset all of your UI customization to the site defaults?", function() {
      fetch("/api/accessibility/reset", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() }, body: "{}" })
        .then(_parseCustomizationResponse).then(function(data) {
          _a11yPrefs = data.defaults || {}; applyA11yPrefs(_a11yPrefs); syncPanel(); showToast("Customization reset");
        }).catch(function(error) { showToast(error.message); });
    });
  });

  function syncChoice(selector, key, datasetKey) {
    panel.querySelectorAll(selector).forEach(function(button) {
      button.classList.toggle("active", String(button.dataset[datasetKey]) === String(_a11yPrefs[key]));
    });
  }
  function syncColors() {
    var computed = getComputedStyle(document.documentElement);
    Object.keys(colorMap).forEach(function(name) {
      var input = document.getElementById("a11y-color-" + name);
      if (!input) return;
      var color = _a11yPrefs[colorMap[name][0]] || computed.getPropertyValue(colorMap[name][1]);
      var hex = _rgbToHex(color); if (hex) input.value = hex;
    });
  }
  function syncSidebarWidth() {
    if (!sidebarWidth) return;
    sidebarWidth.value = String(_a11yPrefs.sidebar_width || 260);
    var output = document.getElementById("a11y-sidebar-width-output");
    if (output) output.textContent = sidebarWidth.value + " px";
  }
  function syncBackground() {
    var preview = document.getElementById("a11y-background-preview");
    var url = _backgroundUrl(_a11yPrefs.background_image);
    if (preview) { preview.style.backgroundImage = url ? 'url("' + url + '")' : ""; preview.classList.toggle("is-empty", !url); }
    if (backgroundClear) backgroundClear.disabled = !url;
  }
  function syncPanel() {
    syncChoice("[data-theme-mode]", "theme_mode", "themeMode");
    syncChoice("[data-scale]", "font_scale", "scale");
    syncChoice("[data-contrast]", "contrast", "contrast");
    syncChoice("[data-line-height]", "line_height", "lineHeight");
    syncChoice("[data-letter-spacing]", "letter_spacing", "letterSpacing");
    panel.querySelectorAll("[data-semantic]").forEach(function(button) {
      button.classList.toggle("active", Number(button.dataset.level) === Number(_a11yPrefs["semantic_" + button.dataset.semantic] || 0));
    });
    if (motion) motion.checked = !!_a11yPrefs.reduce_motion;
    if (language) language.value = _a11yPrefs.interface_language || "default";
    syncColors(); syncSidebarWidth(); syncBackground();
  }
  syncPanel();

  window.addEventListener("beforeunload", function() {
    if (_a11ySaveTimer) { clearTimeout(_a11ySaveTimer); _a11ySaveTimer = null; _postA11yPrefs({ keepalive: true, silent: true }).catch(function() {}); }
  });
  window.addEventListener("pageshow", function(event) { if (event.persisted) applyA11yPrefs(_a11yPrefs); });
}
