"""Signing a pooled subscription account in from the administration pages.

The official CLI runs its own login in the account's private profile (inside
the same sandbox as its answers) and BananaChat only relays what it prints:

* **Claude** (``claude auth login --claudeai``, on a pseudo-terminal): it prints
  a link to Anthropic's sign-in page. The administrator opens it, signs in to
  the subscription, approves the access and copies the code the page then
  shows; BananaChat types that code into the CLI, which exchanges it and stores
  its own credentials in the profile.
* **Codex** (``codex login --device-auth``): it prints a link and a one-time
  code. The administrator opens the link, signs in to ChatGPT and enters the
  code; the CLI notices on its own and stores its credentials.

No token, password or cookie passes through BananaChat. The code typed into
Claude's prompt is a single-use authorisation code that only the waiting CLI
(which holds the matching verifier) can redeem; it is kept in the database
for at most a second, until the process that runs the login reads it.

A login runs in a thread of the process that started it. Its state lives in
``runtime_state`` (``subscription_login:<provider>:<account id>``), so every
process can show it and accept the code or a cancellation; when the owning
process disappears (a restart), the next status read marks the login as
interrupted.
"""

from __future__ import annotations

import fcntl
import logging
import os
import pty
import re
import secrets
import select
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
from urllib.parse import urlsplit

from bananachat import db
from bananachat.db import settings as site_settings
from bananachat.db import subscription_pool as pool_db

log = logging.getLogger("bananachat.subscription_login")

SESSION_SECONDS = 15 * 60          # Codex device codes expire after 15 minutes; Claude's prompt is given as long
LINK_SECONDS = 90                  # the CLI must print its link within this
FINISH_SECONDS = 120               # after the code was typed in
STOP_SECONDS = 5
MAX_OUTPUT = 256 * 1024
ACTIVE = ("starting", "waiting", "verifying")
FINAL = ("done", "failed", "cancelled")
CODE_RE = re.compile(r"^[A-Za-z0-9._~#=+/\-]{8,600}$")
# Where each CLI may send the administrator (anything else it prints is never shown as a link).
LINK_HOSTS = {
    "claude": ("claude.ai", "claude.com", "anthropic.com"),
    "codex": ("openai.com", "chatgpt.com"),
}
_URL = re.compile(r"https://[^\s\x07\x1b<>\"'`]+")
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_DEVICE_CODE = re.compile(r"\b([A-Z0-9]{4,8}-[A-Z0-9]{4,8})\b")
_SECRETISH = re.compile(r"[A-Za-z0-9_\-.+/=]{32,}")


class LoginError(ValueError):
    """Shown to the administrator as it is."""


def _key(provider: str, account_id: int) -> str:
    return f"subscription_login:{provider}:{int(account_id)}"


def status_key(provider: str, account_id: int) -> str:
    return f"subscription_signin:{provider}:{int(account_id)}"


# ----- the stored session -----------------------------------------------------------------------------------

def _public(session) -> dict | None:
    if not isinstance(session, dict):
        return None
    return {key: session.get(key) for key in ("id", "provider", "account_id", "state", "kind", "url", "user_code",
                                              "message", "plan", "started_at", "expires_at", "by", "needs_code")}


def _alive(session) -> bool:
    if session.get("host") != socket.gethostname():
        return True  # another machine: trust its expiry only
    pid = session.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _update(provider: str, account_id: int, session_id: str, **changes) -> dict | None:
    """Change the stored session if it is still *session_id* (a newer login is never overwritten)."""
    key = _key(provider, account_id)
    with db.transaction():
        session = site_settings.state_get(key)
        if not isinstance(session, dict) or session.get("id") != session_id:
            return None
        session.update(changes, updated_at=time.time())
        site_settings.state_set(key, session)
    return session


