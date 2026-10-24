// The built-in terminal of a worker PC (admin/worker_terminal.html): xterm.js over a session WebSocket.
// Binary messages carry the terminal's bytes; text messages are JSON status (from the server) or resize
// requests (to the worker).
import { createSession, data, ended, onReconnect, setStatus } from "./worker-session.js";

// xterm.js adds <style> elements of its own; give them this page's nonce so the strict policy accepts them.
const createElement = Document.prototype.createElement;
Document.prototype.createElement = function createWithNonce(name, options) {
  const node = createElement.call(this, name, options);
  if (data.nonce && String(name).toLowerCase() === "style") {
    node.nonce = data.nonce;
    node.setAttribute("nonce", data.nonce);
  }
  return node;
};
const { Terminal, FitAddon } = await import("../vendor/xterm/xterm.esm.js");

const container = document.getElementById("terminal");
const term = new Terminal({ cursorBlink: true, convertEol: false, scrollback: 5000, fontSize: 14,
                            fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" });
const fit = new FitAddon();
term.loadAddon(fit);
term.open(container);
fit.fit();
const encoder = new TextEncoder();
let socket = null;

function sendResize() {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }
}

term.onData((text) => {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(encoder.encode(text));
});
term.onBinary((text) => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(Uint8Array.from(text, (char) => char.charCodeAt(0) & 0xff));
  }
});
term.onResize(sendResize);
new ResizeObserver(() => { try { fit.fit(); } catch { /* hidden */ } }).observe(container);

async function start() {
  setStatus("Opening a session…");
  let opened;
  try {
    opened = await createSession("shell", 0);
  } catch {
    return;
  }
  socket = new WebSocket(opened.url);
  socket.binaryType = "arraybuffer";
  let connected = false;
  socket.onmessage = (event) => {
    if (typeof event.data === "string") {
      let message = {};
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === "status") {
        if (message.state === "connected") {
          connected = true;
          setStatus("Connected.", "ok");
          sendResize();
          term.focus();
        } else if (message.state === "failed") {
          setStatus(message.message || "The worker did not connect.", "error");
        } else {
          setStatus(message.message || "Waiting for the worker…");
        }
      } else if (message.type === "error") {
        term.write(`\r\n\x1b[31m${String(message.message || "Error")}\x1b[0m\r\n`);
      }
      return;
    }
    term.write(new Uint8Array(event.data));
  };
  socket.onclose = () => {
    ended(connected ? "The session ended." : "The session could not be opened. Is the worker online, and has its owner allowed the terminal?", connected ? "muted" : "error");
  };
  socket.onerror = () => setStatus("The connection failed.", "error");
}

onReconnect(() => { term.reset(); start(); });
start();
