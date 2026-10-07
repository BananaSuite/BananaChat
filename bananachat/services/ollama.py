"""Ollama client and catalog synchronisation.

The base URL is ``BC_OLLAMA_URL`` (or the fallback while the primary is
down, see ``services.health``). ``BC_OLLAMA_API_KEY`` is sent as a bearer
token, which is how a web server authenticates to a BananaChat compute node.
Each server gets only its own token: the fallback receives
``BC_INFERENCE_FALLBACK_API_KEY`` (or nothing), never the compute token.
Model downloads and deletions always go to the primary server.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from flask import current_app

from bananachat.db import settings as site_settings
from bananachat.services import health
from bananachat.services.upstream import CancelToken, UpstreamError, open_request, request_json

log = logging.getLogger("bananachat.ollama")
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/\-]{0,299}$")


def _config(config=None):
    return config or current_app.config["BC"]


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


def endpoint(config=None, *, primary: bool = False) -> tuple[str, dict]:
    """The server to use now and the headers that belong to that server.

    ``primary`` ignores the outage fallback (model management targets the
    configured compute server only).
    """
    config = _config(config)
    if not primary and health.using_fallback(config):
        return config.inference_fallback_url, _bearer(config.inference_fallback_api_key)
    return config.ollama_url, _bearer(config.ollama_api_key)


# ``base_url()`` followed by ``_headers()`` must describe the same server even
# when the health state flips in between, or a token could reach the wrong
# host. ``base_url()`` remembers its choice for the calling thread and the
# next ``_headers()`` call with the same configuration uses it.
_chosen = threading.local()


def base_url(config=None) -> str:
    config = _config(config)
    url, headers = endpoint(config)
    _chosen.value = (id(config), url, headers)
    return url


def _headers(config=None) -> dict:
    config = _config(config)
    chosen, _chosen.value = getattr(_chosen, "value", None), None
    if chosen is not None and chosen[0] == id(config):
        return dict(chosen[2])
    return endpoint(config)[1]


def describe_server(config=None) -> str:
    """The primary server's address for administrators (no credentials, path or query)."""
    parts = urlsplit(_config(config).ollama_url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}{f':{parts.port}' if parts.port else ''}" if host else ""


# ----- metadata ---------------------------------------------------------------

def version(config=None, timeout: float = 5, *, primary: bool = False) -> str:
    url, headers = endpoint(config, primary=primary)
    data = request_json("GET", url, "/api/version", headers=headers, timeout=timeout)
    if not isinstance(data, dict) or not isinstance(data.get("version"), str) or not data["version"]:
        raise UpstreamError("The model server returned invalid version information.")
    return data["version"][:100]


def list_tags(config=None, *, timeout: float = 15, primary: bool = False) -> list[dict]:
    url, headers = endpoint(config, primary=primary)
    data = request_json("GET", url, "/api/tags", headers=headers, timeout=timeout)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        # An unsuccessful inventory must never withdraw existing models.
        raise UpstreamError("The model server returned an invalid model list.")
    models = data["models"]
    if len(models) > 5000:
        raise UpstreamError("The model server returned too many models.")
    # An incomplete inventory must not make installed models disappear.
    # Duplicate valid records are harmless; malformed records fail the listing.
    seen: set[str] = set()
    result = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("name"), str):
            raise UpstreamError("The model server returned an invalid model record.")
        name = model["name"].strip()
        if not MODEL_NAME_RE.fullmatch(name):
            raise UpstreamError("The model server returned an invalid model name.")
        if name in seen:
            continue
        seen.add(name)
        result.append({**model, "name": name})
    return result


def list_running(config=None) -> list[dict]:
    url, headers = endpoint(config)
    data = request_json("GET", url, "/api/ps", headers=headers, timeout=8)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise UpstreamError("The model server returned an invalid running-model list.")
    return [model for model in data["models"] if isinstance(model, dict)][:5000]


def show(name: str, config=None, timeout: float = 15, *, primary: bool = False) -> dict:
    """Model details from ``/api/show`` (``capabilities`` lists e.g. ``completion``, ``tools``, ``vision``)."""
    if primary:
        url, headers = endpoint(config, primary=True)
    else:
        url = base_url(config)
        headers = _headers(config)
    data = request_json("POST", url, "/api/show", body={"model": name, "name": name}, headers=headers,
                        timeout=timeout, max_bytes=8 * 1024 * 1024)
    return data if isinstance(data, dict) else {}


def probe(name: str, config=None, *, prompt: str = "Reply with the single word OK.", timeout: float = 180) -> None:
    """Ask *name* for a tiny answer (the health re-check of a failing model). Raises UpstreamError on failure."""
    config = _config(config)
    url, headers = endpoint(config)
    data = request_json("POST", url, "/api/generate", body={
        "model": name, "prompt": prompt, "stream": False, "options": {"num_predict": 8},
        "keep_alive": config.keep_alive}, headers=headers, timeout=timeout, max_bytes=1024 * 1024)
    if not isinstance(data, dict) or data.get("error"):
        raise UpstreamError(str((data or {}).get("error") or "The model returned no answer.")[:500]
                            if isinstance(data, dict) else "The model returned no answer.")