def status(provider: str, account_id: int) -> dict | None:
    """The account's latest login (``None`` when there was none); an abandoned one is reported as interrupted."""
    session = site_settings.state_get(_key(provider, account_id))
    if not isinstance(session, dict):
        return None
    if session.get("state") in ACTIVE:
        if time.time() > float(session.get("expires_at") or 0) + 30:
            session = _update(provider, account_id, session["id"], state="failed", needs_code=False,
                              message="The sign-in expired. Start it again.") or session
        elif not _alive(session):
            session = _update(provider, account_id, session["id"], state="failed", needs_code=False,
                              message="The sign-in was interrupted (the server process restarted). Start it "
                                      "again.") or session
    return _public(session)


def signin_status(provider: str, account_id: int) -> dict | None:
    """The last known sign-in of the account's profile (from a login, a check or a sign-out)."""
    value = site_settings.state_get(status_key(provider, account_id))
    return value if isinstance(value, dict) else None


def record_signin(provider: str, account_id: int, *, signed_in: bool, plan=None, message: str = "") -> None:
    site_settings.state_set(status_key(provider, account_id), {
        "signed_in": bool(signed_in), "plan": plan if isinstance(plan, str) else None,
        "checked_at": time.time(), "message": message[:300]})


def forget(provider: str, account_id: int) -> None:
    for key in (_key(provider, account_id), _key(provider, account_id) + ":code",
                _key(provider, account_id) + ":cancel", status_key(provider, account_id)):
        site_settings.state_delete(key)


# ----- starting, answering and cancelling ---------------------------------------------------------------------

def start(app, adapter, provider: str, account, *, by: str = "") -> dict:
    """Start signing *account* in; returns the public session (poll :func:`status` for the link)."""
    name, _profile = adapter.profile_for(account)
    if not callable(getattr(adapter, "login_command", None)):
        raise LoginError("This adapter cannot sign accounts in from this page.")
    account_id = int(account["id"])
    key = _key(provider, account_id)
    now = time.time()
    with db.transaction():
        current = site_settings.state_get(key)
        if isinstance(current, dict) and current.get("state") in ACTIVE and _alive(current) \
                and now < float(current.get("expires_at") or 0):
            raise LoginError("A sign-in for this account is already running. Finish or cancel it first.")
        session = {"id": secrets.token_hex(8), "provider": provider, "account_id": account_id, "profile": name,
                   "state": "starting", "kind": "code" if provider == "claude" else "device", "url": None,
                   "user_code": None, "needs_code": False, "message": "Starting the official sign-in…",
                   "plan": None, "started_at": now, "updated_at": now, "expires_at": now + SESSION_SECONDS,
                   "pid": os.getpid(), "host": socket.gethostname(), "by": by[:80]}
        site_settings.state_set(key, session)
        site_settings.state_delete(key + ":code")
        site_settings.state_delete(key + ":cancel")
    thread = threading.Thread(target=_run, args=(app, adapter, provider, account_id, name, session["id"]),
                              name=f"bananachat-{provider}-login-{account_id}", daemon=True)
    thread.start()
    return _public(session)


def submit_code(provider: str, account_id: int, code) -> dict:
    """Pass the code the sign-in page showed to the waiting Claude login."""
    code = "".join(str(code or "").split())
    if not CODE_RE.fullmatch(code):
        raise LoginError("Paste the whole code shown after you approved the sign-in.")
    session = status(provider, account_id)
    if not session or session["state"] != "waiting" or not session.get("needs_code"):
        raise LoginError("This sign-in is not waiting for a code. Start it again.")
    site_settings.state_set(_key(provider, account_id) + ":code", {"session": session["id"], "code": code})
    return session


def cancel(provider: str, account_id: int) -> dict | None:
    session = status(provider, account_id)
    if not session or session["state"] not in ACTIVE:
        return session
    site_settings.state_set(_key(provider, account_id) + ":cancel", session["id"])
    stored = site_settings.state_get(_key(provider, account_id))
    if isinstance(stored, dict) and not _alive(stored):
        _update(provider, account_id, session["id"], state="cancelled", needs_code=False, message="Cancelled.")
    return status(provider, account_id)


