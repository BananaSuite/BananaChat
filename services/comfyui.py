"""Minimal, server-controlled ComfyUI client for standard SD/SDXL workflows."""

import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import db


_logger = logging.getLogger("bananachat.comfyui")
_CORE_NODES = (
    "CheckpointLoaderSimple",
    "CLIPTextEncode",
    "EmptyLatentImage",
    "KSampler",
    "VAEDecode",
    "PreviewImage",
)
_MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024


class ComfyUIError(RuntimeError):
    pass


class ComfyUITimeoutError(TimeoutError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirectHandler())


def is_enabled():
    return config.IMAGE_BACKEND == "comfyui"


def _require_enabled():
    if not is_enabled():
        raise ComfyUIError("Image generation is disabled")


def _base_url():
    parsed = urllib.parse.urlsplit(config.COMFYUI_URL)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ComfyUIError("BC_COMFYUI_URL must be an HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ComfyUIError("BC_COMFYUI_URL contains unsupported URL components")
    return config.COMFYUI_URL.rstrip("/")


def _read_bounded(response, limit):
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ComfyUIError("ComfyUI response exceeded the size limit")
    return payload


def _request(path, *, body=None, timeout=None, max_bytes=_MAX_JSON_BYTES):
    _require_enabled()
    url = _base_url() + path
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(
            request, timeout=timeout or config.COMFYUI_TIMEOUT
        ) as response:
            return _read_bounded(response, max_bytes), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise ComfyUIError(f"ComfyUI returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ComfyUIError(f"Could not connect to ComfyUI: {exc}") from exc


def _get_json(path, timeout=None):
    payload, _ = _request(path, timeout=timeout)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComfyUIError("ComfyUI returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise ComfyUIError("ComfyUI returned an invalid JSON object")
    return value


def _post_json(path, body, timeout=None):
    payload, _ = _request(path, body=body, timeout=timeout)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComfyUIError("ComfyUI returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise ComfyUIError("ComfyUI returned an invalid JSON object")
    return value


def _get_bytes(path, timeout=None):
    payload, content_type = _request(
        path, timeout=timeout, max_bytes=MAX_IMAGE_BYTES
    )
    if content_type and not content_type.lower().startswith("image/"):
        raise ComfyUIError("ComfyUI view response was not an image")
    if not payload:
        raise ComfyUIError("ComfyUI returned an empty image")
    return payload


def _node_entry(data, node_name):
    entry = data.get(node_name)
    if not isinstance(entry, dict):
        raise ComfyUIError(f"ComfyUI core node '{node_name}' is unavailable")
    return entry


def discover_checkpoints():
    """Return checkpoints only after all required built-in nodes are verified."""
    _require_enabled()
    loader_data = _get_json("/object_info/CheckpointLoaderSimple")
    node_data = dict(loader_data)
    for node_name in _CORE_NODES:
        if node_name not in node_data:
            node_data.update(
                _get_json("/object_info/" + urllib.parse.quote(node_name, safe=""))
            )
        _node_entry(node_data, node_name)

    loader = _node_entry(node_data, "CheckpointLoaderSimple")
    try:
        choices = loader["input"]["required"]["ckpt_name"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ComfyUIError("ComfyUI checkpoint metadata is malformed") from exc
    if not isinstance(choices, list):
        raise ComfyUIError("ComfyUI checkpoint metadata is malformed")
    checkpoints = []
    for value in choices:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ComfyUIError("ComfyUI returned an invalid checkpoint name")
        checkpoints.append(value.strip())
    return checkpoints


def sync_models():
    """Discover and atomically reconcile ComfyUI models when enabled."""
    if not is_enabled():
        return []
    checkpoints = discover_checkpoints()
    models = db.sync_comfyui_models(checkpoints)
    _logger.debug("Model sync complete: %d checkpoints from ComfyUI", len(checkpoints))
    return models


def build_workflow(checkpoint, prompt, width, height, seed=None):
    """Build the only workflow accepted by this integration."""
    if not isinstance(checkpoint, str) or not checkpoint:
        raise ComfyUIError("Invalid ComfyUI checkpoint")
    if not isinstance(prompt, str) or not prompt or len(prompt) > 10_000:
        raise ComfyUIError("Image prompt is required")
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or not (256 <= width <= 2048)
        or not (256 <= height <= 2048)
        or width * height > 4_194_304
        or width % 8
        or height % 8
    ):
        raise ComfyUIError("Invalid ComfyUI image dimensions")
    if seed is None:
        seed = secrets.randbits(64)
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "", "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": config.COMFYUI_STEPS,
                "cfg": config.COMFYUI_CFG,
                "sampler_name": config.COMFYUI_SAMPLER,
                "scheduler": config.COMFYUI_SCHEDULER,
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["6", 0]},
        },
    }


def _validate_prompt_id(value):
    if not isinstance(value, str) or not (1 <= len(value) <= 128):
        raise ComfyUIError("ComfyUI returned an invalid prompt id")
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in value):
        raise ComfyUIError("ComfyUI returned an invalid prompt id")
    return value


