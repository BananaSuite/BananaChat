// Developer page: create and rotate API tokens without leaving the page; rename them through a dialog.
//
// Forms work without JavaScript too (the server then answers with a page that
// shows the new token once). With JavaScript the token is shown once in a
// dialog built from the JSON response; it is never stored anywhere.
import { api, promptDialog, secretDialog, setBusy, t, toast } from "./core.js";

function showToken(data, rotated) {
  const dialog = secretDialog({
    title: rotated ? t("developer_token_rotated") : t("developer_token_created"),
    message: t("developer_token_once"),
    secret: data.token,
    note: data.name ? t("developer_token_named", { name: data.name }) : "",
  });
  // The list on the page changed: reload once the token was seen.
  dialog.addEventListener("close", () => window.location.reload());
}

/** Forms marked data-token-form submit through fetch (after core.js confirmed them if needed). */
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.tokenForm || event.defaultPrevented) return;
  event.preventDefault();
  const button = form.querySelector("[type=submit]");
  setBusy(button, true);
  try {
    const name = form.elements.name ? form.elements.name.value.trim() : undefined;
    const data = await api(form.action, { json: name === undefined ? {} : { name } });
    if (form.elements.name) form.elements.name.value = "";
    showToken(data, form.dataset.tokenForm === "rotate");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
});

/** Rename through a dialog instead of the inline form. */
for (const details of document.querySelectorAll("details.dev-rename")) {
  const summary = details.querySelector("summary");
  const form = details.querySelector("form");
  const input = form.elements.name;
  summary.addEventListener("click", async (event) => {
    event.preventDefault();
    const name = await promptDialog({ title: t("developer_rename_title"), label: t("developer_token_name"), value: input.value, maxLength: 64 });
    if (name === null) return;
    if (!name.trim()) {
      toast(t("developer_name_required"), "error");
      return;
    }
    // Submit the form itself: the page comes back with the new name everywhere (the Rotate and Revoke
    // labels and confirmation questions too) and the server's confirmation.
    input.value = name.trim();
    form.submit();
  });
}