def sign_out(adapter, provider: str, account) -> None:
    """Remove the profile's sign-in with the CLI's own logout."""
    name, _profile = adapter.profile_for(account)
    command, environment, cwd = adapter.logout_command(name)
    try:
        subprocess.run(command, env=environment, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, check=False, start_new_session=True, close_fds=True)
    except subprocess.TimeoutExpired:
        raise LoginError("The CLI did not finish signing out in time.") from None
    except OSError:
        raise LoginError("The CLI could not be started to sign out.") from None
    _settle(adapter, provider, int(account["id"]), name)
    record_signin(provider, int(account["id"]), signed_in=False, message="Signed out from this page.")


# ----- the login itself ---------------------------------------------------------------------------------------

class _Cancelled(Exception):
    pass


class _Process:
    """The CLI's login process: on a pseudo-terminal (Claude's interactive prompt) or plain pipes (Codex)."""

    def __init__(self, command, environment, cwd, *, terminal: bool):
        self.master = None
        if terminal:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 4000, 0, 0))
            try:
                self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=environment,
                                                cwd=cwd, start_new_session=True, close_fds=True)
            finally:
                os.close(slave)
            self.master = master
            self.reader = master
        else:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, env=environment, cwd=cwd,
                                            start_new_session=True, close_fds=True)
            self.reader = self.process.stdout.fileno()
        os.set_blocking(self.reader, False)
        self.output = bytearray()
        self.total = 0
        self.closed = False

    def read(self, wait: float) -> bool:
        """Read what is there (waiting up to *wait* seconds); False once the output ended."""
        if self.closed:
            time.sleep(min(wait, 0.2))
            return False
        ready, _, _ = select.select([self.reader], [], [], wait)
        if not ready:
            return True
        try:
            data = os.read(self.reader, 65536)
        except BlockingIOError:
            return True
        except OSError:  # EIO: the terminal's other side closed
            data = b""
        if not data:
            self.closed = True
            return False
        self.total += len(data)
        self.output.extend(data)
        if len(self.output) > MAX_OUTPUT:
            del self.output[:len(self.output) - MAX_OUTPUT]
        return True

    def text(self, start: int = 0) -> str:
        """The output from byte *start* on (a position taken from ``len(output)``)."""
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def write(self, data: bytes) -> None:
        if self.master is None:
            return
        view = memoryview(data)
        deadline = time.monotonic() + 5
        while view and time.monotonic() < deadline:
            try:
                written = os.write(self.master, view)
                view = view[written:]
            except BlockingIOError:
                time.sleep(0.05)

    def exited(self):
        return self.process.poll()

    def stop(self) -> None:
        for number in (signal.SIGTERM, signal.SIGKILL):
            if self.process.poll() is not None:
                break
            try:
                os.killpg(self.process.pid, number)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.process.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                continue
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            log.warning("A subscription login process did not stop")
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        elif self.process.stdout is not None:
            self.process.stdout.close()


def plain(text: str) -> str:
    """Terminal output without colours, hyperlinks and cursor movements."""
    return _ANSI.sub("", text).replace("\r", "\n")


def find_link(provider: str, text: str) -> str | None:
    """The first sign-in link of the provider in the CLI's output."""
    allowed = LINK_HOSTS[provider]
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;)")
        if "\\" in url or "@" in url.split("?", 1)[0] or any(ord(char) < 33 for char in url):
            continue  # parsers disagree about such addresses (a browser may go elsewhere than checked here)
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        host = (parts.hostname or "").lower()
        if parts.scheme == "https" and len(url) <= 4096 and not parts.username and not parts.password \
                and any(host == domain or host.endswith("." + domain) for domain in allowed):
            return url
    return None


