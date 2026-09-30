"""Public Git repositories as source archives: strict parsing and an SSRF-safe download.

A person types a repository address (``https://github.com/owner/repo``) and
optionally a branch, tag or commit. The address is **parsed, never fetched**:
:func:`parse` accepts only an allowlisted host and owner/repository/ref parts
that match strict patterns, and :meth:`Source.archive` builds the provider's
archive URL from those parts:

* GitHub:  ``https://codeload.github.com/<owner>/<repo>/tar.gz/<ref>``
* GitLab:  ``https://<host>/<namespace>/<repo>/-/archive/<ref>/<repo>-<ref>.tar.gz``
* Gitea/Forgejo (Codeberg): ``https://<host>/<owner>/<repo>/archive/<ref>.tar.gz``

:func:`download` fetches that archive over HTTPS only, with no credentials:

* the host name is resolved once per hop and **every** address must be public
  (no private, loopback, link-local, multicast, reserved, shared or unspecified
  address, including IPv4 addresses embedded in IPv6); the connection goes to
  an address that was checked (no second resolution), and TLS is verified for
  the host name;
* redirects are followed only to allowlisted hosts (checked again, with a new
  resolution, at each hop; at most :data:`MAX_REDIRECTS`), and a redirect to a
  sign-in page means the repository is private;
* timeouts: :data:`CONNECT_TIMEOUT` to connect (and resolve), :data:`TOTAL_TIMEOUT`
  for the whole download;
* the body is streamed into a new mode-0600 file and the size cap is enforced
  while reading (and against ``Content-Length`` first); it must be gzip.

The web server never runs ``git`` and never unpacks the archive: the sandbox
runner validates and extracts it inside the container.

An outbound proxy is used only when the operator opts in with
``BC_AGENTS_GIT_PROXY=1`` (then ``HTTPS_PROXY`` is used, ``http://`` proxies
only). Even then the host is resolved and checked here and the proxy is asked
to ``CONNECT`` to the checked address, so the proxy cannot be used to reach
private addresses either.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from bananachat.services.upstream import Cancelled

PROVIDERS = ("github", "gitlab", "gitea")
KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "gitea"}
DEFAULT_HOSTS = ("github.com", "gitlab.com", "codeberg.org")
ARCHIVE_HOSTS = {"github.com": ("github.com", "codeload.github.com")}
MAX_HOSTS = 20
CONNECT_TIMEOUT = 10.0
TOTAL_TIMEOUT = 120.0
READ_TIMEOUT = 30.0
MAX_REDIRECTS = 3
MAX_ADDRESSES = 4
CHUNK = 64 * 1024
MAX_URL = 300
USER_AGENT = "BananaChat-agent-import/1"

_HOSTNAME_RE = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_OWNER_RE = {
    "github": re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"),
    "gitlab": re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9_])?"),
    "gitea": re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,38}[A-Za-z0-9_])?"),
}
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_REF_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}")
# Where GitLab and Gitea send anonymous visitors of private or missing repositories.
_SIGN_IN_RE = re.compile(r"/(?:users/sign_in|user/login|login|session|signin)(?:/|$)", re.IGNORECASE)
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


class SourceError(ValueError):
    """The address or ref was refused. ``key`` is an i18n key, ``params`` its values."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params


