from bananachat.app import _trust_local_proxy
from werkzeug.middleware.proxy_fix import ProxyFix


def _remote_addr_seen(peer):
    def app(environ, start_response):
        start_response("200 OK", [])
        return [environ["REMOTE_ADDR"].encode()]

    wrapped = _trust_local_proxy(ProxyFix(app, x_for=1), app)
    environ = {"REMOTE_ADDR": peer, "HTTP_X_FORWARDED_FOR": "203.0.113.9"}
    return b"".join(wrapped(environ, lambda status, headers: None)).decode()


def test_forwarded_address_is_honoured_from_a_loopback_proxy():
    assert _remote_addr_seen("127.0.0.1") == "203.0.113.9"
    assert _remote_addr_seen("::1") == "203.0.113.9"


def test_forwarded_address_is_ignored_from_any_other_peer():
    assert _remote_addr_seen("198.51.100.4") == "198.51.100.4"
    assert _remote_addr_seen("") == ""
