/* BananaSuite easter eggs, format banana-eggs/v1. This file is shared byte for byte by BananaWiki, the
 * BananaWiki Hosting portal, BananaChat and BananaGit: change it in BananaWiki and copy it to the others.
 *
 * The egg list comes only from <script type="application/json" id="banana-eggs"> in the document head, which
 * the layout renders before any page content, so nothing a page contains can stand in for it. The server has
 * already cleaned it (eggs.py, bananagit_eggs.go); the checks here only keep a broken list from breaking a page.
 *
 * A finished key sequence drops the egg's emoji (or the mascot sprite) down the page, records the find for a
 * signed-in person (in this browser for everyone else) and then, as the egg asks, opens the easter egg page,
 * shows a message or does nothing. Keys pressed while typing in a field, with Ctrl/Alt/Meta held, or held down
 * (auto-repeat) never count; Shift alone is ignored, so "B" counts as "b". With reduced motion asked for (the
 * system setting, the config, or data-reduce-motion="true" on <html>) nothing falls: one still item instead.
 * Unsaved edits are never thrown away: with a changed field on the page, "page" becomes a message with a link.
 *
 * window.BananaEggs.preview(egg) plays an egg without recording it or leaving the page (the editor's preview).
 */
(function () {
  "use strict";

  var VERSION = 1;
  var MAX_ITEMS = 300;
  var MAX_KEYS = 32;
  var LIMITS = { count: [1, 200, 30], speed_min: [0.3, 20, 1.5], speed_max: [0.3, 20, 3.5], delay: [10, 2000, 80] };
  var UNLOCK_DELAY = [100, 30000, 2500];
  var MODIFIERS = ["Shift", "Control", "Alt", "AltGraph", "Meta", "CapsLock", "Fn", "FnLock", "OS", "Hyper", "Super"];
  var LOCAL_KEY = "banana-eggs.found";
  var BANANA = "🍌";

  var live = 0;      // falling items on the page right now
  var running = {};  // egg id -> true while its run lasts
  var toastTimer = 0;

  function num(value, limits) {
    var n = typeof value === "number" ? value : parseFloat(value);
    if (!isFinite(n)) return limits[2];
    return Math.min(limits[1], Math.max(limits[0], n));
  }

  function normaliseKey(key) {
    if (typeof key !== "string" || !key) return null;
    return key.length === 1 ? key.toLowerCase() : key;
  }

  // A usable copy of one egg from the config, or null.
  function egg(raw) {
    if (!raw || typeof raw !== "object" || raw.enabled === false) return null;
    var id = typeof raw.id === "string" && /^[a-z0-9-]{1,32}$/.test(raw.id) ? raw.id : null;
    var keys = Array.isArray(raw.keys) ? raw.keys.map(function (k) {
      return typeof k === "string" && k.length <= 16 ? normaliseKey(k) : null;
    }) : [];
    if (!id || keys.length < 2 || keys.length > MAX_KEYS || keys.indexOf(null) !== -1) return null;
    var sprite = raw.sprite && typeof raw.sprite === "object" ? raw.sprite : {};
    var emoji = typeof sprite.value === "string" && sprite.value ? sprite.value.slice(0, 16) : BANANA;
    var lo = num(raw.speed_min, LIMITS.speed_min);
    var hi = num(raw.speed_max, LIMITS.speed_max);
    var unlock = raw.unlock && typeof raw.unlock === "object" ? raw.unlock : {};
    return {
      id: id,
      keys: keys,
      mascot: sprite.type === "mascot",
      emoji: emoji,
      count: Math.round(num(raw.count, LIMITS.count)),
      speedMin: Math.min(lo, hi),
      speedMax: Math.max(lo, hi),
      delay: Math.round(num(raw.delay, LIMITS.delay)),
      action: unlock.action === "page" || unlock.action === "toast" ? unlock.action : "none",
      unlockDelay: Math.round(num(unlock.delay, UNLOCK_DELAY)),
      record: raw.record !== false,
      message: typeof raw.message === "string" ? raw.message.slice(0, 200) : ""
    };
  }

  // Only URLs of this site; foundUrl must keep its "{id}" placeholder.
  function sameOrigin(value, needsId) {
    if (typeof value !== "string" || !value || (needsId && value.indexOf("{id}") === -1)) return null;
    try {
      var target = new URL(value.replace("{id}", "x"), window.location.href);
      return target.origin === window.location.origin ? value : null;
    } catch (e) {
      return null;
    }
  }

  function readConfig() {
    var node = document.head && document.head.querySelector('script#banana-eggs[type="application/json"]');
    if (!node) return null;
    var raw;
    try { raw = JSON.parse(node.textContent || "{}"); } catch (e) { return null; }
    if (!raw || typeof raw !== "object" || raw.version !== VERSION) return null;
    var eggs = [];
    var seen = {};
    (Array.isArray(raw.eggs) ? raw.eggs : []).slice(0, 10).forEach(function (item) {
      var e = egg(item);
      if (e && !seen[e.id]) { seen[e.id] = true; eggs.push(e); }
    });
    var csrf = raw.csrf && typeof raw.csrf === "object" ? raw.csrf : {};
    return {
      eggs: eggs,
      signedIn: raw.signedIn === true,
      foundUrl: sameOrigin(raw.foundUrl, true),
      pageUrl: sameOrigin(raw.pageUrl, false),
      csrfHeader: typeof csrf.header === "string" && /^[A-Za-z0-9-]{1,64}$/.test(csrf.header) ? csrf.header : null,
      csrfToken: typeof csrf.token === "string" ? csrf.token : "",
      reduceMotion: raw.reduceMotion === true,
      strings: raw.strings && typeof raw.strings === "object" ? raw.strings : {}
    };
  }

  var config = readConfig();

  function text(name, fallback) {
    var value = config && config.strings[name];
    return typeof value === "string" && value ? value : fallback;
  }

  function reduceMotion() {
    return (config && config.reduceMotion)
      || document.documentElement.getAttribute("data-reduce-motion") === "true"
      || !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function typing(target) {
    return !!target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName || ""));
  }

  // Whether leaving the page now would lose something the person typed or chose.
  function unsaved() {
    if (document.querySelector('[data-unsaved="true"], [data-dirty="true"]')) return true;
    var fields = document.querySelectorAll("input, textarea, select");
    for (var i = 0; i < fields.length; i += 1) {
      var f = fields[i];
      if (f.disabled || (f.closest && f.closest("[data-banana-eggs-ignore]"))) continue;
      var type = (f.type || "").toLowerCase();
      if (f.tagName === "SELECT") {
        for (var j = 0; j < f.options.length; j += 1) {
          if (f.options[j].selected !== f.options[j].defaultSelected) return true;
        }
      } else if (type === "checkbox" || type === "radio") {
        if (f.checked !== f.defaultChecked) return true;
      } else if (type === "file") {
        if (f.files && f.files.length) return true;
      } else if (!/^(hidden|submit|button|reset|image|search|password)$/.test(type)) {
        if (f.value !== f.defaultValue) return true;
      }
    }
    return false;
  }

  function localFinds() {
    try {
      var data = JSON.parse(window.localStorage.getItem(LOCAL_KEY) || "{}");
      return data && typeof data === "object" && !Array.isArray(data) ? data : {};
    } catch (e) {
      return {};
    }
  }

  function remember(id) {
    var finds = localFinds();
    if (finds[id]) return;
    finds[id] = new Date().toISOString();
    try { window.localStorage.setItem(LOCAL_KEY, JSON.stringify(finds)); } catch (e) { /* storage blocked */ }
  }

  function record(e) {
    if (!config || !e.record) return;
    if (!config.signedIn || !config.foundUrl) {
      remember(e.id);
      return;
    }
    var headers = { "Accept": "application/json", "Content-Type": "application/json" };
    if (config.csrfHeader) headers[config.csrfHeader] = config.csrfToken;
    window.fetch(config.foundUrl.replace("{id}", encodeURIComponent(e.id)), {
      method: "POST", credentials: "same-origin", headers: headers, body: "{}"
    }).catch(function () { /* offline or refused: the shower already played, nothing else to do */ });
  }

  function toast(message, link) {
    var old = document.querySelector(".banana-eggs-toast");
    if (old && old.parentNode) old.parentNode.removeChild(old);
    window.clearTimeout(toastTimer);
    var box = document.createElement("div");
    box.className = "banana-eggs-toast";
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    box.appendChild(document.createTextNode(message));
    if (link) {
      var a = document.createElement("a");
      a.className = "banana-eggs-toast__link";
      a.href = link;
      a.textContent = text("open_page", "See your easter eggs");
      box.appendChild(document.createTextNode(" "));
      box.appendChild(a);
    }
    document.body.appendChild(box);
    toastTimer = window.setTimeout(function () { if (box.parentNode) box.parentNode.removeChild(box); }, 6000);
  }

  function unlock(e, options) {
    var message = e.message || text("found", "You found an easter egg!");
    if (options.preview) {
      if (e.action !== "none") toast(message);
      return;
    }
    if (e.action === "toast") {
      toast(message);
    } else if (e.action === "page" && config && config.pageUrl) {
      if (unsaved()) toast(message, config.pageUrl);
      else window.location.href = config.pageUrl + "#egg-" + e.id;
    }
  }

  function spriteNode(e) {
    if (e.mascot) {
      var source = document.querySelector("[data-banana-eggs-sprite], .mascot-sprite");
      if (source) {
        var copy = source.cloneNode(true);
        copy.removeAttribute("id");
        if (copy.classList) copy.classList.remove("mascot-sprite--shades");
        return copy;
      }
    }
    return document.createTextNode(e.mascot ? BANANA : e.emoji);
  }

  function layer() {
    var node = document.querySelector(".banana-eggs-layer");
    if (node) return node;
    node = document.createElement("div");
    node.className = "banana-eggs-layer";
    node.setAttribute("aria-hidden", "true");
    document.body.appendChild(node);
    return node;
  }

  function drop(e, still) {
    var item = document.createElement("span");
    item.className = "banana-eggs-item" + (e.mascot ? " banana-eggs-item--sprite" : "") + (still ? " banana-eggs-item--still" : "");
    item.appendChild(spriteNode(e));
    var seconds = e.speedMin + Math.random() * (e.speedMax - e.speedMin);
    if (!still) {
      item.style.left = (Math.random() * 96).toFixed(1) + "%";
      item.style.animationDuration = seconds.toFixed(2) + "s";
      item.style.fontSize = (1.2 + Math.random() * 1.5).toFixed(2) + "rem";
    }
    live += 1;
    var gone = false;
    function remove() {
      if (gone) return;
      gone = true;
      live -= 1;
      if (item.parentNode) item.parentNode.removeChild(item);
    }
    item.addEventListener("animationend", remove);
    window.setTimeout(remove, still ? 1600 : seconds * 1000 + 1000);
    layer().appendChild(item);
  }

  function play(e, options) {
    if (running[e.id]) return;
    running[e.id] = true;
    var still = reduceMotion();
    var total = still ? 1 : Math.min(e.count, Math.max(0, MAX_ITEMS - live));
    for (var i = 0; i < total; i += 1) {
      window.setTimeout(function () { drop(e, still); }, still ? 0 : i * e.delay);
    }
    if (!options.preview) record(e);
    var unlockAt = still ? 800 : total * e.delay + e.unlockDelay;
    var finished = still ? 1600 : (total - 1) * e.delay + e.speedMax * 1000;
    window.setTimeout(function () { unlock(e, options); }, unlockAt);
    window.setTimeout(function () { running[e.id] = false; }, Math.max(unlockAt, finished));
  }

  window.BananaEggs = {
    version: VERSION,
    normaliseKey: normaliseKey,
    localFinds: localFinds,
    preview: function (raw) {
      var copy = raw && typeof raw === "object" ? Object.assign({}, raw, { enabled: true }) : raw;
      if (copy && !copy.id) copy.id = "preview";
      var e = egg(copy);
      if (!e) return false;
      play(e, { preview: true });
      return true;
    }
  };

  if (!config || !config.eggs.length) return;

  var longest = config.eggs.reduce(function (n, e) { return Math.max(n, e.keys.length); }, 0);
  var buffer = [];

  function endsWith(keys) {
    if (buffer.length < keys.length) return false;
    var offset = buffer.length - keys.length;
    for (var i = 0; i < keys.length; i += 1) {
      if (buffer[offset + i] !== keys[i]) return false;
    }
    return true;
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.isComposing
        || typing(event.target)) {
      buffer = [];
      return;
    }
    if (event.repeat || MODIFIERS.indexOf(event.key) !== -1) return;
    var key = normaliseKey(event.key);
    if (!key) return;
    buffer.push(key);
    if (buffer.length > longest) buffer.shift();
    for (var i = 0; i < config.eggs.length; i += 1) {
      if (endsWith(config.eggs[i].keys)) {
        buffer = [];
        play(config.eggs[i], {});
        return;
      }
    }
  });
})();
