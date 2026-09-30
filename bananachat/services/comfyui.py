"""ComfyUI client for image generation (``BC_IMAGE_BACKEND=comfyui``).

BananaChat submits one fixed, server-built workflow (checkpoint loader, two
text encoders, empty latent, sampler, VAE decode, preview) and never accepts
workflows from users. Requests go through ``services.upstream``: no
environment proxies, no redirects, bounded response sizes and deadlines.

Generation (:func:`generate`) is a generator that yields ``"pending"`` or
``"running"`` on every poll and returns the image bytes, so callers can report
progress and notice cancellation. A prompt that times out, fails or is
cancelled is interrupted (when it is the one running) or removed from the
ComfyUI queue, and its history entry is always deleted afterwards.

Checkpoints are discovered through ``/object_info/CheckpointLoaderSimple``
and reconciled into the model catalog every ten minutes by a background job.
"""

from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import quote, urlencode

from flask import current_app

from bananachat.db import catalog
from bananachat.db import settings as site_settings
from bananachat.services import background
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError, open_request, request_json

log = logging.getLogger("bananachat.comfyui")

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MIN_DIMENSION, MAX_DIMENSION, MAX_PIXELS = 256, 2048, 4_194_304
MAX_PROMPT_CHARS = 10_000
OUTPUT_NODE = "7"
SYNC_STATE_KEY = "comfyui_sync"
SYNC_INTERVAL = 600
_PROMPT_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class ComfyUIError(RuntimeError):
    """ComfyUI failed, refused the workflow or returned something unexpected."""


class ComfyUITimeout(ComfyUIError):
    """The image was not ready before the generation deadline."""


def _config(config=None):
    return config or current_app.config["BC"]


def enabled(config=None) -> bool:
    return _config(config).images_enabled


def _get(path: str, config, *, cancel: CancelToken | None = None):
    try:
        return request_json("GET", config.comfyui_url, path, timeout=config.comfyui_timeout,
                            max_bytes=MAX_JSON_BYTES, cancel=cancel)
    except UpstreamError as error:
        raise ComfyUIError(f"ComfyUI request failed: {error}") from None


def _post(path: str, body: dict, config, *, cancel: CancelToken | None = None):
    try:
        return request_json("POST", config.comfyui_url, path, body=body, timeout=config.comfyui_timeout,
                            max_bytes=MAX_JSON_BYTES, cancel=cancel)
    except UpstreamError as error:
        raise ComfyUIError(f"ComfyUI rejected the request: {error}") from None


# ----- checkpoints ------------------------------------------------------------

def discover_checkpoints(config=None) -> list[str]:
    """Checkpoint file names offered by ComfyUI's ``CheckpointLoaderSimple`` node."""
    config = _config(config)
    data = _get("/object_info/CheckpointLoaderSimple", config)
    try:
        choices = data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except (KeyError, IndexError, TypeError):
        raise ComfyUIError("ComfyUI returned malformed checkpoint information.") from None
    if not isinstance(choices, list):
        raise ComfyUIError("ComfyUI returned malformed checkpoint information.")
    return [value.strip() for value in choices
            if isinstance(value, str) and value.strip() and "\x00" not in value and len(value) <= 512]


def sync_models(config=None) -> int:
    """Reconcile the catalog with ComfyUI's checkpoints. Returns the number found.

    Raises :class:`ComfyUIError` when ComfyUI cannot be reached (the catalog is
    left untouched then). The outcome is kept for :func:`status`.
    """
    config = _config(config)
    if not config.images_enabled:
        return 0
    try:
        checkpoints = discover_checkpoints(config)
    except ComfyUIError as error:
        site_settings.state_set(SYNC_STATE_KEY, {"at": time.time(), "ok": False, "error": str(error)[:300]})
        raise
    catalog.sync_comfyui(checkpoints)
    site_settings.state_set(SYNC_STATE_KEY, {"at": time.time(), "ok": True, "count": len(checkpoints)})
    return len(checkpoints)


