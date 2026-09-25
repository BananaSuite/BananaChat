"use strict";


//  Music player: multi-track retro loop
//  Persists playback position and current track index in localStorage so
//  playback resumes across page navigations.  No user controls.

function initMusicPlayer() {
  if (!window.BC_MUSIC_ACTIVE) return;
  var tracks = window.BC_MUSIC_TRACKS || [];
  var mode = window.BC_MUSIC_MODE || "sequential";
  var audio = document.getElementById("bc-music-audio");
  var player = document.getElementById("bc-music-player");
  var titleEl = document.getElementById("bc-music-title");
  if (!audio || !player || tracks.length === 0) return;

  var POS_KEY = "bc_music_pos";
  var IDX_KEY = "bc_music_idx";
  var ORDER_KEY = "bc_music_order";
  var SAVE_INTERVAL = 500;

  // Build play order (shuffle generates a random permutation stored for the session)
  var playOrder;
  if (mode === "shuffle") {
    var savedOrder = localStorage.getItem(ORDER_KEY);
    if (savedOrder) {
      try { playOrder = JSON.parse(savedOrder); } catch(e) { playOrder = null; }
      // Regenerate if length changed (admin added/removed tracks)
      if (!playOrder || playOrder.length !== tracks.length) playOrder = null;
    }
    if (!playOrder) {
      playOrder = [];
      for (var si = 0; si < tracks.length; si++) playOrder.push(si);
      // Fisher-Yates shuffle
      for (var sj = playOrder.length - 1; sj > 0; sj--) {
        var sk = Math.floor(Math.random() * (sj + 1));
        var tmp = playOrder[sj]; playOrder[sj] = playOrder[sk]; playOrder[sk] = tmp;
      }
      localStorage.setItem(ORDER_KEY, JSON.stringify(playOrder));
    }
  } else {
    playOrder = [];
    for (var qi = 0; qi < tracks.length; qi++) playOrder.push(qi);
  }

  // Restore saved index within play order
  var orderIdx = parseInt(localStorage.getItem(IDX_KEY), 10);
  if (isNaN(orderIdx) || orderIdx < 0 || orderIdx >= playOrder.length) orderIdx = 0;

  function currentTrack() { return tracks[playOrder[orderIdx]]; }

  function loadTrack() {
    var t = currentTrack();
    audio.src = t.url;
    if (titleEl) titleEl.textContent = t.name;
  }

  // When a track ends, advance to the next
  audio.addEventListener("ended", function() {
    orderIdx = (orderIdx + 1) % playOrder.length;
    localStorage.setItem(IDX_KEY, String(orderIdx));
    localStorage.removeItem(POS_KEY);
    loadTrack();
    audio.currentTime = 0;
    audio.play().catch(function(){});
  });

  // Load the current track
  loadTrack();

  // Restore saved position within the track
  var savedPos = parseFloat(localStorage.getItem(POS_KEY));
  if (savedPos && !isNaN(savedPos) && savedPos > 0) {
    audio.currentTime = savedPos;
  }

  // Periodically save position
  setInterval(function() {
    if (!audio.paused && audio.currentTime > 0) {
      localStorage.setItem(POS_KEY, String(audio.currentTime));
      localStorage.setItem(IDX_KEY, String(orderIdx));
    }
  }, SAVE_INTERVAL);

  window.addEventListener("beforeunload", function() {
    if (audio.currentTime > 0) {
      localStorage.setItem(POS_KEY, String(audio.currentTime));
      localStorage.setItem(IDX_KEY, String(orderIdx));
    }
  });

  // Auto-play with user-interaction fallback
  function tryPlay() {
    var p = audio.play();
    if (p && typeof p.catch === "function") {
      p.catch(function() {
        var events = ["click", "keydown", "touchstart"];
        function resumeOnce() {
          audio.play().catch(function(){});
          events.forEach(function(e) { document.removeEventListener(e, resumeOnce); });
        }
        events.forEach(function(e) { document.addEventListener(e, resumeOnce, { once: true }); });
      });
    }
  }

  audio.volume = 0.35;
  tryPlay();
}


