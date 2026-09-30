"""Chat: sending messages, background generation, streaming and retention.

Sending (``POST /chat/<sid>/send``)
-----------------------------------
:func:`prepare` checks a request in this order, cheapest first: the message
(size, no files in no-history chats), the chat pool's request rate
(``services.limits``, administrators exempt), the chat's 5-hour and weekly
tokens, whether a run is already active, and only then the uploaded files
(images are decoded and PDFs parsed only for requests that may run). It then
selects the model (vision when the chat holds images), the reasoning effort
(``effort``: a level the account has unlocked for the model, else 403
``reasoning_effort_locked``), checks the model's own limits
(``limits.admit``) and builds its options.

:func:`start` stores the user's message and takes the chat's lease
(:mod:`bananachat.db.runs`), then runs :func:`services.inference.generate`
in a worker thread. The run is independent of the HTTP connection: when the
browser disconnects it keeps going, saves checkpoints of the partial answer
every :data:`CHECKPOINT_SECONDS` and stores the final answer, credits and
metrics. Stop requests and lease renewal go through ``services.supervisor``,
so they work whichever process serves them.

Streaming protocol
------------------
The response is ``text/event-stream``. Every event is one line
``data: {json}`` followed by a blank line; comment lines (``: ping``) are sent
every :data:`HEARTBEAT_SECONDS` while nothing else happens. Events (``type``):

``queued``  ``{position}`` - waiting for a free inference slot (1 = next).
``start``   ``{model, display_name, notice, via_worker, reasoning}`` - a model
            began answering; sent again when another model takes over after a
            failure (``notice`` explains the switch).
``delta``   ``{text, thinking}`` - the next piece of the answer; ``thinking``
            pieces are the model's reasoning.
``done``    terminal, the answer was saved (``state`` is ``completed`` or
            ``stopped``).
``error``   terminal, the run failed (``state`` is ``failed`` or
            ``interrupted``).

Terminal events carry ``{state, message, message_id, user_message_id,
tokens_in, tokens_out, title, model, display_name}``; ``message`` explains
non-completed states, ``message_id`` is the saved answer (null when nothing
was produced) and ``user_message_id`` the saved message that was sent.

A request refused before it runs gets a JSON error instead of a stream:
400 (invalid input), 403 (a locked model or effort level), 404, 409 (a response
is already being generated), 429 (rate limit, used-up tokens or a full queue,
with ``Retry-After``) or 503 (no
model available). If a stream ends without a terminal event, the client polls
``GET /chat/<sid>/status``, which reports the saved partial answer and state.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from flask import current_app

from bananachat import db
from bananachat.db import catalog, chats, credits, runs, users
from bananachat.i18n import translate
from bananachat.services import attachments, background, inference, limits, queue, supervisor
from bananachat.services import personalities as personality_service
from bananachat.services.access import AccessContext, usable_models
from bananachat.services.upstream import MAX_CONTEXT_IMAGE_BYTES, Cancelled, CancelToken

log = logging.getLogger("bananachat.chat")

CHECKPOINT_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0
FIRST_EVENT_WAIT = 15.0
TITLE_LENGTH = 60
OPTION_FIELDS = ("temperature", "top_p", "top_k", "num_ctx")


class SendError(Exception):
    """A send request refused before it ran (JSON error with this status)."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request", retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after


# ----- small helpers ------------------------------------------------------------

def display_title(title, lang: str) -> str:
    return translate(lang, "chat.untitled") if chats.is_untitled(title) else title