class FetchError(RuntimeError):
    """The download failed. ``key`` is an i18n key, ``params`` its values."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params


# ----- allowed hosts ------------------------------------------------------------------

def parse_host_entry(entry: str) -> tuple[str, str]:
    """``github.com`` (a known host) or ``gitlab:git.example.org``/``gitea:git.example.org`` -> (host, provider)."""
    if not isinstance(entry, str):
        raise ValueError("not a string")
    text = entry.strip().lower()
    provider = None
    if ":" in text:
        provider, _, text = text.partition(":")
        if provider not in ("gitlab", "gitea"):
            raise ValueError("unknown provider")
    if not _HOSTNAME_RE.fullmatch(text):
        raise ValueError("invalid host name")
    known = KNOWN_HOSTS.get(text)
    if provider is None:
        if known is None:
            raise ValueError("a self-hosted server needs a provider prefix (gitlab: or gitea:)")
        provider = known
    elif known is not None and known != provider:
        raise ValueError("wrong provider for a known host")
    return text, provider


def canonical_entry(host: str, provider: str) -> str:
    return host if KNOWN_HOSTS.get(host) == provider else f"{provider}:{host}"


def normalise_hosts(values) -> tuple[str, ...]:
    """Canonical, de-duplicated host entries; invalid ones are dropped (at most :data:`MAX_HOSTS`)."""
    if not isinstance(values, (list, tuple)):
        return DEFAULT_HOSTS
    result: list[str] = []
    seen: set[str] = set()
    for value in values[:100]:
        try:
            host, provider = parse_host_entry(value)
        except ValueError:
            continue
        if host not in seen:
            seen.add(host)
            result.append(canonical_entry(host, provider))
        if len(result) >= MAX_HOSTS:
            break
    return tuple(result)


def host_providers(entries) -> dict[str, str]:
    result = {}
    for entry in entries or ():
        try:
            host, provider = parse_host_entry(entry)
        except ValueError:
            continue
        result[host] = provider
    return result


def download_hosts(entries) -> frozenset[str]:
    """Hosts a download (and its redirects) may connect to: the allowed ones and their archive hosts."""
    hosts: set[str] = set()
    for host in host_providers(entries):
        hosts.update(ARCHIVE_HOSTS.get(host, (host,)))
    return frozenset(hosts)


def host_names(entries) -> str:
    return ", ".join(host_providers(entries)) or "-"


# ----- sources ------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    host: str
    provider: str
    owner: str  # GitLab: the namespace, which may contain "/"
    repo: str
    ref: str

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.repo}@{self.ref}"

    @property
    def directory(self) -> str:
        """The folder under /workspace the repository is imported into."""
        return self.repo

    def archive(self) -> tuple[str, str]:
        """``(host, path)`` of the source archive, built from validated parts only."""
        if self.provider == "github":
            return "codeload.github.com", f"/{self.owner}/{self.repo}/tar.gz/{self.ref}"
        if self.provider == "gitlab":
            name = f"{self.repo}-{self.ref.replace('/', '-')}"
            return self.host, f"/{self.owner}/{self.repo}/-/archive/{self.ref}/{name}.tar.gz"
        return self.host, f"/{self.owner}/{self.repo}/archive/{self.ref}.tar.gz"

    def to_dict(self) -> dict:
        return {"host": self.host, "provider": self.provider, "owner": self.owner, "repo": self.repo,
                "ref": self.ref}

    @classmethod
    def from_dict(cls, data) -> "Source":
        """A stored source, validated again (raises :class:`SourceError`)."""
        if not isinstance(data, dict):
            raise SourceError("agents.import_bad_url")
        values = [data.get(name) for name in ("host", "provider", "owner", "repo", "ref")]
        if not all(isinstance(value, str) for value in values):
            raise SourceError("agents.import_bad_url")
        host, provider, owner, repo, ref = values
        if provider not in PROVIDERS or not _HOSTNAME_RE.fullmatch(host):
            raise SourceError("agents.import_bad_url")
        _check_path(provider, owner.split("/") + [repo])
        return cls(host, provider, owner, repo, check_ref(ref))


def _check_segment(pattern, value: str) -> bool:
    return bool(pattern.fullmatch(value)) and ".." not in value


def _check_path(provider: str, segments: list[str]) -> None:
    owners, repo = segments[:-1], segments[-1] if segments else ""
    count = len(owners)
    if count < 1 or (provider != "gitlab" and count != 1) or count > 4:
        raise SourceError("agents.import_bad_url")
    if not all(_check_segment(_OWNER_RE[provider], owner) for owner in owners):
        raise SourceError("agents.import_bad_url")
    if (not _check_segment(_REPO_RE, repo) or repo.startswith(("-", ".")) or repo.lower().endswith(".git")
            or repo.lower().startswith(".bananachat")):
        raise SourceError("agents.import_bad_url")


def check_ref(ref) -> str:
    """A branch, tag or commit: a conservative subset of Git's ref-name rules. Empty means ``HEAD``."""
    if ref is None:
        return "HEAD"
    if not isinstance(ref, str):
        raise SourceError("agents.import_bad_ref")
    ref = ref.strip()
    if not ref:
        return "HEAD"
    if (not _REF_RE.fullmatch(ref) or ".." in ref or "//" in ref or "/." in ref or ref.endswith(("/", "."))
            or ref.endswith(".lock") or ".lock/" in ref):
        raise SourceError("agents.import_bad_ref")
    return ref


