"""Git repositories in a task's sandbox: the pending import, the scripts run *inside* the sandbox, the record.

The web server downloads a repository archive (:mod:`gitfetch`) and hands it
to the sandbox runner, which validates and extracts it inside the container.
Everything Git does happens in the sandbox, through these fixed shell scripts
(values are validated and quoted with :func:`shlex.quote`); the web server
never runs ``git`` or anything from the repository.

* :func:`import_command` moves the extracted tree out of its single top-level
  folder into ``/workspace/<repo>``, drops any ``.git`` the archive carried and,
  when the image has ``git``, creates a repository with one commit
  ("Imported owner/repo@ref") so the agent's changes can be diffed later.
* :func:`export_command` computes ``git diff --binary <initial commit>`` of the
  working tree (untracked files included, ignored ones not) **without touching
  the agent's repository**: it uses a fresh Git directory in a scratch folder
  that borrows the repository's objects read-only (``objects/info/alternates``),
  a temporary index, and no global or system configuration, so nothing the
  agent wrote into ``.git/config``, ``~/.gitconfig``, hooks or attributes can
  make Git run a program. The patch is split into 1 MB parts that the web server
  reads with the runner's file API and checks against the size and SHA-256 the
  script reported.

The import is recorded as a task step (``agents.notice_imported``) holding the
repository's folder and initial commit; :func:`parse_record` validates it again
whenever it is read.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from bananachat.services.agents import gitfetch, uploads

IMPORT_FILE = "import.json"
ARCHIVE_FILE = "source.tar.gz"
WORKSPACE = "/workspace"
STAGING_PREFIX = "/workspace/.bananachat-import-"
EXPORT_PREFIX = "/workspace/.bananachat-export-"
PART_BYTES = 1024 * 1024
IMPORT_TIMEOUT = 300
EXPORT_TIMEOUT = 120
RECORD_KEYS = ("agents.notice_imported", "agents.notice_imported_nogit")
GIT_NAME = "BananaChat"
GIT_EMAIL = "agent@localhost"

_SCRATCH_RE = re.compile(r"/workspace/\.bananachat-(?:import|export)-[0-9a-f]{16}")
_DIR_RE = re.compile(r"/workspace/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_IMPORT_LINE = re.compile(r"BCIMPORT (?:ok (git) ([0-9a-f]{40}(?:[0-9a-f]{24})?)|ok (nogit)|error ([a-z]{1,20}))")
_EXPORT_LINE = re.compile(r"BCEXPORT (?:ok (\d{1,12}) ([0-9a-f]{64})|error ([a-z]{1,20})(?: (\d{1,12}))?)")


# ----- the pending import (stored with the task's uploads until its first run) --------------------

def _folder(task_id: str, config=None) -> Path:
    return uploads._folder(task_id, config)


def store_request(task_id: str, source: gitfetch.Source, config=None) -> None:
    folder = _folder(task_id, config)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / IMPORT_FILE
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(source.to_dict(), handle)


def pending_request(task_id: str, config=None) -> gitfetch.Source | None:
    """The import waiting for the task's first run (validated again), or None."""
    try:
        data = json.loads((_folder(task_id, config) / IMPORT_FILE).read_text(encoding="utf-8"))
        return gitfetch.Source.from_dict(data)
    except (OSError, ValueError):
        return None


def archive_path(task_id: str, config=None) -> Path:
    return _folder(task_id, config) / ARCHIVE_FILE


def clear_request(task_id: str, config=None) -> None:
    """Forget the pending import (and a partial download); the folder goes too once it is empty."""
    try:
        folder = _folder(task_id, config)
    except ValueError:
        return
    for name in (IMPORT_FILE, ARCHIVE_FILE):
        try:
            (folder / name).unlink()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass  # missing, or uploads are still waiting in it


# ----- scripts run inside the sandbox ---------------------------------------------------------------

# Git without any configuration the agent (or the repository) could have written: no system or
# global files (HOME is the workspace), no pager, no prompts, no optional locks.
_GIT_ENV = ("export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 GIT_OPTIONAL_LOCKS=0 "
            "GIT_PAGER=cat GIT_ADVICE=0 LC_ALL=C; ")
