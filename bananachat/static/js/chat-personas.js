// Chat page: the personality picker, the active personality in the header, and the greeting and
// conversation starters shown in an empty chat. Used by chat.js.
//
// Personality texts come from page data and are only ever set with textContent.
import { api, el, icon, t, toast } from "./core.js";
import { createComposerPopover } from "./chat-popover.js";

const $ = (id) => document.getElementById(id);
const COLOR = /^[a-z]{1,16}$/;

/** The avatar of a personality (emoji, or the first letter of its name) in its accent colour. */
export function personaAvatar(persona, size = "") {
  const color = COLOR.test(persona.color || "") ? persona.color : "default";
  const emoji = persona.avatar || "";
  return el("span", {
    class: `persona-avatar accent-${color}${size ? ` ${size}` : ""}${emoji ? "" : " is-letter"}`, "aria-hidden": "true",
  }, emoji || (persona.name || "?").slice(0, 1).toUpperCase());
}

/**
 * Set up the picker. Options:
 *   personalities  list from page data (null when the account may not use personalities)
 *   session        page data session (personality_id is updated in place)
 *   chatUrl        (id, action) => URL
 *   models         model picker with .use(name) and .has(name)
 *   isEmpty        () => true while the conversation has no messages
 *   fillInput      (text) => put a starter in the message box
 *   announce       (text) => screen reader announcement
 *   manageUrl      link to the personalities page
 */
export function initPersonas({ personalities, session, chatUrl, models, isEmpty, fillInput, announce, manageUrl }) {
  const picker = $("persona-picker");
  const intro = $("persona-intro");
  const welcome = $("chat-welcome");
  const starters = $("persona-starters");
  const chip = $("persona-chip");

  // Starters work even when the picker is not shown (the chat keeps the personality it has).
  starters?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-starter]");
    if (button) fillInput(button.dataset.starter);
  });
  if (!personalities || !picker) return;

  const button = $("persona-button");
  const popover = $("persona-popover");
  const list = $("persona-list");
  const none = { id: null, name: t("personality_none"), description: t("personality_none_hint"), avatar: "", color: "" };
  const own = personalities.filter((item) => !item.featured);
  const featured = personalities.filter((item) => item.featured);
  const options = [none, ...own, ...featured];
  let current = personalities.find((item) => item.id === session.personality_id) || null;
  let active = 0;
  let busy = false;

  function renderButton() {
    const label = current ? current.name : t("personality_none");
    button.replaceChildren(current ? personaAvatar(current, "xs") : icon("user"),
      el("span", { class: "truncate persona-button-label", text: label }), icon("chevron-down"));
    button.classList.toggle("is-set", Boolean(current));
    button.setAttribute("aria-label", t("personality_button", { name: label }));
    button.title = t("personality_button", { name: label });
    if (chip) {
      chip.hidden = !current;
      if (current) {
        const color = COLOR.test(current.color || "") ? current.color : "default";
        chip.className = `persona-chip accent-${color}`;
        chip.replaceChildren(personaAvatar(current, "xs"), el("span", { class: "truncate", text: current.name }));
        chip.setAttribute("aria-label", t("personality_change", { name: current.name }));
        chip.title = t("personality_change", { name: current.name });
      }
    }
  }

  function renderIntro() {
    if (!intro) return;
    const persona = current;
    intro.hidden = !persona;
    if (welcome) welcome.hidden = Boolean(persona);
    const starterList = persona ? persona.starters || [] : [];
    if (starters) {
      starters.hidden = starterList.length === 0;
      starters.replaceChildren(...starterList.map((text) => el("button", {
        type: "button", class: "suggestion", dataset: { starter: text },
      }, el("span", { class: "suggestion-text", text }))));
    }
    if (!persona) { intro.replaceChildren(); return; }
    const color = COLOR.test(persona.color || "") ? persona.color : "default";
    intro.className = `persona-intro accent-${color}`;
    intro.replaceChildren(...[
      personaAvatar(persona, "xl"),
      el("h2", { class: "persona-intro-name", text: persona.name }),
      persona.description ? el("p", { class: "persona-intro-description", text: persona.description }) : null,
      persona.greeting ? el("p", { class: "persona-greeting", text: persona.greeting }) : null,
      persona.preferred_model && !persona.preferred_available
        ? el("p", { class: "persona-note", text: t("personality_model_unavailable", { model: persona.preferred_model }) }) : null,
    ].filter(Boolean));
  }

  /** Select the personality's preferred model when a chat starts with it (the user can still change it). */
  function preferModel(persona, { announceIt = false } = {}) {
    if (!persona || !persona.preferred_model || !isEmpty()) return;
    if (persona.preferred_available && models.has(persona.preferred_model)) {
      models.use(persona.preferred_model);
      if (announceIt) announce(t("personality_model_selected", { model: models.labelOf(persona.preferred_model), name: persona.name }));
    }
  }

  function optionNode(persona, index) {
    const selected = (persona.id ?? null) === (current ? current.id : null);
    const node = el("li", {
      id: `persona-option-${index}`, role: "option", class: `model-option persona-option${index === active ? " is-active" : ""}`,
      "aria-selected": selected ? "true" : "false",
    }, persona.id === null ? el("span", { class: "persona-avatar persona-avatar-none", "aria-hidden": "true" }, icon("user"))
      : personaAvatar(persona),
    el("span", { class: "persona-option-text" },
      el("span", { class: "model-option-name", text: persona.name }),
      persona.description ? el("span", { class: "model-option-description", text: persona.description }) : null));
    node.addEventListener("pointerdown", (event) => event.preventDefault());
    node.addEventListener("click", () => choose(persona));
    return node;
  }

  function render() {
    const items = [];
    options.forEach((persona, index) => {
      if (index === 1 && own.length) items.push(el("li", { class: "persona-group-label", role: "presentation", text: t("personality_yours") }));
      if (index === 1 + own.length && featured.length) items.push(el("li", { class: "persona-group-label", role: "presentation", text: t("personality_featured") }));
      items.push(optionNode(persona, index));
    });
    list.replaceChildren(...items);
    list.setAttribute("aria-activedescendant", `persona-option-${active}`);
    $(`persona-option-${active}`)?.scrollIntoView({ block: "nearest" });
  }

  const popup = createComposerPopover({
    root: picker, button, panel: popover, focusTarget: list, triggers: chip ? [chip] : [],
    onOpen: () => {
      active = Math.max(0, options.findIndex((item) => (item.id ?? null) === (current ? current.id : null)));
      render();
    },
  });

  async function choose(persona) {
    const next = persona && persona.id !== null ? persona : null;
    popup.close(true);
    if ((next ? next.id : null) === (current ? current.id : null) || busy) return;
    busy = true;
    try {
      await api(chatUrl(session.id, "personality"), { json: { personality_id: next ? next.id : null } });
    } catch (error) {
      toast(error.message, "error");
      return;
    } finally {
      busy = false;
    }
    current = next;
    session.personality_id = next ? next.id : null;
    renderButton();
    renderIntro();
    const message = next ? t("personality_set", { name: next.name }) : t("personality_cleared");
    toast(message, "success", { timeout: 2500 });
    announce(message);
    preferModel(next, { announceIt: true });
  }

  function onKey(event) {
    const count = options.length;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      active = (active + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
      render();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      active = event.key === "Home" ? 0 : count - 1;
      render();
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      choose(options[active]);
    }
  }

  list.addEventListener("keydown", onKey);
  if (manageUrl) $("persona-manage")?.setAttribute("href", manageUrl);

  renderButton();
  renderIntro();
  preferModel(current);
}
