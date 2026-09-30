// Administrator page for remote workers: registration (token shown once) and a live list.
// Every value from the server is inserted as text (el()/textContent), never as HTML.
import { api, boot, el, icon, pageData, secretDialog, setBusy, toast } from "./core.js";

const data = pageData();
const urls = data.urls || {};
const body = document.getElementById("workers-body");
const empty = document.getElementById("workers-empty");
const updated = document.getElementById("workers-updated");
const REFRESH_MS = 15000;

const STATUS_CLASS = { online: "badge-success", busy: "badge-warning", disabled: "badge-danger" };
const ACTIVITY_CLASS = { idle: "badge-success", light: "badge-info", active: "badge-warning", gaming: "badge-danger" };

function badge(text, cls) {
  return el("span", { class: `badge ${cls || ""}`.trim(), text });
}

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function actionForm(action, fields, button, confirm) {
  const form = el("form", { method: "post", action },
    el("input", { type: "hidden", name: "csrf_token", value: boot.csrf }),
    ...Object.entries(fields).map(([name, value]) => el("input", { type: "hidden", name, value })),
    button);
  if (confirm) {
    form.dataset.confirm = confirm.message;
    form.dataset.confirmLabel = confirm.label;
    if (confirm.danger) form.dataset.confirmDanger = "";
  }
  return form;
}

function row(worker) {
  const url = (template) => template.replace("WORKER_ID", encodeURIComponent(worker.id));
  const disabled = worker.status === "disabled";
  const nameCell = el("td", {}, el("strong", { text: worker.name }), el("br"),
    el("span", { class: "small faint mono", text: worker.id.slice(0, 12) }));
  if (worker.platform) nameCell.append(el("br"), el("span", { class: "small muted", text: worker.platform }));

  const statusCell = el("td", {}, badge(worker.status, STATUS_CLASS[worker.status]));
  if (worker.working) statusCell.append(" ", badge("running a job", "badge-primary"));

  const activityCell = el("td", {}, worker.activity ? badge(worker.activity, ACTIVITY_CLASS[worker.activity]) : el("span", { class: "faint", text: "—" }));

  const gpuCell = el("td", { text: worker.gpu_name || "—" });
  if (worker.gpu_util !== null && worker.gpu_util !== undefined) gpuCell.append(el("br"), el("span", { class: "small muted", text: `${worker.gpu_util}% busy` }));

  const modelsCell = el("td");
  if (worker.models && worker.models.length) {
    const count = worker.models.length;
    modelsCell.append(el("details", {}, el("summary", { text: `${count} model${count === 1 ? "" : "s"}` }),
      el("ul", { class: "small" }, worker.models.map((model) => el("li", { class: "mono", text: model })))));
  } else {
    modelsCell.append(el("span", { class: "faint", text: "none reported" }));
  }
  if (worker.ollama_version) modelsCell.append(el("br"), el("span", { class: "small muted", text: `Ollama ${worker.ollama_version}` }));

  const seen = el("td", {}, el("span", { title: worker.last_heartbeat || "", text: ago(worker.seconds_since_heartbeat) }));

  const toggle = actionForm(url(urls.state), { enabled: disabled ? "1" : "0" },
    el("button", { type: "submit", class: "btn btn-sm", text: disabled ? "Enable" : "Disable" }),
    disabled ? null : { message: `Stop sending jobs to “${worker.name}”? A job it is running now is stopped.`, label: "Disable" });
  const remove = actionForm(url(urls.delete), {},
    el("button", { type: "submit", class: "btn btn-sm btn-danger" }, icon("trash"), el("span", { text: "Remove" })),
    { message: `Remove “${worker.name}”? Its token stops working immediately and a job it is running now is stopped.`, label: "Remove", danger: true });

  return el("tr", { dataset: { workerId: worker.id } }, nameCell, statusCell, activityCell, gpuCell, modelsCell, seen,
    el("td", { class: "actions" }, toggle, " ", remove));
}

function render(workers) {
  // Keep open model lists open across refreshes.
  const open = new Set([...body.querySelectorAll("tr")].filter((tr) => tr.querySelector("details[open]")).map((tr) => tr.dataset.workerId));
  body.replaceChildren(...workers.map((worker) => {
    const node = row(worker);
    if (open.has(worker.id)) node.querySelector("details")?.setAttribute("open", "");
    return node;
  }));
  empty.hidden = workers.length > 0;
}

async function refresh() {
  if (document.hidden) return;
  try {
    const result = await api(urls.data);
    render(result.workers || []);
    updated.textContent = `Refreshes every 15 seconds · updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    updated.textContent = `Could not refresh: ${error.message}`;
  }
}

const form = document.getElementById("register-form");
form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = form.querySelector("input[name=name]");
  const button = form.querySelector("button[type=submit]");
  setBusy(button, true);
  try {
    const result = await api(urls.register, { json: { name: input.value } });
    input.value = "";
    secretDialog({
      title: `Token for “${result.worker.name}”`,
      message: "Copy it now: it is shown only once and cannot be recovered.",
      secret: result.token,
      note: "Put it in BC_WORKER_TOKEN in the worker's settings file. If it is lost, remove the worker and register it again.",
    });
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
});

setInterval(refresh, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
