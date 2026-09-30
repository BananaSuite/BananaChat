// Agents: one task. A live timeline (polling, reconnect-safe), sub-agent lanes,
// Stop, follow-up messages and the workspace browser. All text from the task
// (prompts, model output, command output, file contents) is untrusted: it is
// inserted with textContent, or through the escaping Markdown renderer.
import { api, ApiError, boot, checkStatusSoon, el, icon, onStatusChange, pageData, setBusy, t, toast } from "./core.js";
import { enhanceMarkdown, renderMarkdown } from "./markdown.js";

const data = pageData();
const urls = data.urls;
const labels = {
  copy: t("copy"),
  copied: t("copied"),
  download: t("chat_download_code"),
  code: t("chat_code"),
};
const number = new Intl.NumberFormat(boot.lang === "it" ? "it-IT" : "en-US");
const STATUS_BADGE = {
  queued: "badge-info", running: "badge-primary", paused: "badge-warning", finished: "badge-success",
  failed: "badge-danger", stopped: "", out_of_budget: "badge-warning", interrupted: "badge-warning",
};

const state = {
  task: data.task,
  last: 0,
  lanes: new Map(),
  seen: new Set(),
  canSend: data.can_send,
  serviceOk: true,
  sending: false,
  timer: null,
  failures: 0,
  workspacePath: "/workspace",
  workspaceFile: null,
  workspaceTimer: null,
};

const $ = (id) => document.getElementById(id);
const timeline = $("timeline");

// ----- formatting -------------------------------------------------------------------
function parseTime(value) {
  if (!value) return null;
  const date = new Date(String(value).replace(" ", "T") + "Z");
  return Number.isNaN(date.getTime()) ? null : date;
}

function clock(value) {
  const date = parseTime(value);
  return date ? date.toLocaleTimeString(boot.lang === "it" ? "it-IT" : "en-US", { hour: "2-digit", minute: "2-digit" }) : "";
}

function duration(ms) {
  if (!ms) return "";
  return ms < 1000 ? t("agents.duration_ms", { value: ms }) : t("agents.duration_s", { value: (ms / 1000).toFixed(1) });
}

function markdown(text) {
  const node = el("div", { class: "prose" });
  node.innerHTML = renderMarkdown(text || "", { labels }); // escaping renderer (see markdown.js)
  return node;
}

function preview(step) {
  const args = step.tool_args && typeof step.tool_args === "object" ? step.tool_args : {};
  switch (step.tool_name) {
    case "bash": return String(args.command || "");
    case "search": return `${args.pattern || ""}  ${args.path || ""}`.trim();
    case "delegate": return (Array.isArray(args.tasks) ? args.tasks : []).map((item) => item && item.title).filter(Boolean).join(", ");
    case "finish": return String(args.summary || "").split("\n")[0];
    default: return String(args.path || "");
  }
}

function argumentsText(step) {
  const args = step.tool_args;
  if (!args || typeof args !== "object" || Array.isArray(args)) return JSON.stringify(args ?? null, null, 2);
  if (step.tool_name === "bash") return String(args.command ?? "") + (args.timeout ? `\n# timeout: ${args.timeout} s` : "");
  const lines = [];
  for (const [key, value] of Object.entries(args)) {
    if (typeof value === "string" && value.includes("\n")) lines.push(`${key}:`, value.replace(/\n$/, ""), "");
    else if (typeof value === "string" || typeof value === "number") lines.push(`${key}: ${value}`);
    else lines.push(`${key}: ${JSON.stringify(value, null, 2)}`);
  }
  return lines.join("\n").trim();
}

const TOOL_ICONS = {
  bash: "terminal", read_file: "file", write_file: "edit", edit_file: "edit", list_files: "folder",
  search: "search", finish: "check", delegate: "users",
};

