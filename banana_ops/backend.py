"""BananaChat: connect a web server to its compute server, and use an existing Ollama.

The compute server prints one pairing code (``bcpair1.`` followed by
base64url JSON with the gateway URL and its token). The web server takes that
code at installation (``install --mode web --pair``) or later
(``backend connect``), tests the connection and stores both values in its
private ``config/app.env``. Only BananaChat uses this module; it is shared with
the other products like the rest of ``banana_ops``.
"""

import base64
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
from urllib.parse import urlsplit

from .files import atomic_write, maintenance_lock, read_environment, read_json, write_environment
from . import profile

PAIRING_PREFIX = "bcpair1."
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 4096
MAX_CODE_LENGTH = 16384


def backend_url(value):
    """The web server's compute address: HTTPS, or a loopback SSH-tunnel port."""
    try:
        parts = urlsplit(value or "")
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        parts = None
    if (parts is None or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in profile.LOOPBACK_HOSTS))):
        raise ValueError("Use the compute server's HTTPS address (https://compute.example.org) or a loopback SSH-tunnel "
                         "address (http://127.0.0.1:11435), without credentials; the token is stored separately.")
    return value.rstrip("/")


def ollama_url(value):
    """An Ollama that already runs on this machine: loopback plain HTTP only (Ollama has no authentication)."""
    if not profile.loopback_http(value):
        raise ValueError("--ollama-url must be the loopback address of an Ollama on this machine, such as "
                         "http://127.0.0.1:11434; Ollama has no authentication, so it is never used over the network.")
    return value.rstrip("/")


def check_token(token):
    token = (token or "").strip()
    if (not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH or not token.isascii() or not token.isprintable()
            or any(c.isspace() for c in token)):
        raise ValueError(f"The compute token must be {MIN_TOKEN_LENGTH} to {MAX_TOKEN_LENGTH} printable ASCII "
                         "characters without spaces.")
    return token


def fingerprint(token):
    """A short, non-secret identifier to compare the token on both servers."""
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:16]


