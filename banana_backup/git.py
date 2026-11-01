"""HTTPS Git transport with private-repository checks for GitHub and Forgejo."""

import json
import os
from pathlib import Path
import re
import resource
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .files import atomic_write, private_bytes

PART_BYTES = 32 * 1024 * 1024
OVERHEAD = 4 * 1024 * 1024
SNAPSHOT = re.compile(r"[0-9]{8}T[0-9]{6}(?:\.[0-9]{6})?Z-[0-9a-f]{8}")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def redact(text, secrets=(), limit=600):
    """Return a short, single-line tail of Git output without credentials.

    Git and remote servers can echo URLs, headers, or tokens. Known secrets are
    removed verbatim, then anything shaped like a credential is masked.
    """
    text = str(text or "")
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]*@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)[^\r\n]*", r"\1[redacted]", text)
    text = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 [redacted]", text)
    text = re.sub(r"(?i)\b((?:access[-_]?|private[-_]?)?(?:token|password|passwd|secret))(\s*[:=]\s*)[^\s'\"]+",
                  r"\1\2[redacted]", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,})", "[redacted]", text)
    text = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    return text[-limit:]


def location(url, forge):
    parsed = urllib.parse.urlsplit(url)
    if (forge not in {"github", "forgejo"} or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in url)):
        raise ValueError("Use a GitHub or Forgejo HTTPS repository URL without embedded credentials.")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if len(parts) < 2 or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) or ".." in part for part in parts):
        raise ValueError("Use the HTTPS clone URL for owner/repository.")
    repository = "/".join(parts[-2:])
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(parts[:-2]), "", "")).rstrip("/")
    api = ("https://api.github.com" if base == "https://github.com" else base + ("/api/v3" if forge == "github" else "/api/v1"))
    return {"url": base + "/" + repository + ".git", "api": api, "repository": repository, "forge": forge}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def check_private(config, token_file):
    """Fail closed before any backup data is sent, even after a visibility change."""
    token = private_bytes(token_file).decode().strip()
    if not token or any(char.isspace() or ord(char) < 32 for char in token):
        raise ValueError("The repository token file must contain one token.")
    target = location(config["url"], config["forge"])
    request = urllib.request.Request(  # noqa: S310 - https forge API built by location()
        target["api"] + "/repos/" + target["repository"], headers={
        "Authorization": ("Bearer " if target["forge"] == "github" else "token ") + token,
        "Accept": "application/json", "User-Agent": "BananaSuite-Backups/1",
    })
    # Do not inherit a developer's proxy or forward credentials through redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Repository metadata exceeded the size limit.")
        data = json.loads(raw)
    except (OSError, urllib.error.URLError, ValueError):
        raise RuntimeError("Could not verify the private backup repository. Check its URL, visibility, and token permissions.") from None
    if (not isinstance(data, dict) or data.get("private") is not True
            or str(data.get("full_name", "")).lower() != target["repository"].lower()):
        raise ValueError("Backups require a private repository. No backup was uploaded.")
    if data.get("archived") or data.get("mirror"):
        raise ValueError("Choose a writable private repository, not an archive or mirror.")
    return True


