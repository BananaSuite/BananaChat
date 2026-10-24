// Administration → Models → Claude / Codex: signing pooled accounts in with the official CLI's own login.
// The server runs the CLI and relays its sign-in link (and, for Codex, its one-time code); the administrator
// signs in on the provider's page. Every value from the server is inserted as text, never as HTML.
import { api, copyText, el, icon, pageData, setBusy, toast } from "./core.js";

const data = pageData();
const urls = data.urls || {};
const POLL_MS = 2000;
const polling = new Map();

const urlFor = (template, accountId) => template.replace("ACCOUNT_ID", encodeURIComponent(accountId));

function rowFor(accountId) {
  return document.querySelector(`[data-signin-account="${CSS.escape(String(accountId))}"]`);
}

function confirmationField(panel) {
  let input = panel.querySelector("input[name=reauth_secret]");
  if (input) return input;
  const id = `signin-reauth-${Math.random().toString(36).slice(2)}`;
  const field = el("div", { class: "field field-narrow" },
    el("label", { for: id, class: "required", text: "Your password or authenticator code" }),
    el("input", { type: "password", id, name: "reauth_secret", maxlength: "1024", autocomplete: "current-password",
                  spellcheck: "false", "aria-describedby": `${id}-hint` }),
    el("span", { class: "hint", id: `${id}-hint`, text: "Signing a subscription in needs a recent confirmation; not asked again for 10 minutes." }));
  panel.replaceChildren(field, el("div", { class: "form-actions form-actions-start" },
    el("button", { type: "button", class: "btn btn-primary", "data-signin-confirm": "" }, "Continue"),
    el("button", { type: "button", class: "btn btn-ghost", "data-signin-close": "" }, "Cancel")));
  panel.hidden = false;
  input = field.querySelector("input");
  input.focus();
  return input;
}

function linkBlock(url) {
  return el("p", { class: "mb-0" },
    el("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "btn btn-primary btn-sm" },
      icon("globe"), el("span", { text: data.provider === "claude" ? "Open Anthropic's sign-in page" : "Open OpenAI's sign-in page" })),
    " ",
    el("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: async () => { await copyText(url); toast("Link copied.", "success"); } },
      icon("copy"), el("span", { text: "Copy link" })));
}

