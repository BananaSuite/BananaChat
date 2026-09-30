"""Fetch a configured Git source, including private forks, without URL credentials."""

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from .files import atomic_write

# Every git process gets the same hardened configuration: no hooks, no stored
# credentials, no ext:: transport, and no redirects or stalled transfers.
HARDENED = ("-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "protocol.ext.allow=never",
            "-c", "http.followRedirects=false", "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=30")
ASKPASS = ("import os,sys\nfrom pathlib import Path\n"
           "print(os.environ['BANANA_REPO_USER'] if 'username' in sys.argv[1].lower() "
           "else Path(os.environ['BANANA_REPO_TOKEN_FILE']).read_text().strip())\n")


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


def valid_branch(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", value)
            or ".." in value or any(part in {"", ".", ".."} or part.endswith((".lock", ".")) for part in value.split("/"))):
        raise ValueError("Invalid Git branch name.")
    return value


def valid_url(value, *, allow_local=False):
    if (not isinstance(value, str) or not value or value.startswith("-")
            or any(char.isspace() or ord(char) < 32 for char in value)):
        # A leading dash would be parsed by git or ssh as an option.
        raise ValueError("Invalid Git URL.")
    if allow_local and Path(value).is_absolute():
        return value
    if re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+", value):
        user, _, rest = value.partition("@")
        if user.startswith("-") or rest.startswith("-") or rest.partition(":")[2].startswith("-"):
            raise ValueError("Invalid Git URL.")
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTPS or SSH Git URL. Store credentials separately.")
    if parsed.hostname.startswith("-") or (parsed.username or "").startswith("-"):
        raise ValueError("Invalid Git URL.")
    if parsed.scheme == "https" and parsed.username:
        raise ValueError("Do not put credentials in the Git URL. Use --token-file instead.")
    return value


