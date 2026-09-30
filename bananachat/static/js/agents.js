// Agents: the task list and the "New task" form.
import { api, ApiError, checkStatusSoon, onStatusChange, setBusy, t, toast } from "./core.js";

const form = document.getElementById("new-task-form");
const files = document.getElementById("task-files");
const summary = document.getElementById("task-files-summary");
const submit = document.getElementById("new-task-submit");

if (files && summary) {
  files.addEventListener("change", () => {
    const count = files.files.length;
    summary.textContent = count ? t("agents.files_selected", { count }) : t("agents.files_choose");
  });
}

let sending = false;
let canSend = true;
onStatusChange((state) => {
  canSend = state.canSend;
  if (submit && !sending) submit.disabled = !canSend;
});

if (form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending || !canSend) return;
    const prompt = form.querySelector("textarea[name=prompt]");
    if (!prompt.value.trim()) {
      prompt.focus();
      return;
    }
    sending = true;
    setBusy(submit, true);
    try {
      const data = await api(form.action, { form: new FormData(form) });
      window.location.assign(data.url);
    } catch (error) {
      if (error instanceof ApiError && error.status === 503) checkStatusSoon();
      toast(error.message, "error");
      sending = false;
      setBusy(submit, false);
      submit.disabled = !canSend;
    }
  });
  const prompt = form.querySelector("textarea[name=prompt]");
  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) form.requestSubmit();
  });
}
