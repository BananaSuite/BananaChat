/* BananaSuite easter egg editor (banana-eggs/v1), shared byte for byte by BananaWiki, the hosting portal,
 * BananaChat and BananaGit. It turns
 *
 *   <div class="banana-eggs-editor" data-banana-eggs-editor data-input="ID">
 *     <script type="application/json" data-banana-eggs-editor-options>{"strings": {...}, ...}</script>
 *   </div>
 *   <input type="hidden" id="ID" name="..." value='{"version":1,"eggs":[...]}'>
 *
 * into one card per egg (key recorder, emoji picker, numbers, unlock, badge, message, preview) and keeps the
 * hidden field up to date, so the surrounding form posts the whole list as JSON. The server cleans it again
 * (eggs.py, bananagit_eggs.go) and answers with field errors; the limits here only help while typing.
 * Options: strings (labels, see SPEC.md), emojis (picker), badges ([{value, label}], empty hides the field),
 * mascot (true when the app has a mascot sprite), egg (the egg "Add" starts from, keys left empty),
 * reset (the eggs "Back to the default" restores, e.g. eggs.default_config()["eggs"]; without it, [egg]),
 * buttonClass (the app's button classes, default "btn btn-outline btn-sm"). No markup gets a style
 * attribute, so the editor works under the strictest of the four apps' content security policies.
 */
