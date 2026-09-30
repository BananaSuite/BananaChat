"""The agent's tools: JSON schemas for the model, strict argument validation, execution through the runner.

Every tool call from a model is untrusted input. :func:`validate` accepts it
only when the tool exists for this agent, the arguments are a JSON object
with exactly the documented keys, every value has the documented type (no
coercion, booleans are not numbers) and fits its bounds, and paths stay
inside ``/workspace`` after normalisation. A rejected call is answered with an
error message the model can act on; nothing is executed.

Execution never happens on the web server: each tool becomes one or a few
sandbox-runner calls. Commands run as ``sh -c`` *inside the sandbox*; paths
and patterns placed into shell commands are quoted with :func:`shlex.quote`.
Results fed back to the model are truncated to :data:`RESULT_LIMIT` characters.
"""

from __future__ import annotations

import json
import posixpath
import shlex
from dataclasses import dataclass

from bananachat.services.ollama import TOO_DEEP, nesting_too_deep

WORKSPACE = "/workspace"
RESULT_LIMIT = 8000
MAX_ARGUMENT_BYTES = 300_000
MAX_PATH = 1024
MAX_COMMAND = 16_000
MAX_WRITE_BYTES = 256 * 1024
MAX_EDIT_FILE_BYTES = 1024 * 1024
MAX_READ_LINES = 2000
LIST_MAX_ENTRIES = 400
LIST_MAX_DIRECTORIES = 40
SEARCH_MAX_LINES = 200


class ToolError(ValueError):
    """Invalid arguments or a failed operation; the message goes back to the model."""


# ----- schema ------------------------------------------------------------------------------
# Parameter spec: (type, required, bounds). Types: "string", "integer", "tasks".
#   string bounds: (min_length, max_length); integer bounds: (minimum, maximum).
SPECS: dict[str, dict] = {
    "bash": {
        "description": "Run a shell command (sh -c) in /workspace inside the Linux sandbox. Returns the exit "
                       "code, stdout and stderr (long output is truncated). Use for building, testing, installing "
                       "into the workspace and inspecting the system.",
        "params": {
            "command": ("string", True, (1, MAX_COMMAND), "The command line to run."),
            "timeout": ("integer", False, (1, 3600), "Seconds before the command is killed (capped by the "
                                                     "administrator's limit)."),
        },
    },
    "read_file": {
        "description": "Read a text file, with line numbers. Use offset/limit for long files.",
        "params": {
            "path": ("path", True, None, "File path, relative to /workspace or absolute under /workspace."),
            "offset": ("integer", False, (1, 10_000_000), "First line to read (1-based)."),
            "limit": ("integer", False, (1, MAX_READ_LINES), f"Number of lines (at most {MAX_READ_LINES})."),
        },
    },
    "write_file": {
        "description": "Create or overwrite a file with the given content (parent folders are created).",
        "params": {
            "path": ("path", True, None, "File path under /workspace."),
            "content": ("string", True, (0, MAX_WRITE_BYTES), "The complete new content of the file."),
        },
    },
    "edit_file": {
        "description": "Replace one exact occurrence of `old` with `new` in a text file. `old` must match the "
                       "file exactly (including spaces) and appear exactly once; include more context if needed.",
        "params": {
            "path": ("path", True, None, "File path under /workspace."),
            "old": ("string", True, (1, 100_000), "The exact text to replace."),
            "new": ("string", True, (0, MAX_WRITE_BYTES), "The replacement text."),
        },
    },
    "list_files": {
        "description": "List files and folders under a path.",
        "params": {
            "path": ("path", False, None, "Folder to list (default /workspace)."),
            "depth": ("integer", False, (1, 4), "How many levels to descend (1-4, default 2)."),
        },
    },
    "search": {
        "description": "Search file contents with a regular expression (grep -E), recursively.",
        "params": {
            "pattern": ("string", True, (1, 500), "Extended regular expression."),
            "path": ("path", False, None, "Folder or file to search (default /workspace)."),
        },
    },
    "finish": {
        "description": "Finish the task and report to the user. Call this once the work is done (or cannot be "
                       "done), with a clear summary of what you did, the files you changed and anything left.",
        "params": {
            "summary": ("string", True, (1, 20_000), "Summary for the user (Markdown)."),
        },
    },
    "delegate": {
        "description": "Run sub-agents in parallel on independent parts of the task. They share this sandbox "
                       "and each returns a summary. Give each precise, self-contained instructions.",
        "params": {
            "tasks": ("tasks", True, (1, 8), "List of {title, instructions} objects, one per sub-agent."),
        },
    },
}
BASE_TOOLS = ("bash", "read_file", "write_file", "edit_file", "list_files", "search", "finish")
ORCHESTRATOR_TOOLS = BASE_TOOLS + ("delegate",)