def capabilities(name: str, config=None) -> list[str]:
    """The capabilities Ollama reports for *name* (empty for servers too old to report them)."""
    values = show(name, config).get("capabilities")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and len(value) <= 40][:20]


def describe(tag: dict) -> str:
    details = tag.get("details") if isinstance(tag.get("details"), dict) else {}
    parts = []
    for key in ("parameter_size", "quantization_level"):
        if isinstance(details.get(key), str) and details[key]:
            parts.append(details[key][:40])
    size = tag.get("size")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and 0 < size <= 10 ** 15:
        parts.append(f"{size / 1024 ** 3:.1f} GB")
    return " · ".join(parts)


def sync_catalog(config=None, *, source: str = "background") -> int:
    """Refresh which Ollama models exist on the primary server (see ``services.model_lifecycle.sync``).

    Returns the number found; raises UpstreamError/OSError when the server cannot be listed.
    """
    from bananachat.services import model_lifecycle

    return model_lifecycle.sync(config, source=source)["count"]


SIZES_KEY = "ollama_model_sizes"
MAX_REMEMBERED_SIZES = 2000


def remember_sizes(tags: list[dict]) -> None:
    """Keep each model's download size, so a missing model's size is known later (after a restore)."""
    sizes = {tag["name"]: tag["size"] for tag in tags
             if isinstance(tag.get("size"), int) and not isinstance(tag.get("size"), bool) and tag["size"] > 0}
    if not sizes:
        return
    known = known_sizes()
    merged = {name: size for name, size in known.items() if name not in sizes}
    merged.update(sizes)
    merged = dict(list(merged.items())[-MAX_REMEMBERED_SIZES:])
    if merged != known:
        site_settings.state_set(SIZES_KEY, merged)


def known_sizes() -> dict[str, int]:
    value = site_settings.state_get(SIZES_KEY, {}) or {}
    if not isinstance(value, dict):
        return {}
    return {name: size for name, size in value.items()
            if isinstance(name, str) and isinstance(size, int) and not isinstance(size, bool) and size > 0}


def installed_names(config=None, *, timeout: float = 15, primary: bool = False) -> set[str]:
    return {tag["name"] for tag in list_tags(config, timeout=timeout, primary=primary)}


# ----- inference ------------------------------------------------------------

@dataclass
class Chunk:
    """One piece of a streamed answer."""
    content: str = ""
    thinking: str = ""
    done: bool = False
    finish_reason: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    extra: dict = field(default_factory=dict)
    # Tool calls requested by the model (only when ``tools`` were sent), as
    # ``{"name": str, "arguments": dict | str}``; validated by the caller.
    tool_calls: list = field(default_factory=list)


def chat_stream(model: str, messages: list[dict], *, options: dict | None = None, keep_alive=None,
                think: bool | None = None, cancel: CancelToken | None = None, config=None,
                first_token_timeout: float | None = None, read_timeout: float | None = None,
                total_timeout: float | None = None, max_bytes: int | None = None, tools: list | None = None):
    """Stream ``/api/chat``. Yields :class:`Chunk` objects; the last has ``done=True``.

    With *tools* (Ollama function definitions) the model may answer with tool
    calls, reported in ``Chunk.tool_calls``; without them nothing changes.
    """
    config = _config(config)
    body = {"model": model, "messages": messages, "stream": True,
            "keep_alive": config.keep_alive if keep_alive is None else keep_alive}
    if options:
        body["options"] = options
    if think is not None:
        body["think"] = think
    if tools:
        body["tools"] = tools
    # NDJSON framing costs roughly 150 bytes per token on top of the text itself.
    budget = max_bytes or (config.chat_max_response_bytes * 8 + 4 * 1024 * 1024)
    with open_request("POST", base_url(config), "/api/chat", body=body, headers=_headers(config),
                      first_byte_timeout=first_token_timeout or config.first_token_timeout,
                      read_timeout=first_token_timeout or config.first_token_timeout,
                      total_timeout=total_timeout or config.generation_timeout,
                      max_bytes=budget, cancel=cancel) as response:
        first = True
        for record in (_tool_records(response) if tools else response.iter_json_lines(strict=True)):
            if not isinstance(record, dict):
                raise UpstreamError("The model server returned an invalid stream record.")
            if record.get("error"):
                raise UpstreamError(str(record["error"])[:500])
            if first:
                response.set_read_timeout(read_timeout or config.inference_read_timeout)
                first = False
            message = record.get("message", {})
            if not isinstance(message, dict) or not isinstance(record.get("done", False), bool):
                raise UpstreamError("The model server returned an invalid message record.")
            content, thinking = message.get("content"), message.get("thinking")
            content = "" if content is None else content
            thinking = "" if thinking is None else thinking
            if not isinstance(content, str) or not isinstance(thinking, str):
                raise UpstreamError("The model server returned an invalid message delta.")
            chunk = Chunk(content=content, thinking=thinking)
            if tools:
                chunk.tool_calls = _tool_calls(message.get("tool_calls"))
            if record.get("done"):
                chunk.done = True
                chunk.finish_reason = str(record.get("done_reason") or "stop")
                chunk.prompt_tokens = _count(record.get("prompt_eval_count"))
                chunk.completion_tokens = _count(record.get("eval_count"))
                yield chunk
                return
            if chunk.content or chunk.thinking or chunk.tool_calls:
                yield chunk
    raise UpstreamError("The model stopped before finishing its answer.")