//  Drag-and-drop sortable list utility
//  Usage: initSortable(containerEl, { onReorder: function(ids){...} })
//  Each direct child of containerEl must have [data-sort-id].

function initSortable(container, opts) {
  if (!container) return;
  var onReorder = (opts && opts.onReorder) || function(){};
  var dragEl = null;
  var placeholder = null;

  function getItems() {
    return Array.prototype.slice.call(container.children).filter(function(el) {
      return el.hasAttribute("data-sort-id");
    });
  }

  function collectIds() {
    return getItems().map(function(el) { return el.getAttribute("data-sort-id"); });
  }

  function createPlaceholder() {
    var ph = document.createElement("div");
    ph.className = "sort-placeholder";
    ph.style.cssText = "border:2px dashed var(--accent,#e8b84b);border-radius:6px;opacity:0.5;";
    return ph;
  }

  // Make each item draggable
  getItems().forEach(function(item) {
    item.setAttribute("draggable", "true");
    item.style.cursor = "grab";

    item.addEventListener("dragstart", function(e) {
      dragEl = item;
      placeholder = createPlaceholder();
      placeholder.style.height = item.offsetHeight + "px";
      setTimeout(function() { item.style.opacity = "0.4"; }, 0);
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", item.getAttribute("data-sort-id"));
    });

    item.addEventListener("dragend", function() {
      item.style.opacity = "";
      item.style.cursor = "grab";
      if (placeholder && placeholder.parentNode) placeholder.parentNode.removeChild(placeholder);
      dragEl = null;
      placeholder = null;
      onReorder(collectIds());
    });

    item.addEventListener("dragover", function(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!dragEl || dragEl === item) return;
      var rect = item.getBoundingClientRect();
      var midY = rect.top + rect.height / 2;
      if (e.clientY < midY) {
        container.insertBefore(placeholder, item);
      } else {
        container.insertBefore(placeholder, item.nextSibling);
      }
    });

    item.addEventListener("drop", function(e) {
      e.preventDefault();
      if (!dragEl) return;
      if (placeholder && placeholder.parentNode) {
        container.insertBefore(dragEl, placeholder);
        container.removeChild(placeholder);
      }
    });
  });

  // Allow dropping on the container itself (for empty areas / end of list)
  container.addEventListener("dragover", function(e) { e.preventDefault(); });
  container.addEventListener("drop", function(e) {
    e.preventDefault();
    if (!dragEl) return;
    if (placeholder && placeholder.parentNode) {
      container.insertBefore(dragEl, placeholder);
      container.removeChild(placeholder);
    }
  });
}

// Helper: POST reorder IDs to a JSON endpoint and show a toast
function saveReorder(url, ids) {
  fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken(),
    },
    body: JSON.stringify({ ids: ids }),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) showToast(tr("order_saved", "Order saved"));
  })
  .catch(function() {});
}

// Simple toast (auto-dismiss after 2s)
function showToast(msg) {
  var existing = document.getElementById("bc-toast");
  if (existing) existing.remove();
  var toast = document.createElement("div");
  toast.id = "bc-toast";
  toast.className = "bc-toast";
  toast.textContent = msg;
  toast.style.cssText =
    "position:fixed;bottom:20px;right:20px;z-index:9999;padding:8px 18px;" +
    "border-radius:8px;font-size:0.85rem;font-weight:600;opacity:0;" +
    "transition:opacity 0.2s;pointer-events:none;";
  document.body.appendChild(toast);
  requestAnimationFrame(function() { toast.style.opacity = "1"; });
  setTimeout(function() {
    toast.style.opacity = "0";
    setTimeout(function() { toast.remove(); }, 300);
  }, 2000);
}