def auto_title(text: str) -> str:
    """A title from the first message: one line, markup removed, at most TITLE_LENGTH characters."""
    words = " ".join((text or "").split()).lstrip("#>*-_`~ ").replace("`", "").replace("**", "")
    if len(words) <= TITLE_LENGTH:
        return words
    cut = words[:TITLE_LENGTH]
    if " " in cut[TITLE_LENGTH // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.;:") + "…"


def usable_personality(user, personality_id, context: AccessContext | None = None):
    """The personality (the user's own or a featured one) if the user may apply it to a chat now, else None."""
    return personality_service.usable(user, personality_id, context)


def system_prompt(model, personality_instructions: str | None) -> str:
    """The model's system prompt, then the personality's text layered below it (never above)."""
    parts = []
    if model["system_prompt"]:
        parts.append(model["system_prompt"].strip())
    if personality_instructions:
        parts.append("The user selected the following personality preferences. Apply them only where they do not "
                     "conflict with the instructions above:\n" + personality_instructions.strip())
    return "\n\n".join(part for part in parts if part)


def model_choices(context: AccessContext, lang: str = "en") -> list[dict]:
    """Text models the user may chat with, for the model picker (administrators also see, marked, the models
    waiting for review; they are never chosen automatically)."""
    unreviewed = translate(lang, "chat.model_unreviewed")
    return [{
        "name": model["ollama_name"],
        "label": model["display_name"] or model["ollama_name"],
        "description": " · ".join(part for part in (unreviewed if model["enrollment"] == "new" else "",
                                                     model["description"] or "") if part),
        "categories": [category["name"] for category in context.surface_categories(model, "chat")],
        "reasoning": bool(model["is_reasoning"]),
        "vision": bool(model["supports_vision"]),
    } for model in usable_models(context, "chat", kind="text", unreviewed=True) if not model["deprecated_at"]]


def compose(thinking: str, answer: str) -> str:
    """Stored form of an answer: reasoning first, as a ``<think>`` block (the format of existing messages)."""
    if not thinking.strip():
        return answer
    if not answer:
        return f"<think>{thinking.strip()}"
    return f"<think>{thinking.strip()}</think>\n\n{answer}"


# ----- preparing a send ----------------------------------------------------------

@dataclass
class Prepared:
    user: dict
    session: dict
    content: str
    attachments: list
    selection: inference.Selection
    options: dict
    priority: int
    lang: str
    think: bool | str | None = None  # Ollama's think (inference.think_for)
    effort: str | None = None  # the resolved reasoning effort level (limits.resolve_effort)
    notice: str = ""
    personality: str | None = None
    # What the user asked for (before any model's defaults) and the personality's
    # creativity, so a fallback model gets options built for itself.
    overrides: dict = field(default_factory=dict)
    creativity: str | None = None


def options_for(model, overrides: dict, creativity: str | None, config) -> dict:
    """Generation options for *model*: the user's values, else the personality's style, else the model's."""
    overrides = dict(overrides)
    if creativity and not overrides.get("temperature"):
        # The personality's creativity, unless the user set a temperature for this message.
        style = personality_service.style_temperature(model, creativity)
        if style is not None:
            overrides["temperature"] = style
    return inference.build_options(model, overrides, config=config)


def _field(form, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def prepare(user, session, form, files, *, lang: str) -> Prepared:
    """Validate a send request (see the module docstring for the order). Raises :class:`SendError`."""
    with limits.snapshot():  # the rate, the tokens, auto and the fallbacks read each limit setting once
        return _prepare(user, session, form, files, lang=lang)


def _prepare(user, session, form, files, *, lang: str) -> Prepared:
    config = current_app.config["BC"]
    t = lambda key, **params: translate(lang, key, **params)  # noqa: E731
    is_admin = user["role"] == "admin"

    raw = form.get("content", "")
    if not isinstance(raw, str):
        raise SendError(t("chat.error_content"))
    content = raw.strip()
    uploads = attachments.present(files)
    if len(content.encode("utf-8")) > config.chat_max_message_bytes:
        raise SendError(t("chat.error_too_long", size=config.chat_max_message_bytes // 1024), 413, "too_large")
    if not content and not uploads:
        raise SendError(t("chat.error_empty"))
    if uploads and session["is_incognito"]:
        raise SendError(t("chat.error_no_history_files"))

    decision = limits.check_rate(user, "chat")
    if not decision.allowed:
        raise SendError(t("chat.error_rate", seconds=decision.retry_after), 429, "rate_limited",
                        retry_after=decision.retry_after)
    requested = _field(form, "model") or "auto"
    budget = credits.budget(user, "chat")
    # A model that does not count toward the chat limits can still be asked for by name, and ``auto`` moves to
    # one when there is one (limits.prefer_usable). Otherwise refuse before any upload is parsed.
    # A local model chosen by name may also switch to a cloud model outside the used-up tokens
    # (limits.quota_fallback), when the administrator allows it.
    if not budget.available and not (limits.any_outside_pool() if requested == "auto" else
                                     limits.outside_pool(requested) or
                                     (limits.fallback_directions()[1] and limits.any_outside_pool())):
        raise SendError(quota_message(budget, lang), 429, "quota_exhausted", retry_after=budget.seconds_until_reset())
    if runs.session_busy(session["id"]) or (not is_admin and runs.user_busy(user["id"])):
        raise SendError(t("chat.error_busy"), 409, "busy", retry_after=5)

    try:
        stored = attachments.process(uploads, config)
    except attachments.AttachmentError as error:
        raise SendError(t(error.key, **error.params)) from None
    if stored and chats.attachment_bytes(session["id"]) + sum(item["size_bytes"] for item in stored) > \
            config.chat_max_session_attachment_bytes:
        raise SendError(t("chat.file_session_budget"))
    if not content:
        content = t("chat.default_attachment_prompt")

    vision = any(item["kind"] == "image" for item in stored) or chats.has_images(session["id"])
    context = AccessContext.load(user)
    try:
        selection = inference.select_model(context, requested, surface="chat", vision=vision)
    except inference.ModelUnavailable as error:
        key = "chat.error_model_forbidden" if error.status == 403 else (
            "chat.error_no_vision_model" if vision else "chat.error_no_model")
        raise SendError(t(key), 403 if error.status == 403 else 503, "model_unavailable") from None
    selection = limits.prefer_usable(user, selection, requested, pool="chat", candidates=lambda: (
        inference.candidates(context, "chat", vision=vision)))
    selection = limits.quota_fallback(user, selection, requested, pool="chat", candidates=lambda: (
        inference.candidates(context, "chat", vision=vision)))
    model = selection.model
    notice = ""
    if selection.reason:
        # The requested model was deprecated or retired by an administrator.
        notice = t(f"chat.notice_model_{selection.reason}", requested=selection.requested["display_name"],
                   model=model["display_name"])
    elif requested != "auto" and model["ollama_name"] != requested:
        previous = catalog.get_by_name(requested)
        notice = t("chat.notice_switched", requested=previous["display_name"] if previous else requested,
                   model=model["display_name"])
    requested_effort = _field(form, "effort")
    try:
        effort = limits.resolve_effort(user, model, requested_effort if requested_effort in limits.EFFORT_CHOICES
                                       else None)
    except limits.EffortLocked as error:
        raise SendError(t("chat.error_effort_locked", model=model["display_name"],
                          level=limits.effort_label(error.allowed, lang)), 403, "reasoning_effort_locked") from None
    think = inference.think_for(model, effort)
    admission = limits.admit(user, "chat", model)
    if not admission.allowed:
        raise send_refusal(admission.refusal, lang)
    selection.fallbacks = limits.usable_fallbacks(user, "chat", selection.fallbacks, think=think, effort=effort)

    overrides = {name: _field(form, name) for name in OPTION_FIELDS}
    personality = usable_personality(user, session["personality_id"], context)
    creativity = personality["creativity"] if personality is not None else None
    return Prepared(
        user=dict(user), session=dict(session), content=content, attachments=stored, selection=selection,
        options=options_for(model, overrides, creativity, config),
        priority=queue.priority_for(user, slow=admission.slow), lang=lang, think=think, effort=effort, notice=notice,
        personality=personality_service.prompt_text(personality) if personality else None,
        overrides=overrides, creativity=creativity)


def quota_message(budget: credits.Budget, lang: str) -> str:
    """Why the chat's token limits refuse a message, and when the tokens come back."""
    return limits.pool_refusal(budget).message(lang)


# Codes the chat page knows, for the limit refusals of services.limits.
_REFUSAL_CODES = {"insufficient_quota": "quota_exhausted", "rate_limit_exceeded": "rate_limited"}


def send_refusal(refusal: limits.Refusal, lang: str) -> SendError:
    return SendError(refusal.message(lang), refusal.status, _REFUSAL_CODES.get(refusal.code, refusal.code),
                     retry_after=refusal.retry_after)


# ----- the event channel ---------------------------------------------------------------

class Channel:
    """Events from the worker thread to the HTTP response, bounded in memory.

    Consecutive deltas are merged, so a slow reader costs at most the answer
    size. If the reader goes away the run continues and events are dropped;
    if it falls too far behind, the stream ends and the client polls instead.
    """

    MAX_EVENTS = 2000

    def __init__(self):
        self._items: deque = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._attached = True

    def send(self, event: dict) -> None:
        with self._condition:
            if self._closed or not self._attached:
                return
            last = self._items[-1] if self._items else None
            if (event.get("type") == "delta" and last is not None and last.get("type") == "delta"
                    and last["thinking"] == event["thinking"]):
                last["text"] += event["text"]
            elif len(self._items) >= self.MAX_EVENTS:
                self._items.clear()
                self._closed = True
            else:
                self._items.append(dict(event))
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def detach(self) -> None:
        """The client went away: stop buffering."""
        with self._condition:
            self._attached = False
            self._items.clear()
            self._condition.notify_all()

    def stream(self, heartbeat: float = HEARTBEAT_SECONDS):
        try:
            while True:
                with self._condition:
                    if not self._items and not self._closed:
                        self._condition.wait(heartbeat)
                    if self._items:
                        item = self._items.popleft()
                    elif self._closed or not self._attached:
                        return
                    else:
                        item = None
                if item is None:
                    yield ": ping\n\n"
                else:
                    yield "data: " + json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        finally:
            self.detach()


# ----- runs ------------------------------------------------------------------------

_local_lock = threading.Lock()
_local_runs: dict[str, "Run"] = {}


@dataclass
class Run:
    """One generation, executed in its own thread (see :func:`start`)."""

    app: object
    prepared: Prepared
    begun: runs.Begun
    cancel: CancelToken = field(default_factory=CancelToken)
    channel: Channel = field(default_factory=Channel)
    first: threading.Event = field(default_factory=threading.Event)
    rejected: SendError | None = None

    def __post_init__(self):
        self.session_id = self.prepared.session["id"]
        self.model = self.prepared.selection.model
        self._thinking: list[str] = []
        self._answer: list[str] = []
        self._last_checkpoint = time.monotonic()
        self._refusal = ""
        self._started_models = 0

    def t(self, key: str, **params) -> str:
        return translate(self.prepared.lang, key, **params)

    # lifecycle ----------------------------------------------------------------
    def start(self) -> None:
        with _local_lock:
            _local_runs[self.session_id] = self
        supervisor.register_run(self.session_id, self.begun.token, self.cancel)
        thread = threading.Thread(target=self._main, name=f"chat-{self.session_id[:8]}", daemon=True)
        try:
            thread.start()
        except BaseException:
            self._release()
            with self.app.app_context():
                runs.abort(self.session_id, self.begun)
            raise

    def _release(self) -> None:
        supervisor.unregister_run(self.session_id, self.begun.token)
        with _local_lock:
            if _local_runs.get(self.session_id) is self:
                del _local_runs[self.session_id]

    def _main(self) -> None:
        with self.app.app_context():
            try:
                self._execute()
            except Exception:  # noqa: BLE001 - the outcome must still be recorded
                log.exception("Chat generation failed")
                try:
                    self._end("failed", self.t("chat.error_failed"))
                except Exception:  # noqa: BLE001 - storage is down: the lease expires and is recovered
                    log.exception("Chat outcome could not be saved")
                    self._lost()
            finally:
                self._release()
                self.channel.close()
                self.first.set()
                db.close_thread_connection()

    def _execute(self) -> None:
        prepared = self.prepared
        request = inference.TextRequest(
            user=prepared.user, model=self.model, messages=self._messages(), options=prepared.options,
            request_type="chat_incognito" if prepared.session["is_incognito"] else "chat", priority=prepared.priority, owner_key=f"user:{prepared.user['id']}:chat",
            fallbacks=prepared.selection.fallbacks, think=prepared.think, effort=prepared.effort,
            authorize=self._authorize, prepare=self._for_model)
        try:
            for event in inference.generate(request, self.cancel):
                self._handle(event)
                self.first.set()
        except queue.QueueFull:
            if not self.first.is_set():
                runs.abort(self.session_id, self.begun)
                self.rejected = SendError(self.t("chat.error_server_busy"), 429, "busy", retry_after=10)
                return
            self._end("failed", self.t("chat.error_server_busy"))
        except queue.QueueTimeout:
            self._end("failed", self.t("chat.error_queue_timeout"))
        except Cancelled:
            if self.cancel.reason == "lease lost":
                self._lost()
            else:
                self._end("stopped", "")

    def _for_model(self, model) -> tuple[list[dict], dict]:
        """Messages and options for a fallback model: its own system prompt and defaults."""
        config = current_app.config["BC"]
        return self._messages(model), options_for(model, self.prepared.overrides, self.prepared.creativity, config)

    def _messages(self, model=None) -> list[dict]:
        config = current_app.config["BC"]
        rows = chats.context_messages(self.session_id, max_messages=config.chat_max_history_messages,
                                      max_chars=config.chat_max_context_chars)
        stored = chats.context_attachments([row["id"] for row in rows], max_chars=config.chat_max_context_chars,
                                           max_images=config.chat_max_context_images,
                                           max_image_bytes=MAX_CONTEXT_IMAGE_BYTES)
        messages = attachments.model_messages(rows, stored)
        prompt = system_prompt(model or self.model, self.prepared.personality)
        return ([{"role": "system", "content": prompt}] if prompt else []) + messages

    def _authorize(self) -> str | None:
        """Checked once admitted: the account may still chat."""
        user = users.get(self.prepared.user["id"])
        if user is None or users.is_suspended(user):
            self._refusal = self.t("chat.error_account")
        elif not (admission := limits.admit(user, "chat", self.model, take_rate=False)).allowed:
            self._refusal = admission.refusal.message(self.prepared.lang)
        return self._refusal or None

    # events -------------------------------------------------------------------
    def _handle(self, event) -> None:
        if isinstance(event, inference.Queued):
            if event.position:
                self.channel.send({"type": "queued", "position": event.position})
        elif isinstance(event, inference.Started):
            self._started_models += 1
            if self._started_models == 1:
                notice = self.prepared.notice
            else:
                notice = self.t("chat.notice_fallback", model=event.model["display_name"])
            self.model = event.model
            runs.mark_running(self.session_id, self.begun.token, self.model["id"])
            self.channel.send({"type": "start", "model": self.model["ollama_name"],
                               "display_name": self.model["display_name"], "notice": notice,
                               "via_worker": event.via_worker, "reasoning": bool(self.model["is_reasoning"])})
        elif isinstance(event, inference.Delta):
            (self._thinking if event.thinking else self._answer).append(event.text)
            self.channel.send({"type": "delta", "text": event.text, "thinking": event.thinking})
            if time.monotonic() - self._last_checkpoint >= CHECKPOINT_SECONDS:
                self._last_checkpoint = time.monotonic()
                if not runs.checkpoint(self.session_id, self.begun.token, self._content(), self.model["id"]):
                    self.cancel.cancel("lease lost")
        elif isinstance(event, inference.Finished):
            self._finished(event)

    def _content(self) -> str:
        return compose("".join(self._thinking), "".join(self._answer))

    def _finished(self, finished: inference.Finished) -> None:
        if self.cancel.reason == "lease lost":
            self._lost()
            return
        message = ""
        if finished.state == "failed":
            if finished.error and not self._refusal:
                log.warning("Chat answer failed: %s", finished.error)
            message = self._refusal or self.t("chat.error_partial" if self._content() else "chat.error_failed")
        elif finished.truncated:
            message = self.t("chat.notice_truncated")
        elif self.cancel.reason == "deadline":
            message = self.t("chat.error_deadline")
        self._end(finished.state, message, finished)

    def _end(self, state: str, message: str, finished: inference.Finished | None = None) -> None:
        content = self._content()
        outcome = runs.finish(
            self.session_id, self.begun.token, user_id=self.prepared.user["id"], content=content, state=state,
            error=message, model_id=self.model["id"] if (content or finished) else None,
            tokens_in=finished.prompt_tokens if finished else 0,
            tokens_out=finished.completion_tokens if finished else 0,
            usage_estimated=finished.usage_estimated if finished else False,
            duration_ms=finished.duration_ms + finished.wait_ms if finished else 0,
            queue_wait_ms=finished.wait_ms if finished else 0,
            charge=bool(content) or state == "completed")
        if outcome is None:
            self._lost()
            return
        self.channel.send({
            "type": "done" if state in ("completed", "stopped") else "error", "state": state, "message": message,
            "message_id": outcome.message_id, "user_message_id": self.begun.message_id,
            "tokens_in": finished.prompt_tokens if finished else 0,
            "tokens_out": finished.completion_tokens if finished else 0,
            "title": display_title(outcome.title, self.prepared.lang),
            "model": self.model["ollama_name"], "display_name": self.model["display_name"],
        })

    def _lost(self) -> None:
        self.channel.send({"type": "error", "state": "interrupted", "message": self.t("chat.error_lease"),
                           "message_id": None, "user_message_id": self.begun.message_id, "tokens_in": 0,
                           "tokens_out": 0, "title": None,
                           "model": self.model["ollama_name"], "display_name": self.model["display_name"]})


def start(prepared: Prepared) -> Run:
    """Store the message, start the run and wait for its first event. Raises :class:`SendError`."""
    t = lambda key: translate(prepared.lang, key)  # noqa: E731
    user = prepared.user
    try:
        begun = runs.begin(prepared.session, user, content=prepared.content, attachments=prepared.attachments,
                           title=auto_title(prepared.content), one_per_user=user["role"] != "admin")
    except runs.Busy:
        raise SendError(t("chat.error_busy"), 409, "busy", retry_after=5) from None
    except LookupError:
        raise SendError(t("chat.error_missing"), 404, "not_found") from None
    run = Run(current_app._get_current_object(), prepared, begun)
    run.start()
    run.first.wait(FIRST_EVENT_WAIT)
    if run.rejected is not None:
        raise run.rejected
    return run


def stop(session_id: str) -> bool:
    """Ask the chat's run to stop (any process; immediate in this one)."""
    requested = runs.request_stop(session_id)
    with _local_lock:
        run = _local_runs.get(session_id)
    if run is not None:
        run.cancel.cancel("stopped")
    return requested or run is not None


# ----- deletion and retention --------------------------------------------------------

def delete_chat(session) -> None:
    """Delete a chat as its owner: no-history chats and ``BC_DELETED_CHAT_RETENTION_DAYS=0`` erase at once."""
    stop(session["id"])
    if session["is_incognito"] or current_app.config["BC"].deleted_chat_retention_days == 0:
        chats.hard_delete(session["id"])
    else:
        chats.soft_delete(session["id"])


def delete_all_chats(user_id: str) -> int:
    """"Delete all my chats", honouring the retention setting. Returns the number of chats."""
    with _local_lock:
        local = [run for run in _local_runs.values() if run.prepared.user["id"] == user_id]
    for run in local:
        run.cancel.cancel("stopped")
    return chats.delete_all_for_user(user_id, hard=current_app.config["BC"].deleted_chat_retention_days == 0)


def _repeat(purge, cutoff: str) -> int:
    total = 0
    for _ in range(20):
        count = purge(cutoff)
        total += count
        if count < 500:
            break
    return total


@background.job("chat-recover-runs", every=30, initial_delay=10)
def recover_runs(app) -> None:
    """Save the partial answers of runs whose process stopped."""
    count = runs.recover_stale()
    if count:
        log.info("Recovered %d interrupted chat answer(s)", count)


@background.job("chat-purge-no-history", every=600, initial_delay=60)
def purge_no_history(app) -> None:
    hours = app.config["BC"].no_history_ttl_hours
    count = _repeat(chats.purge_no_history, db.now(-timedelta(hours=hours)))
    if count:
        log.info("Erased %d inactive no-history chat(s)", count)


@background.job("chat-purge-deleted", every=3600, initial_delay=120)
def purge_deleted(app) -> None:
    days = app.config["BC"].deleted_chat_retention_days
    count = _repeat(chats.purge_deleted, db.now(-timedelta(days=days)))
    count += _repeat(chats.purge_empty, db.now(-timedelta(days=7)))
    if count:
        log.info("Erased %d deleted or abandoned chat(s)", count)