def parse(url, ref, hosts) -> Source:
    """Parse ``https://host/owner/repo[.git]`` (``host/owner/repo`` also works) for an allowlisted host.

    Raises :class:`SourceError`. Anything unusual is refused rather than repaired: other schemes,
    credentials, ports, queries, fragments, percent-escapes, spaces, non-ASCII and extra path parts.
    """
    if not isinstance(url, str):
        raise SourceError("agents.import_bad_url")
    text = url.strip()
    if not text or len(text) > MAX_URL or any(ord(char) < 33 or ord(char) > 126 for char in text):
        raise SourceError("agents.import_bad_url")
    if "://" in text:
        scheme, _, rest = text.partition("://")
        if scheme.lower() != "https":
            raise SourceError("agents.import_https_only")
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)", text) or "@" in text.split("/", 1)[0]:
        raise SourceError("agents.import_https_only")  # git@host:owner/repo, ssh:..., file:...
    else:
        rest = text
    if any(char in rest for char in "?#%@\\;,\"'<>`{}|^[]"):
        raise SourceError("agents.import_bad_url")
    authority, _, path = rest.partition("/")
    host = authority.lower()
    if host.endswith(":443"):
        host = host[:-4]
    if not _HOSTNAME_RE.fullmatch(host):
        raise SourceError("agents.import_bad_url")
    providers = host_providers(hosts)
    if host not in providers:
        raise SourceError("agents.import_host_not_allowed", host=host, hosts=host_names(hosts))
    provider = providers[host]
    path = path[:-1] if path.endswith("/") else path
    segments = path.split("/") if path else []
    if not segments or any(not segment for segment in segments):
        raise SourceError("agents.import_bad_url")
    if segments[-1].lower().endswith(".git"):
        segments[-1] = segments[-1][:-4]
    _check_path(provider, segments)
    return Source(host, provider, "/".join(segments[:-1]), segments[-1], check_ref(ref))


# ----- network safety -----------------------------------------------------------------

_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def is_public_address(value: str) -> bool:
    """True only for globally routable unicast addresses.

    IPv6 addresses that embed an IPv4 address (mapped, 6to4, Teredo, NAT64,
    IPv4-compatible) are always refused: whether Python calls them global
    changed between patch releases, and a direct IPv4 connection is always
    available for a public host anyway.
    """
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and (address.ipv4_mapped is not None or address.sixtofour is not None
                                 or address.teredo is not None or any(address in net for net in _NAT64)
                                 or address in _IPV4_COMPATIBLE):
        return False
    if (not address.is_global or address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified):
        return False
    if address.version == 4 and address in _SHARED_ADDRESS_SPACE:
        return False
    return True


def _resolve(host: str) -> list[str]:
    """The host's addresses. ``getaddrinfo`` runs in a helper thread so it cannot outlast the connect timeout."""
    outcome: dict = {}

    def work():
        try:
            outcome["value"] = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError) as error:
            outcome["error"] = error

    worker = threading.Thread(target=work, name="agent-import-dns", daemon=True)
    worker.start()
    worker.join(CONNECT_TIMEOUT)
    if worker.is_alive() or "error" in outcome:
        raise FetchError("agents.import_network")
    addresses: list[str] = []
    for family, _, _, _, sockaddr in outcome.get("value") or ():
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr[0] not in addresses:
            addresses.append(str(sockaddr[0]))
    return addresses


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((address, port), timeout=timeout)


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    authorization: str | None = None