def find_device_code(text: str) -> str | None:
    lowered = text.lower()
    start = lowered.find("one-time code")
    for match in _DEVICE_CODE.finditer(text, start if start >= 0 else 0):
        return match.group(1)
    return None


def summary(text: str) -> str:
    """The last few lines the CLI printed, for the administrator: no links, nothing that looks like a secret."""
    lines = [line.strip() for line in plain(text).splitlines()]
    lines = [_SECRETISH.sub("[…]", _URL.sub("[link]", line)) for line in lines if line]
    lines = [line for line in lines if "paste code here" not in line.lower()]
    tail = " · ".join(lines[-3:])
    tail = "".join(char for char in tail if char.isprintable())
    return tail[:300]


def _run(app, adapter, provider: str, account_id: int, name: str, session_id: str) -> None:
    with app.app_context():
        try:
            command, environment, cwd = adapter.login_command(name)
            process = _Process(command, environment, cwd, terminal=provider == "claude")
            try:
                if provider == "claude":
                    _claude(process, provider, account_id, session_id)
                else:
                    _codex(process, provider, account_id, session_id)
            finally:
                process.stop()
            _update(provider, account_id, session_id, state="verifying", needs_code=False,
                    message="Checking the new sign-in…")
            result = _verify(adapter, provider, account_id, name)
            plan = result.get("subscription_type") or result.get("plan")
            record_signin(provider, account_id, signed_in=True, plan=plan)
            _update(provider, account_id, session_id, state="done", plan=plan,
                    message="Signed in." + (f" Plan: {plan}." if plan else ""))
            log.info("%s account %s signed in from the administration pages", provider, account_id)
        except _Cancelled:
            _update(provider, account_id, session_id, state="cancelled", needs_code=False, message="Cancelled.")
        except LoginError as error:
            _update(provider, account_id, session_id, state="failed", needs_code=False, message=str(error))
        except Exception as error:  # noqa: BLE001 - reported without provider output, which may hold details
            from bananachat.services.subscription_cli import ConnectorError

            message = str(error) if isinstance(error, ConnectorError) else \
                "The sign-in could not run. Check the server log and the subscription setup on this page."
            log.warning("%s sign-in of account %s failed (%s)", provider, account_id, type(error).__name__)
            _update(provider, account_id, session_id, state="failed", needs_code=False, message=message)
        finally:
            for suffix in (":code", ":cancel"):
                try:
                    site_settings.state_delete(_key(provider, account_id) + suffix)
                except Exception:  # noqa: BLE001 - the next login clears them too
                    log.debug("Could not clear a sign-in message", exc_info=True)
            db.release_thread_connection()


def _check_cancel(provider, account_id, session_id) -> None:
    if site_settings.state_get(_key(provider, account_id) + ":cancel") == session_id:
        raise _Cancelled()


def _take_code(provider, account_id, session_id) -> str | None:
    key = _key(provider, account_id) + ":code"
    with db.transaction():
        value = site_settings.state_get(key)
        if not isinstance(value, dict) or value.get("session") != session_id:
            return None
        site_settings.state_delete(key)
    code = value.get("code")
    return code if isinstance(code, str) and CODE_RE.fullmatch(code) else None


