// A worker PC's screen (admin/worker_vnc.html): noVNC over a session WebSocket to the PC's own VNC server.
import { createSession, data, ended, onReconnect, setStatus } from "./worker-session.js";
import { RFB } from "../vendor/novnc/novnc.esm.js";

const screen = document.getElementById("screen");
const credentials = document.getElementById("vnc-credentials");
const usernameField = document.getElementById("vnc-username-field");
const cad = document.getElementById("vnc-cad");
const fullscreen = document.getElementById("vnc-fullscreen");
let rfb = null;

/**
 * The session's WebSocket as noVNC sees it: only the binary messages (the VNC protocol) reach noVNC; the text
 * messages (the server's status, the worker's explanations) go to *onText*.
 */
class VncChannel {
  constructor(socket, onText) {
    this.socket = socket;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    socket.binaryType = "arraybuffer";
    socket.onopen = (event) => this.onopen && this.onopen(event);
    socket.onerror = (event) => this.onerror && this.onerror(event);
    socket.onclose = (event) => this.onclose && this.onclose(event);
    socket.onmessage = (event) => {
      if (typeof event.data === "string") onText(event.data);
      else if (this.onmessage) this.onmessage(event);
    };
  }

  get binaryType() { return this.socket.binaryType; }

  set binaryType(value) { this.socket.binaryType = value; }

  get readyState() { return this.socket.readyState; }

  get protocol() { return this.socket.protocol; }

  send(payload) { this.socket.send(payload); }

  close(...args) { this.socket.close(...args); }
}

async function start() {
  setStatus("Opening a session…");
  let opened;
  try {
    opened = await createSession("vnc", data.port);
  } catch {
    return;
  }
  setStatus("Waiting for the worker…");
  screen.replaceChildren();
  const socket = new WebSocket(opened.url);
  let problem = "";
  let connected = false;
  const channel = new VncChannel(socket, (text) => {
    let message = {};
    try { message = JSON.parse(text); } catch { return; }
    if (message.type === "error") {
      problem = String(message.message || "The worker refused the session.");
      setStatus(problem, "error");
    } else if (message.type === "status" && message.state === "failed") {
      problem = String(message.message || "The worker did not connect.");
      setStatus(problem, "error");
    } else if (message.type === "status" && message.state === "connected" && !connected) {
      connected = true;
      attach(channel, () => problem);
    } else if (message.type === "status" && message.message) {
      setStatus(message.message);
    }
  });
  socket.addEventListener("close", () => {
    if (!connected) ended(problem || "The session could not be opened. Is the worker online, and has its owner allowed VNC?", "error");
  });
}

function attach(channel, problem) {
  rfb = new RFB(screen, channel, { shared: true });
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.focusOnClick = true;
  rfb.addEventListener("connect", () => {
    setStatus("Connected.", "ok");
    credentials.hidden = true;
    cad.hidden = false;
    fullscreen.hidden = false;
  });
  rfb.addEventListener("disconnect", (event) => {
    cad.hidden = true;
    fullscreen.hidden = true;
    if (problem()) ended(problem(), "error");
    else ended(event.detail.clean ? "The session ended." : "The connection was lost. Is the worker online and its VNC server running?",
               event.detail.clean ? "muted" : "error");
  });
  rfb.addEventListener("credentialsrequired", (event) => {
    const types = (event.detail && event.detail.types) || ["password"];
    usernameField.hidden = !types.includes("username");
    credentials.hidden = false;
    setStatus("The VNC server asks for its login.");
    credentials.querySelector(types.includes("username") ? "#vnc-username" : "#vnc-password").focus();
    credentials.onsubmit = (submitEvent) => {
      submitEvent.preventDefault();
      const values = { password: document.getElementById("vnc-password").value };
      if (types.includes("username")) values.username = document.getElementById("vnc-username").value;
      document.getElementById("vnc-password").value = "";
      credentials.hidden = true;
      rfb.sendCredentials(values);
    };
  });
  rfb.addEventListener("securityfailure", (event) => {
    setStatus(`The VNC server refused the login${event.detail.reason ? `: ${event.detail.reason}` : "."}`, "error");
  });
}

cad.addEventListener("click", () => rfb && rfb.sendCtrlAltDel());
fullscreen.addEventListener("click", () => screen.requestFullscreen && screen.requestFullscreen());
onReconnect(start);
start();