def encode_pairing(url, token):
    payload = json.dumps({"v": 1, "url": backend_url(url), "token": check_token(token)}, separators=(",", ":"))
    return PAIRING_PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_pairing(code):
    """Return ``(url, token)`` from a pairing code; line breaks from copying are ignored."""
    code = "".join((code or "").split())
    if not code.startswith(PAIRING_PREFIX) or len(code) > MAX_CODE_LENGTH:
        raise ValueError("This is not a BananaChat pairing code. On the compute server run 'bananachat compute "
                         "pairing-code' and copy the whole line starting with " + PAIRING_PREFIX)
    body = code[len(PAIRING_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode())
    except (ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("url"), str) \
            or not isinstance(data.get("token"), str):
        raise ValueError("The pairing code is incomplete or damaged; copy it again from the compute server.")
    return backend_url(data["url"]), check_token(data["token"])


def read_pairing(code=None, file=None):
    """The pairing code from a private file, the command line, or a hidden prompt (``code`` "-" or None)."""
    if file is not None:
        path = Path(file)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CODE_LENGTH:
            raise ValueError("Use a regular pairing-code file.")
        code = path.read_text()
    elif code in (None, "-"):
        try:
            code = getpass.getpass("Pairing code from the compute server (input hidden): ")
        except EOFError:
            raise ValueError("No pairing code was entered; use --pair-file FILE when there is no terminal.") from None
    return decode_pairing(code)


def confirm_address(url, assume_yes=False):
    """Show the gateway address named by a pairing code and ask before its token is sent there.

    A copied code could have been altered on the way; the operator recognises
    the compute server's address. ``--yes`` confirms without a terminal.
    """
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise ValueError(f"The pairing code names the compute gateway {url}. There is no terminal to confirm it: "
                         "check the address and add --yes.")
    try:
        answer = input(f"The pairing code names the compute gateway {url}. Send its token there? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        raise ValueError("Connection cancelled; nothing was sent or changed.")


def _get(url, path, token=None, timeout=10.0):
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout,
                                                 context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    headers = {"Accept": "application/json", "Connection": "close", "User-Agent": "BananaChat-lifecycle"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request("GET", parts.path.rstrip("/") + path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(65536)
    finally:
        connection.close()


def _version(body):
    try:
        value = json.loads(body)
    except ValueError:
        return None
    return str(value.get("version") or "") if isinstance(value, dict) else None


def check_backend(url, token, timeout=10.0):
    """Ask the compute gateway for Ollama's version with the token, as the web server will.

    Returns the version; raises ValueError with what to do about a failure.
    """
    try:
        status, body = _get(url, "/api/version", token, timeout)
    except ssl.SSLError as error:
        raise ValueError(f"The HTTPS certificate of {url} was not accepted ({getattr(error, 'verify_message', '') or error}). "
                         "Run 'bananachat proxy --install' on the compute server and check that its DNS name points there.") from None
    except (OSError, http.client.HTTPException) as error:
        raise ValueError(f"The compute server at {url} cannot be reached ({error.__class__.__name__}: {error}). "
                         "Check its DNS name, that ports 80/443 are open there, or that the SSH tunnel is running.") from None
    if status in (401, 403):
        raise ValueError("The compute server rejected the token. Print a fresh pairing code there with "
                         "'bananachat compute pairing-code' and connect again.")
    if 300 <= status < 400:
        raise ValueError(f"{url} redirects elsewhere; use the compute server's final HTTPS address.")
    if status in (502, 503, 504):
        raise ValueError(f"The compute gateway answered, but Ollama there is not ready or maintenance is running "
                         f"(HTTP {status}). Check 'bananachat status' on the compute server.")
    version = _version(body) if status == 200 else None
    if version is None:
        raise ValueError(f"{url} answered HTTP {status}, but it is not a BananaChat compute gateway or an Ollama server.")
    return version


def check_ollama(url, timeout=5.0):
    """The version of an existing Ollama on this machine; raises ValueError when none answers."""
    try:
        status, body = _get(url, "/api/version", timeout=timeout)
    except (OSError, http.client.HTTPException) as error:
        status, body = None, str(error)
    version = _version(body) if status == 200 else None
    if version is None:
        raise ValueError(f"No Ollama answers at {url}/api/version. Start the existing Ollama service first, "
                         "or leave out --ollama-url to let BananaChat run its own.")
    return version


# ----- compute server ---------------------------------------------------------------

def token_file(manager):
    environment = read_environment(manager.config_dir / "app.env")
    return Path(environment.get("BC_COMPUTE_TOKEN_FILE") or manager.root / "data/.compute-api-token")


def gateway_url(settings):
    """Where a web server reaches this compute gateway: its HTTPS name, else the SSH-tunnel port."""
    return "https://" + settings["domain"] if settings.get("domain") else f"http://127.0.0.1:{settings['port']}"


def pairing_code(manager, url=None):
    settings = _require(manager, "compute", "Pairing codes are created on the compute server.")
    path = token_file(manager)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"The compute token file {path} is missing. Run 'restart' to create it again.")
    token = check_token(path.read_text())
    address = backend_url(url or gateway_url(settings))
    return {"pairing_code": encode_pairing(address, token), "url": address, "token_fingerprint": fingerprint(token)}


def rotate_token(manager, url=None):
    """Replace the gateway token and restart the gateway; the old token stops working immediately."""
    with maintenance_lock(manager.root):
        manager.recover()
        settings = _require(manager, "compute", "The token is rotated on the compute server.")
        # Check the address for the new pairing code first: a mistake must not
        # disconnect the web server by replacing the token without a new code.
        backend_url(url or gateway_url(settings))
        path = token_file(manager)
        if path.is_symlink():
            raise ValueError(f"The compute token file {path} must not be a symlink.")
        previous = path.stat() if path.is_file() else None
        atomic_write(path, secrets.token_hex(32) + "\n")
        if previous is not None:
            # A token file outside the data directory keeps the owner that lets the gateway read it.
            os.chown(path, previous.st_uid, previous.st_gid)
        manager.system.data_permissions(settings)
        _restart(manager, settings, [settings["service"]])
        manager.event("compute_token", "rotated")
    return pairing_code(manager, url)


# ----- web server --------------------------------------------------------------------

def connect(manager, url, token, *, check=True):
    """Store a new compute address and token on this web server, then restart it if it runs."""
    url = backend_url(url)
    token = check_token(token) if token else ""
    version = check_backend(url, token) if check else None
    with maintenance_lock(manager.root):
        manager.recover()
        settings = _require(manager, "web", "Only a web server connects to a compute server; single and compute "
                                            "servers use their own Ollama.")
        environment = read_environment(manager.config_dir / "app.env")
        previous = (manager.config_dir / "app.env").read_bytes(), dict(settings)
        environment["BC_OLLAMA_URL"] = url
        if token:
            environment["BC_OLLAMA_API_KEY"] = token
        else:
            environment.pop("BC_OLLAMA_API_KEY", None)
        try:
            write_environment(manager.config_dir / "app.env", environment)
            settings["backend_url"] = url
            manager.save(settings)
            restarted = _restart(manager, settings, list(profile.service_commands(settings)))
        except BaseException:
            # Put the previous connection back and run the service with it again.
            atomic_write(manager.config_dir / "app.env", previous[0])
            manager.save(previous[1])
            try:
                _restart(manager, previous[1], list(profile.service_commands(previous[1])))
            except (OSError, RuntimeError, ValueError) as error:
                print(f"Restarting with the previous connection failed as well: {error}", file=sys.stderr)
            raise
        manager.event("backend_connect", "complete", url=url)
    return {"outcome": "connected", "url": url, "token_fingerprint": fingerprint(token) if token else None,
            "ollama_version": version, "checked": check, "restarted": restarted}


def configured_backend(manager):
    """``(url, token)`` from this web server's app.env."""
    _require(manager, "web", "Only a web server has a compute backend; on a compute server use 'compute pairing-code'.")
    environment = read_environment(manager.config_dir / "app.env")
    url = environment.get("BC_OLLAMA_URL", "")
    if not url:
        raise ValueError("No compute server is configured. Use 'backend connect' with a pairing code.")
    # The token never travels in clear text to another machine, not even for a test.
    return backend_url(url), environment.get("BC_OLLAMA_API_KEY", "")


def test(manager):
    url, token = configured_backend(manager)
    return {"url": url, "reachable": True, "ollama_version": check_backend(url, token)}


def summary(manager, settings):
    """What 'status' shows about inference for each BananaChat role (never a secret)."""
    try:
        environment = read_environment(manager.config_dir / "app.env")
    except (OSError, ValueError):
        environment = {}
    if settings["mode"] == "web":
        token = environment.get("BC_OLLAMA_API_KEY", "")
        result = {"compute_server": environment.get("BC_OLLAMA_URL") or None,
                  "token_fingerprint": fingerprint(token) if token else None}
    else:
        managed = profile.managed_ollama(settings)
        result = {"ollama": ("managed service " + settings["service"] + "-ollama") if managed else "existing (not managed)",
                  "ollama_url": profile.ollama_upstream(settings, environment)}
        if settings["mode"] == "compute":
            result["gateway_url"] = gateway_url(settings)
            try:
                result["token_fingerprint"] = fingerprint(check_token(token_file(manager).read_text()))
            except (OSError, ValueError):
                result["token_fingerprint"] = None
    try:
        record = read_json(manager.root / "data/.model-recovery.json", None)
    except (OSError, ValueError):
        record = {"state": "invalid"}  # the web interface explains it; status must still work
    if isinstance(record, dict) and record.get("state"):
        result["model_recovery"] = record["state"]
    return result


def _require(manager, mode, message):
    settings = manager.settings()
    if manager.product != "BananaChat" or settings["mode"] != mode:
        raise ValueError(message)
    return settings


def _restart(manager, settings, services):
    """Restart services that are running, then wait for readiness; stopped ones stay stopped."""
    running = [name for name in services if manager.system.active(name)]
    if not running:
        return False
    print("Restarting " + ", ".join(running) + ".", file=sys.stderr, flush=True)
    manager.system.stop(running)
    manager.system.start(running)
    if not manager.system.healthy(settings):
        raise RuntimeError("The service failed its readiness checks after the change. Inspect its journal "
                           "(journalctl -u " + settings["service"] + ").")
    return True
