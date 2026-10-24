"""Profiles signed in from the administration pages (shared by the Claude Code and Codex transports).

An operator's manifest may name a ``managed_root``: a private directory (mode
0700, owned by the service user, below root-owned parents in production) in
which BananaChat keeps one profile per pooled account, ``managed-<account id>``.
Administrators then add an account and sign it in from Administration →
Models → Claude or Codex through the official CLI's own login flow
(:mod:`bananachat.services.subscription_login`); nobody needs a shell on the
server, and no credential ever passes through BananaChat: the CLI writes it
into that profile.

Every gunicorn process reads the directory again (at most every
:data:`REFRESH_SECONDS`), so a profile created by one process is used by the
others at once. The profile dictionaries are replaced, never changed in place,
so readers iterating over them are never disturbed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path

from bananachat.services import claude_sandbox
from bananachat.services.subscription_cli import ConnectorError

MANAGED_NAME = re.compile(r"^managed-([1-9][0-9]{0,11})$")
NEW_PROFILE = "__new__"  # the account form's choice "a new profile signed in on this page"
REFRESH_SECONDS = 2.0


def managed_name(account_id) -> str:
    return f"managed-{int(account_id)}"


def account_of(name) -> int | None:
    match = MANAGED_NAME.fullmatch(name) if isinstance(name, str) else None
    return int(match.group(1)) if match else None


class ManagedProfiles:
    """Mixin for the CLI adapters. The adapter provides ``profiles`` (its static profiles, set before
    :meth:`_init_managed`), ``_profile_locks`` and two hooks: ``_managed_profile(path)`` (the profile dictionary
    for a managed directory) and ``_prepare_managed(path)`` (create what the CLI expects inside it)."""

    managed_root: str | None = None

    def _init_managed(self, manifest: dict, *, sandboxed: bool, forbidden=()) -> None:
        self._static_profiles = dict(self.profiles)
        self._managed_lock = threading.Lock()
        self._managed_checked = 0.0
        self.managed_root = None
        value = manifest.get("managed_root")
        if value in (None, ""):
            return
        if not isinstance(value, str):
            raise ConnectorError("managed_root must be an absolute directory path.")
        if any(account_of(name) is not None for name in self._static_profiles):
            raise ConnectorError("Profile names of the form managed-<number> are reserved for profiles signed in "
                                 "from the administration pages.")
        try:
            root = claude_sandbox.managed_root(value, uid=os.getuid(), forbidden=forbidden, sandboxed=sandboxed)
        except FileNotFoundError:
            raise ConnectorError("The managed_root directory does not exist. Run scripts/setup_subscriptions.py "
                                 "or create it (mode 0700, owned by the service user).") from None
        except (OSError, claude_sandbox.SandboxError) as error:
            raise ConnectorError(f"The managed_root directory cannot be used: {error}") from None
        for profile in self._static_profiles.values():
            for key in ("home", "config_dir"):
                path = Path(profile.get(key) or profile["home"])
                if path.is_relative_to(root) or root.is_relative_to(path):
                    raise ConnectorError("Keep manually configured profiles outside managed_root.")
        self.managed_root = str(root)
        self._refresh_managed(force=True)

    # ----- reading ------------------------------------------------------------------------------------------

    def _managed_entries(self) -> dict[str, str] | None:
        """``{profile name: directory}`` of the managed profiles on disk; None when the root cannot be read."""
        found = {}
        try:
            with os.scandir(self.managed_root) as entries:
                for entry in entries:
                    if account_of(entry.name) is None or not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if info.st_uid != os.getuid() or info.st_mode & 0o077:
                        continue  # not private: never used (and never shown as signed in)
                    found[entry.name] = entry.path
        except OSError:
            return None
        return found

    def _refresh_managed(self, *, force: bool = False) -> None:
        if self.managed_root is None:
            return
        now = time.monotonic()
        if not force and now - self._managed_checked < REFRESH_SECONDS:
            return
        with self._managed_lock:
            if not force and now - self._managed_checked < REFRESH_SECONDS:
                return
            found = self._managed_entries()
            if found is None:
                self._managed_checked = now
                return  # keep what is known: a transient error must not unbind every account
            current = self.profiles
            profiles = dict(self._static_profiles)
            for name, path in sorted(found.items()):
                existing = current.get(name)
                profiles[name] = existing if existing is not None and existing.get("home") == path \
                    else self._managed_profile(path)
                self._profile_locks.setdefault(name, threading.RLock())
            self.profiles = profiles
            self._managed_checked = now

    def _managed_bindings(self, stored: dict) -> dict:
        """Stored bindings plus each managed profile's own account (an explicit binding wins)."""
        self._refresh_managed()
        result = dict(stored)
        taken = set(result.values())
        for name in self.profiles:
            account = account_of(name)
            if account is not None and str(account) not in result and name not in taken:
                result[str(account)] = name
        return result

    # ----- creating and removing ----------------------------------------------------------------------------

    def create_managed_profile(self, account_id) -> str:
        """Create (or reuse) the private profile of a pooled account; returns its name."""
        if self.managed_root is None:
            raise ConnectorError("Signing in from this page is not set up: add managed_root to the connector "
                                 "configuration (scripts/setup_subscriptions.py does it).")
        name = managed_name(account_id)
        path = Path(self.managed_root) / name
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ConnectorError("The account's profile directory is not a private directory of the service user.")
        if info.st_mode & 0o077:
            os.chmod(path, 0o700, follow_symlinks=False)
        self._prepare_managed(path)
        self._refresh_managed(force=True)
        if name not in self.profiles:
            raise ConnectorError("The account's profile directory could not be prepared.")
        return name

    def remove_managed_profile(self, name) -> bool:
        """Delete a managed profile and the sign-in stored in it. False when there is none."""
        if self.managed_root is None or account_of(name) is None:
            return False
        path = Path(self.managed_root) / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ConnectorError("Refusing to delete a profile that is not a private directory of the service user.")
        if not shutil.rmtree.avoids_symlink_attacks:  # pragma: no cover - Linux always does
            raise ConnectorError("This platform cannot delete profiles safely.")
        shutil.rmtree(path)
        self._forget_profile(name)
        self._refresh_managed(force=True)
        return True

    def _forget_profile(self, name, *, identity: bool = True) -> None:
        """Drop what this process remembers about a profile (its models and cached metadata, and with *identity*
        the account it was signed in to: keep it after a new sign-in, so the next check notices a change of
        account and holds requests until that account's own usage is read)."""
        identities = getattr(self, "_identities", None)
        if identity and isinstance(identities, dict):
            identities.pop(name, None)
        invalidate = getattr(self, "invalidate_models", None)
        if callable(invalidate):
            invalidate()
        cache_lock = getattr(self, "_metadata_cache_lock", None)
        cache = getattr(self, "_metadata_cache", None)
        if cache_lock is not None and isinstance(cache, dict):
            with cache_lock:
                cache.clear()

    def is_managed(self, name) -> bool:
        return self.managed_root is not None and account_of(name) is not None