def _proxy() -> Proxy | None:
    """The operator's opt-in proxy (``BC_AGENTS_GIT_PROXY=1`` + ``HTTPS_PROXY``), else None."""
    if (os.environ.get("BC_AGENTS_GIT_PROXY") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    raw = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port or 8080
    except ValueError:
        raise FetchError("agents.import_network") from None
    if parts.scheme != "http" or not parts.hostname:
        raise FetchError("agents.import_network")
    authorization = None
    if parts.username is not None:
        credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        authorization = "Basic " + base64.b64encode(credentials).decode("ascii")
    return Proxy(parts.hostname, port, authorization)


def _tunnel(proxy: Proxy, address: str, timeout: float) -> socket.socket:
    """A CONNECT tunnel through the operator's proxy to an address that was already checked."""
    sock = _connect(proxy.host, proxy.port, timeout)
    try:
        target = f"[{address}]:443" if ":" in address else f"{address}:443"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}", f"User-Agent: {USER_AGENT}"]
        if proxy.authorization:
            lines.append(f"Proxy-Authorization: {proxy.authorization}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        answer = b""
        while b"\r\n\r\n" not in answer:
            chunk = sock.recv(4096)
            if not chunk or len(answer) > 16384:
                raise OSError("the proxy closed the tunnel")
            answer += chunk
        if not re.match(rb"HTTP/1\.[01] 200[ \r]", answer):
            raise OSError("the proxy refused the tunnel")
        return sock
    except BaseException:
        sock.close()
        raise


# ----- download -----------------------------------------------------------------------------

def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchError("agents.import_timeout")
    return left


class _Hop:
    """One HTTPS request to a checked address; :meth:`close` closes the response and the socket."""

    def __init__(self, host: str, path: str, deadline: float, cancel=None):
        self.response = None
        self._cancel = cancel
        self._sock = None
        addresses = _resolve(host)
        if not addresses:
            raise FetchError("agents.import_network")
        if not all(is_public_address(address) for address in addresses):
            raise FetchError("agents.import_blocked_address", host=host)
        proxy = _proxy()
        raw = None
        for address in addresses[:MAX_ADDRESSES]:
            timeout = min(CONNECT_TIMEOUT, _remaining(deadline))
            try:
                raw = _tunnel(proxy, address, timeout) if proxy else _connect(address, 443, timeout)
                break
            except OSError:
                continue
        if raw is None:
            raise FetchError("agents.import_network")
        self._sock = raw
        if cancel is not None:
            cancel.on_cancel(self.abort)
        try:
            raw.settimeout(min(CONNECT_TIMEOUT, _remaining(deadline)))
            tls = _ssl_context().wrap_socket(raw, server_hostname=host)
        except ssl.SSLError:
            self.close()
            if cancel is not None and cancel.cancelled:
                raise Cancelled() from None
            raise FetchError("agents.import_tls", host=host) from None
        except OSError:
            self.close()
            if cancel is not None and cancel.cancelled:
                raise Cancelled() from None
            raise FetchError("agents.import_network") from None
        except FetchError:
            self.close()
            raise
        self._sock = tls
        # The request is written by hand on the socket connected to the checked address (http.client would
        # connect by name, and closes the socket it hands to a "Connection: close" response). The path is
        # built from validated parts or a checked redirect: printable ASCII only, so it cannot inject headers.
        request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {USER_AGENT}\r\n"
                   "Accept: application/gzip, application/x-gzip, application/octet-stream, */*;q=0.1\r\n"
                   "Accept-Encoding: identity\r\nConnection: close\r\n\r\n")
        try:
            tls.settimeout(min(READ_TIMEOUT, _remaining(deadline)))
            tls.sendall(request.encode("ascii"))
            self.response = http.client.HTTPResponse(tls, method="GET")
            self.response.begin()
        except TimeoutError:
            self.close()
            raise FetchError("agents.import_timeout") from None
        except (OSError, http.client.HTTPException):
            self.close()
            if cancel is not None and cancel.cancelled:
                raise Cancelled() from None
            raise FetchError("agents.import_network") from None
        except FetchError:
            self.close()
            raise

    def settimeout(self, seconds: float) -> None:
        self._sock.settimeout(seconds)

    def abort(self) -> None:
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        if self._cancel is not None:
            self._cancel.remove(self.abort)
        if self.response is not None:
            self.response.close()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


