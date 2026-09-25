"""HTTPS Git transport with private-repository checks for GitHub and Forgejo."""

import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from .files import atomic_write, private_bytes

PART_BYTES = 32 * 1024 * 1024
OVERHEAD = 4 * 1024 * 1024
SNAPSHOT = re.compile(r"[0-9]{8}T[0-9]{6}(?:\.[0-9]{6})?Z-[0-9a-f]{8}")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


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
    request = urllib.request.Request(target["api"] + "/repos/" + target["repository"], headers={
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
        private_bytes(token_file)
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
        result = subprocess.run([*self.options, "init", "--bare", str(self.repository)], env=self.environment,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Could not initialize backup Git storage.")

    def run(self, *arguments, input=None, source=None, output=None, bounded=False):
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (self.maximum, self.maximum))
        result = subprocess.run([*self.options, "--git-dir", str(self.repository), *map(str, arguments)],
                                env=self.environment, input=input, stdin=source,
                                stdout=output or subprocess.PIPE, stderr=subprocess.PIPE, timeout=900,
                                preexec_fn=limits if bounded else None)
        if result.returncode:
            # Git can repeat tokens or remote response bodies. Withhold both.
            raise RuntimeError(f"Backup Git {arguments[0]} failed. Check repository access, free space, and branch permissions.")
        return result.stdout or b""

    def heads(self, prefix):
        raw = self.run("ls-remote", "--heads", self.url, prefix + "*")
        if len(raw) > 1024 * 1024:
            raise ValueError("Too many backup branches. Review retention in the repository.")
        result = {}
        for line in raw.decode().splitlines():
            sha, ref = line.split("\t", 1)
            if ref.startswith(prefix) and SNAPSHOT.fullmatch(ref[len(prefix):]) and SHA.fullmatch(sha):
                result[ref[len(prefix):]] = sha
        return result

    def ref_sha(self, ref):
        raw = self.run("ls-remote", "--heads", self.url, ref).decode().strip()
        if not raw:
            return None
        sha, name = raw.split("\t", 1)
        if name != ref or not SHA.fullmatch(sha):
            raise ValueError("Unexpected backup repository reference.")
        return sha

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
        self.run("push", "--porcelain", "--force-with-lease=" + ref + ":", self.url, sha + ":" + ref)

    def fetch(self, ref, expected):
        self.run("fetch", "--no-tags", "--depth=1", self.url, ref, bounded=True)
        sha = self.run("rev-parse", "FETCH_HEAD").decode().strip()
        if sha != expected:
            raise ValueError("The backup branch changed while downloading. Retry after reviewing the repository.")
        raw = self.run("ls-tree", "-rlz", "--full-tree", sha)
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

    def remove(self, ref, sha):
        self.run("push", "--porcelain", "--force-with-lease=" + ref + ":" + sha, self.url, ":" + ref)
