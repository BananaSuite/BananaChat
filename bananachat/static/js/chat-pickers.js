// Model and reasoning selectors for the chat composer. This module owns their
// remembered choices and keyboard interaction; chat.js owns sending and streaming.
import { el, icon, t } from "./core.js";
import { createComposerPopover } from "./chat-popover.js";

const $ = (id) => document.getElementById(id);
const MODEL_KEY = "bc-chat-model";
const EFFORT_KEY = "bc-chat-effort";

/** Initialize once. The returned model picker also supports personality preferences. */
export function initChatPickers({ models, lastModel, storage, input, announce }) {
  // ----- model picker -----------------------------------------------------------------------

  const picker = {
    button: $("model-button"),
    label: $("model-button-label"),
    popover: $("model-popover"),
    search: $("model-search"),
    list: $("model-list"),
    models: [{ name: "auto", label: t("chat_model_auto"), description: t("chat_model_auto_hint"), categories: [], reasoning: false, vision: false },
      ...models],
    selected: "auto",
    visible: [],
    active: 0,

    init() {
      const known = (name) => name && this.models.some((model) => model.name === name);
      const remembered = storage.getItem(MODEL_KEY);
      this.selected = known(remembered) ? remembered : known(lastModel) ? lastModel : "auto";
      this.updateButton();
      this.popup = createComposerPopover({
        root: $("model-picker"), button: this.button, panel: this.popover, focusTarget: this.search,
        onOpen: () => {
          this.search.value = "";
          this.active = Math.max(0, this.models.findIndex((model) => model.name === this.selected));
          this.render();
        },
      });
      this.search.addEventListener("input", () => { this.active = 0; this.render(); });
      this.search.addEventListener("keydown", (event) => this.onKey(event));
    },

    updateButton() {
      const model = this.models.find((item) => item.name === this.selected) || this.models[0];
      this.label.textContent = model.label;
      this.button.title = model.label;
      if (this.onChange) this.onChange(model);
    },
    onChange: null,

    open() { this.popup.open(); },
    close(focusButton) { this.popup.close(focusButton); },

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
        const metadata = el("span", { class: "model-metadata" },
          ...model.categories.map((name) => el("span", { text: name })),
          model.reasoning ? el("span", { text: t("chat_badge_reasoning") }) : null,
          model.vision ? el("span", { text: t("chat_badge_vision") }) : null,
          model.weight && model.weight !== 1
            ? el("span", { class: model.weight > 1 ? "model-cost" : null, text: t("chat_badge_weight", { factor: model.weight.toLocaleString(document.documentElement.lang || undefined) }) })
            : null);
        const option = el("li", {
          id: `model-option-${index}`, role: "option", class: `model-option${index === this.active ? " is-active" : ""}`,
          "aria-selected": model.name === this.selected ? "true" : "false",
        }, el("span", { class: "model-option-heading" },
          el("span", { class: "model-option-name", text: model.label }),
          model.name === this.selected ? icon("check") : null),
        model.description ? el("span", { class: "model-option-description", text: model.description }) : null,
        metadata.childElementCount ? metadata : null);
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
      }
    },

    choose(model) {
      this.selected = model.name;
      storage.setItem(MODEL_KEY, model.name);
      this.updateButton();
      this.close(false);
      announce(t("chat_model_selected", { name: model.label }));
      input.focus();
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
      this.popup = createComposerPopover({
        root: this.root, button: this.button, panel: this.popover, focusTarget: this.list,
        onOpen: () => {
          this.active = Math.max(0, this.effort.levels.findIndex((level) => level.value === this.selected));
          this.render();
        },
      });
      this.list.addEventListener("keydown", (event) => this.onKey(event));
      this.update(picker.models.find((item) => item.name === picker.selected));
    },

    remembered() {
      try {
        const saved = JSON.parse(storage.getItem(EFFORT_KEY) || "{}");
        if (saved && typeof saved === "object" && !Array.isArray(saved)) {
          return Object.assign(Object.create(null), saved);
        }
      } catch { /* An invalid browser preference should not prevent selection. */ }
      return Object.create(null);
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

    open() { this.popup.open(); },
    close(focusButton) { this.popup?.close(focusButton); },

    render() {
      this.list.replaceChildren(...this.effort.levels.map((level, index) => {
        const locked = !level.allowed;
        const option = el("li", {
          id: `effort-option-${index}`, role: "option",
          class: `model-option effort-option${index === this.active ? " is-active" : ""}${locked ? " is-locked" : ""}`,
          // A locked level stays operable: choosing it opens the request form.
          "aria-selected": level.value === this.selected ? "true" : "false",
        }, el("span", { class: "model-option-name", text: level.label }),
        locked ? el("span", { class: "effort-lock" }, icon("lock"), el("span", { text: t("chat_effort_request") }))
          : level.value === this.selected ? icon("check") : null);
        option.addEventListener("pointerdown", (event) => event.preventDefault());
        option.addEventListener("click", () => this.choose(level));
        return option;
      }));
      this.list.setAttribute("aria-activedescendant", `effort-option-${this.active}`);
      this.list.children[this.active]?.scrollIntoView({ block: "nearest" });
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
  return { model: picker, effort: effortPicker };
}
