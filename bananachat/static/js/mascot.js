// The mascot in place of the top bar logo (partials/mascot.html, css/mascot.css): it hops on every click;
// enough clicks in a row put its sunglasses on, kept in this browser, and as many again take them off.
// Each of these easter eggs can be switched off for the site (Administration → Settings) and by each person
// (Appearance): the button's data-mascot-motion and -shades say which are on, read at every use so the
// customise page can change them live. The eggs typed on the keyboard (the Konami code's shower and the
// others an administrator adds) are BananaSuite's shared banana-eggs.js, not part of the mascot.
//
// Ported from BananaWiki mascot a4d2870e4ed4 (bananawiki/core/mascot.py and bananawiki/core/static/mascot/
// in that tree). When those change, BananaWiki's scripts/make_icons.py fails until the change is ported here
// and recorded with its --ported option.
import { t, toast } from "./core.js";

const STORE_KEY = "bananachat.mascot.shades";
const SVG_NS = "http://www.w3.org/2000/svg";

function remembered() {
  try {
    return window.localStorage.getItem(STORE_KEY) === "1";
  } catch {
    return false;
  }
}

function remember(on) {
  try {
    if (on) window.localStorage.setItem(STORE_KEY, "1");
    else window.localStorage.removeItem(STORE_KEY);
  } catch {
    // Storage blocked: the sunglasses stay until the page is left.
  }
}

const on = (button, egg) => button.dataset[egg] !== "0"; // an easter egg is on for this page

function initMascot(button, sprite) {
  const needed = Math.max(1, Number.parseInt(button.dataset.mascotClicks, 10) || 0);
  const timers = {};
  let clicks = 0;

  const replay = (className, ms) => {
    clearTimeout(timers[className]); // a click mid-hop restarts it instead of cutting the next one short
    button.classList.remove(className);
    void button.offsetWidth; // restart the animation
    button.classList.add(className);
    timers[className] = setTimeout(() => button.classList.remove(className), ms);
  };

  const wear = (on) => {
    sprite.classList.toggle("mascot-sprite--shades", on);
    const label = t(on ? "mascot.label_shades" : "mascot.label");
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
  };

  // The glint is one white art pixel wide and runs across both lenses once they have landed (mascot.css).
  const drop = () => {
    const shades = sprite.querySelector(".mascot__shades");
    if (shades && !shades.querySelector(".mascot__glint")) {
      const glint = document.createElementNS(SVG_NS, "rect");
      const attributes = { class: "mascot__glint", x: 4, y: 6, width: 1, height: 2, fill: "#ffffff" };
      for (const [name, value] of Object.entries(attributes)) glint.setAttribute(name, value);
      shades.append(glint);
    }
    replay("mascot--shades-drop", 950);
  };

  if (remembered() && on(button, "mascotShades")) wear(true);

  // Switched off live (customise page): the sunglasses come off and a half-done click count is forgotten.
  button.addEventListener("bananachat:eggs", () => {
    if (!on(button, "mascotShades") && sprite.classList.contains("mascot-sprite--shades")) wear(false);
    else if (on(button, "mascotShades") && remembered()) wear(true);
    clicks = 0;
    button.classList.remove("mascot--eager");
  });

  button.addEventListener("click", () => {
    if (!on(button, "mascotShades")) {
      if (on(button, "mascotMotion")) replay("mascot--hop", 450);
      return;
    }
    clicks += 1;
    // Past half way, the hop gets higher.
    if (clicks * 2 >= needed) button.classList.add("mascot--eager");
    if (on(button, "mascotMotion")) replay("mascot--hop", 450);
    if (clicks < needed) return;
    const wearing = !sprite.classList.contains("mascot-sprite--shades");
    remember(wearing);
    wear(wearing);
    if (wearing) drop();
    toast(t(wearing ? "mascot.unlocked" : "mascot.shades_off"), "success");
    clicks = 0;
    button.classList.remove("mascot--eager");
  });
}

const button = document.querySelector("[data-mascot]");
const sprite = button?.querySelector(".mascot-sprite");
if (sprite) initMascot(button, sprite);