def status(config=None) -> dict:
    """A summary for the administrator area: reachability, queue, devices and the last sync."""
    config = _config(config)
    result = {"enabled": config.images_enabled, "url": config.comfyui_url, "reachable": False, "error": "",
              "version": "", "devices": [], "queue_running": 0, "queue_pending": 0,
              "last_sync": site_settings.state_get(SYNC_STATE_KEY, {}) or {},
              "checkpoints": sum(1 for model in catalog.list_models(backend="comfyui")
                                 if model["backend_available"])}
    if not config.images_enabled:
        return result
    try:
        stats = _get("/system_stats", config) or {}
        queue_state = _get("/queue", config) or {}
    except ComfyUIError as error:
        result["error"] = str(error)[:300]
        return result
    system = stats.get("system") if isinstance(stats, dict) else None
    result["reachable"] = True
    if isinstance(system, dict):
        result["version"] = str(system.get("comfyui_version") or "")[:40]
    for device in (stats.get("devices") or []) if isinstance(stats, dict) else []:
        if isinstance(device, dict):
            result["devices"].append({"name": str(device.get("name") or "")[:120],
                                      "vram_total": device.get("vram_total"), "vram_free": device.get("vram_free")})
    if isinstance(queue_state, dict):
        result["queue_running"] = len(queue_state.get("queue_running") or [])
        result["queue_pending"] = len(queue_state.get("queue_pending") or [])
    return result


@background.job("comfyui-sync", every=SYNC_INTERVAL, initial_delay=20)
def _sync_job(app) -> None:
    config = app.config["BC"]
    if not config.images_enabled:
        return
    try:
        count = sync_models(config)
        log.debug("ComfyUI sync found %d checkpoints", count)
    except (ComfyUIError, OSError) as error:
        log.info("ComfyUI checkpoint sync skipped: %s", error)


# ----- workflow ---------------------------------------------------------------

def check_dimensions(width: int, height: int) -> None:
    if (isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int)
            or not isinstance(height, int) or not MIN_DIMENSION <= width <= MAX_DIMENSION
            or not MIN_DIMENSION <= height <= MAX_DIMENSION or width % 8 or height % 8
            or width * height > MAX_PIXELS):
        raise ValueError("Image sizes must be 256-2048 pixels per side, multiples of 8, at most 4 megapixels.")


