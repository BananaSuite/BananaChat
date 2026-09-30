// Music program player (participants only).
//
// * Never starts by itself: playback begins when the person presses "Start music".
//   After that, moving to another page continues where it stopped (the person chose
//   to listen); if the browser refuses, the button offers to resume.
// * Pause, next track and volume are always available (WCAG 2.1 SC 1.4.2).
// * Track, position, volume and play state are remembered in localStorage.
// * Only one tab plays at a time.
import { pageData, safeStorage, t } from "./core.js";

const RESUME_WINDOW_MS = 30 * 60 * 1000;
const SAVE_EVERY_MS = 2000;
const DEFAULT_VOLUME = 35;

function shuffled(items) {
  const copy = [...items];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const other = Math.floor(Math.random() * (index + 1));
    [copy[index], copy[other]] = [copy[other], copy[index]];
  }
  return copy;
}

function sameSet(a, b) {
  if (!Array.isArray(a) || a.length !== b.length) return false;
  const known = new Set(b);
  return a.every((item) => known.has(item));
}

function init(root) {
  const data = pageData("music-player-data");
  const tracks = Array.isArray(data.tracks) ? data.tracks : [];
  if (!tracks.length) return;
  const byId = new Map(tracks.map((track) => [track.id, track]));
  const storage = safeStorage();
  const key = `bc-music:${data.user}`;

  let state = {};
  try { state = JSON.parse(storage.getItem(key) || "{}") || {}; } catch { state = {}; }
  const ids = tracks.map((track) => track.id);
  let order = ids;
  if (data.mode === "shuffle") order = sameSet(state.order, ids) ? state.order : shuffled(ids);
  let index = Math.max(0, order.indexOf(state.trackId));
  let started = false;
  let failures = 0;

  const audio = root.querySelector("[data-music-audio]");
  const toggle = root.querySelector("[data-music-toggle]");
  const label = root.querySelector("[data-music-label]");
  const title = root.querySelector("[data-music-title]");
  const volume = root.querySelector("[data-music-volume]");
  const volumeValue = root.querySelector("[data-music-volume-value]");
  const next = root.querySelector("[data-music-next]");

  // Sit in the top bar (early in the reading order); float at the bottom when a page has no top bar.
  const slot = document.querySelector(".topbar-end");
  if (slot) slot.prepend(root);
  else root.classList.add("is-floating");
  root.hidden = false;

  const channel = "BroadcastChannel" in window ? new BroadcastChannel("bc-music") : null;
  const tab = Math.random().toString(36).slice(2);

  function save() {
    storage.setItem(key, JSON.stringify({
      trackId: order[index], position: Number.isFinite(audio.currentTime) ? audio.currentTime : 0,
      volume: Math.round(audio.volume * 100), playing: !audio.paused, order: data.mode === "shuffle" ? order : undefined,
      savedAt: Date.now(),
    }));
  }

  function render() {
    const playing = !audio.paused;
    root.querySelector('[data-music-icon="play"]').hidden = playing;
    root.querySelector('[data-music-icon="pause"]').hidden = !playing;
    label.textContent = playing ? t("music_pause") : started ? t("music_resume") : t("music_start");
    root.classList.toggle("is-playing", playing);
    const track = byId.get(order[index]);
    title.textContent = track ? track.name : "—";
    if ("mediaSession" in navigator && track) {
      try {
        navigator.mediaSession.metadata = new MediaMetadata({ title: track.name, artist: document.title });
        navigator.mediaSession.playbackState = playing ? "playing" : "paused";
      } catch { /* unsupported */ }
    }
  }

  function load(position = 0) {
    const track = byId.get(order[index]);
    audio.src = track.url;
    if (position > 0) {
      audio.addEventListener("loadedmetadata", () => {
        if (position < (audio.duration || Infinity)) audio.currentTime = position;
      }, { once: true });
    }
    render();
  }

  async function play() {
    if (!audio.src) load();
    try {
      await audio.play();
      started = true;
      failures = 0;
      channel?.postMessage({ type: "playing", tab });
    } catch {
      // Blocked by the browser (no gesture yet) or not playable: the button offers to start.
    }
    render();
    save();
  }

  function pause() {
    audio.pause();
    render();
    save();
  }

  function advance(autoplay) {
    index = (index + 1) % order.length;
    if (index === 0 && data.mode === "shuffle") order = shuffled(ids);
    load(0);
    if (autoplay) play();
    else save();
  }

  toggle.addEventListener("click", () => (audio.paused ? play() : pause()));
  next.addEventListener("click", () => advance(!audio.paused || started));
  audio.addEventListener("ended", () => advance(true));
  audio.addEventListener("error", () => {
    if (!audio.src) return;
    failures += 1;
    if (failures < tracks.length) advance(started);
    else pause();
  });
  audio.addEventListener("play", render);
  audio.addEventListener("pause", render);

  let lastSave = 0;
  audio.addEventListener("timeupdate", () => {
    if (Date.now() - lastSave > SAVE_EVERY_MS) { lastSave = Date.now(); save(); }
  });
  window.addEventListener("pagehide", save);

  function setVolume(value) {
    const clamped = Math.min(100, Math.max(0, Number.isFinite(value) ? value : DEFAULT_VOLUME));
    audio.volume = clamped / 100;
    volume.value = String(clamped);
    volumeValue.textContent = `${clamped}%`;
  }
  volume.addEventListener("input", () => { setVolume(Number(volume.value)); save(); });

  channel?.addEventListener("message", (event) => {
    if (event.data?.type === "playing" && event.data.tab !== tab && !audio.paused) pause();
  });

  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.setActionHandler("play", () => play());
      navigator.mediaSession.setActionHandler("pause", () => pause());
      navigator.mediaSession.setActionHandler("nexttrack", () => advance(true));
    } catch { /* unsupported */ }
  }

  setVolume(state.volume ?? DEFAULT_VOLUME);
  const recent = Number.isFinite(state.savedAt) && Date.now() - state.savedAt < RESUME_WINDOW_MS;
  load(recent && state.trackId === order[index] ? Number(state.position) || 0 : 0);
  if (recent && state.playing) {
    started = true;
    play();
  }
  render();
}

const root = document.getElementById("music-player");
if (root) init(root);