class Git:
    """Use a disposable bare repository. Never check out or execute remote code."""

    def __init__(self, work, config, token_file, *, allow_local=False):
        self.work, self.config = Path(work), config
        self.repository = self.work / "git"
        self.maximum = int(config["max_mib"]) * 1024 * 1024 + OVERHEAD
        self.allow_local = allow_local
        self.url = str(config["url"]) if allow_local else location(config["url"], config["forge"])["url"]
        if self.url.startswith("-"):
            raise ValueError("Invalid backup repository URL.")
        token = private_bytes(token_file).decode(errors="replace").strip()
        # Never repeat the token in an error message, even if Git echoes it.
        self.secrets = (token,) if token else ()
        helper = self.work / "askpass"
        atomic_write(helper, "#!" + sys.executable + "\nimport os,sys\nfrom pathlib import Path\n"
                     "print(os.environ['BANANA_BACKUP_USER'] if 'username' in sys.argv[1].lower() else Path(os.environ['BANANA_BACKUP_TOKEN_FILE']).read_text().strip())\n")
        helper.chmod(0o700)
        self.environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(helper), "GIT_LFS_SKIP_SMUDGE": "1",
                            "BANANA_BACKUP_USER": config.get("username", "git"),
                            "BANANA_BACKUP_TOKEN_FILE": str(Path(token_file).absolute()),
                            "GIT_AUTHOR_NAME": "BananaSuite Backup", "GIT_COMMITTER_NAME": "BananaSuite Backup",
                            "GIT_AUTHOR_EMAIL": "backup@localhost", "GIT_COMMITTER_EMAIL": "backup@localhost"}
        self.options = ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "core.fsmonitor=false",
                        "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=" + ("always" if allow_local else "never"),
                        "-c", "http.followRedirects=false", "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=30",
                        "-c", "fetch.unpackLimit=1", "-c", "transfer.fsckObjects=true", "-c", "gc.auto=0"]
        self._init(self.repository)

    def _init(self, repository):
        result = subprocess.run([*self.options, "init", "--bare", "--", str(repository)], env=self.environment,
                                capture_output=True, timeout=30)
        if result.returncode:
            detail = redact(result.stderr.decode(errors="replace"), self.secrets)
            raise RuntimeError("Could not initialize backup Git storage." + (f" Git reported: {detail}" if detail else ""))

    def run(self, *arguments, input=None, source=None, output=None, bounded=False, limit=None, repository=None):
        """Run Git in the disposable repository (or in *repository*) and return its output.

        With *limit*, the output is read as it comes and Git is stopped once
        it printed more than *limit* bytes: the caller gets those
        ``limit + 1`` bytes and refuses them, without the rest ever being in
        memory (a few-KB tree can list gigabytes).
        """
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (self.maximum, self.maximum))
        command = [*self.options, "--git-dir", str(repository or self.repository), *map(str, arguments)]
        if limit is not None:
            return self._run_limited(command, arguments, source, limit, limits if bounded else None)
        result = subprocess.run(command, env=self.environment, input=input, stdin=source,
                                stdout=output or subprocess.PIPE, stderr=subprocess.PIPE, timeout=900,
                                preexec_fn=limits if bounded else None)
        if result.returncode:
            self._failed(arguments, result.stderr)
        return result.stdout or b""

    def _run_limited(self, command, arguments, source, limit, preexec):
        deadline = time.monotonic() + 900
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, env=self.environment, stdin=source, stdout=subprocess.PIPE,
                                       stderr=errors, preexec_fn=preexec)
            data = bytearray()
            try:
                with selectors.DefaultSelector() as waiting:
                    waiting.register(process.stdout, selectors.EVENT_READ)
                    while len(data) <= limit:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, 900)
                        if not waiting.select(remaining):
                            continue
                        chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(data)))
                        if not chunk:
                            break
                        data += chunk
                if len(data) > limit:
                    return bytes(data)
                process.wait(max(0.1, deadline - time.monotonic()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
            if process.returncode:
                errors.seek(0)
                self._failed(arguments, errors.read(64 * 1024))
        return bytes(data)

    def _failed(self, arguments, stderr):
        # Git can repeat tokens or remote response bodies: report only a
        # short tail with known secrets and credential-shaped text removed.
        detail = redact(stderr.decode(errors="replace") if stderr else "", self.secrets)
        raise RuntimeError(f"Backup Git {arguments[0]} failed. Check repository access, free space, and branch permissions."
                           + (f" Git reported: {detail}" if detail else ""))

    def heads(self, prefix):
        raw = self.run("ls-remote", "--heads", "--", self.url, prefix + "*", limit=1024 * 1024)
        if len(raw) > 1024 * 1024:
            raise ValueError("Too many backup branches. Review retention in the repository.")
        result = {}
        # Branch names are bytes chosen by whoever can push: what is no snapshot is skipped, whatever its bytes.
        for line in raw.split(b"\n"):
            sha, _, ref = line.decode(errors="replace").partition("\t")
            if ref.startswith(prefix) and SNAPSHOT.fullmatch(ref[len(prefix):]) and SHA.fullmatch(sha):
                result[ref[len(prefix):]] = sha
        return result

    def ref_sha(self, ref):
        raw = self.run("ls-remote", "--heads", "--", self.url, ref, limit=1024 * 1024)
        if len(raw) > 1024 * 1024:
            raise ValueError("Too many backup branches. Review retention in the repository.")
        # Branches that only end in "/" + *ref* are listed too.
        for line in raw.split(b"\n"):
            sha, _, name = line.decode(errors="replace").partition("\t")
            if name == ref:
                if not SHA.fullmatch(sha):
                    raise ValueError("Unexpected backup repository reference.")
                return sha
        return None

    def commit(self, paths):
        entries = []
        for path in sorted(paths):
            with path.open("rb") as source:
                sha = self.run("hash-object", "-w", "--stdin", source=source).decode().strip()
            entries.append(f"100644 blob {sha}\t{path.name}\n")
        tree = self.run("mktree", input="".join(entries).encode()).decode().strip()
        return self.run("commit-tree", tree, "-m", "Encrypted application backup").decode().strip()

    def push(self, sha, ref):
        # Even a coincident snapshot name cannot replace someone else's branch.
        self.run("push", "--porcelain", "--force-with-lease=" + ref + ":", "--", self.url, sha + ":" + ref)

    def fetch(self, ref, expected):
        self.run("fetch", "--no-tags", "--depth=1", "--", self.url, ref, bounded=True)
        sha = self.run("rev-parse", "FETCH_HEAD").decode().strip()
        if sha != expected:
            raise ValueError("The backup branch changed while downloading. Retry after reviewing the repository.")
        # Snapshot trees are flat: without -r a subtree is one entry the mode check refuses, never its
        # contents (a few-KB tree whose subtrees share one another lists W^D lines recursively).
        raw = self.run("ls-tree", "-lz", "--full-tree", sha, limit=128 * 1024)
        if len(raw) > 128 * 1024:
            raise ValueError("The backup tree exceeds the file limit.")
        entries, total = {}, 0
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            info, name = entry.decode().split("\t", 1)
            mode, kind, oid, size = info.split()
            if (mode != "100644" or kind != "blob" or not SHA.fullmatch(oid)
                    or name in entries or not (name == "index.json" or re.fullmatch(r"part-[0-9]{5}\.age", name))):
                raise ValueError("Unexpected files or links in the backup branch.")
            size = int(size)
            if not 0 < size <= (128 * 1024 if name == "index.json" else PART_BYTES):
                raise ValueError("A backup file exceeds the size limit.")
            total += size
            entries[name] = (oid, size)
        if total > self.maximum or len(entries) > 36 or "index.json" not in entries:
            raise ValueError("The backup exceeds its configured size or file limit.")
        return entries

    def index(self, ref, expected):
        """The object ID of ``index.json`` in snapshot branch *ref* (head *expected*), fetching as little as possible.

        Where the repository allows partial fetches, its encrypted parts stay
        on the server (retention checks every snapshot it keeps); elsewhere
        the whole snapshot is fetched. Only trees and the index are read, and
        an index the fetch left out (over 129 KiB) or over 128 KiB is refused
        before Git reads it, so nothing missing is ever fetched later.

        Each snapshot is fetched into a repository of its own, removed
        afterwards, and only its index is kept: retention reads every branch
        anyone who can push makes, so what they fetch must not pile up (disk
        for one snapshot at a time, one pack to search).
        """
        scratch = Path(tempfile.mkdtemp(prefix="index-", dir=self.work))
        try:
            self._init(scratch)
            self.run("fetch", "--no-tags", "--depth=1", "--filter=blob:limit=129k", "--", self.url, ref,
                     bounded=True, repository=scratch)
            sha = self.run("rev-parse", "FETCH_HEAD", repository=scratch).decode().strip()
            if sha != expected:
                raise ValueError("The backup branch changed while reading it. Retry after reviewing the repository.")
            raw = self.run("ls-tree", "-z", "--full-tree", sha, limit=128 * 1024, repository=scratch)
            for entry in raw[:128 * 1024].split(b"\0"):
                info, _, name = entry.decode(errors="replace").partition("\t")
                parts = info.split()
                if name == "index.json" and len(parts) == 3 and parts[:2] == ["100644", "blob"] and SHA.fullmatch(parts[2]):
                    # Git reading a blob that is not here (even only its size) fetches it from the server.
                    if (not self._stored(parts[2], scratch)
                            or not 0 < int(self.run("cat-file", "-s", parts[2], repository=scratch)) <= 128 * 1024):
                        raise ValueError("The backup index exceeds the size limit.")
                    content = self.run("cat-file", "blob", parts[2], bounded=True, repository=scratch)
                    if self.run("hash-object", "-w", "--stdin", input=content).decode().strip() != parts[2]:
                        raise ValueError("The backup index could not be read.")
                    return parts[2]
            raise ValueError("The backup branch has no index.")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _stored(self, oid, repository=None):
        """Whether object *oid* is in this repository (or *repository*), found from the pack indexes without Git
        looking it up."""
        objects = (repository or self.repository) / "objects"
        if (objects / oid[:2] / oid[2:]).is_file():
            return True
        for pack in sorted((objects / "pack").glob("*.idx")):
            with pack.open("rb") as source:
                if oid.encode() in self.run("show-index", source=source).split():
                    return True
        return False

    def discard(self):
        """Delete the objects fetched or written so far, freeing their space; the repository is not used again."""
        shutil.rmtree(self.repository)

    def remove(self, ref, sha):
        self.run("push", "--porcelain", "--force-with-lease=" + ref + ":" + sha, "--", self.url, ":" + ref)
