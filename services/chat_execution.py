"""Bounded chat execution with durable outcomes independent of its SSE client."""

from collections import deque
import json
import logging
import sqlite3
import threading
import time

import config
import db
from db import _chat_runs as runs
from services import chat_files, chat_routing, dispatcher, model_access, ollama, queue as q

_logger = logging.getLogger("bananachat.chat")


class GenerationStopped(RuntimeError):
    pass


class EventChannel:
    """A slow/disconnected reader cannot retain an unbounded response buffer."""
    MAX_BYTES = 256 * 1024
    MAX_EVENTS = 128

    def __init__(self):
        self._items = deque()
        self._bytes = 0
        self._closed = False
        self._attached = True
        self._condition = threading.Condition()

    def send(self, event):
        encoded = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        size = len(encoded.encode("utf-8"))
        with self._condition:
            if self._closed or not self._attached:
                return
            if len(self._items) >= self.MAX_EVENTS or self._bytes + size > self.MAX_BYTES:
                self._items.clear()
                encoded = 'data: {"type":"reconnect","message":"Connection too slow. Reload this chat to see the saved response."}\n\n'
                self._bytes = 0
                size = len(encoded)
                self._closed = True
            self._items.append((encoded, size))
            self._bytes += size
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def detach(self):
        with self._condition:
            self._attached = False
            self._items.clear()
            self._bytes = 0
            self._condition.notify_all()

    def stream(self):
        try:
            while True:
                with self._condition:
                    if not self._items and not self._closed:
                        self._condition.wait(10)
                    if self._items:
                        item, size = self._items.popleft()
                        self._bytes -= size
                    elif self._closed or not self._attached:
                        return
                    else:
                        item = ": heartbeat\n\n"
                yield item
        finally:
            self.detach()


