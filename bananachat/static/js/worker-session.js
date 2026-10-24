// Opening a maintenance session to a worker PC from the browser (terminal or screen pages).
// The session is created with a POST (which may need the administrator's confirmation), then reached
// through a WebSocket on this site that only works for the session's creator, from this site's pages.
import { api, pageData, setBusy } from "./core.js";

export const data = pageData();
const status = document.getElementById("remote-status");
const confirmForm = document.getElementById("remote-confirm");
const endButton = document.getElementById("remote-end");
const reconnect = document.getElementById("remote-reconnect");

export function setStatus(text, kind = "muted") {
  status.textContent = text;
  status.className = `small grow ${kind === "error" ? "danger-text" : kind === "ok" ? "success-text" : "muted"}`;
}

function socketUrl(path) {
  const url = new URL(path, window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

/** Create a session; resolves ``{session, url}`` (the WebSocket address), asking for a confirmation if needed. */
export function createSession(kind, port, { statusMessages = true } = {}) {
  return new Promise((resolve, reject) => {
    const attempt = async (secret) => {
      const button = confirmForm.querySelector("button[type=submit]");
      setBusy(button, true);
      try {
        const result = await api(data.session, { json: { kind, port, reauth_secret: secret || "" } });
        confirmForm.hidden = true;
        const session = result.session;
        endButton.hidden = false;
        endButton.onclick = async () => {
          try { await api(data.close.replace("TUNNEL_ID", session.id), { json: {} }); } catch { /* ended anyway */ }
          setStatus("Session ended.");
          endButton.hidden = true;
          reconnect.hidden = false;
        };
        resolve({ session, url: socketUrl(session.ws + (statusMessages ? "?status=1" : "")) });
      } catch (error) {
        if (error.code === "reauth_required") {
          confirmForm.hidden = false;
          setStatus(secret ? error.message : "Confirm it is you to open the session.", secret ? "error" : "muted");
          confirmForm.querySelector("input").focus();
          confirmForm.onsubmit = (event) => {
            event.preventDefault();
            const input = confirmForm.querySelector("input");
            const value = input.value;
            input.value = "";
            attempt(value);
          };
        } else {
          setStatus(error.message, "error");
          reconnect.hidden = false;
          reject(error);
        }
      } finally {
        setBusy(button, false);
      }
    };
    attempt("");
  });
}

export function onReconnect(start) {
  reconnect.onclick = () => {
    reconnect.hidden = true;
    start();
  };
}

export function ended(message, kind = "muted") {
  setStatus(message, kind);
  endButton.hidden = true;
  reconnect.hidden = false;
}