def definitions(names, *, max_subagents: int = 4) -> list[dict]:
    """Ollama/OpenAI function definitions for *names*."""
    result = []
    for name in names:
        spec = SPECS[name]
        properties, required = {}, []
        for param, (kind, needed, bounds, description) in spec["params"].items():
            if kind == "tasks":
                properties[param] = {
                    "type": "array", "description": description, "maxItems": max_subagents,
                    "items": {"type": "object", "properties": {
                        "title": {"type": "string", "description": "Short name of the sub-task."},
                        "instructions": {"type": "string", "description": "What the sub-agent must do."}},
                        "required": ["title", "instructions"]}}
            elif kind == "integer":
                properties[param] = {"type": "integer", "description": description,
                                     "minimum": bounds[0], "maximum": bounds[1]}
            else:
                properties[param] = {"type": "string", "description": description}
            if needed:
                required.append(param)
        result.append({"type": "function", "function": {
            "name": name, "description": spec["description"],
            "parameters": {"type": "object", "properties": properties, "required": required}}})
    return result


# ----- validation ----------------------------------------------------------------------

def resolve_path(value) -> str:
    """A normalised absolute path under /workspace, or :class:`ToolError`."""
    if not isinstance(value, str) or not value.strip():
        raise ToolError("path must be a non-empty string.")
    if len(value) > MAX_PATH:
        raise ToolError(f"path is longer than {MAX_PATH} characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ToolError("path contains control characters.")
    value = value.strip()
    joined = value if value.startswith("/") else posixpath.join(WORKSPACE, value)
    normal = posixpath.normpath(joined)
    if normal.startswith("//"):
        normal = "/" + normal.lstrip("/")
    if normal != WORKSPACE and not normal.startswith(WORKSPACE + "/"):
        raise ToolError("path must stay inside /workspace.")
    return normal


def _string(name, value, bounds):
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string.")
    if "\x00" in value:
        raise ToolError(f"{name} must not contain NUL characters.")
    low, high = bounds
    if len(value) < low:
        raise ToolError(f"{name} must not be empty." if low == 1 else f"{name} is too short.")
    if len(value) > high:
        raise ToolError(f"{name} is longer than {high:,} characters.")
    return value


def _integer(name, value, bounds):
    if isinstance(value, bool):
        raise ToolError(f"{name} must be an integer.")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise ToolError(f"{name} must be an integer.")
    low, high = bounds
    if not low <= value <= high:
        raise ToolError(f"{name} must be between {low} and {high}.")
    return value


def _tasks(name, value, bounds, max_items):
    if not isinstance(value, list) or not value:
        raise ToolError(f"{name} must be a non-empty list of {{title, instructions}} objects.")
    if len(value) > max_items:
        raise ToolError(f"{name} can hold at most {max_items} sub-tasks.")
    tasks = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) != {"title", "instructions"}:
            raise ToolError(f"{name}[{index}] must be an object with exactly 'title' and 'instructions'.")
        tasks.append({"title": _string(f"{name}[{index}].title", item["title"], (1, 100)).strip() or f"Sub-task {index}",
                      "instructions": _string(f"{name}[{index}].instructions", item["instructions"], (1, 8000))})
    return tasks


@dataclass
class Call:
    name: str
    arguments: dict


