"""Image generation shared by ``POST /v1/images/generations`` and the Images page.

:class:`ImageJob` validates a request, then :meth:`ImageJob.events` runs it:

1. per-user rate limit (``BC_IMAGE_GENERATION_RPM``, administrators exempt)
   and the model's own limits (``services.limits.admit``);
2. the fixed image charge (``BC_IMAGE_TOKENS_PER_GENERATION`` tokens × the
   model's weight) is reserved against the API pool (``db.credits.reserve_image``);
3. the request waits in the shared inference queue (``queue.Slot``);
4. ComfyUI generates the image while a heartbeat keeps the reservation alive;
5. on success the reservation becomes a charge together with the metrics row.

Whatever goes wrong after step 2 - an error, a timeout, a cancelled or
abandoned request, an unexpected exception - the reservation is refunded.

Error messages are worded in the job's ``lang``: English for the API, the
reader's language on the Images page.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass

from flask import current_app

from bananachat import db
from bananachat.db import catalog, credits, tokens, users
from bananachat.i18n import translate
from bananachat.services import api_usage, background, comfyui, limits, queue
from bananachat.services.access import AccessContext, is_image_model, usable_models
from bananachat.services.completions import sanitize_backend_message
from bananachat.services.upstream import Cancelled, CancelToken

log = logging.getLogger("bananachat.images")

SIZE_PRESETS = ("512x512", "768x768", "1024x1024", "832x1216", "1216x832", "1344x768", "768x1344")
DEFAULT_SIZE = "1024x1024"
MIME_TYPES = (("image/png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
              ("image/jpeg", lambda data: data.startswith(b"\xff\xd8\xff")),
              ("image/webp", lambda data: data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


class ImageError(Exception):
    """An image request that cannot be served; ``status`` is the HTTP status."""

    def __init__(self, message: str, status: int = 400, code: str = "invalid_request", *,
                 retry_after: int | None = None, param: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.param = param


# ----- events -------------------------------------------------------------------

@dataclass
class Queued:
    position: int


@dataclass
class Admitted:
    wait_ms: int


@dataclass
class Progress:
    state: str  # pending | running (inside ComfyUI)


@dataclass
class ImageResult:
    b64_json: str
    mime_type: str
    model: object
    width: int
    height: int
    tokens: float  # the image's token cost
    wait_ms: int
    duration_ms: int
    created: int


# ----- validation ---------------------------------------------------------------

def parse_size(value, lang: str = "en") -> tuple[int, int]:
    text = DEFAULT_SIZE if value is None else value
    if not isinstance(text, str):
        raise ImageError(translate(lang, "images.error_size_type"), param="size")
    parts = text.strip().lower().split("x")
    if len(parts) != 2 or not all(part.isdigit() and len(part) <= 5 for part in parts):
        raise ImageError(translate(lang, "images.error_size_format"), param="size")
    width, height = int(parts[0]), int(parts[1])
    try:
        comfyui.check_dimensions(width, height)
    except ValueError:
        raise ImageError(translate(lang, "images.error_size_limits"), param="size", code="invalid_size") from None
    return width, height


def image_models(user, config=None) -> list:
    """Image models *user* may use (the ``api`` surface governs images everywhere)."""
    config = config or current_app.config["BC"]
    if not config.images_enabled:
        return []
    return usable_models(AccessContext.load(user), "api", kind="image", images_enabled=True)


def resolve_model(user, requested, config=None, lang: str = "en"):
    config = config or current_app.config["BC"]
    if requested is None:
        requested = "auto"
    if not isinstance(requested, str) or not requested.strip() or len(requested) > 600:
        raise ImageError(translate(lang, "images.error_model_invalid"), param="model")
    requested = requested.strip()
    context = AccessContext.load(user)
    if requested == "auto":
        options = usable_models(context, "api", kind="image", images_enabled=config.images_enabled)
        if not options:
            raise ImageError(translate(lang, "images.error_no_model"), 503, "model_unavailable", retry_after=60,
                             param="model")
        return options[0]
    model = catalog.get_by_name(requested)
    if model is None:
        raise ImageError(translate(lang, "images.error_model_missing", model=requested), 404, "model_not_found",
                         param="model")
    if not context.can_use(model, "api"):
        raise ImageError(translate(lang, "images.error_model_forbidden", model=requested), 403, "model_not_allowed",
                         param="model")
    if not is_image_model(model, config.images_enabled):
        if model["backend"] == "comfyui":
            raise ImageError(translate(lang, "images.error_model_unavailable", model=requested), 503,
                             "model_unavailable", retry_after=60, param="model")
        raise ImageError(translate(lang, "images.error_model_unsuitable", model=requested), 400, "model_not_suitable",
                         param="model")
    return model


def detect_mime(data: bytes) -> str | None:
    for mime, check in MIME_TYPES:
        if check(data):
            return mime
    return None


# ----- reservation heartbeat ----------------------------------------------------

class _Heartbeat:
    """Keep an image reservation fresh from a small thread while the request runs."""

    def __init__(self, reservation_id, interval: float):
        self.reservation_id = reservation_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if self.reservation_id is not None:
            self._thread = threading.Thread(target=self._loop, name="bananachat-image-heartbeat", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self):
        try:
            while not self._stop.wait(self.interval):
                try:
                    if not credits.touch_reservation(self.reservation_id):
                        log.warning("Image reservation %s expired; the image will still be charged",
                                    self.reservation_id)
                        return
                except Exception:  # noqa: BLE001 - the charge is still recorded at the end
                    log.warning("Refreshing an image reservation failed", exc_info=True)
                    db.close_thread_connection()
        finally:
            db.close_thread_connection()


# ----- the job --------------------------------------------------------------------

class ImageJob:
    """One image generation. Construct (validates, no side effects), then iterate :meth:`events`."""

    def __init__(self, user, *, prompt, model="auto", size=None, token_id=None, config=None, lang: str = "en"):
        self.config = config or current_app.config["BC"]
        self.lang = lang
        if not self.config.images_enabled:
            raise ImageError(self._say("images.error_disabled"), 404, "images_disabled")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageError(self._say("images.error_prompt_required"), param="prompt")
        if len(prompt) > comfyui.MAX_PROMPT_CHARS:
            raise ImageError(self._say("images.error_prompt_too_long"), param="prompt", code="prompt_too_long")
        self.user = user
        self.prompt = prompt.strip()
        self.width, self.height = parse_size(size, lang)
        self.token_id = token_id
        self.model = resolve_model(user, model, self.config, lang)
        self.cost = float(self.config.image_tokens_per_generation)
        self.counted = True
        self.cancel = CancelToken()
        self._iterator = None
        self._buffer: list = []

    def _say(self, key: str, **params) -> str:
        return translate(self.lang, key, **params)

    # public iteration ----------------------------------------------------------
    def prime(self) -> None:
        """Run the pre-flight steps now, so their errors can become plain HTTP errors."""
        if self._iterator is None:
            self._iterator = self._run()
        for event in self._iterator:
            self._buffer.append(event)
            return

    def events(self):
        while self._buffer:
            yield self._buffer.pop(0)
        if self._iterator is None:
            self._iterator = self._run()
        yield from self._iterator

    def run(self) -> ImageResult:
        """Run to completion and return the image (for JSON responses)."""
        result = None
        for event in self.events():
            if isinstance(event, ImageResult):
                result = event
        if result is None:  # pragma: no cover - _run always ends with a result or an error
            raise ImageError("Image generation failed.", 500, "server_error")
        return result

    def close(self) -> None:
        if self._iterator is not None:
            self._iterator.close()

    # implementation -------------------------------------------------------------
    def _check_rate(self) -> None:
        limit = self.config.image_generation_rpm
        if self.user["role"] == "admin" or limit <= 0:
            return
        if not users.hit(f"images:user:{self.user['id']}", limit, 60):
            raise ImageError(self._say("images.error_rate", limit=limit), 429, "rate_limit_exceeded",
                             retry_after=60)

    def _admit(self) -> None:
        """The model's own limits (lock, tokens, request rate); the pool's are checked by the reservation."""
        admission = limits.admit(self.user, "api", self.model)
        if admission.model is not None:
            self.counted = admission.model.counts_toward_pool
        if not admission.allowed and (admission.model is not None or not self.counted):
            refusal = admission.refusal
            raise ImageError(refusal.message(self.lang), refusal.status, refusal.code, retry_after=refusal.retry_after)

    def _reserve(self):
        budget = credits.budget(self.user, "api")
        weight = limits.charge_terms(self.model["id"])[0]
        charge = self.cost * weight
        cost = limits.tokens_label(self.cost, self.lang)
        try:
            reservation = credits.reserve_image(self.user, charge, ttl_seconds=self.config.image_credit_reservation_ttl,
                                                model_id=self.model["id"], token_id=self.token_id,
                                                counted=self.counted)
        except credits.InsufficientCredits as error:
            if error.weekly:
                date = budget.weekly_resets_at.strftime("%Y-%m-%d %H:%M UTC") if budget.weekly_resets_at else ""
                raise ImageError(self._say("images.error_cost_weekly", cost=cost, date=date),
                                 429, "insufficient_quota",
                                 retry_after=budget.seconds_until_reset(weekly=True)) from None
            message = self._say("images.error_cost_window_until", cost=cost,
                                date=budget.resets_at.strftime("%Y-%m-%d %H:%M UTC")) if budget.resets_at else \
                self._say("images.error_cost_window", cost=cost)
            raise ImageError(message, 429, "insufficient_quota",
                             retry_after=budget.seconds_until_reset(weekly=False)) from None
        return reservation

    def _authorize(self) -> None:
        user = users.get(self.user["id"])
        if user is None or users.is_suspended(user):
            raise ImageError(self._say("images.error_suspended"), 403, "account_suspended")
        if self.token_id is not None and not any(row["id"] == self.token_id for row in tokens.list_for(user["id"])):
            raise ImageError(self._say("images.error_token_revoked"), 401, "invalid_api_key")

    def _run(self):
        began = time.monotonic()
        self._check_rate()
        self._admit()
        reservation = self._reserve()
        finalized = False
        wait_ms = 0
        try:
            priority = queue.priority_for(self.user, api=True)
            with _Heartbeat(reservation, self.config.image_credit_reservation_heartbeat):
                try:
                    with queue.Slot(priority, owner_key=f"user:{self.user['id']}:image") as slot:
                        for position in slot.positions(timeout=self.config.comfyui_queue_timeout, cancel=self.cancel):
                            yield Queued(position)
                        wait_ms = slot.wait_ms
                        self._authorize()
                        yield Admitted(wait_ms)
                        data = yield from self._generate()
                except queue.QueueFull as error:
                    stats = queue.stats()
                    if stats["running"] + stats["waiting"] >= stats["max_depth"]:
                        raise ImageError(self._queue_text(error, "images.error_busy"), 503, "queue_full",
                                         retry_after=5) from None
                    raise ImageError(self._queue_text(error, "images.error_too_many"), 429, "too_many_requests",
                                     retry_after=10) from None
                except queue.QueueTimeout as error:
                    raise ImageError(self._queue_text(error, "images.error_queue_timeout"), 503, "queue_timeout",
                                     retry_after=10) from None
            mime = detect_mime(data)
            if mime is None:
                raise ImageError(self._say("images.error_not_image"), 502, "upstream_error")
            duration_ms = int((time.monotonic() - began) * 1000)
            api_usage.charge_image(reservation_id=reservation, user_id=self.user["id"], tokens_due=self.cost,
                                   model_id=self.model["id"], token_id=self.token_id, duration_ms=duration_ms,
                                   queue_wait_ms=wait_ms, counted=self.counted)
            finalized = True
            yield ImageResult(base64.b64encode(data).decode("ascii"), mime, self.model, self.width, self.height,
                              self.cost, wait_ms, duration_ms, int(time.time()))
        finally:
            if not finalized:
                self.cancel.cancel("abandoned")
                try:
                    credits.refund_reservation(reservation)
                except Exception:  # noqa: BLE001 - the reservation expires on its own
                    log.exception("Refunding image reservation %s failed", reservation)
                self._record_failure(began, wait_ms)

    def _generate(self):
        """Run ComfyUI, mapping its problems to :class:`ImageError`. Returns the image bytes."""
        checkpoint = self.model["backend_model_name"] or self.model["ollama_name"].removeprefix("comfyui:")
        try:
            generation = comfyui.generate(checkpoint, self.prompt, self.width, self.height, cancel=self.cancel,
                                          config=self.config)
            try:
                while True:
                    try:
                        state = next(generation)
                    except StopIteration as done:
                        data = done.value
                        break
                    yield Progress(state)
            finally:
                generation.close()
        except comfyui.ComfyUITimeout:
            raise ImageError(self._say("images.error_timeout"), 504, "timeout") from None
        except comfyui.ComfyUIError as error:
            raise ImageError(self._say("images.error_failed", reason=sanitize_backend_message(str(error))), 502,
                             "upstream_error") from None
        except Cancelled:
            raise ImageError(self._say("images.error_cancelled"), 503, "cancelled") from None
        return data

    def _queue_text(self, error: Exception, key: str) -> str:
        """The queue's own (English) explanation for the API, the translated one for the page."""
        return str(error) if self.lang == "en" else self._say(key)

    def _record_failure(self, began: float, wait_ms: int) -> None:
        try:
            api_usage.record_metric(request_type="image", user_id=self.user["id"], model_id=self.model["id"],
                                    duration_ms=int((time.monotonic() - began) * 1000), queue_wait_ms=wait_ms,
                                    status="error")
        except Exception:  # noqa: BLE001 - metrics are best effort
            log.debug("Recording a failed image request failed", exc_info=True)


@background.job("image-reservations", every=300, initial_delay=60)
def purge_reservations(app) -> None:
    """Expire reservations whose request died without a refund (a killed process): they count as used tokens."""
    count = credits.purge_expired_reservations(app.config["BC"].image_credit_reservation_ttl)
    if count:
        log.info("Released %d abandoned image reservation(s)", count)