class GitSource:
    def __init__(self, root, source):
        self.root, self.source = Path(root), source
        self.git_dir = self.root / "repository.git"

    def environment(self):
        values = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0",
                  "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_LFS_SKIP_SMUDGE": "1"}
        auth = self.source.get("auth", "none")
        if auth == "token":
            credential = self.root / "config" / "repo.token"
            self.private_file(credential)
            helper = self.root / "config" / "git-askpass"
            script = "#!" + sys.executable + "\n" + ASKPASS
            # Rewrite the helper only when it is missing or differs, not on every git call.
            try:
                current = None if helper.is_symlink() else helper.lstat()
                unchanged = (current is not None and current.st_uid == os.geteuid()
                             and current.st_mode & 0o7777 == 0o700 and helper.read_text() == script)
            except (FileNotFoundError, UnicodeDecodeError):
                unchanged = False
            if not unchanged:
                atomic_write(helper, script, 0o700)
            values.update(GIT_ASKPASS=str(helper), BANANA_REPO_TOKEN_FILE=str(credential), BANANA_REPO_USER=self.source.get("username", "git"))
        elif auth == "ssh":
            key = self.root / "config" / "repo.key"
            known = self.root / "config" / "repo.known_hosts"
            self.private_file(key)
            self.private_file(known)
            values["GIT_SSH_COMMAND"] = shlex.join(["ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known), "-o", "ConnectTimeout=20"])
        elif auth != "none":
            raise ValueError("Unknown repository authentication method.")
        return values

    @staticmethod
    def private_file(path):
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid():
            raise ValueError("Repository credentials must be owned by the operator and have mode 0600.")

    def secrets(self):
        """Values that must never appear in an error message."""
        if self.source.get("auth", "none") != "token":
            return ()
        try:
            return (self.root.joinpath("config", "repo.token").read_text().strip(),)
        except (OSError, UnicodeDecodeError):
            return ()

    def git(self, *arguments, output=None, check=True, timeout=300):
        command = ["git", *HARDENED, "--git-dir", str(self.git_dir), *map(str, arguments)]
        result = subprocess.run(command, env=self.environment(), stdout=output or subprocess.PIPE, stderr=subprocess.PIPE,
                                text=output is None, timeout=timeout)
        if check and result.returncode:
            # Git error bodies can include authentication information: report
            # only a short tail with credentials and URL user details removed.
            error = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
            detail = redact(error, self.secrets())
            raise RuntimeError(f"Git {arguments[0]} failed. Check repository access, credentials, and the configured branch."
                               + (f" Git reported: {detail}" if detail else ""))
        return result

    def initialize(self):
        if self.git_dir.is_symlink():
            raise ValueError("The repository cache must not be a symlink.")
        if not self.git_dir.exists():
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            self.git_dir.mkdir(mode=0o700)
            # Creating the empty cache needs no repository credentials.
            environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
            subprocess.run(["git", *HARDENED, "init", "--bare", "--", str(self.git_dir)], env=environment,
                           check=True, capture_output=True, timeout=60)
        self.git_dir.chmod(0o700)

    def seed(self, checkout):
        checkout = Path(checkout).resolve()
        local = ["git", "-c", "safe.directory=" + str(checkout), "-c", "core.hooksPath=/dev/null",
                 "-c", "core.fsmonitor=false", "-C", str(checkout)]
        status = subprocess.run([*local, "status", "--porcelain"], env=self.environment(),
                                capture_output=True, text=True, check=True, timeout=60)
        if status.stdout:
            raise ValueError("Commit the reviewed source before installing; the checkout contains changes.")
        self.initialize()
        # A local fetch launches upload-pack, which discards command-scope
        # safe.directory settings. A bundle imports the reviewed checkout even
        # under sudo, without relaxing ownership checks globally.
        with tempfile.TemporaryDirectory(prefix="source-", dir=self.root / "staging") as directory:
            bundle = Path(directory) / "source.bundle"
            subprocess.run([*local, "bundle", "create", str(bundle), "HEAD"], env=self.environment(),
                           capture_output=True, check=True, timeout=300)
            self.git("fetch", "--no-tags", "--", str(bundle), "HEAD")
        return self.git("rev-parse", "FETCH_HEAD").stdout.strip()

    def verify(self, revision):
        """Refuse a revision that the operator's allowed signers did not sign.

        Fast-forward-only updates stop rewritten history, and a failed release
        rolls back, but neither notices a genuine commit pushed by someone who
        should not have been able to push it. An operator who signs releases
        can require that here, and an unsigned or unknown-key revision is
        rejected before anything is staged.
        """
        if self.source.get("signing", "none") != "ssh":
            return None
        signers = self.root / "config" / "repo.allowed_signers"
        self.private_file(signers)
        # Git picks its verifier from the signature itself, not from
        # gpg.format. An OpenPGP-signed commit would therefore be checked
        # against whatever the machine's GnuPG keyring happens to trust,
        # ignoring the allowed signers file entirely. Only accept the format
        # this setting actually controls.
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(revision)):
            raise ValueError("Invalid source revision.")
        header = self.git("cat-file", "commit", revision).stdout.split("\n\n", 1)[0]
        if "BEGIN SSH SIGNATURE" not in header:
            detail = ("carries an OpenPGP signature, which this setting does not accept"
                      if "gpgsig" in header else "is not signed")
            raise RuntimeError(
                "Revision " + revision[:12] + " " + detail + ". Updates require an SSH signature "
                "from a key in the allowed signers file. Nothing was fetched into a release."
            )
        result = self.git("-c", "gpg.format=ssh",
                          "-c", "gpg.ssh.allowedSignersFile=" + str(signers),
                          "verify-commit", revision, check=False)
        if result.returncode:
            raise RuntimeError(
                "Revision " + revision[:12] + " is not signed by a key in the allowed signers "
                "file. Nothing was fetched into a release. Review the commit, then either sign "
                "it or add its key."
            )
        return revision

    def resolve(self, current):
        self.initialize()
        url = valid_url(self.source["url"], allow_local=Path(self.source["url"]).is_absolute())
        chosen = valid_branch(self.source["branch"])
        result = self.git("ls-remote", "--heads", "--", url, "refs/heads/" + chosen)
        if not result.stdout.strip():
            fallback = self.source.get("fallback_branch")
            if not fallback:
                raise RuntimeError("The selected branch was deleted. Updates are paused until a branch or explicit fallback is configured.")
            chosen = valid_branch(fallback)
            result = self.git("ls-remote", "--heads", "--", url, "refs/heads/" + chosen)
            if not result.stdout.strip():
                raise RuntimeError("Both the selected branch and its configured fallback are missing. Nothing was updated.")
        self.git("fetch", "--no-tags", "--", url, "refs/heads/" + chosen)
        sha = self.git("rev-parse", "FETCH_HEAD").stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise RuntimeError("Git did not return a valid source revision.")
        self.verify(sha)
        forward = sha == current or (bool(re.fullmatch(r"[0-9a-f]{40,64}", str(current)))
                                     and self.git("merge-base", "--is-ancestor", current, sha, check=False).returncode == 0)
        return sha, chosen, forward

    def archive(self, revision, destination):
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(revision)):
            raise ValueError("Invalid source revision.")
        with Path(destination).open("wb") as target:
            self.git("archive", "--format=tar.gz", revision, output=target)