// ----- steps ------------------------------------------------------------------------
function stepNode(step) {
  if (step.kind === "user") {
    return el("li", { class: "step step-user" },
      el("div", { class: "step-label", text: step.agent ? t("agents.instructions") : t("agents.you") }),
      el("div", { class: "step-text", text: step.content }));
  }
  if (step.kind === "assistant") {
    const hasText = (step.content || "").trim();
    const hasThinking = (step.thinking || "").trim();
    if (!hasText && !hasThinking) return null;
    const node = el("li", { class: "step step-assistant" }, el("div", { class: "step-label", text: t("agents.agent") }));
    if (hasThinking) {
      node.append(el("details", { class: "step-thinking" },
        el("summary", { text: t("agents.thinking") }),
        el("div", { class: "step-thinking-text", text: step.thinking })));
    }
    if (hasText) node.append(markdown(step.content));
    return node;
  }
  if (step.kind === "tool") {
    const status = step.tool_status || "ok";
    const summary = el("summary", {},
      el("span", { class: "tool-icon" }, icon(TOOL_ICONS[step.tool_name] || "alert")),
      el("span", { class: "tool-name", text: step.tool_name || "?" }),
      el("code", { class: "tool-preview", text: preview(step) }),
      el("span", { class: `tool-state tool-${status}`, title: status === "ok" ? "" : t(`agents.tool_${status === "invalid" ? "invalid" : "error"}`) }),
      el("span", { class: "tool-time", text: duration(step.duration_ms) }));
    const body = el("div", { class: "tool-body" },
      el("div", { class: "tool-label", text: t("agents.arguments") }),
      el("pre", { class: "tool-pre", text: argumentsText(step) }),
      el("div", { class: "tool-label", text: t("agents.output") }),
      el("pre", { class: "tool-pre", text: step.tool_result || t("agents.no_output") }));
    return el("li", { class: `step step-tool is-${status}` }, el("details", { class: "tool-call" }, summary, body));
  }
  if (step.kind === "summary") {
    return el("li", { class: "step step-summary" },
      el("div", { class: "step-summary-head" }, icon("check"), el("strong", { text: t("agents.summary") })),
      markdown(step.content));
  }
  const error = step.kind === "error";
  return el("li", { class: `step step-notice${error ? " is-error" : ""}` }, icon(error ? "alert" : "info"),
    el("span", { text: step.content }));
}

function laneFor(agent) {
  let lane = state.lanes.get(agent);
  if (lane) return lane;
  const list = el("ol", { class: "timeline lane-timeline" });
  const badge = el("span", { class: "badge" });
  const title = el("strong", { class: "lane-title" });
  const count = el("span", { class: "lane-count muted" });
  const report = el("div", { class: "lane-report", hidden: true });
  const details = el("details", { class: "lane-steps" }, el("summary", { text: t("agents.lane_show_steps") }), list);
  const card = el("article", { class: "lane card", dataset: { agent: String(agent) } },
    el("div", { class: "lane-head" }, el("span", { class: "lane-number", text: String(agent) }), title, badge),
    count, report, details);
  $("lane-grid").append(card);
  $("lanes").hidden = false;
  lane = { card, list, badge, title, count, report, summary: "" };
  state.lanes.set(agent, lane);
  return lane;
}

function applyLanes(lanes) {
  for (const info of lanes || []) {
    const lane = laneFor(info.agent);
    lane.title.textContent = info.title || t("agents.subagent", { n: info.agent });
    lane.badge.className = `badge ${STATUS_BADGE[info.status] || ""}`;
    lane.badge.textContent = t(`agents.status_${info.status === "finished" ? "finished" : info.status}`);
    lane.count.textContent = t("agents.lane_steps", { count: info.steps_used || 0 });
    if (info.summary && info.summary !== lane.summary) {
      lane.summary = info.summary;
      lane.report.replaceChildren(markdown(info.summary));
      lane.report.hidden = false;
    }
  }
}

function appendSteps(steps) {
  const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 240;
  let touchedWorkspace = false;
  for (const step of steps || []) {
    if (state.seen.has(step.id)) continue;
    state.seen.add(step.id);
    state.last = Math.max(state.last, step.id);
    const node = stepNode(step);
    if (step.kind === "tool" && step.tool_name !== "finish" && step.tool_name !== "read_file") touchedWorkspace = true;
    if (!node) continue;
    if (step.agent > 0) laneFor(step.agent).list.append(node);
    else timeline.append(node);
  }
  enhanceMarkdown(timeline, labels);
  if (nearBottom && steps && steps.length) window.scrollTo({ top: document.body.scrollHeight });
  if (touchedWorkspace) scheduleWorkspaceRefresh();
}

// ----- task state ---------------------------------------------------------------------
function applyTask(task, { first = false } = {}) {
  const previous = first ? null : state.task;
  state.task = task;
  const badge = $("task-status");
  badge.className = `badge ${STATUS_BADGE[task.status] || ""}`;
  badge.dataset.status = task.status;
  badge.textContent = task.stopping ? t("agents.stopping") : t(`agents.status_${task.status}`);
  $("task-stats").textContent = t("agents.stats", {
    steps: number.format(task.steps_used), max: number.format(task.limits.steps), tokens: number.format(task.tokens),
  });
  $("fact-steps").textContent = `${number.format(task.steps_used)} / ${number.format(task.limits.steps)}`;
  $("fact-tokens").textContent = number.format(task.tokens);
  $("fact-tools").textContent = number.format(task.tool_calls);
  $("task-repository").hidden = !task.repository;
  $("task-repository-label").textContent = task.repository ? task.repository.label : "";
  const stop = $("stop-button");
  stop.hidden = !task.active;
  stop.disabled = task.stopping;

  const live = $("timeline-live");
  live.hidden = !task.active;
  const liveText = task.stopping ? t("agents.stopping") : task.status === "queued" ? t("agents.starting")
    : task.status === "paused" ? t("agents.paused_live") : t("agents.working");
  $("timeline-live-text").textContent = liveText;
  live.classList.toggle("is-paused", task.status === "paused");

  const banner = $("task-banner");
  let text = "";
  let kind = "info";
  if (task.status === "paused") { text = task.notice; kind = "warning"; }
  else if (["failed", "out_of_budget", "interrupted"].includes(task.status)) { text = task.error; kind = task.status === "failed" ? "error" : "warning"; }
  else if (task.status === "stopped") { text = task.error || t("agents.status_stopped"); }
  banner.hidden = !text;
  banner.className = `agent-banner alert alert-${kind}`;
  banner.replaceChildren(icon(kind === "info" ? "info" : "alert"), el("p", { text }));

  updateComposer();
  if (!previous || previous.workspace !== task.workspace || previous.active !== task.active) updateWorkspaceNote();
}

