"""Direct HTTP(S) with pinned DNS, bounded reads and no credential redirects."""

import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def resolve_target(url, *, public_only=True, address_policy=None):
    """Resolve once and validate every candidate before opening a connection."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or len(url) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in url)):
        raise ValueError("Use an HTTP(S) URL without credentials or a fragment")
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise urllib.error.URLError("The server has no usable address")
    for family, _, _, _, sockaddr in addresses:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise ValueError("Unsupported network address")
        address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        effective = getattr(address, "ipv4_mapped", None) or address
        if (public_only and not effective.is_global) or (address_policy is not None and not address_policy(effective)):
            raise ValueError("The server resolved to a disallowed network address")
    return parsed, host, port, addresses


def _dial(addresses, timeout, source_address=None):
    """Connect to validated numeric sockaddr values without a second DNS lookup."""
    last = None
    for family, kind, protocol, _, sockaddr in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as error:
            last = error
            connection.close()
    raise last or OSError("Connection failed")


class Response:
    """Context-managed response whose aggregate body has a fixed byte budget."""
    def __init__(self, response, connection, max_bytes, deadline):
        self._response, self._connection, self._remaining = response, connection, max_bytes
        self._deadline = deadline

    def __getattr__(self, name):
        return getattr(self._response, name)

    def _read(self, method, size):
        self._deadline.check()
        amount = self._remaining + 1 if size is None or size < 0 else min(size, self._remaining + 1)
        try:
            data = method(amount)
        except (OSError, http.client.HTTPException):
            self._deadline.check()
            raise
        self._deadline.check()
        self._remaining -= len(data)
        if self._remaining < 0:
            self.close()
            raise urllib.error.URLError("The server response exceeded its size limit")
        return data

    def read(self, size=-1):
        return self._read(self._response.read, size)

    def readline(self, size=-1):
        return self._read(self._response.readline, size)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline(128 * 1024 + 1)
        if len(line) > 128 * 1024:
            self.close()
            raise urllib.error.URLError("The server returned an oversized stream record")
        if not line:
            raise StopIteration
        return line

    def close(self):
        self._deadline.cancel()
        self._response.close()
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Deadline:
    """An idle socket timeout alone cannot stop a response that trickles bytes."""
    def __init__(self, connection, seconds):
        self.socket = connection.sock
        self.expired = threading.Event()
        self.timer = threading.Timer(seconds, self.interrupt)
        self.timer.daemon = True
        self.timer.start()

    def interrupt(self):
        self.expired.set()
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # A completed/closed response needs no interruption.

    def check(self):
        if self.expired.is_set():
            raise urllib.error.URLError("The server response exceeded its time limit")

    def cancel(self):
        self.timer.cancel()


def open_http(request, timeout=15, *, max_bytes=8 * 1024 * 1024, public_only=True, address_policy=None, total_timeout=None):
    """Open a direct request; trusted private services opt out of public-only DNS."""
    if isinstance(request, str):
        request = urllib.request.Request(request)
    parsed, host, port, addresses = resolve_target(request.full_url, public_only=public_only, address_policy=address_policy)
    total_timeout = max(60, timeout * 2) if total_timeout is None else total_timeout
    if not 0 < timeout <= 3600 or not 0 < max_bytes <= 1024 ** 3 or not 0 < total_timeout <= 43200:
        raise ValueError("Invalid HTTP timeout or response budget")
    connection = (http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
                  if parsed.scheme == "https" else http.client.HTTPConnection(host, port, timeout=timeout))
    # HTTPSConnection still uses the original hostname for SNI and certificate
    # verification. Only the underlying TCP socket uses the checked addresses.
    connection._create_connection = lambda _address, timeout, source_address=None: _dial(addresses, timeout, source_address)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    headers = {name: value for name, value in request.header_items() if name.lower() not in {"host", "connection", "proxy-authorization"}}
    headers["Connection"] = "close"
    deadline = None
    started = time.monotonic()
    try:
        connection.timeout = min(timeout, total_timeout)
        connection.connect()
        # Keep the socket itself: http.client may detach it from the connection
        # after headers while its response file is still blocked in a read.
        deadline = _Deadline(connection, max(0, total_timeout - (time.monotonic() - started)))
        connection.request(request.get_method(), target, body=request.data, headers=headers)
        response = Response(connection.getresponse(), connection, max_bytes, deadline)
        deadline.check()
    except (OSError, http.client.HTTPException) as error:
        if deadline:
            deadline.cancel()
        connection.close()
        raise urllib.error.URLError("Connection to the configured server failed") from error
    if not 200 <= response.status < 300:
        # No redirects, including cross-origin, are ever followed with credentials.
        raise urllib.error.HTTPError(request.full_url, response.status, response.reason, response.headers, response)
    return response