MAX_TOOL_CALLS_PER_RECORD = 16
# Tool-call arguments are model output: nesting deeper than this (or more values than MAX_ARGUMENT_NODES)
# is refused as a tool call, before anything recursive (validation, storage, the next request) sees it.
MAX_ARGUMENT_DEPTH = 32
MAX_ARGUMENT_NODES = 20_000
TOO_DEEP = "The arguments are nested too deeply."
_TOO_DEEP_MARKER = "bananachat_tool_call_error"


def nesting_too_deep(value, max_depth: int = MAX_ARGUMENT_DEPTH, max_nodes: int = MAX_ARGUMENT_NODES) -> bool:
    """True when a decoded JSON value nests deeper than *max_depth* or holds more than *max_nodes* values."""
    stack, nodes = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            return True
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return False


def _tool_records(response):
    """NDJSON records of an answer with tools. A record nested too deeply for the JSON parser (only tool-call
    arguments can nest) becomes a refused tool call instead of an exception that would end the task."""
    while True:
        line = response.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            raise UpstreamError("The backend returned invalid stream JSON.") from None
        except RecursionError:
            yield {"message": {"tool_calls": [{_TOO_DEEP_MARKER: TOO_DEEP}]}}


def _tool_calls(value) -> list[dict]:
    """Normalise ``message.tool_calls`` to ``[{"name", "arguments"}]``; malformed entries are kept as errors.

    An entry whose arguments nest too deeply keeps its name, gets empty arguments and an ``error`` that the
    caller reports back to the model.
    """
    if not isinstance(value, list):
        return []
    calls = []
    for item in value[:MAX_TOOL_CALLS_PER_RECORD]:
        if isinstance(item, dict) and _TOO_DEEP_MARKER in item:
            calls.append({"name": "", "arguments": {}, "error": TOO_DEEP})
            continue
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict):
            calls.append({"name": "", "arguments": {}})
            continue
        name = function.get("name")
        arguments = function.get("arguments")
        call = {"name": name if isinstance(name, str) else "",
                "arguments": arguments if isinstance(arguments, (dict, str)) else {}}
        if isinstance(call["arguments"], dict) and nesting_too_deep(call["arguments"]):
            call.update(arguments={}, error=TOO_DEEP)
        calls.append(call)
    return calls


def _count(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000:
        return value
    raise UpstreamError("The model server returned invalid token usage.")


# ----- model management -----------------------------------------------------

def pull(name: str, *, cancel: CancelToken | None = None, config=None):
    """Download a model; yields progress dicts from Ollama (``status``, ``completed``, ``total``)."""
    if not MODEL_NAME_RE.fullmatch((name or "").strip()) or ".." in name or "//" in name:
        from bananachat.services.upstream import UpstreamError as _UpstreamError

        raise _UpstreamError(f"Invalid model name: {name[:120]}")
    url, headers = endpoint(config, primary=True)
    with open_request("POST", url, "/api/pull", body={"model": name, "name": name, "stream": True},
                      headers=headers, first_byte_timeout=120, read_timeout=300,
                      total_timeout=24 * 3600, max_bytes=512 * 1024 * 1024, cancel=cancel) as response:
        for record in response.iter_json_lines():
            if not isinstance(record, dict):
                continue
            if record.get("error"):
                raise UpstreamError(str(record["error"])[:500])
            yield record


def delete(name: str, config=None) -> bool:
    """Remove a model from the Ollama server. Returns False when it did not exist."""
    url, headers = endpoint(config, primary=True)
    try:
        with open_request("DELETE", url, "/api/delete", body={"model": name, "name": name},
                          headers=headers, total_timeout=60):
            return True
    except UpstreamError as error:
        if error.status == 404:
            return False
        raise


def unload(name: str, config=None) -> None:
    url, headers = endpoint(config)
    request_json("POST", url, "/api/generate", body={"model": name, "keep_alive": 0, "stream": False},
                 headers=headers, timeout=30)


def unload_all(config=None) -> int:
    count = 0
    for model in list_running(config):
        name = model.get("name") or model.get("model")
        if isinstance(name, str):
            unload(name, config)
            count += 1
    return count
