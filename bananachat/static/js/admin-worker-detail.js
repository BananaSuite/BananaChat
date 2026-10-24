// Administration → Workers → one worker: hardware, maintenance access (terminal, VNC, SSH, RDP) and its models.
// Every value from the server is inserted as text (el()/textContent), never as HTML.
import { api, confirmDialog, copyText, el, icon, pageData, setBusy, toast } from "./core.js";

let data = pageData();
const urls = data.urls || {};
const remotePanel = document.getElementById("remote-panel");
const modelsPanel = document.getElementById("models-panel");
const commandsBody = document.getElementById("commands-body");
const commandsEmpty = document.getElementById("commands-empty");
const updated = document.getElementById("detail-updated");
const ARM_EVERY_MS = 10 * 60 * 1000;

const bytes = (value) => {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}`;
};
const badge = (text, cls = "") => el("span", { class: `badge ${cls}`.trim(), text });
const STATUS_CLASS = { queued: "badge-info", running: "badge-primary", done: "badge-success", failed: "badge-danger",
                       cancelled: "", pending: "badge-info", connecting: "badge-info", open: "badge-success",
                       closed: "" };

function setField(name, text) {
  const node = document.querySelector(`#worker-overview [data-field="${name}"]`);
  if (node) node.textContent = text;
}

function renderOverview() {
  const worker = data.worker || {};
  const system = data.system || {};
  setField("status", worker.status ? worker.status.charAt(0).toUpperCase() + worker.status.slice(1) : "—");
  setField("platform", worker.platform || "—");
  const gpu = [worker.gpu_name || "—"];
  if (system.vram_total) gpu.push(`${bytes(system.vram_free)} free of ${bytes(system.vram_total)} video memory`);
  setField("gpu", gpu.join(" · "));
  setField("memory", system.ram_total ? `${bytes(system.ram_free)} free of ${bytes(system.ram_total)}` +
    (system.cpu_count ? ` · ${system.cpu_count} CPU threads` : "") : "not reported");
  setField("disk", system.disk_total ? `${bytes(system.disk_free)} free of ${bytes(system.disk_total)} (where Ollama keeps its models)` : "not reported");
  const features = (system.features || []).join(", ");
  setField("daemon", system.version ? `worker ${system.version}${features ? ` · ${features}` : ""}` :
    "an older worker daemon (update it for model management and maintenance access)");
}

function codeLine(text) {
  return el("div", { class: "code-line" }, el("code", { text }),
    el("button", { type: "button", class: "icon-btn", "aria-label": "Copy", title: "Copy",
                   onclick: async () => { await copyText(text); toast("Copied.", "success"); } }, icon("copy")));
}

async function openSession(kind, port, button) {
  // Sessions for the browser open on their own pages, which ask for the confirmation themselves.
  const target = kind === "shell" ? urls.terminal : `${urls.vnc}?port=${encodeURIComponent(port)}`;
  window.open(target, "_blank", "noopener");
  if (button) setBusy(button, false);
}

function nativeCommand(service, address) {
  const host = address || "WORKER-ADDRESS";
  if (service.kind === "ssh") {
    return [`ssh -o ProxyCommand="python bananachat_connect.py proxy %h %p" user@${host}` +
            (service.port !== 22 ? ` -p ${service.port}` : "")];
  }
  if (service.kind === "rdp") {
    return [`python bananachat_connect.py forward ${host}:${service.port} --local 13389`,
            "then connect your RDP client (mstsc, Remmina, Microsoft Remote Desktop) to localhost:13389"];
  }
  if (service.kind === "vnc") {
    return [`python bananachat_connect.py forward ${host}:${service.port} --local 15900`,
            "then connect your VNC viewer to localhost:15900"];
  }
  return [`python bananachat_connect.py forward ${host}:${service.port} --local ${service.port + 10000 <= 65535 ? service.port + 10000 : 0}`];
}