def _validate_output_descriptor(value):
    if not isinstance(value, dict):
        raise ComfyUIError("ComfyUI returned an invalid image descriptor")
    filename = value.get("filename")
    subfolder = value.get("subfolder", "")
    output_type = value.get("type")
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or filename in (".", "..")
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise ComfyUIError("ComfyUI returned an unsafe image filename")
    if not isinstance(subfolder, str) or len(subfolder) > 512 or "\x00" in subfolder:
        raise ComfyUIError("ComfyUI returned an unsafe image subfolder")
    normalized = subfolder.replace("\\", "/")
    if normalized.startswith("/") or any(
        part in ("", ".", "..") for part in normalized.split("/") if normalized
    ):
        raise ComfyUIError("ComfyUI returned an unsafe image subfolder")
    if output_type != "temp":
        raise ComfyUIError("ComfyUI returned an unexpected image output type")
    return {"filename": filename, "subfolder": subfolder, "type": output_type}


def _history_output(history, prompt_id):
    record = history.get(prompt_id)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise ComfyUIError("ComfyUI returned malformed prompt history")
    status = record.get("status") or {}
    if isinstance(status, dict) and status.get("status_str") in ("error", "failed"):
        raise ComfyUIError("ComfyUI reported that image generation failed")
    outputs = record.get("outputs")
    if not isinstance(outputs, dict):
        if isinstance(status, dict) and status.get("completed"):
            raise ComfyUIError("ComfyUI completed without an image")
        return None
    preview = outputs.get("7")
    if not isinstance(preview, dict):
        raise ComfyUIError("ComfyUI did not return the server-controlled output node")
    images = preview.get("images")
    if not isinstance(images, list) or len(images) != 1:
        raise ComfyUIError("ComfyUI returned an unexpected number of images")
    return _validate_output_descriptor(images[0])


def _delete_history(prompt_id):
    try:
        _post_json("/history", {"delete": [prompt_id]}, timeout=config.COMFYUI_TIMEOUT)
    except Exception as exc:
        _logger.debug("Could not delete ComfyUI history %s: %s", prompt_id, exc)


def _cancel_prompt(prompt_id):
    """Cancel only this prompt, falling back to deleting it if still queued."""
    try:
        _post_json(
            "/api/jobs/" + urllib.parse.quote(prompt_id, safe="") + "/cancel",
            {},
            timeout=config.COMFYUI_TIMEOUT,
        )
        return
    except Exception as exc:
        _logger.debug(
            "Targeted ComfyUI job cancellation unavailable for %s: %s",
            prompt_id,
            exc,
        )
    try:
        _post_json("/queue", {"delete": [prompt_id]}, timeout=config.COMFYUI_TIMEOUT)
    except Exception as exc:
        _logger.debug("Could not remove queued ComfyUI prompt %s: %s", prompt_id, exc)


def generate_image(checkpoint, prompt, width, height):
    """Queue one fixed workflow, poll its history, and return bounded raw bytes."""
    _require_enabled()
    workflow = build_workflow(checkpoint, prompt, width, height)
    prompt_id = None
    succeeded = False
    try:
        response = _post_json(
            "/prompt", {"prompt": workflow}, timeout=config.COMFYUI_TIMEOUT
        )
        if response.get("node_errors"):
            raise ComfyUIError("ComfyUI rejected the image workflow")
        prompt_id = _validate_prompt_id(response.get("prompt_id"))
        deadline = time.monotonic() + config.COMFYUI_GENERATION_TIMEOUT
        descriptor = None
        history_path = "/history/" + urllib.parse.quote(prompt_id, safe="")
        while time.monotonic() < deadline:
            history = _get_json(history_path, timeout=config.COMFYUI_TIMEOUT)
            descriptor = _history_output(history, prompt_id)
            if descriptor:
                break
            time.sleep(config.COMFYUI_POLL_INTERVAL)
        if descriptor is None:
            raise ComfyUITimeoutError("ComfyUI image generation timed out")
        query = urllib.parse.urlencode(descriptor)
        image = _get_bytes("/view?" + query, timeout=config.COMFYUI_TIMEOUT)
        succeeded = True
        return image
    finally:
        if prompt_id is not None:
            if not succeeded:
                _cancel_prompt(prompt_id)
            _delete_history(prompt_id)