function updateComposer() {
  const input = $("follow-up-input");
  const send = $("follow-up-send");
  if (!input) return;
  const task = state.task;
  send.disabled = state.sending || !state.serviceOk || !state.canSend;
  let hint = task.active ? t("agents.hint_active") : t("agents.hint_idle");
  if (task.pending_messages) hint = t("agents.pending_messages", { count: task.pending_messages });
  if (!state.serviceOk) hint = t("status_paused");
  $("follow-up-hint").textContent = hint;
}

// ----- polling --------------------------------------------------------------------------
function schedule(ms) {
  clearTimeout(state.timer);
  state.timer = setTimeout(poll, ms);
}

async function poll() {
  let more = false;
  try {
    const result = await api(`${urls.events}?after=${state.last}`);
    state.failures = 0;
    applyTask(result.task);
    appendSteps(result.steps);
    applyLanes(result.lanes);
    more = result.more;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      window.location.assign(urls.index);
      return;
    }
    state.failures += 1;
  }
  const task = state.task;
  if (more) return schedule(50);
  if (task.active || task.pending_messages) {
    const base = document.visibilityState === "visible" ? 1500 : 6000;
    schedule(Math.min(base * 2 ** state.failures, 20000));
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && (state.task.active || state.task.pending_messages)) schedule(100);
});

// ----- actions ------------------------------------------------------------------------------
$("stop-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true);
  try {
    await api(urls.stop, { method: "POST" });
    toast(t("agents.stop_requested"), "info");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
    schedule(100);
  }
});

const composer = $("follow-up");
if (composer) {
  const input = $("follow-up-input");
  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = input.value.trim();
    if (!content || state.sending) return;
    state.sending = true;
    const send = $("follow-up-send");
    setBusy(send, true);
    try {
      const result = await api(urls.message, { json: { content } });
      input.value = "";
      toast(result.queued ? t("agents.message_queued") : t("agents.message_sent"), "success");
    } catch (error) {
      if (error instanceof ApiError && error.status === 503) checkStatusSoon();
      toast(error.message, "error");
    } finally {
      state.sending = false;
      setBusy(send, false);
      updateComposer();
      schedule(100);
    }
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) composer.requestSubmit();
  });
}

onStatusChange((status) => {
  state.serviceOk = status.canSend;
  updateComposer();
});

// ----- workspace -------------------------------------------------------------------------
function updateWorkspaceNote() {
  const task = state.task;
  const note = $("workspace-note");
  $("archive-link").hidden = !task.workspace;
  $("patch-link").hidden = !(task.workspace && task.repository && task.repository.git);
  const upload = document.querySelector("label[for=workspace-upload]");
  if (upload) upload.hidden = !task.workspace;
  if (!task.workspace) {
    $("workspace-list").replaceChildren();
    $("workspace-crumbs").replaceChildren();
    $("workspace-preview").hidden = true;
    note.textContent = task.active || task.status === "queued" ? t("agents.workspace_not_yet") : t("agents.workspace_removed");
    return;
  }
  note.textContent = task.workspace_until ? t("agents.workspace_until", { time: clock(task.workspace_until) }) : "";
  loadWorkspace(state.workspacePath);
}

function scheduleWorkspaceRefresh() {
  clearTimeout(state.workspaceTimer);
  state.workspaceTimer = setTimeout(() => {
    if (state.task.workspace && !state.workspaceFile) loadWorkspace(state.workspacePath, { quiet: true });
  }, 1500);
}