def _redirect(host: str, path: str, location: str | None, allowed: frozenset[str]) -> tuple[str, str]:
    location = (location or "").strip()
    if not location or len(location) > 2048 or any(ord(char) < 33 or ord(char) > 126 for char in location):
        raise FetchError("agents.import_redirect_blocked")
    parts = urlsplit(urljoin(f"https://{host}{path}", location))
    try:
        port = parts.port
    except ValueError:
        raise FetchError("agents.import_redirect_blocked") from None
    target = (parts.hostname or "").lower()
    if (parts.scheme != "https" or port not in (None, 443) or parts.username is not None
            or parts.password is not None or target not in allowed):
        raise FetchError("agents.import_redirect_blocked")
    if _SIGN_IN_RE.search(parts.path):
        raise FetchError("agents.import_private")
    new_path = parts.path or "/"
    if parts.query:
        new_path += "?" + parts.query
    return target, new_path


def _status_error(status: int) -> FetchError:
    if status in (404, 410):
        return FetchError("agents.import_not_found")
    if status in (401, 403):
        return FetchError("agents.import_private")
    if status == 429:
        return FetchError("agents.import_rate_limited")
    return FetchError("agents.import_upstream", status=status)


def _save(hop: _Hop, destination: Path, max_bytes: int, deadline: float, cancel) -> int:
    response = hop.response
    content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type in ("text/html", "application/json", "text/plain", "application/xhtml+xml"):
        raise FetchError("agents.import_not_archive")
    size_mb = max(1, max_bytes // (1024 * 1024))
    declared = (response.getheader("Content-Length") or "").strip()
    if declared.isdigit() and int(declared) > max_bytes:
        raise FetchError("agents.import_too_large", size=size_mb)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    total = 0
    head = b""
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                if cancel is not None and cancel.cancelled:
                    raise Cancelled()
                hop.settimeout(min(READ_TIMEOUT, _remaining(deadline)))
                try:
                    chunk = response.read(CHUNK)
                except TimeoutError:
                    raise FetchError("agents.import_timeout") from None
                except (OSError, http.client.HTTPException):
                    if cancel is not None and cancel.cancelled:
                        raise Cancelled() from None
                    raise FetchError("agents.import_network") from None
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("agents.import_too_large", size=size_mb)
                if len(head) < 2:
                    head += chunk[:2 - len(head)]
                handle.write(chunk)
        if total < 2 or head != b"\x1f\x8b":
            raise FetchError("agents.import_not_archive")
    except BaseException:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise
    return total


def download(source: Source, destination: Path, *, max_bytes: int, hosts, cancel=None) -> int:
    """Download *source*'s archive into *destination* (created, must not exist). Returns its size.

    Raises :class:`FetchError` (or ``Cancelled`` when *cancel* fires); nothing is left behind on failure.
    """
    allowed = download_hosts(hosts)
    if source.host not in host_providers(hosts):
        raise FetchError("agents.import_host_not_allowed", host=source.host, hosts=host_names(hosts))
    deadline = time.monotonic() + TOTAL_TIMEOUT
    host, path = source.archive()
    if host not in allowed:
        raise FetchError("agents.import_host_not_allowed", host=host, hosts=host_names(hosts))
    for _ in range(MAX_REDIRECTS + 1):
        if cancel is not None and cancel.cancelled:
            raise Cancelled()
        hop = _Hop(host, path, deadline, cancel)
        try:
            status = hop.response.status
            if status in (301, 302, 303, 307, 308):
                host, path = _redirect(host, path, hop.response.getheader("Location"), allowed)
                continue
            if status != 200:
                raise _status_error(status)
            return _save(hop, destination, max_bytes, deadline, cancel)
        finally:
            hop.close()
    raise FetchError("agents.import_redirects")
