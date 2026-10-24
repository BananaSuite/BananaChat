"""The mascot in the top bar: markup, translations, assets and less motion; its script runs under Node.

The script tests are skipped when Node is not installed, except where
``BC_REQUIRE_NODE=1`` (set in CI).
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.app.test_chat_markdown import NODE, require_node
from tests.app.test_limits_tokens import _signed_in

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "bananachat" / "static"
SCRIPT = STATIC / "js" / "mascot.js"
STYLES = STATIC / "css" / "mascot.css"
LABELS = {"en": "BananaChat mascot", "it": "Mascotte di BananaChat", "de": "BananaChat-Maskottchen"}


def _topbar(html: str) -> str:
    return html[html.index('<header class="topbar">'):html.index("</header>")]


def test_the_mascot_takes_the_logo_s_place_and_the_name_still_links_home(app):
    topbar = _topbar(_signed_in(app, "admin").get("/account").get_data(as_text=True))
    button = re.search(r'<button type="button" class="mascot" data-mascot data-mascot-clicks="11" '
                       r'data-mascot-motion="1" data-mascot-shades="1" data-mascot-konami="1" '
                       r'aria-label="BananaChat mascot" title="BananaChat mascot">\s*'
                       r'<span class="mascot__body">(<svg class="mascot-sprite" .*?</svg>)</span>\s*</button>',
                       topbar, re.S)
    assert button
    sprite = button.group(1)
    assert 'aria-hidden="true"' in sprite and 'shape-rendering="crispEdges"' in sprite
    assert '<g class="mascot__eyes">' in sprite and '<g class="mascot__shades">' in sprite
    assert "mascot-sprite--shades" not in sprite and "mascot__glint" not in sprite
    assert "<img" not in topbar
    brand = re.search(r'<a class="brand" href="/">\s*<span>Test Chat</span>\s*</a>', topbar)
    assert brand and topbar.index("data-mascot") < brand.start()


@pytest.mark.parametrize("language", sorted(LABELS))
def test_the_label_and_the_messages_are_translated(app, language):
    html = _signed_in(app, "admin", language=language).get("/account").get_data(as_text=True)
    assert f'aria-label="{LABELS[language]}" title="{LABELS[language]}"' in _topbar(html)
    boot = json.loads(re.search(r'<script type="application/json" id="bc-boot">(.*?)</script>', html, re.S).group(1))
    strings = boot["strings"]
    assert strings["mascot.label"] == LABELS[language]
    assert LABELS[language] in strings["mascot.label_shades"]
    assert strings["mascot.unlocked"] and strings["mascot.shades_off"]
    if language != "en":
        english = _signed_in(app, "admin").get("/account").get_data(as_text=True)
        assert strings["mascot.shades_off"] not in english


def test_every_page_with_the_top_bar_loads_the_script_and_the_stylesheet(admin):
    html = admin.get("/account").get_data(as_text=True)
    css = re.search(r'<link rel="stylesheet" href="(/static/v[0-9a-f]{20}/css/mascot\.css)">', html)
    js = re.search(r'<script type="module" src="(/static/v[0-9a-f]{20}/js/mascot\.js)"></script>', html)
    assert css and js
    for url, path, mimetype in ((css.group(1), STYLES, "text/css"), (js.group(1), SCRIPT, "text/javascript")):
        response = admin.get(url)
        assert (response.status_code, response.mimetype) == (200, mimetype)
        assert response.data == path.read_bytes()
    # A module of the same tree, so it shares core.js (strings and toasts) with the page.
    assert 'import { t, toast } from "./core.js";' in SCRIPT.read_text(encoding="utf-8")


def test_the_script_names_the_bananawiki_mascot_it_follows():
    assert re.search(r"BananaWiki mascot [0-9a-f]{12} ", SCRIPT.read_text(encoding="utf-8"))


def test_the_mascot_stands_still_when_less_motion_is_asked():
    css = STYLES.read_text(encoding="utf-8")
    system = css[css.rindex("@media (prefers-reduced-motion: reduce)"):]
    assert ".mascot__body" in system and ".mascot-sprite .mascot__eyes" in system and "animation: none" in system
    assert ".mascot-shower { display: none; }" in system
    # The Customise preference, which app.css only shortens to no time.
    preference = css[css.index("body.reduce-motion :is("):]
    assert ".mascot__body" in preference and "animation: none" in preference
    assert "body.reduce-motion .mascot-shower { display: none; }" in preference


# Behaviour (Node)

HARNESS = r"""
const scenario = JSON.parse(%(scenario)s);
function classes(initial = []) {
  const names = new Set(initial);
  return { add: (n) => names.add(n), remove: (n) => names.delete(n), contains: (n) => names.has(n),
           toggle: (n, on) => (on ? names.add(n) : names.delete(n)) };
}
let now = 0, nextId = 1, timers = [];
globalThis.setTimeout = (fn, ms) => { const id = nextId++; timers.push({ id, at: now + (ms || 0), fn }); return id; };
globalThis.clearTimeout = (id) => { timers = timers.filter((timer) => timer.id !== id); };
function advance(ms) {
  const end = now + ms;
  for (;;) {
    const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break;
    clearTimeout(due.id);
    now = due.at;
    due.fn();
  }
  now = end;
}
function element(className = "") {
  const node = { className, classList: classes(), style: {}, children: [], parent: null, attributes: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); } };
  return node;
}
const shades = element("mascot__shades");
shades.querySelector = (selector) => shades.children.find((c) => selector === ".mascot__glint"
                                                         && c.attributes.class === "mascot__glint") || null;
const sprite = { classList: classes(), querySelector: (s) => (s === ".mascot__shades" ? shades : null),
                 cloneNode: () => ({ classList: classes(["mascot-sprite"]) }) };