def validate(raw: dict, allowed, *, max_subagents: int = 4) -> Call:
    """Check a tool call ``{"name", "arguments"}`` from the model. Raises :class:`ToolError`."""
    if isinstance(raw, dict) and raw.get("error"):  # refused while decoding (see ollama._tool_calls)
        raise ToolError(str(raw["error"])[:200])
    name = raw.get("name") if isinstance(raw, dict) else None
    if not isinstance(name, str) or name not in allowed:
        shown = name[:60] if isinstance(name, str) and name else "(none)"
        raise ToolError(f"Unknown tool {shown!r}. Available tools: {', '.join(allowed)}.")
    arguments = raw.get("arguments")
    if isinstance(arguments, str):
        if len(arguments.encode("utf-8", "replace")) > MAX_ARGUMENT_BYTES:
            raise ToolError("The arguments are too large.")
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            raise ToolError("The arguments are not valid JSON.") from None
        except RecursionError:
            raise ToolError(TOO_DEEP) from None
    if arguments is None:
        arguments = {}
    if nesting_too_deep(arguments):
        raise ToolError(TOO_DEEP)
    if not isinstance(arguments, dict):
        raise ToolError("The arguments must be a JSON object.")
    if len(json.dumps(arguments, ensure_ascii=False, default=str).encode("utf-8", "replace")) > MAX_ARGUMENT_BYTES:
        raise ToolError("The arguments are too large.")
    params = SPECS[name]["params"]
    unknown = sorted(set(arguments) - set(params))
    if unknown:
        raise ToolError(f"Unknown argument(s) for {name}: {', '.join(str(key)[:40] for key in unknown)}. "
                        f"Allowed: {', '.join(params)}.")
    clean = {}
    for param, (kind, required, bounds, _) in params.items():
        if param not in arguments or arguments[param] is None:
            if required:
                raise ToolError(f"{name} needs the argument '{param}'.")
            continue
        value = arguments[param]
        if kind == "string":
            clean[param] = _string(param, value, bounds)
        elif kind == "integer":
            clean[param] = _integer(param, value, bounds)
        elif kind == "path":
            clean[param] = resolve_path(value)
        elif kind == "tasks":
            clean[param] = _tasks(param, value, bounds, max_subagents)
    return Call(name, clean)


# ----- results ------------------------------------------------------------------------------