_GIT = ("git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null "
        "-c core.excludesFile=/dev/null -c commit.gpgSign=false -c init.defaultBranch=main "
        f"-c user.name={GIT_NAME} -c user.email={GIT_EMAIL}")

_IMPORT_SCRIPT = r"""
say() { echo "BCIMPORT $*"; }
fail() { rm -rf -- "$staging"; say error "$1"; exit 0; }
[ -d "$staging" ] && [ ! -L "$staging" ] || fail missing
if [ -e "$target" ] || [ -L "$target" ]; then fail exists; fi
count=0; only=
for entry in "$staging"/* "$staging"/.[!.]* "$staging"/..?*; do
  if [ -e "$entry" ] || [ -L "$entry" ]; then count=$((count + 1)); only=$entry; fi
done
if [ "$count" -eq 1 ] && [ -d "$only" ] && [ ! -L "$only" ]; then
  mv -- "$only" "$target" || fail move
  rmdir -- "$staging" 2>/dev/null || rm -rf -- "$staging"
else
  mv -- "$staging" "$target" || fail move
fi
rm -rf -- "$target/.git"
command -v git >/dev/null 2>&1 || { say ok nogit; exit 0; }
cd -- "$target" || fail move
GIT init -q . >/dev/null 2>&1 || { say error git; exit 0; }
GIT add -A -f . >/dev/null 2>&1 || { rm -rf .git; say error git; exit 0; }
GIT commit -q --no-verify --allow-empty -m "$message" >/dev/null 2>&1 || { rm -rf .git; say error git; exit 0; }
head=$(GIT rev-parse --verify HEAD 2>/dev/null) || { say error git; exit 0; }
say ok git "$head"
"""

_EXPORT_SCRIPT = r"""
say() { echo "BCEXPORT $*"; }
finish() { rm -rf -- "$out"; say "$@"; exit 0; }
command -v git >/dev/null 2>&1 || { say error nogit; exit 0; }
[ -d "$repo" ] && [ ! -L "$repo" ] && [ -d "$repo/.git/objects" ] && [ ! -L "$repo/.git" ] || { say error norepo; exit 0; }
mkdir -m 700 -- "$out" || { say error scratch; exit 0; }
cd -- "$repo" || finish error norepo
export GIT_DIR="$out/git" GIT_WORK_TREE="$repo" GIT_INDEX_FILE="$out/index"
GIT init -q >/dev/null 2>&1 || finish error scratch
printf '%s\n' "$repo/.git/objects" > "$GIT_DIR/objects/info/alternates" || finish error scratch
GIT cat-file -e "$base^{commit}" 2>/dev/null || finish error nobase
GIT read-tree "$base" 2>/dev/null || finish error nobase
GIT add -A >/dev/null 2>&1 || finish error add
GIT diff --cached --binary --no-color --no-ext-diff --no-textconv "$base" > "$out/changes.patch" 2>/dev/null \
  || finish error diff
size=$(wc -c < "$out/changes.patch") || finish error diff
size=$((size + 0))
[ "$size" -le "$max" ] || finish error toolarge "$size"
sum=$(sha256sum < "$out/changes.patch") || finish error diff
sum=${sum%% *}
if [ "$size" -gt 0 ]; then
  (cd -- "$out" && split -b PARTBYTES -d -a 3 changes.patch part-) || finish error split
fi
rm -rf -- "$out/git" "$out/index" "$out/changes.patch"
say ok "$size" "$sum"
"""


def _script(body: str, **values: str) -> str:
    assignments = "".join(f"{name}={shlex.quote(value)}; " for name, value in values.items())
    return "set -u; " + _GIT_ENV + assignments + body.replace("GIT ", _GIT + " ")


def import_command(staging: str, target: str, message: str) -> str:
    if not _SCRATCH_RE.fullmatch(staging) or not staging.startswith(STAGING_PREFIX):
        raise ValueError("invalid staging folder")
    if not _DIR_RE.fullmatch(target) or ".." in target:
        raise ValueError("invalid target folder")
    return _script(_IMPORT_SCRIPT, staging=staging, target=target, message=message[:300])