const attributes = {};
let onClick = null, onKey = null;
const button = {
  classList: classes(), dataset: { mascotClicks: "11" }, offsetWidth: 32,
  setAttribute: (name, value) => { attributes[name] = String(value); },
  querySelector: (s) => (s === ".mascot-sprite" ? sprite : null),
  addEventListener: (type, fn) => { if (type === "click") onClick = fn; },
};
const body = element();
body.classList = classes(scenario.reduceSwitch ? ["reduce-motion"] : []);
const showers = () => body.children.filter((child) => child.className === "mascot-shower");
globalThis.document = {
  body,
  createElement: () => element(),
  createElementNS: () => element(),
  querySelector: (s) => (s === "[data-mascot]" ? button : s === ".mascot-shower" ? showers()[0] || null : null),
  addEventListener: (type, fn) => { if (type === "keydown") onKey = fn; },
};
const stored = new Map(Object.entries(scenario.stored || {}));
const storage = {
  getItem: (key) => { if (scenario.blocked) throw new Error("denied"); return stored.has(key) ? stored.get(key) : null; },
  setItem: (key, value) => { if (scenario.blocked) throw new Error("denied"); stored.set(key, String(value)); },
  removeItem: (key) => { if (scenario.blocked) throw new Error("denied"); stored.delete(key); },
};
const toasts = [];
globalThis.window = { localStorage: storage,
                      matchMedia: (q) => ({ matches: !!scenario.reduced && q === "(prefers-reduced-motion: reduce)" }) };
globalThis.__core = { t: (key) => "t:" + key, toast: (message, kind) => toasts.push([message, kind]) };
await import(%(module)s);
const snapshots = [];
for (const step of scenario.steps) {
  if (step === "click") onClick();
  else if (step.startsWith("key:")) {
    const key = step.slice(4);
    const target = key === "a@input" ? { tagName: "INPUT" } : { tagName: "BODY" };
    onKey({ key: key.split("@")[0], target, defaultPrevented: false, ctrlKey: false, metaKey: false, altKey: false });
  }
  else if (step.startsWith("wait:")) advance(Number(step.slice(5)));
  else if (step === "state") snapshots.push({
    shades: sprite.classList.contains("mascot-sprite--shades"), label: attributes["aria-label"] || null,
    title: attributes.title || null, stored: stored.get("bananachat.mascot.shades") ?? null,
    toasts: toasts.splice(0), drop: button.classList.contains("mascot--shades-drop"),
    eager: button.classList.contains("mascot--eager"), glint: shades.children.length,
    lean: button.classList.contains("mascot--lean"), hop: button.classList.contains("mascot--hop"),
    shower: showers().map((layer) => layer.children.length) });
}
console.log(JSON.stringify(snapshots));
"""


def run(steps, **scenario):
    require_node()
    source = SCRIPT.read_text(encoding="utf-8")
    imported = 'import { t, toast } from "./core.js";'
    assert imported in source
    module = source.replace(imported, "const { t, toast } = globalThis.__core;")
    url = "data:text/javascript;base64," + base64.b64encode(module.encode()).decode()
    script = HARNESS % {"scenario": json.dumps(json.dumps({**scenario, "steps": steps})), "module": json.dumps(url)}
    result = subprocess.run([NODE, "--input-type=module"], input=script, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "B", "a"]


def test_eleven_clicks_put_the_sunglasses_on_and_eleven_more_take_them_off():
    before, eleventh, landed, still_on, off = run(
        ["click"] * 10 + ["state", "click", "state", "wait:1000", "state"] + ["click"] * 10 + ["state", "click", "state"])
    assert not before["shades"] and before["eager"] and before["stored"] is None and before["toasts"] == []
    assert eleventh["shades"] and eleventh["drop"] and eleventh["glint"] == 1 and eleventh["stored"] == "1"
    assert eleventh["label"] == eleventh["title"] == "t:mascot.label_shades"
    assert eleventh["toasts"] == [["t:mascot.unlocked", "success"]] and not eleventh["eager"]
    assert not landed["drop"] and landed["shades"]
    assert still_on["shades"] and still_on["toasts"] == []
    assert not off["shades"] and off["stored"] is None and off["label"] == "t:mascot.label"
    assert off["toasts"] == [["t:mascot.shades_off", "success"]] and off["glint"] == 1


def test_the_browser_keeps_the_sunglasses_for_the_next_page():
    (loaded,) = run(["state"], stored={"bananachat.mascot.shades": "1"})
    assert loaded["shades"] and loaded["label"] == "t:mascot.label_shades" and loaded["toasts"] == []
    (fresh,) = run(["state"])
    assert not fresh["shades"] and fresh["label"] is None


def test_blocked_storage_keeps_them_for_the_page_view():
    (blocked,) = run(["click"] * 11 + ["state"], blocked=True)
    assert blocked["shades"] and blocked["toasts"] == [["t:mascot.unlocked", "success"]]


def test_the_konami_code_sends_one_shower_and_typing_does_not():
    shower, gone, typed = run([f"key:{key}" for key in KONAMI] + ["state", "wait:3000", "state"]
                              + [f"key:{key}" for key in KONAMI[:-1]] + ["key:a@input", "state"])
    assert shower["shower"] == [10] and shower["hop"]
    assert gone["shower"] == []
    assert typed["shower"] == []


@pytest.mark.parametrize("scenario", [{"reduced": True}, {"reduceSwitch": True}])
def test_with_less_motion_the_konami_code_only_leans_the_mascot(scenario):
    (still,) = run([f"key:{key}" for key in KONAMI] + ["state"], **scenario)
    assert still["lean"] and not still["hop"] and still["shower"] == []
