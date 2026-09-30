// Personalities pages: live character counters, emoji quick picks and the editor's live preview.
// Everything here only enhances server-rendered forms, which work without it.
import { el, t } from "./core.js";

/** Fields with data-counter and maxlength show "used / max" below themselves. */
function initCounters(root = document) {
  for (const field of root.querySelectorAll("[data-counter][maxlength]")) {
    if (!field.id) continue;
    const limit = Number(field.getAttribute("maxlength"));
    const counter = el("span", { class: "persona-counter", id: `${field.id}-counter`, "aria-live": "off" });
    const anchor = field.nextElementSibling?.classList.contains("hint") ? field.nextElementSibling : field;
    anchor.after(counter);
    const describedBy = field.getAttribute("aria-describedby");
    field.setAttribute("aria-describedby", describedBy ? `${describedBy} ${counter.id}` : counter.id);
    const update = () => {
      const used = field.value.length;
      counter.textContent = t("personality_characters", { used, max: limit });
      counter.classList.toggle("is-near", used >= limit * 0.9 && used < limit);
      counter.classList.toggle("is-over", used >= limit);
      // Announce only near the limit, to avoid chatter on every keystroke.
      counter.setAttribute("aria-live", used >= limit * 0.9 ? "polite" : "off");
    };
    field.addEventListener("input", update);
    update();
  }
}

/** Readonly share links select themselves when focused, ready to copy. */
function initShareLinks() {
  for (const input of document.querySelectorAll("input[data-select-all]")) {
    input.addEventListener("focus", () => input.select());
  }
}

/** Keep the editor's preview card and chat preview in step with the form. */
function initEditor() {
  const form = document.getElementById("persona-form");
  if (!form) return;
  const field = (name) => form.elements.namedItem(name);
  const avatarInput = field("avatar");
  const picks = document.querySelector(".emoji-picks");
  const previewCard = document.querySelector("[data-preview-card]");
  const previewChat = document.querySelector("[data-preview-chat]");
  const placeholder = document.querySelector("[data-preview-name]")?.dataset.placeholder || "";

  const colorClass = (node, color) => {
    for (const name of [...node.classList]) if (name.startsWith("accent-")) node.classList.remove(name);
    node.classList.add(`accent-${color || "default"}`);
  };
  const avatarNode = (size) => {
    const emoji = avatarInput.value.trim();
    const name = field("name").value.trim() || placeholder;
    const node = el("span", { class: `persona-avatar${size ? ` ${size}` : ""}${emoji ? "" : " is-letter"}`, "aria-hidden": "true" },
      emoji || name.slice(0, 1).toUpperCase());
    colorClass(node, form.querySelector("input[name=color]:checked")?.value);
    return node;
  };
  const setText = (selector, text) => {
    const node = document.querySelector(selector);
    if (node) node.textContent = text;
  };

  const update = () => {
    const color = form.querySelector("input[name=color]:checked")?.value || "";
    const name = field("name").value.trim() || placeholder;
    if (previewCard) colorClass(previewCard, color);
    if (previewChat) colorClass(previewChat, color);
    document.querySelector("[data-preview-avatar]")?.replaceChildren(avatarNode(""));
    document.querySelector("[data-preview-avatar-large]")?.replaceChildren(avatarNode("xl"));
    setText("[data-preview-name]", name);
    setText("[data-preview-intro-name]", name);
    setText("[data-preview-description]", field("description").value.trim());
    setText("[data-preview-greeting]", field("greeting").value.trim());
    const starters = [1, 2, 3, 4].map((number) => field(`starter_${number}`)?.value.trim()).filter(Boolean);
    document.querySelector("[data-preview-starters]")?.replaceChildren(...starters.map((text) => el("li", { text })));
    if (picks) {
      const current = avatarInput.value.trim();
      for (const button of picks.querySelectorAll("[data-emoji]")) {
        if (button.dataset.emoji) button.setAttribute("aria-pressed", button.dataset.emoji === current ? "true" : "false");
      }
    }
  };

  if (picks) {
    picks.hidden = false;
    picks.addEventListener("click", (event) => {
      const button = event.target.closest("[data-emoji]");
      if (!button) return;
      avatarInput.value = button.dataset.emoji;
      update();
      avatarInput.focus();
    });
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
}

initCounters();
initShareLinks();
initEditor();