def export_command(repo: str, base: str, out: str, max_bytes: int) -> str:
    if not _DIR_RE.fullmatch(repo) or ".." in repo:
        raise ValueError("invalid repository folder")
    if not _COMMIT_RE.fullmatch(base):
        raise ValueError("invalid commit")
    if not _SCRATCH_RE.fullmatch(out) or not out.startswith(EXPORT_PREFIX):
        raise ValueError("invalid scratch folder")
    body = _EXPORT_SCRIPT.replace("PARTBYTES", str(PART_BYTES))
    return _script(body, repo=repo, base=base, out=out, max=str(int(max_bytes)))


def cleanup_command(out: str) -> str:
    if not _SCRATCH_RE.fullmatch(out):
        raise ValueError("invalid scratch folder")
    return f"rm -rf -- {shlex.quote(out)}"


def _marker_line(result: dict, prefix: str) -> str:
    stdout = result.get("stdout") if isinstance(result, dict) else None
    if not isinstance(stdout, str):
        return ""
    lines = [line.strip() for line in stdout.splitlines() if line.startswith(prefix)]
    return lines[-1] if lines else ""


@dataclass(frozen=True)
class ImportOutcome:
    ok: bool
    git: bool = False
    commit: str = ""
    error: str = ""


def parse_import(result: dict) -> ImportOutcome:
    match = _IMPORT_LINE.fullmatch(_marker_line(result, "BCIMPORT "))
    if match is None:
        timed_out = isinstance(result, dict) and result.get("timed_out")
        return ImportOutcome(False, error="timeout" if timed_out else "unknown")
    if match.group(4):
        return ImportOutcome(False, error=match.group(4))
    if match.group(3):
        return ImportOutcome(True, git=False)
    return ImportOutcome(True, git=True, commit=match.group(2))


@dataclass(frozen=True)
class ExportOutcome:
    ok: bool
    size: int = 0
    sha256: str = ""
    error: str = ""

    @property
    def parts(self) -> int:
        return (self.size + PART_BYTES - 1) // PART_BYTES


def parse_export(result: dict) -> ExportOutcome:
    match = _EXPORT_LINE.fullmatch(_marker_line(result, "BCEXPORT "))
    if match is None:
        timed_out = isinstance(result, dict) and result.get("timed_out")
        return ExportOutcome(False, error="timeout" if timed_out else "unknown")
    if match.group(3):
        return ExportOutcome(False, size=int(match.group(4) or 0), error=match.group(3))
    return ExportOutcome(True, size=int(match.group(1)), sha256=match.group(2))


def part_path(out: str, index: int) -> str:
    return f"{out}/part-{index:03d}"


# ----- the record of an import (a task step) ----------------------------------------------------------

def record_params(source: gitfetch.Source, *, directory: str, outcome: ImportOutcome, files: int,
                  size: int) -> dict:
    return {"repo": source.label, "host": source.host, "name": source.repo, "dir": directory,
            "git": outcome.git, "commit": outcome.commit, "files": files, "size": size}


def parse_record(content: str | None) -> dict | None:
    """The stored import step's parameters, validated (None when absent or malformed)."""
    if not content:
        return None
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("key") not in RECORD_KEYS or not isinstance(data.get("params"), dict):
        return None
    params = data["params"]
    directory, name, commit = params.get("dir"), params.get("name"), params.get("commit")
    label, host = params.get("repo"), params.get("host")
    if not (isinstance(directory, str) and _DIR_RE.fullmatch(directory) and ".." not in directory):
        return None
    if not (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)):
        return None
    if not (isinstance(label, str) and len(label) <= 700 and isinstance(host, str) and len(host) <= 253):
        return None
    git = params.get("git") is True and isinstance(commit, str) and bool(_COMMIT_RE.fullmatch(commit))
    return {"repo": label, "host": host, "name": name, "dir": directory, "git": git,
            "commit": commit if git else ""}
