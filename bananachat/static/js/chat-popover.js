// Shared interaction for the composer's model, reasoning and personality menus.
// Only one menu is open at a time. Leaving it by pointer or keyboard never moves
// focus; Escape explicitly returns to its button.
const openPopovers = new Set();

export function createComposerPopover({ root, button, panel, focusTarget, onOpen, triggers = [] }) {
  const contains = (target) => root.contains(target) || triggers.some((trigger) => trigger.contains(target));

  function close(returnFocus = false) {
    panel.hidden = true;
    button.setAttribute("aria-expanded", "false");
    openPopovers.delete(controller);
    if (returnFocus) button.focus();
  }

  function open() {
    if (!panel.hidden) return;
    for (const other of openPopovers) other.close();
    panel.hidden = false;
    button.setAttribute("aria-expanded", "true");
    openPopovers.add(controller);
    onOpen();
    focusTarget.focus();
  }

  const controller = { open, close };
  button.addEventListener("click", () => panel.hidden ? open() : close(true));
  for (const trigger of triggers) trigger.addEventListener("click", open);
  for (const trigger of [button, ...triggers]) {
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        open();
      }
    });
  }
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    }
  });
  // Close after focus has moved. Hiding a focused field during its Tab keydown
  // would make the browser lose its place in the keyboard order.
  document.addEventListener("focusin", (event) => {
    if (!panel.hidden && !contains(event.target)) close();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!panel.hidden && !contains(event.target)) close();
  });
  return controller;
}