(function () {
  "use strict";

  var MAX_EGGS = 10;
  var MAX_KEYS = 32;
  var RECORD_SECONDS = 15;
  var MODIFIERS = ["Shift", "Control", "Alt", "AltGraph", "Meta", "CapsLock", "Fn", "FnLock", "OS", "Hyper", "Super"];
  var ARROWS = { ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→" };
  var KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight",
                "b", "a"];
  var NUMBERS = [
    ["count", 1, 200, 1, 30], ["speed_min", 0.3, 20, 0.1, 1.5], ["speed_max", 0.3, 20, 0.1, 3.5], ["delay", 10, 2000, 10, 80]
  ];
  var counter = 0;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (name) {
      var value = attrs[name];
      if (value === null || value === undefined || value === false) return;
      if (name === "text") node.textContent = value;
      else if (name === "className") node.className = value;
      else node.setAttribute(name, value === true ? "" : String(value));
    });
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function normaliseKey(key) {
    if (typeof key !== "string" || !key) return null;
    return key.length === 1 ? key.toLowerCase() : key;
  }

  function slug(n) { return "egg-" + n; }

  function editor(root) {
    var input = document.getElementById(root.getAttribute("data-input") || "");
    var optionsNode = root.querySelector("script[data-banana-eggs-editor-options]");
    if (!input || !optionsNode) return;
    var options;
    try { options = JSON.parse(optionsNode.textContent || "{}"); } catch (e) { return; }
    var strings = options.strings || {};
    var emojis = Array.isArray(options.emojis) ? options.emojis : [];
    var badges = Array.isArray(options.badges) ? options.badges : [];
    var mascot = options.mascot === true;
    var buttonClass = typeof options.buttonClass === "string" ? options.buttonClass : "btn btn-outline btn-sm";
    var recording = null; // {card, keys, timer, tick, stop}

    function t(name, fallback, values) {
      var text = typeof strings[name] === "string" && strings[name] ? strings[name] : fallback;
      Object.keys(values || {}).forEach(function (key) { text = text.split("{" + key + "}").join(values[key]); });
      return text;
    }

    function keyLabel(key) {
      if (ARROWS[key]) return ARROWS[key];
      if (key === " ") return t("key_space", "Space");
      return key.length === 1 ? key.toUpperCase() : key;
    }

    var list = el("div", { className: "banana-eggs-editor__list" });
    var addButton = el("button", { type: "button", className: buttonClass, text: t("add", "Add an easter egg") });
    var resetButton = el("button", { type: "button", className: buttonClass, text: t("reset", "Back to the default egg") });
    var limitNote = el("p", { className: "banana-eggs-editor__hint", text: t("limit", "Up to {n} easter eggs.", { n: MAX_EGGS }) });

    function stopRecording() {
      if (!recording) return;
      var r = recording;
      recording = null;
      window.clearInterval(r.tick);
      document.removeEventListener("keydown", r.listener, true);
      r.card.keysBox.classList.remove("banana-eggs-editor__keys--recording");
      r.card.recordButton.textContent = t("record", "Record keys");
      r.card.recordButton.setAttribute("aria-pressed", "false");
      if (r.keys.length) r.card.keys = r.keys;
      renderKeys(r.card);
      sync();
    }

    function renderKeys(card, keys) {
      var shown = keys || card.keys;
      card.keysBox.textContent = "";
      if (!shown.length) {
        card.keysBox.appendChild(document.createTextNode(t("keys_empty", "No keys yet")));
        return;
      }
      shown.forEach(function (key) {
        card.keysBox.appendChild(el("kbd", { className: "banana-eggs-editor__key", text: keyLabel(key) }));
      });
    }

    function startRecording(card) {
      if (recording) stopRecording();
      var r = { card: card, keys: [], left: RECORD_SECONDS };
      r.listener = function (event) {
        if (event.target === card.recordButton && (event.key === "Enter" || event.key === " ")) return;
        if (event.key === "Escape") { event.preventDefault(); stopRecording(); return; }
        if (event.repeat || event.ctrlKey || event.metaKey || event.altKey || MODIFIERS.indexOf(event.key) !== -1) return;
        var key = normaliseKey(event.key);
        if (!key || key.length > 16) return;
        event.preventDefault();
        event.stopPropagation();
        r.keys.push(key);
        renderKeys(card, r.keys);
        if (r.keys.length >= MAX_KEYS) stopRecording();
      };
      r.tick = window.setInterval(function () {
        r.left -= 1;
        card.recordButton.textContent = t("stop", "Stop ({s})", { s: r.left });
        if (r.left <= 0) stopRecording();
      }, 1000);
      recording = r;
      document.addEventListener("keydown", r.listener, true);
      card.keysBox.classList.add("banana-eggs-editor__keys--recording");
      card.recordButton.textContent = t("stop", "Stop ({s})", { s: r.left });
      card.recordButton.setAttribute("aria-pressed", "true");
      renderKeys(card, r.keys);
    }

    function field(card, labelText, control, wide) {
      var id = "banana-eggs-" + (counter += 1);
      control.id = id;
      return el("div", { className: "banana-eggs-editor__field" + (wide ? " banana-eggs-editor__field--wide" : "") },
        [el("label", { "for": id, text: labelText }), control]);
    }

    function check(labelText, checked) {
      var box = el("input", { type: "checkbox" });
      box.checked = !!checked;
      var id = "banana-eggs-" + (counter += 1);
      box.id = id;
      return { box: box, node: el("div", { className: "banana-eggs-editor__check" }, [box, el("label", { "for": id, text: labelText })]) };
    }

    function card(raw) {
      raw = raw && typeof raw === "object" ? raw : {};
      var c = { keys: Array.isArray(raw.keys) ? raw.keys.map(normaliseKey).filter(Boolean).slice(0, MAX_KEYS) : [] };
      var sprite = raw.sprite && typeof raw.sprite === "object" ? raw.sprite : {};
      var unlock = raw.unlock && typeof raw.unlock === "object" ? raw.unlock : {};

      c.title = el("h3", { className: "banana-eggs-editor__title" });
      c.id = el("input", { type: "text", maxlength: 32, pattern: "[a-z0-9-]{1,32}", spellcheck: "false", value: raw.id || "" });
      c.enabled = check(t("enabled", "On"), raw.enabled !== false);

      c.keysBox = el("div", { className: "banana-eggs-editor__keys", "aria-live": "polite" });
      c.recordButton = el("button", { type: "button", className: buttonClass, "aria-pressed": "false", text: t("record", "Record keys") });
      var konamiButton = el("button", { type: "button", className: buttonClass, text: t("konami", "Konami code") });

      c.spriteType = el("select", {}, [
        el("option", { value: "emoji", text: t("sprite_emoji", "Emoji") }),
        mascot ? el("option", { value: "mascot", text: t("sprite_mascot", "The mascot") }) : null
      ]);
      c.spriteType.value = mascot && sprite.type === "mascot" ? "mascot" : "emoji";
      c.emoji = el("input", { type: "text", maxlength: 16, value: typeof sprite.value === "string" ? sprite.value : "🍌" });
      var picker = el("div", { className: "banana-eggs-editor__emojis", role: "group", "aria-label": t("emoji_pick", "Pick an emoji") });
      emojis.forEach(function (emoji) {
        var b = el("button", { type: "button", className: "banana-eggs-editor__emoji", "aria-pressed": "false", text: emoji });
        b.addEventListener("click", function () { c.emoji.value = emoji; c.spriteType.value = "emoji"; sync(); });
        picker.appendChild(b);
      });
      c.picker = picker;

      c.numbers = {};
      var numberFields = NUMBERS.map(function (spec) {
        var value = raw[spec[0]];
        var input = el("input", { type: "number", min: spec[1], max: spec[2], step: spec[3],
                                  value: typeof value === "number" ? value : spec[4] });
        c.numbers[spec[0]] = input;
        return field(c, t(spec[0], spec[0]), input);
      });

      c.action = el("select", {}, [
        el("option", { value: "page", text: t("unlock_page", "Open the easter egg page") }),
        el("option", { value: "toast", text: t("unlock_toast", "Show a message") }),
        el("option", { value: "none", text: t("unlock_none", "Nothing else") })
      ]);
      c.action.value = ["page", "toast", "none"].indexOf(unlock.action) !== -1 ? unlock.action : "page";
      c.unlockDelay = el("input", { type: "number", min: 100, max: 30000, step: 100,
                                    value: typeof unlock.delay === "number" ? unlock.delay : 2500 });
      c.record = check(t("record_find", "Remember who found it"), raw.record !== false);
      c.badge = badges.length ? el("select", {}, [el("option", { value: "", text: t("badge_none", "No badge") })]
        .concat(badges.map(function (b) { return el("option", { value: b.value, text: b.label }); }))) : null;
      if (c.badge) c.badge.value = raw.badge === null || raw.badge === undefined ? "" : String(raw.badge);
      c.message = el("input", { type: "text", maxlength: 200, value: typeof raw.message === "string" ? raw.message : "" });

      var previewButton = el("button", { type: "button", className: buttonClass, text: t("preview", "Preview") });
      var removeButton = el("button", { type: "button", className: buttonClass, text: t("remove", "Remove") });

      c.node = el("section", { className: "banana-eggs-editor__card" }, [
        el("div", { className: "banana-eggs-editor__head" }, [c.title, el("div", { className: "banana-eggs-editor__actions" }, [previewButton, removeButton])]),
        el("div", { className: "banana-eggs-editor__row" }, [field(c, t("id", "Name (a-z, 0-9, -)"), c.id), c.enabled.node]),
        el("div", { className: "banana-eggs-editor__field banana-eggs-editor__field--wide" }, [
          el("span", { text: t("keys", "Key sequence") }), c.keysBox,
          el("div", { className: "banana-eggs-editor__actions" }, [c.recordButton, konamiButton]),
          el("p", { className: "banana-eggs-editor__hint", text: t("keys_hint", "Press Record, type 2 to 32 keys, then Stop (Escape also stops).") })
        ]),
        el("div", { className: "banana-eggs-editor__row" }, [field(c, t("sprite", "What falls"), c.spriteType), field(c, t("emoji", "Emoji"), c.emoji)]),
        picker,
        el("div", { className: "banana-eggs-editor__row" }, numberFields),
        el("div", { className: "banana-eggs-editor__row" }, [field(c, t("unlock", "Afterwards"), c.action), field(c, t("unlock_delay", "Wait (ms)"), c.unlockDelay)]),
        el("div", { className: "banana-eggs-editor__row" }, [c.record.node, c.badge ? field(c, t("badge", "Badge"), c.badge) : null]),
        field(c, t("message", "Message (optional)"), c.message, true)
      ]);

      c.recordButton.addEventListener("click", function () {
        if (recording && recording.card === c) stopRecording(); else startRecording(c);
      });
      konamiButton.addEventListener("click", function () { c.keys = KONAMI.slice(); renderKeys(c); sync(); });
      previewButton.addEventListener("click", function () {
        if (window.BananaEggs && typeof window.BananaEggs.preview === "function") window.BananaEggs.preview(value(c));
      });
      removeButton.addEventListener("click", function () {
        if (recording && recording.card === c) stopRecording();
        cards.splice(cards.indexOf(c), 1);
        list.removeChild(c.node);
        sync();
      });
      c.node.addEventListener("input", sync);
      c.node.addEventListener("change", sync);
      renderKeys(c);
      return c;
    }

    function numberValue(input, fallback) {
      var n = parseFloat(input.value);
      return isFinite(n) ? n : fallback;
    }

    function value(c) {
      var egg = {
        id: c.id.value.trim().toLowerCase(),
        enabled: c.enabled.box.checked,
        keys: c.keys.slice(),
        sprite: c.spriteType.value === "mascot" ? { type: "mascot" } : { type: "emoji", value: c.emoji.value.trim() },
        unlock: { action: c.action.value, delay: numberValue(c.unlockDelay, 2500) },
        record: c.record.box.checked,
        badge: c.badge && c.badge.value ? c.badge.value : null,
        message: c.message.value.trim()
      };
      NUMBERS.forEach(function (spec) { egg[spec[0]] = numberValue(c.numbers[spec[0]], spec[4]); });
      return egg;
    }

    var cards = [];

    function sync() {
      cards.forEach(function (c, i) {
        c.title.textContent = t("title", "Easter egg {n}", { n: i + 1 }) + (c.id.value ? " · " + c.id.value : "");
        c.emoji.disabled = c.spriteType.value === "mascot";
        Array.prototype.forEach.call(c.picker.children, function (b) {
          b.setAttribute("aria-pressed", String(c.spriteType.value === "emoji" && b.textContent === c.emoji.value));
        });
      });
      addButton.disabled = cards.length >= MAX_EGGS;
      input.value = JSON.stringify({ version: 1, eggs: cards.map(value) });
    }

    function add(raw) {
      var c = card(raw);
      cards.push(c);
      list.appendChild(c.node);
      return c;
    }

    var initial;
    try { initial = JSON.parse(input.value || "{}"); } catch (e) { initial = {}; }
    var eggs = initial && Array.isArray(initial.eggs) ? initial.eggs : Array.isArray(initial) ? initial : [];
    eggs.slice(0, MAX_EGGS).forEach(add);

    addButton.addEventListener("click", function () {
      if (cards.length >= MAX_EGGS) return;
      var used = cards.map(function (c) { return c.id.value; });
      var n = cards.length + 1;
      while (used.indexOf(slug(n)) !== -1) n += 1;
      var base = options.egg && typeof options.egg === "object" ? JSON.parse(JSON.stringify(options.egg)) : {};
      base.id = slug(n);
      base.keys = [];
      var c = add(base);
      sync();
      c.id.focus();
    });
    resetButton.addEventListener("click", function () {
      if (recording) stopRecording();
      cards.slice().forEach(function (c) { list.removeChild(c.node); });
      cards = [];
      var defaults = Array.isArray(options.reset) ? options.reset
        : options.egg && typeof options.egg === "object" ? [options.egg] : [];
      defaults.slice(0, MAX_EGGS).forEach(function (raw) { add(JSON.parse(JSON.stringify(raw))); });
      sync();
    });

    root.appendChild(list);
    root.appendChild(el("div", { className: "banana-eggs-editor__actions" }, [addButton, resetButton]));
    root.appendChild(limitNote);
    sync();
  }

  function start() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-banana-eggs-editor]"), editor);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