def truncate(text: str, limit: int = RESULT_LIMIT) -> str:
    """Keep the head and the tail of long output, with a marker in between."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head - 80
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[… {omitted:,} characters omitted …]\n{text[-tail:]}"


def preview(call_name: str, arguments: dict) -> str:
    """One line describing a call, for the timeline."""
    if call_name == "bash":
        return str(arguments.get("command", ""))[:200]
    if call_name in ("read_file", "write_file", "edit_file", "list_files"):
        return str(arguments.get("path", WORKSPACE))
    if call_name == "search":
        return f"{arguments.get('pattern', '')} in {arguments.get('path', WORKSPACE)}"[:200]
    if call_name == "delegate":
        return ", ".join(task["title"] for task in arguments.get("tasks", []))[:200]
    return ""


@dataclass
class Result:
    text: str
    ok: bool = True
    finish: str | None = None


# ----- execution -----------------------------------------------------------------------------

def execute(call: Call, context) -> Result:
    """Run a validated call through *context* (see ``loop.ToolContext``). Raises runner errors."""
    handler = _HANDLERS[call.name]
    return handler(call.arguments, context)


def _bash(arguments, context) -> Result:
    timeout = min(arguments.get("timeout") or context.command_timeout, context.command_timeout)
    data = context.exec(arguments["command"], timeout)
    code = data.get("exit_code")
    parts = [f"exit code: {code}" + (" (timed out: the command was killed)" if data.get("timed_out") else "")]
    stdout, stderr = str(data.get("stdout") or ""), str(data.get("stderr") or "")
    if stdout:
        parts.append("stdout:\n" + stdout)
    if stderr:
        parts.append("stderr:\n" + stderr)
    if data.get("truncated"):
        parts.append("(the runner truncated the output)")
    ok = code == 0 and not data.get("timed_out")
    return Result(truncate("\n".join(parts)), ok)


def _read_text(context, path: str, limit_bytes: int | None = None) -> str:
    data = context.read(path)
    if "entries" in data:
        raise ToolError(f"{path} is a folder; use list_files.")
    content = context.file_bytes(data)
    if content is None:
        raise ToolError(f"{path} could not be read.")
    truncated = bool(data.get("truncated"))
    size = data.get("size") if isinstance(data.get("size"), int) else len(content)
    if limit_bytes is not None and (truncated or len(content) > limit_bytes):
        raise ToolError(f"{path} is too large to edit ({size:,} bytes); use bash instead.")
    if b"\x00" in content[:8192]:
        raise ToolError(f"{path} is a binary file ({size:,} bytes).")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not UTF-8 text.") from None
    if truncated:
        text += f"\n[the file has {size:,} bytes; only the beginning could be read - use bash (sed, head) for the rest]"
    return text


def _read_file(arguments, context) -> Result:
    path = arguments["path"]
    text = _read_text(context, path)
    lines = text.splitlines()
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", 400)
    chosen = lines[offset - 1: offset - 1 + limit]
    if not chosen:
        return Result(f"{path} has {len(lines)} line(s); nothing at line {offset}." if lines else f"{path} is empty.")
    width = len(str(offset + len(chosen)))
    body = "\n".join(f"{number:>{width}}\t{line}" for number, line in enumerate(chosen, offset))
    more = len(lines) - (offset - 1 + len(chosen))
    footer = f"\n[{more} more line(s); read with offset={offset + len(chosen)}]" if more > 0 else ""
    return Result(truncate(body) + footer)


def _write_file(arguments, context) -> Result:
    data = arguments["content"].encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError(f"content is larger than {MAX_WRITE_BYTES:,} bytes.")
    context.write(arguments["path"], data)
    return Result(f"Wrote {len(data):,} bytes to {arguments['path']}.")


def _edit_file(arguments, context) -> Result:
    path, old, new = arguments["path"], arguments["old"], arguments["new"]
    with context.workspace_lock:
        text = _read_text(context, path, MAX_EDIT_FILE_BYTES)
        count = text.count(old)
        if count == 0:
            raise ToolError(f"`old` was not found in {path}. Read the file and copy the exact text.")
        if count > 1:
            raise ToolError(f"`old` appears {count} times in {path}; include more surrounding text so it is unique.")
        updated = text.replace(old, new, 1).encode("utf-8")
        if len(updated) > MAX_EDIT_FILE_BYTES:
            raise ToolError("The edited file would be too large.")
        context.write(path, updated, locked=True)
    return Result(f"Edited {path} (1 replacement).")


def _list_files(arguments, context) -> Result:
    root = arguments.get("path", WORKSPACE)
    depth = arguments.get("depth", 2)
    first = context.read(root)
    if "entries" not in first:
        return Result(f"{root} is a file ({first.get('size', '?')} bytes).")
    lines: list[str] = []
    pending, reads, truncated = [(root, 1, first)], 0, False
    while pending and not truncated:
        folder, level, listing = pending.pop(0)
        if listing is None:
            try:
                listing = context.read(folder)
            except ToolError:
                continue
        reads += 1
        entries = listing.get("entries") if isinstance(listing.get("entries"), list) else []
        for entry in entries:
            name = str(entry.get("name") or "") if isinstance(entry, dict) else ""
            if not name or "/" in name or name in (".", ".."):
                continue
            path = posixpath.join(folder, name)
            relative = posixpath.relpath(path, root)
            if entry.get("type") == "dir":
                lines.append(relative + "/")
                if level < depth and name not in (".git", "node_modules", "__pycache__", ".venv"):
                    if reads + len(pending) < LIST_MAX_DIRECTORIES:
                        pending.append((path, level + 1, None))
                    else:
                        truncated = True
            else:
                size = entry.get("size")
                lines.append(f"{relative}  ({size} bytes)" if isinstance(size, int) else relative)
            if len(lines) >= LIST_MAX_ENTRIES:
                truncated = True
                break
    if not lines:
        return Result(f"{root} is empty.")
    footer = "\n[listing truncated]" if truncated else ""
    return Result(truncate(f"{root}:\n" + "\n".join(sorted(lines))) + footer)


def _search(arguments, context) -> Result:
    path = arguments.get("path", WORKSPACE)
    command = (f"grep -rnIE --exclude-dir=.git --exclude-dir=node_modules -e {shlex.quote(arguments['pattern'])} "
               f"-- {shlex.quote(path)} 2>&1 | head -n {SEARCH_MAX_LINES}")
    data = context.exec(command, min(60, context.command_timeout))
    output = str(data.get("stdout") or "") + str(data.get("stderr") or "")
    if data.get("timed_out"):
        return Result("The search took too long and was stopped; narrow the path.", False)
    if not output.strip():
        return Result("No matches.")
    return Result(truncate(output))


def _finish(arguments, context) -> Result:
    return Result("Task finished.", True, finish=arguments["summary"])


_HANDLERS = {
    "bash": _bash,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_files": _list_files,
    "search": _search,
    "finish": _finish,
}
