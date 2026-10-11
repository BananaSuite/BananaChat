// Administrator page for external API providers (English-only by design). Progressive enhancement:
// every form also works without this script (a blank base URL takes the chosen service's connection).
import { pageData } from "./core.js";

const presets = pageData().presets || {};

function headerText(headers) {
  return Object.entries(headers || {}).map(([name, value]) => `${name}: ${value}`).join("\n");
}

// Choosing a service fills in its connection; every value stays editable.
function initPresetForms() {
  for (const form of document.querySelectorAll("[data-provider-form]")) {
    const select = form.querySelector("[data-preset-select]");
    if (!select) continue;
    const field = (name) => form.querySelector(`[data-preset-field="${name}"]`);
    const hint = form.querySelector("[data-preset-hint]");
    const name = form.querySelector("[data-preset-name]");
    select.addEventListener("change", () => {
      const preset = presets[select.value];
      if (!preset) return;
      const url = field("base_url");
      url.value = preset.base_url;
      url.placeholder = preset.base_url || "https://api.example.com/v1";
      field("protocol").value = preset.protocol;
      field("auth_style").value = preset.auth_style;
      field("headers").value = headerText(preset.headers);
      field("local").checked = Boolean(preset.local);
      if (name && !name.value && select.value !== "custom") name.value = preset.label.replace(/ \(local\)$/, "");
      if (hint) hint.textContent = preset.hint || "";
      if (preset.base_url.includes("YOUR-RESOURCE")) {
        url.focus();
        url.select();
      }
    });
  }
}

// Filter a provider's model list by name or ID.
function initModelFilters() {
  for (const input of document.querySelectorAll("[data-model-filter]")) {
    const table = document.getElementById(input.dataset.modelFilter);
    if (!table) continue;
    input.addEventListener("input", () => {
      const query = input.value.trim().toLowerCase();
      for (const row of table.querySelectorAll("[data-model-row]")) {
        row.hidden = Boolean(query) && !row.textContent.toLowerCase().includes(query);
      }
    });
  }
}

initPresetForms();
initModelFilters();