function render(accountId, login) {
  const row = rowFor(accountId);
  if (!row) return;
  const panel = row.querySelector("[data-signin-panel]");
  const startButton = row.querySelector("[data-signin-start]");
  if (!panel) return;
  if (!login) {
    panel.hidden = true;
    panel.replaceChildren();
    return;
  }
  panel.hidden = false;
  const parts = [];
  const state = login.state;
  if (state === "starting") {
    parts.push(el("p", { class: "small mb-0", text: login.message || "Starting the official sign-in…" }));
  } else if (state === "waiting" && login.url) {
    if (login.kind === "device") {
      parts.push(el("p", { class: "small mb-0", text: "1. Open OpenAI's page and sign in to the ChatGPT account of this subscription." }));
      parts.push(linkBlock(login.url));
      parts.push(el("p", { class: "small mb-0", text: "2. Enter this one-time code there (it expires in 15 minutes):" }));
      parts.push(el("p", { class: "device-code mb-0" }, el("code", { text: login.user_code || "" }), " ",
        el("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: async () => { await copyText(login.user_code || ""); toast("Code copied.", "success"); } }, icon("copy"), el("span", { text: "Copy" }))));
      parts.push(el("p", { class: "small muted mb-0", text: "This page continues on its own once you approve. If OpenAI says device codes are not allowed, turn on device code sign-in for Codex in the ChatGPT security settings (or ask the workspace administrator)." }));
    } else {
      parts.push(el("p", { class: "small mb-0", text: "1. Open Anthropic's page, sign in to the Claude account of this subscription and approve the access." }));
      parts.push(linkBlock(login.url));
      const id = `signin-code-${accountId}`;
      const input = el("input", { type: "text", id, name: "code", autocomplete: "off", spellcheck: "false", maxlength: "600", class: "mono", required: "" });
      const button = el("button", { type: "submit", class: "btn btn-primary" }, "Finish sign-in");
      const form = el("form", { class: "stack", "data-signin-code": "" },
        el("div", { class: "field" }, el("label", { for: id, text: "2. Paste the code the page shows after you approve" }), input),
        el("div", { class: "form-actions form-actions-start" }, button));
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        setBusy(button, true);
        try {
          const result = await api(urlFor(urls.code, accountId), { json: { code: input.value } });
          input.value = "";
          render(accountId, { ...result.login, state: "verifying", message: "Code sent; finishing the sign-in…" });
          poll(accountId);
        } catch (error) {
          toast(error.message, "error");
        } finally {
          setBusy(button, false);
        }
      });
      parts.push(form);
    }
  } else if (state === "verifying") {
    parts.push(el("p", { class: "small mb-0", text: login.message || "Checking the new sign-in…" }));
  } else if (state === "done") {
    parts.push(el("p", { class: "small success-text mb-0" }, icon("check"), " ", login.message || "Signed in."));
    setTimeout(() => window.location.reload(), 1500);
  } else if (state === "failed" || state === "cancelled") {
    parts.push(el("p", { class: `small mb-0 ${state === "failed" ? "danger-text" : "muted"}`, text: login.message || (state === "failed" ? "The sign-in failed." : "Cancelled.") }));
  }
  const active = ["starting", "waiting", "verifying"].includes(state);
  const actions = el("div", { class: "row gap-1" });
  if (active) {
    actions.append(el("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: () => cancel(accountId) }, "Cancel sign-in"));
  } else {
    actions.append(el("button", { type: "button", class: "btn btn-sm btn-ghost", "data-signin-close": "" }, "Close"));
  }
  parts.push(actions);
  panel.replaceChildren(...parts);
  if (startButton) startButton.disabled = active;
}

async function poll(accountId) {
  if (polling.has(accountId)) return;
  const tick = async () => {
    try {
      const result = await api(urlFor(urls.login, accountId));
      const login = result.login;
      const row = rowFor(accountId);
      // Keep a half-typed code: only redraw when the state changed.
      const panel = row?.querySelector("[data-signin-panel]");
      const shown = panel?.dataset.state;
      const key = login ? `${login.id}:${login.state}:${login.url || ""}` : "";
      if (shown !== key) {
        render(accountId, login);
        if (panel) panel.dataset.state = key;
      }
      if (!login || !["starting", "waiting", "verifying"].includes(login.state)) {
        clearInterval(polling.get(accountId));
        polling.delete(accountId);
      }
    } catch (error) {
      if (error.status === 404) {
        clearInterval(polling.get(accountId));
        polling.delete(accountId);
      }
    }
  };
  polling.set(accountId, setInterval(tick, POLL_MS));
  await tick();
}

async function start(accountId, secret) {
  const row = rowFor(accountId);
  const panel = row.querySelector("[data-signin-panel]");
  const button = row.querySelector("[data-signin-start]");
  setBusy(button, true);
  try {
    const result = await api(urlFor(urls.login, accountId), { json: { reauth_secret: secret || "" } });
    render(accountId, result.login);
    panel.dataset.state = "";
    poll(accountId);
  } catch (error) {
    if (error.code === "reauth_required") {
      const input = confirmationField(panel);
      if (secret) toast(error.message, "error");
      panel.querySelector("[data-signin-confirm]").onclick = () => start(accountId, input.value);
      input.onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); start(accountId, input.value); } };
    } else {
      toast(error.message, "error");
    }
  } finally {
    setBusy(button, false);
  }
}

async function cancel(accountId) {
  try {
    const result = await api(urlFor(urls.cancel, accountId), { json: {} });
    render(accountId, result.login);
  } catch (error) {
    toast(error.message, "error");
  }
}

document.addEventListener("click", (event) => {
  const startButton = event.target.closest("[data-signin-start]");
  if (startButton) {
    const row = startButton.closest("[data-signin-account]");
    start(row.dataset.signinAccount);
    return;
  }
  const close = event.target.closest("[data-signin-close]");
  if (close) {
    const panel = close.closest("[data-signin-panel]");
    if (panel) {
      panel.hidden = true;
      panel.replaceChildren();
      panel.dataset.state = "";
    }
  }
});

for (const accountId of data.active || []) poll(String(accountId));