function renderRemote() {
  if (!remotePanel) return;
  const view = data.remote;
  if (!view) {
    remotePanel.replaceChildren(el("p", { class: "muted small", text: "Unavailable." }));
    return;
  }
  const remote = view.remote || {};
  const parts = [];
  if (!remote.enabled) {
    parts.push(el("div", { class: "alert alert-info", role: "note" }, icon("info"),
      el("div", {},
        el("p", { text: "The worker's owner has not allowed maintenance access (or the worker daemon is too old to offer it). On the worker, add to its settings file and restart it:" }),
        el("pre", { class: "card small", text: "BC_WORKER_REMOTE_ACCESS=1\n# optional: the built-in terminal (no SSH server needed)\nBC_WORKER_REMOTE_SHELL=1\n# optional: services of the PC (default ssh:22,vnc:5900,rdp:3389)\nBC_WORKER_REMOTE_SERVICES=ssh:22,vnc:5900" }))));
    remotePanel.replaceChildren(...parts);
    return;
  }
  const status = el("p", { class: "small mb-0" },
    el("strong", { text: "Address: " }), el("code", { text: view.address || "—" }), " · ",
    view.online ? badge("Online", "badge-success") : badge("Offline", "badge-danger"), " ",
    view.armed ? badge("Listening for sessions", "badge-info") : badge("Will listen within a heartbeat", ""));
  parts.push(status);

  const actions = el("div", { class: "row gap-1 wrap" });
  if (remote.shell) {
    const button = el("button", { type: "button", class: "btn btn-primary" }, icon("terminal"), el("span", { text: "Open terminal" }));
    button.addEventListener("click", () => openSession("shell", 0, button));
    actions.append(button);
  }
  for (const service of remote.services || []) {
    if (service.kind === "vnc") {
      const button = el("button", { type: "button", class: "btn" }, icon("image"), el("span", { text: `VNC in the browser (port ${service.port})` }));
      button.addEventListener("click", () => openSession("vnc", service.port, button));
      actions.append(button);
    }
  }
  if (actions.childElementCount) parts.push(actions);
  if (!remote.shell) {
    parts.push(el("p", { class: "small muted mb-0", text: "The built-in terminal is off on this worker (its owner can set BC_WORKER_REMOTE_SHELL=1). SSH below needs an SSH server on the PC." }));
  }

  const services = remote.services || [];
  if (services.length) {
    const list = el("ul", { class: "service-list" });
    for (const service of services) {
      const item = el("li", {},
        el("strong", { text: `${service.kind.toUpperCase()} · port ${service.port}` }), " ",
        service.listening ? badge("listening", "badge-success") : badge("not listening right now", "badge-warning"));
      for (const line of nativeCommand(service, view.address)) {
        item.append(line.startsWith("then") ? el("p", { class: "small muted mb-0", text: line }) : codeLine(line));
      }
      list.append(item);
    }
    parts.push(el("h3", { text: "From your computer (connect helper)" }), list);
  } else {
    parts.push(el("p", { class: "small muted", text: "The owner allows no services of the PC itself (BC_WORKER_REMOTE_SERVICES)." }));
  }
  if ((remote.subnets || []).length) {
    parts.push(el("h3", { text: "The owner's network" }),
      el("p", { class: "small muted", text: `Reachable through this worker: ${remote.subnets.join(", ")} on ports ${(remote.subnet_ports || []).join(", ") || "none"}. Use the helper's SOCKS proxy, then connect to those addresses as if you were there:` }),
      codeLine("python bananachat_connect.py socks --listen 127.0.0.1:1080"),
      codeLine(`ssh -o ProxyCommand="python bananachat_connect.py proxy %h %p" user@${remote.subnets[0].split("/")[0].replace(/\.0$/, ".10")}`));
  }

  const sessions = view.sessions || [];
  if (sessions.length) {
    const rows = sessions.map((session) => el("tr", {},
      el("td", { text: session.kind }),
      el("td", { class: "mono", text: session.kind === "shell" ? "terminal" : `${session.target_host}:${session.target_port}` }),
      el("td", {}, badge(session.status, STATUS_CLASS[session.status])),
      el("td", { class: "small", text: `${bytes(session.bytes_up)} sent · ${bytes(session.bytes_down)} received` }),
      el("td", { class: "actions" }, el("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: () => closeSession(session.id) }, "End"))));
    parts.push(el("h3", { text: "Open sessions" }), el("div", { class: "table-wrap" },
      el("table", { class: "table" }, el("thead", {}, el("tr", {}, ...["Kind", "Target", "Status", "Traffic", ""].map((text) => el("th", { text })))),
        el("tbody", {}, ...rows))));
  }
  remotePanel.replaceChildren(...parts);
}

async function closeSession(id) {
  try {
    await api(urls.close.replace("TUNNEL_ID", encodeURIComponent(id)), { json: {} });
    toast("Session ended.", "success");
    refresh();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderModels() {
  const models = (data.worker && data.worker.models) || [];
  const manages = data.manages_models;
  const parts = [];
  if (!manages) {
    parts.push(el("p", { class: "small muted", text: "This worker cannot manage its models from here: its daemon is older, or its owner set BC_WORKER_MODEL_MANAGEMENT=0. Its owner can still use ollama pull on the PC." }));
    document.getElementById("pull-form")?.setAttribute("hidden", "");
  } else {
    document.getElementById("pull-form")?.removeAttribute("hidden");
  }
  if (!models.length) {
    parts.push(el("p", { class: "small muted", text: "No models reported." }));
  } else {
    const list = el("ul", { class: "model-list" });
    for (const model of models) {
      const item = el("li", {}, el("span", { class: "mono", text: model }));
      if (manages) {
        item.append(" ", el("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: () => removeModel(model) }, icon("trash"), el("span", { text: "Remove" })));
      }
      list.append(item);
    }
    parts.push(list);
  }
  modelsPanel.replaceChildren(...parts);
}

async function removeModel(model) {
  const ok = await confirmDialog({ title: `Remove ${model} from ${data.worker.name}?`,
    message: "The worker deletes it from its Ollama. It no longer answers requests for this model until it is downloaded again.",
    confirmLabel: "Remove", danger: true });
  if (!ok) return;
  await sendCommand("delete", model);
}

async function sendCommand(kind, model) {
  try {
    const result = await api(urls.command, { json: { kind, model } });
    toast(result.message, "success");
    refresh();
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
}

function renderCommands() {
  const commands = data.commands || [];
  commandsEmpty.hidden = commands.length > 0;
  commandsBody.replaceChildren(...commands.map((command) => {
    const progress = el("td", { class: "cell-full" });
    if (command.status === "running" || command.status === "queued") {
      progress.append(el("progress", { class: "meter", max: "100", value: String(command.progress_pct || 0), "aria-label": `Progress of ${command.model_name}` }));
    }
    const text = command.status === "failed" ? (command.error || "Failed.") : (command.detail || "");
    progress.append(el("span", { class: `small ${command.status === "failed" ? "danger-text" : "muted"}`, text }));
    const actions = el("td", { class: "actions" });
    if (["queued", "running"].includes(command.status)) {
      actions.append(el("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: () => commandAction(urls.cancel, command.id) }, "Cancel"));
    } else if (["failed", "cancelled"].includes(command.status)) {
      actions.append(el("button", { type: "button", class: "btn btn-sm", onclick: () => commandAction(urls.retry, command.id) }, icon("refresh"), el("span", { text: "Retry" })));
    }
    return el("tr", {},
      el("td", { class: "mono break-anywhere", text: command.model_name }),
      el("td", { text: command.kind === "pull" ? "Download" : "Remove" }),
      el("td", {}, badge(command.status, STATUS_CLASS[command.status])),
      progress, actions);
  }));
}

async function commandAction(template, id) {
  try {
    await api(template.replace("COMMAND_ID", encodeURIComponent(id)), { json: {} });
    refresh();
  } catch (error) {
    toast(error.message, "error");
  }
}

function render() {
  renderOverview();
  renderRemote();
  renderModels();
  renderCommands();
}

let timer = null;
async function refresh() {
  if (document.hidden) return;
  try {
    const fresh = await api(urls.data);
    data = { ...data, ...fresh };
    render();
    updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    updated.textContent = `Could not refresh: ${error.message}`;
  }
  const busy = (data.commands || []).some((command) => ["queued", "running"].includes(command.status)) ||
    ((data.remote && data.remote.sessions) || []).length > 0;
  clearTimeout(timer);
  timer = setTimeout(refresh, busy ? 4000 : 15000);
}

async function arm() {
  if (!data.remote_enabled || !data.remote || !data.remote.remote || !data.remote.remote.enabled) return;
  try { await api(urls.arm, { json: {} }); } catch { /* the page still works; sessions just start a little later */ }
}

document.getElementById("pull-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const input = form.querySelector("input[name=model]");
  const button = form.querySelector("button[type=submit]");
  setBusy(button, true);
  if (await sendCommand("pull", input.value.trim())) input.value = "";
  setBusy(button, false);
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

render();
refresh();
arm();
setInterval(arm, ARM_EVERY_MS);