def _claude(process: _Process, provider, account_id, session_id) -> None:
    started = time.monotonic()
    deadline = time.monotonic() + SESSION_SECONDS
    link = None
    checked = 0.0
    while True:
        alive = process.read(0.25)
        now = time.monotonic()
        if now - checked >= 0.5:
            checked = now
            _check_cancel(provider, account_id, session_id)
            if link is not None:
                code = _take_code(provider, account_id, session_id)
                if code is not None:
                    process.write(code.encode("ascii") + b"\r")
                    _update(provider, account_id, session_id, state="verifying", needs_code=False,
                            message="Code sent; Claude Code is completing the sign-in…")
                    break
        if link is None:
            link = find_link(provider, process.text())
            if link is not None:
                _update(provider, account_id, session_id, state="waiting", url=link, needs_code=True,
                        message="Open the link, sign in to the Claude subscription, approve access and paste the "
                                "code the page shows.")
            elif now - started > LINK_SECONDS:
                raise LoginError("Claude Code did not show a sign-in link. " + (summary(process.text()) or
                                 "Check that the CLI runs on this server."))
        if not alive and process.exited() is not None:
            raise LoginError("Claude Code stopped before the sign-in finished. " + summary(process.text()))
        if now > deadline:
            raise LoginError("The sign-in expired. Start it again.")
    finish = time.monotonic() + FINISH_SECONDS
    mark = len(process.output)  # bytes: the output may hold multi-byte characters before this point
    while time.monotonic() < finish:
        process.read(0.25)
        code = process.exited()
        if code is not None:
            while process.read(0.05):
                pass
            if code != 0:
                raise LoginError("Claude Code could not complete the sign-in: " + (
                    summary(process.text(mark)) or "no reason given") + ". If the code was incomplete or expired, "
                    "start again.")
            return
        later = plain(process.text(mark)).lower()
        if any(phrase in later for phrase in ("login successful", "successfully logged in", "logged in as")):
            return  # the CLI may wait for a key press after reporting success
        if any(word in later for word in ("invalid code", "oauth error", "failed to exchange", "error:")):
            raise LoginError("Claude Code could not complete the sign-in: " + summary(process.text(mark)) +
                             ". If the code was incomplete or expired, start again.")
        _check_cancel(provider, account_id, session_id)
    # Still running after the time allowed: the verification below decides.


def _codex(process: _Process, provider, account_id, session_id) -> None:
    started = time.monotonic()
    deadline = time.monotonic() + SESSION_SECONDS
    shown = False
    checked = 0.0
    while True:
        alive = process.read(0.25)
        now = time.monotonic()
        if now - checked >= 1.0:
            checked = now
            _check_cancel(provider, account_id, session_id)
        if not shown:
            text = plain(process.text())
            link, code = find_link(provider, text), find_device_code(text)
            if link and code:
                shown = True
                _update(provider, account_id, session_id, state="waiting", url=link, user_code=code,
                        message="Open the link, sign in to ChatGPT and enter the code. This page continues on its "
                                "own once you approve.")
            elif now - started > LINK_SECONDS:
                raise LoginError("Codex did not show a sign-in code. " + (summary(process.text()) or
                                 "Check that the CLI runs on this server and can reach auth.openai.com."))
        code = process.exited()
        if code is not None and not alive:
            if code != 0:
                raise LoginError("Codex did not complete the sign-in. " + (summary(process.text()) or
                                 "Start it again."))
            if not shown:
                raise LoginError("Codex stopped before showing a sign-in code. " + summary(process.text()))
            return
        if now > deadline:
            raise LoginError("The sign-in code expired. Start it again.")


def _settle(adapter, provider, account_id, name) -> None:
    """Forget what this process knew about the profile and let the pool look again."""
    from bananachat.services import subscription_pool

    forget = getattr(adapter, "_forget_profile", None)
    if callable(forget):
        forget(name, identity=False)
    try:
        subscription_pool.forget_report(provider, account_id)
    except Exception:  # noqa: BLE001 - only cached state
        log.debug("Could not forget the usage report of %s account %s", provider, account_id, exc_info=True)
    subscription_pool.invalidate(provider)


def _verify(adapter, provider, account_id, name) -> dict:
    _settle(adapter, provider, account_id, name)
    result = adapter.check_profile(name)
    if not isinstance(result, dict):
        result = {}
    pool_db.clear_cooldown(account_id)
    try:
        from bananachat.services import subscription_pool

        subscription_pool.refresh_quota(provider)
    except Exception:  # noqa: BLE001 - the background probe tries again
        log.info("Refreshing %s usage after a sign-in failed", provider, exc_info=True)
    return result
