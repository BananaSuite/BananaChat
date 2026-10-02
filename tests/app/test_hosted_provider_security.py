"""Known cloud metadata addresses remain prohibited for trusted private providers."""
import socket

import pytest

from bananachat.services.external_api import _addresses
from bananachat.services.upstream import UpstreamError


@pytest.mark.parametrize("address", ["100.100.100.200", "fd00:ec2::254", "::ffff:100.100.100.200",
                                      "169.254.169.254", "::ffff:169.254.169.254"])
def test_private_provider_cannot_connect_to_cloud_metadata(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    destination = (address, 80, 0, 0) if family == socket.AF_INET6 else (address, 80)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(family, socket.SOCK_STREAM, 6, "", destination)])
    with pytest.raises(UpstreamError, match="restricted address"):
        _addresses("metadata.test.invalid", 80, True)


def test_private_provider_still_accepts_an_explicitly_trusted_gateway(monkeypatch):
    destination = ("10.20.30.40", 8080)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", destination)])
    assert _addresses("gateway.test.invalid", 8080, True)[0][3] == destination