def build_workflow(checkpoint: str, prompt: str, width: int, height: int, *, seed: int | None = None,
                   config=None) -> dict:
    """The only workflow this integration submits (ComfyUI API format)."""
    config = _config(config)
    if not isinstance(checkpoint, str) or not checkpoint:
        raise ValueError("A checkpoint is required.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("The prompt must have 1-10,000 characters.")
    check_dimensions(width, height)
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "seed": secrets.randbits(48) if seed is None else int(seed),
            "steps": config.comfyui_steps, "cfg": config.comfyui_cfg,
            "sampler_name": config.comfyui_sampler, "scheduler": config.comfyui_scheduler, "denoise": 1.0,
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        OUTPUT_NODE: {"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}},
    }


def _prompt_id(value) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not set(value) <= _PROMPT_ID_CHARS:
        raise ComfyUIError("ComfyUI returned an invalid prompt id.")
    return value


def _descriptor(value) -> dict:
    """Validate the output file reference before it is used in a URL."""
    if not isinstance(value, dict):
        raise ComfyUIError("ComfyUI returned an invalid image reference.")
    filename, subfolder, kind = value.get("filename"), value.get("subfolder", "") or "", value.get("type")
    if (not isinstance(filename, str) or not filename or len(filename) > 255 or filename in (".", "..")
            or any(char in filename for char in "/\\\x00")):
        raise ComfyUIError("ComfyUI returned an unsafe image file name.")
    if not isinstance(subfolder, str) or len(subfolder) > 512 or "\x00" in subfolder:
        raise ComfyUIError("ComfyUI returned an unsafe image folder.")
    parts = subfolder.replace("\\", "/").split("/") if subfolder else []
    if subfolder.startswith(("/", "\\")) or any(part in ("", ".", "..") for part in parts):
        raise ComfyUIError("ComfyUI returned an unsafe image folder.")
    if kind not in ("temp", "output"):
        raise ComfyUIError("ComfyUI returned an unexpected output type.")
    return {"filename": filename, "subfolder": subfolder, "type": kind}


def _history_output(history, prompt_id: str):
    """The image reference once the prompt finished, None while it is still pending."""
    if not isinstance(history, dict):
        raise ComfyUIError("ComfyUI returned malformed history.")
    record = history.get(prompt_id)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise ComfyUIError("ComfyUI returned malformed history.")
    state = record.get("status") if isinstance(record.get("status"), dict) else {}
    if state.get("status_str") in ("error", "failed"):
        detail = ""
        for message in state.get("messages") or []:
            if isinstance(message, list) and len(message) == 2 and message[0] == "execution_error" \
                    and isinstance(message[1], dict):
                detail = str(message[1].get("exception_message") or "").strip()[:200]
        raise ComfyUIError("ComfyUI could not generate the image" + (f": {detail}" if detail else "."))
    outputs = record.get("outputs")
    images = outputs.get(OUTPUT_NODE, {}).get("images") if isinstance(outputs, dict) and \
        isinstance(outputs.get(OUTPUT_NODE), dict) else None
    if not images:
        if state.get("completed"):
            raise ComfyUIError("ComfyUI finished without producing an image.")
        return None
    if not isinstance(images, list) or len(images) != 1:
        raise ComfyUIError("ComfyUI returned an unexpected number of images.")
    return _descriptor(images[0])


def _queue_state(prompt_id: str, config) -> str:
    """``running``, ``pending`` or ``unknown`` for *prompt_id* in ComfyUI's own queue."""
    data = _get("/queue", config)
    if not isinstance(data, dict):
        return "unknown"
    for state, key in (("running", "queue_running"), ("pending", "queue_pending")):
        for item in data.get(key) or []:
            if isinstance(item, list) and len(item) > 1 and item[1] == prompt_id:
                return state
    return "unknown"


def cancel_prompt(prompt_id: str, config=None) -> None:
    """Stop *prompt_id*: interrupt it when it is the running prompt, otherwise drop it from the queue.

    Other users' prompts are never interrupted.
    """
    config = _config(config)
    try:
        state = _queue_state(prompt_id, config)
        if state == "running":
            _post("/interrupt", {"prompt_id": prompt_id}, config)
        elif state == "pending":
            _post("/queue", {"delete": [prompt_id]}, config)
    except (ComfyUIError, OSError) as error:
        log.warning("Could not cancel ComfyUI prompt %s: %s", prompt_id, error)


def forget_prompt(prompt_id: str, config=None) -> None:
    """Delete the prompt's history entry (and with it the reference to the temporary image)."""
    config = _config(config)
    try:
        _post("/history", {"delete": [prompt_id]}, config)
    except (ComfyUIError, OSError) as error:
        log.debug("Could not delete ComfyUI history %s: %s", prompt_id, error)


def _download(descriptor: dict, config, cancel: CancelToken | None, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ComfyUITimeout("The image was not ready in time.")
    try:
        with open_request("GET", config.comfyui_url, "/view?" + urlencode(descriptor), headers={"Accept": "image/*"},
                          connect_timeout=min(config.comfyui_timeout, 10), first_byte_timeout=config.comfyui_timeout,
                          read_timeout=config.comfyui_timeout,
                          total_timeout=max(1.0, min(remaining, config.comfyui_timeout * 4)),
                          max_bytes=MAX_IMAGE_BYTES + 1, cancel=cancel) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type and not content_type.startswith(("image/", "application/octet-stream")):
                raise ComfyUIError("ComfyUI did not return an image.")
            data = response.read(MAX_IMAGE_BYTES + 1)
    except UpstreamError as error:
        raise ComfyUIError(f"Downloading the image from ComfyUI failed: {error}") from None
    if not data:
        raise ComfyUIError("ComfyUI returned an empty image.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ComfyUIError("The generated image is too large.")
    return data


def generate(checkpoint: str, prompt: str, width: int, height: int, *, cancel: CancelToken | None = None,
             config=None):
    """Generate one image. Yields ``"pending"``/``"running"`` per poll; returns the image bytes.

    Use as ``image = yield from comfyui.generate(...)``. Raises
    :class:`ComfyUIError`, :class:`ComfyUITimeout` or ``upstream.Cancelled``.
    """
    config = _config(config)
    workflow = build_workflow(checkpoint, prompt, width, height, config=config)
    deadline = time.monotonic() + config.comfyui_generation_timeout
    response = _post("/prompt", {"prompt": workflow, "client_id": "bananachat"}, config, cancel=cancel)
    if not isinstance(response, dict) or response.get("node_errors"):
        raise ComfyUIError("ComfyUI rejected the image workflow.")
    prompt_id = _prompt_id(response.get("prompt_id"))
    finished = False
    try:
        history_path = "/history/" + quote(prompt_id, safe="")
        while True:
            if cancel is not None:
                cancel.check()
            descriptor = _history_output(_get(history_path, config, cancel=cancel), prompt_id)
            if descriptor is not None:
                break
            if time.monotonic() >= deadline:
                raise ComfyUITimeout("The image was not ready in time.")
            try:
                state = _queue_state(prompt_id, config)
            except ComfyUIError:
                state = "unknown"
            yield state if state != "unknown" else "pending"
            pause = min(config.comfyui_poll_interval, max(0.0, deadline - time.monotonic()))
            if cancel is not None:
                if cancel.wait(pause):
                    raise Cancelled(cancel.reason)
            else:
                time.sleep(pause)
        image = _download(descriptor, config, cancel, deadline + config.comfyui_timeout)
        finished = True
        return image
    finally:
        if not finished:
            cancel_prompt(prompt_id, config)
        forget_prompt(prompt_id, config)
