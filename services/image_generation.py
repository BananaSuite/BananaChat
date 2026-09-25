"""ComfyUI image-generation orchestration and response normalization."""

import base64
import binascii
import logging
import threading
import time

import config
import db
from services import comfyui, model_access, queue as q


MAX_IMAGE_B64_BYTES = 40 * 1024 * 1024
MAX_IMAGES = 4
MIN_DIMENSION = 256
MAX_DIMENSION = 2048
MAX_PIXELS = 4_194_304
_logger = logging.getLogger("bananachat.image_generation")


class ImageGenerationError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def resolve_model(user, requested_name):
    """Resolve and authorize one image-generation model."""
    if not comfyui.is_enabled():
        raise ImageGenerationError("Image generation is not configured", 503)

    if requested_name == "auto":
        candidates = model_access.list_accessible_models(
            user, "api", image_only=True
        )
        if not candidates:
            raise ImageGenerationError(
                "No image models are currently available to your account",
                503,
            )
        return min(candidates, key=lambda model: (model["sort_order"], model["display_name"]))

    model = db.get_model_by_ollama_name(requested_name)
    if not model:
        raise ImageGenerationError("Image model not found", 404)
    if not model_access.can_user_access_model(user, model, "api"):
        raise ImageGenerationError("Image model is not available to your account", 403)
    if model.get("backend") != "comfyui" or not model.get("is_image_generation"):
        raise ImageGenerationError("Selected model does not support image generation", 400)
    if not model.get("backend_available"):
        raise ImageGenerationError("Selected image model is currently unavailable", 503)
    return dict(model)


def validate_size(value):
    size = str(value or "1024x1024").lower().strip()
    parts = size.split("x")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ImageGenerationError("size must use WIDTHxHEIGHT format")
    width, height = (int(part) for part in parts)
    if not (MIN_DIMENSION <= width <= MAX_DIMENSION):
        raise ImageGenerationError("image width must be between 256 and 2048 pixels")
    if not (MIN_DIMENSION <= height <= MAX_DIMENSION):
        raise ImageGenerationError("image height must be between 256 and 2048 pixels")
    if width * height > MAX_PIXELS:
        raise ImageGenerationError("requested image dimensions are too large")
    if width % 8 or height % 8:
        raise ImageGenerationError("image dimensions must be divisible by 8")
    return f"{width}x{height}"


def check_rate_limit(user):
    if user.get("role") == "admin" or config.IMAGE_GENERATION_RPM <= 0:
        return True
    return db.check_rate_limit(
        f"image_generation:{user['id']}", config.IMAGE_GENERATION_RPM, 60
    )


def billable_tokens(result):
    minimum = int(config.IMAGE_CREDITS_PER_GENERATION * db.TOKENS_PER_CREDIT)
    return max(minimum, result["tokens_in"] + result["tokens_out"])


def _normalize_image(value):
    if isinstance(value, bytes):
        if not value or len(value) > comfyui.MAX_IMAGE_BYTES:
            raise ImageGenerationError("Generated image exceeds the response size limit", 502)
        raw = value
        encoded = base64.b64encode(value).decode("ascii")
    elif isinstance(value, str):
        encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
        if not encoded or len(encoded) > MAX_IMAGE_B64_BYTES:
            raise ImageGenerationError("Generated image exceeds the response size limit", 502)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageGenerationError("Image backend returned malformed image data", 502) from exc
    else:
        raise ImageGenerationError("Image backend returned an invalid image payload", 502)
    if not encoded or len(encoded) > MAX_IMAGE_B64_BYTES:
        raise ImageGenerationError("Generated image exceeds the response size limit", 502)
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        mime_type = "image/webp"
    else:
        mime_type = "application/octet-stream"
    return {"b64_json": encoded, "mime_type": mime_type}


def generate(
    model, prompt, priority, size="1024x1024", reservation_heartbeat=None, owner_key=None, authorization_check=None
):
    """Run one queued ComfyUI image request and return images plus usage."""
    if not comfyui.is_enabled():
        raise ImageGenerationError("Image generation is not configured", 503)
    if model.get("backend") != "comfyui" or not model.get("backend_available"):
        raise ImageGenerationError("The selected ComfyUI model is unavailable", 503)
    started = time.monotonic()
    wait_ms = 0
    validated_size = validate_size(size)
    heartbeat_stop = threading.Event()
    heartbeat_failed = threading.Event()
    heartbeat_thread = None
    if reservation_heartbeat is not None:
        try:
            if not reservation_heartbeat():
                raise ImageGenerationError(
                    "Image billing reservation could not be maintained", 500
                )
        except ImageGenerationError:
            raise
        except Exception:
            raise ImageGenerationError(
                "Image billing reservation could not be maintained", 500
            ) from None

        def heartbeat_loop():
            while not heartbeat_stop.wait(config.IMAGE_CREDIT_RESERVATION_HEARTBEAT):
                try:
                    if not reservation_heartbeat():
                        heartbeat_failed.set()
                        return
                except Exception as exc:
                    _logger.warning("Image reservation heartbeat failed: %s", exc)
                    heartbeat_failed.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat_loop, name="image-credit-heartbeat", daemon=True
        )
        heartbeat_thread.start()
    try:
        try:
            with q.acquire(
                priority=priority, timeout=config.COMFYUI_QUEUE_TIMEOUT, owner_key=owner_key
            ) as slot:
                wait_ms = slot.wait_ms
                if authorization_check:
                    authorization_check()
                if heartbeat_failed.is_set():
                    raise ImageGenerationError(
                        "Image billing reservation could not be maintained", 500
                    )
                width, height = (int(part) for part in validated_size.split("x"))
                image = comfyui.generate_image(
                    model["backend_model_name"], prompt, width, height
                )
                images, tokens_in, tokens_out = [image], 0, 0
                if heartbeat_failed.is_set():
                    raise ImageGenerationError(
                        "Image billing reservation could not be maintained", 500
                    )
        except q.QueueFullError as exc:
            raise ImageGenerationError(str(exc), 503) from exc
        except TimeoutError as exc:
            raise ImageGenerationError(str(exc), 504) from exc
        except comfyui.ComfyUIError as exc:
            raise ImageGenerationError("Image generation failed", 502) from exc
        except ImageGenerationError:
            raise
        except Exception:
            raise ImageGenerationError("Image generation failed", 500) from None
    finally:
        heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=1)

    if len(images) > MAX_IMAGES:
        raise ImageGenerationError("Image backend returned too many images", 502)
    return {
        "images": [_normalize_image(image) for image in images],
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "wait_ms": wait_ms,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
