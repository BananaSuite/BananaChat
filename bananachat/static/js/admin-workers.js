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

const capitalize = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

function row(worker) {
  const url = (template) => template.replace("WORKER_ID", encodeURIComponent(worker.id));
  const disabled = worker.status === "disabled";
  const nameCell = el("td", { class: "cell-full" }, el("strong", { text: worker.name }), el("br"),
    el("span", { class: "small faint mono", text: worker.id.slice(0, 12) }));
  if (worker.platform) nameCell.append(el("br"), el("span", { class: "small muted", text: worker.platform }));

  const statusCell = el("td", {}, badge(capitalize(worker.status), STATUS_CLASS[worker.status]));
  if (worker.working) statusCell.append(" ", badge("Running a job", "badge-primary"));
  if (worker.paused) statusCell.append(" ", badge("Paused after failures", "badge-warning"));

  const activityCell = el("td", { dataset: { label: "Activity:" } },
    worker.activity ? badge(capitalize(worker.activity), ACTIVITY_CLASS[worker.activity]) : el("span", { class: "faint", text: "—" }));

  const gpuCell = el("td", { dataset: { label: "GPU:" }, text: worker.gpu_name || "—" });
  if (worker.gpu_util !== null && worker.gpu_util !== undefined && ["online", "busy"].includes(worker.status)) gpuCell.append(el("br"), el("span", { class: "small muted", text: `${worker.gpu_util}% busy` }));

  const modelsCell = el("td", { dataset: { label: "Models:" } });
  if (worker.models && worker.models.length) {
    const count = worker.models.length;
    modelsCell.append(el("details", {}, el("summary", { class: "nowrap", text: `${count} model${count === 1 ? "" : "s"}` }),
      el("ul", { class: "small" }, worker.models.map((model) => el("li", { class: "mono", text: model })))));
  } else {
    modelsCell.append(el("span", { class: "faint", text: "none reported" }));
  }
  if (worker.ollama_version) modelsCell.append(el("br"), el("span", { class: "small muted nowrap", text: `Ollama ${worker.ollama_version}` }));

  const seen = el("td", { class: "nowrap", dataset: { label: "Last heartbeat:" } },
    el("span", { title: worker.last_heartbeat || "", text: ago(worker.seconds_since_heartbeat) }));

  const toggle = actionForm(url(urls.state), { enabled: disabled ? "1" : "0" },
    el("button", { type: "submit", class: "btn btn-sm", text: disabled ? "Enable" : "Disable" }),
    disabled ? null : { message: `Stop sending jobs to “${worker.name}”? A job it is running now is stopped.`, label: "Disable" });
  const remove = actionForm(url(urls.delete), {},
    el("button", { type: "submit", class: "btn btn-sm btn-danger" }, icon("trash"), el("span", { text: "Remove" })),
    { message: `Remove “${worker.name}”? Its token stops working immediately and a job it is running now is stopped.`, label: "Remove", danger: true });

  const manage = el("a", { class: "btn btn-sm", href: url(urls.detail) }, icon("settings"), el("span", { text: "Manage" }));
  return el("tr", { dataset: { workerId: worker.id } }, nameCell, statusCell, activityCell, gpuCell, modelsCell, seen,
    el("td", { class: "actions" }, manage, " ", toggle, " ", remove));
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

// The confirmation field of admin/_reauth.html, for when the ten minutes run out while the page is open.
function addConfirmationField() {
  const id = "worker-reauth";
  const field = el("div", { class: "field field-narrow" }, [
    el("label", { for: id, class: "required", text: "Your password or authenticator code" }),
    el("input", { type: "password", id, name: "reauth_secret", required: "", maxlength: "1024",
                  autocomplete: "current-password", spellcheck: "false", "aria-describedby": `${id}-hint` }),
    el("span", { class: "hint", id: `${id}-hint`, text: "Confirms it is you; not asked again for 10 minutes." }),
  ]);
  form.querySelector("button[type=submit]").before(field);
  return field.querySelector("input");
}

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = form.querySelector("input[name=name]");
  // Present while this session must confirm it is the administrator (recent sign-in or confirmation).
  const confirmation = form.querySelector("input[name=reauth_secret]");
  const button = form.querySelector("button[type=submit]");
  setBusy(button, true);
  try {
    const result = await api(urls.register, { json: { name: input.value, reauth_secret: confirmation?.value ?? "" } });
    input.value = "";
    confirmation?.closest(".field")?.remove(); // not asked again for ten minutes
    secretDialog({
      title: `Token for “${result.worker.name}”`,
      message: "Copy it now: it is shown only once and cannot be recovered.",
      secret: result.token,
      note: "Put it in BC_WORKER_TOKEN in the worker's settings file. If it is lost, remove the worker and register it again.",
    });
    await refresh();
  } catch (error) {
    if (confirmation) confirmation.value = "";
    toast(error.message, "error");
    if (error.code === "reauth_required") (confirmation || addConfirmationField()).focus();
  } finally {
    setBusy(button, false);
  }
});

// Access keys of the connect helper: created with a confirmation, shown once.
const keyForm = document.getElementById("key-form");
keyForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const confirmation = keyForm.querySelector("input[name=reauth_secret]");
  const button = keyForm.querySelector("button[type=submit]");
  setBusy(button, true);
  try {
    const result = await api(urls.key_create, { json: {
      label: keyForm.querySelector("input[name=label]").value,
      hours: Number(keyForm.querySelector("input[name=hours]").value || 8),
      reauth_secret: confirmation?.value ?? "" } });
    confirmation?.closest(".field")?.remove();
    const dialog = secretDialog({
      title: "Access key",
      message: `Copy it now: it is shown only once. It works for ${result.key.hours} hour(s) or until you revoke it.`,
      secret: result.key.token,
      note: "Run: python bananachat_connect.py login " + window.location.origin + " and paste it when asked.",
    });
    dialog?.addEventListener?.("close", () => window.location.reload());
  } catch (error) {
    if (confirmation) confirmation.value = "";
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
});

setInterval(refresh, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