class ChatExecution:
    """One admitted task owns its session until the outcome commits or expires."""
    def __init__(self, *, session_id, token, user, content, model, body, vision_required,
                 notice, slot, stop_event):
        self.session_id, self.token, self.user = session_id, token, dict(user)
        self.content, self.model, self.body = content, dict(model), dict(body)
        self.vision_required, self.notice = vision_required, notice
        self.slot, self.stop_event = slot, stop_event
        self.channel = EventChannel()
        self._monitor_stop = threading.Event()
        self._stop_reason = "Generation stopped."
        self._started = time.monotonic()
        self._parts, self._bytes = [], 0
        self._tokens_in = self._tokens_out = self._chunks = 0
        self._last_checkpoint = 0.0
        self.thread = threading.Thread(target=self._run, name="chat-generation", daemon=True)

    def start(self):
        try:
            self.thread.start()
        except BaseException:
            self.slot.close()
            runs.finish_chat_run(self.session_id, self.token, "", "failed", "The server could not start generation.")
            raise

    def _monitor(self):
        last_touch = time.monotonic()
        while not self._monitor_stop.wait(0.5):
            now = time.monotonic()
            try:
                if now - self._started >= config.GENERATION_TIMEOUT:
                    self._stop_reason = "Generation reached its time limit. You can ask the model to continue."
                    self.stop_event.set()
                elif db.active_stream_should_stop(self.session_id, self.token):
                    self.stop_event.set()
                elif now - last_touch >= 5:
                    if not db.touch_active_stream(self.session_id, self.token):
                        self._stop_reason = "Generation ownership expired. The saved checkpoint will be recovered."
                        self.stop_event.set()
                    last_touch = now
            except (sqlite3.DatabaseError, OSError):
                _logger.exception("Chat lease heartbeat failed")
                self._stop_reason = "Storage became unavailable. The last saved checkpoint is preserved."
                self.stop_event.set()
            if self.stop_event.is_set():
                return

    def _check_stop(self):
        if time.monotonic() - self._started >= config.GENERATION_TIMEOUT:
            self._stop_reason = "Generation reached its time limit. You can ask the model to continue."
            self.stop_event.set()
        if self.stop_event.is_set():
            raise GenerationStopped(self._stop_reason)
        self.slot.check()

    def _consume(self, iterator):
        """Only backend failures before any output may trigger model fallback."""
        iterator = iter(iterator)
        try:
            while True:
                self._check_stop()
                try:
                    chunk, done, usage = next(iterator)
                except StopIteration:
                    self._check_stop()
                    raise chat_routing.InferenceFailed("Backend ended without a completion marker") from None
                except (RuntimeError, OSError) as error:
                    self._check_stop()
                    raise chat_routing.InferenceFailed("Backend inference failed") from error
                self._check_stop()
                if not isinstance(chunk, str):
                    raise chat_routing.InferenceFailed("Backend returned invalid text")
                if chunk:
                    data = chunk.encode("utf-8")
                    remaining = config.CHAT_MAX_RESPONSE_BYTES - self._bytes
                    limited = len(data) > remaining
                    if limited:
                        chunk = data[:remaining].decode("utf-8", errors="ignore")
                    self._parts.append(chunk)
                    self._bytes += len(chunk.encode("utf-8"))
                    self._chunks += 1
                    for start in range(0, len(chunk), 4096):
                        self.channel.send({"type": "delta", "content": chunk[start:start+4096], "tokens": self._chunks})
                    if limited:
                        raise GenerationStopped("Response reached its size limit. You can ask the model to continue.")
                    if time.monotonic() - self._last_checkpoint >= 2:
                        self._last_checkpoint = time.monotonic()
                        if not runs.checkpoint_chat_run(self.session_id, self.token, "".join(self._parts), self.model["id"]):
                            raise GenerationStopped("Generation ownership expired. The saved checkpoint will be recovered.")
                if done:
                    for name in ("prompt_tokens", "completion_tokens"):
                        value = usage.get(name, 0)
                        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**31 - 1:
                            raise chat_routing.InferenceFailed("Backend returned invalid usage")
                    self._tokens_in = usage.get("prompt_tokens", 0)
                    self._tokens_out = usage.get("completion_tokens", 0)
                    if usage.get("finish_reason") == "length":
                        raise GenerationStopped("The model reached its token limit. You can ask it to continue.")
                    return
        finally:
            close = getattr(iterator, "close", None)
            if close:
                close()

    def _infer(self):
        user = db.get_user_by_id(self.user["id"])
        if not user or user["suspended"]:
            raise GenerationStopped("This account can no longer generate a response.")
        self.user = dict(user)
        messages, attachments = runs.load_chat_context(self.session_id)
        context = chat_files.build_model_messages(messages, attachments)
        sess = db.get_session(self.session_id)
        personality = model_access.get_usable_personality(self.user, sess.get("personality_id")) if sess else None
        attempted = set()
        for attempt in range(3):
            self._check_stop()
            current = db.get_model_by_id(self.model["id"])
            if current is None or not current["backend_available"]:
                current, error, _, notice = chat_routing.select(self.user, self.model["ollama_name"], self.vision_required)
                if error:
                    raise GenerationStopped(error)
                self.notice = notice or self.notice
            if not current or not model_access.can_user_access_model(self.user, current, "chat"):
                raise GenerationStopped("This model is no longer available to your account. Choose an available model.")
            self.model = dict(current)
            name, backend = self.model["ollama_name"], self.model["backend_model_name"]
            attempted.update((self.model["id"], name, backend))
            options = chat_routing.options_for(self.model, self.body)
            history = chat_routing.history_for(context, self.model, personality)
            remote = dispatcher.should_use_worker(self.user["role"] == "admin", backend)
            self.channel.send({"type": "start", "model": name, "wait_ms": self.slot.wait_ms,
                               "is_reasoning": bool(self.model.get("is_reasoning")),
                               "via_worker": remote, "notice": self.notice})
            try:
                if remote:
                    job = dispatcher.create_worker_job(backend, history, options,
                        q.PRIORITY_ADMIN if self.user["role"] == "admin" else q.PRIORITY_CHAT)
                    self._consume(dispatcher.stream_from_worker(job, stop_ev=self.stop_event))
                else:
                    self._consume(ollama.generate_chat_stream(backend, history, options=options))
                return
            except chat_routing.InferenceFailed:
                if self._parts or self.stop_event.is_set() or attempt == 2:
                    raise
                replacement, error = ollama.select_auto_model(
                    user=self.user, surface="chat", vision_only=self.vision_required, exclude=attempted)
                if not replacement:
                    raise chat_routing.InferenceFailed("No working model is available") from None
                self.model = dict(replacement)
                self.notice = "The previous model could not respond. This chat is using " + self.model["ollama_name"] + "."

    def _run(self):
        state, error = "failed", "Generation stopped before completion."
        monitor = threading.Thread(target=self._monitor, name="chat-lease", daemon=True)
        try:
            monitor.start()
            if self.slot.queue_position:
                self.channel.send({"type": "queued", "position": self.slot.queue_position})
            with self.slot:
                self._infer()
                self._check_stop()
            state, error = "completed", ""
        except GenerationStopped as exc:
            state, error = "stopped", str(exc)
        except q.QueueCancelledError:
            state, error = "stopped", self._stop_reason if self.stop_event.is_set() else "Generation lost its queue lease. Please retry."
        except TimeoutError:
            error = "The request timed out waiting for inference. Please retry."
        except Exception:
            _logger.exception("Chat generation failed")
            error = "The inference service could not finish this response. Any saved partial output is preserved."
        finally:
            self.slot.close()
            try:
                title = self.content[:60].rstrip() + ("…" if len(self.content) > 60 else "")
                message_id, saved_title = runs.finish_chat_run(
                    self.session_id, self.token, "".join(self._parts), state, error,
                    self.model["id"], self._tokens_in, self._tokens_out,
                    int((time.monotonic() - self._started) * 1000), self.slot.wait_ms,
                    title if self._parts else None, include_title=True,
                )
                payload = {"type": "done" if state in {"completed", "stopped"} else "error",
                           "state": state, "message": error, "message_id": message_id,
                           "tokens_in": self._tokens_in, "tokens_out": self._tokens_out}
                if self._parts and saved_title:
                    payload["auto_title"] = saved_title
                self.channel.send(payload)
            except Exception:
                # Keep the owned checkpoint for crash recovery. Never announce a
                # successful response if saving its message or usage rolled back.
                _logger.exception("Chat outcome could not be committed")
                self.channel.send({"type": "error", "message": "The response could not be saved. The last checkpoint will be recovered when storage is available."})
            finally:
                self._monitor_stop.set()
                if monitor.ident is not None:
                    monitor.join(timeout=1)
                self.channel.close()