function crumbs(path) {
  const nav = $("workspace-crumbs");
  const parts = path.split("/").filter(Boolean);
  const nodes = [];
  let current = "";
  parts.forEach((part, index) => {
    current += `/${part}`;
    const target = current;
    if (index) nodes.push(el("span", { class: "crumb-sep", "aria-hidden": "true", text: "/" }));
    nodes.push(index === parts.length - 1
      ? el("span", { class: "crumb-current", "aria-current": "location", text: part })
      : el("button", { type: "button", class: "crumb", text: part, onclick: () => loadWorkspace(target) }));
  });
  nav.replaceChildren(...nodes);
}

function fileSize(size) {
  if (typeof size !== "number") return "";
  if (size < 1024) return t("agents.size_bytes", { value: size });
  if (size < 1024 * 1024) return t("agents.size_kb", { value: (size / 1024).toFixed(1) });
  return t("agents.size_mb", { value: (size / 1024 / 1024).toFixed(1) });
}

async function loadWorkspace(path, { quiet = false } = {}) {
  const note = $("workspace-note");
  try {
    const result = await api(`${urls.workspace}?path=${encodeURIComponent(path)}`);
    if (result.type === "dir") {
      state.workspacePath = result.path;
      state.workspaceFile = null;
      $("workspace-preview").hidden = true;
      crumbs(result.path);
      const list = $("workspace-list");
      const items = [];
      if (result.path !== "/workspace") {
        const parent = result.path.slice(0, result.path.lastIndexOf("/")) || "/workspace";
        items.push(el("li", {}, el("button", { type: "button", class: "ws-entry is-up", onclick: () => loadWorkspace(parent) },
          icon("arrow-up"), el("span", { class: "grow", text: t("agents.workspace_up") }))));
      }
      for (const entry of result.entries) {
        const target = `${result.path}/${entry.name}`;
        items.push(el("li", {}, el("button", { type: "button", class: `ws-entry is-${entry.type}`, onclick: () => loadWorkspace(target) },
          icon(entry.type === "dir" ? "folder" : "file"),
          el("span", { class: "grow truncate mono", text: entry.name + (entry.type === "dir" ? "/" : "") }),
          el("span", { class: "ws-size", text: entry.type === "dir" ? "" : fileSize(entry.size) }))));
      }
      if (!result.entries.length) items.push(el("li", { class: "hint ws-empty", text: t("agents.workspace_empty") }));
      list.replaceChildren(...items);
    } else {
      state.workspaceFile = result.path;
      crumbs(result.path);
      $("workspace-file-name").textContent = result.path.split("/").pop();
      $("workspace-file-download").href = `${urls.file}?path=${encodeURIComponent(result.path)}`;
      $("workspace-file").textContent = result.text ?? t("agents.workspace_binary", { size: fileSize(result.size) });
      $("workspace-preview").hidden = false;
      const parent = result.path.slice(0, result.path.lastIndexOf("/")) || "/workspace";
      state.workspacePath = parent;
      $("workspace-list").replaceChildren(el("li", {}, el("button", {
        type: "button", class: "ws-entry is-up",
        onclick: () => loadWorkspace(parent),
      }, icon("arrow-up"), el("span", { class: "grow", text: t("agents.workspace_back") }))));
    }
    if (!state.task.workspace_until) note.textContent = "";
  } catch (error) {
    if (quiet) return;
    if (error instanceof ApiError && error.code === "no_workspace") {
      state.task.workspace = false;
      updateWorkspaceNote();
      return;
    }
    note.textContent = error.message;
  }
}

$("workspace-refresh").addEventListener("click", () => {
  if (state.task.workspace) loadWorkspace(state.workspaceFile || state.workspacePath);
  else schedule(50);
});

// The patch is computed in the sandbox on request: fetch it here so errors (no changes, busy) become toasts.
$("patch-link").addEventListener("click", async (event) => {
  event.preventDefault();
  const link = event.currentTarget;
  if (link.getAttribute("aria-busy") === "true") return;
  setBusy(link, true);
  try {
    const response = await api(urls.patch, { raw: true });
    const blob = await response.blob();
    const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    const anchor = el("a", { href: URL.createObjectURL(blob), download: match ? match[1] : "changes.patch", hidden: true });
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(anchor.href), 30000);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(link, false);
  }
});

const uploadInput = $("workspace-upload");
if (uploadInput) {
  uploadInput.addEventListener("change", async () => {
    if (!uploadInput.files.length) return;
    const form = new FormData();
    for (const file of uploadInput.files) form.append("files", file);
    try {
      const result = await api(urls.upload, { form });
      toast(t("agents.uploaded", { count: result.count }), "success");
      loadWorkspace(state.workspacePath);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      uploadInput.value = "";
    }
  });
}

// ----- start ------------------------------------------------------------------------------
applyTask(state.task, { first: true });
appendSteps(data.steps);
applyLanes(data.lanes);
if (data.more || state.task.active || state.task.pending_messages) schedule(data.more ? 50 : 1500);
