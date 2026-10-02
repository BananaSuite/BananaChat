"""Trusted gateways cannot reach metadata through IPv6 transition addresses."""

import socket

import pytest

from bananachat.services.external_api import _addresses
from bananachat.services.upstream import UpstreamError


def dns_result(address):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    destination = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    return family, socket.SOCK_STREAM, 6, '', destination


@pytest.mark.parametrize('allow_private', [False, True])
@pytest.mark.parametrize('address', [
    '2002:a9fe:a9fe::1',  # 6to4: link-local metadata.
    '2002:6464:64c8::1',  # 6to4: Alibaba metadata in shared address space.
    '2002:0808:0808::1',  # Public embedded IPv4 is refused consistently too.
    '2001:0:4136:e378:8000:63bf:5601:5601',  # Teredo client 169.254.169.254.
    '64:ff9b::a9fe:a9fe',  # Well-known NAT64.
    '64:ff9b:1::a9fe:a9fe',  # Local-use NAT64.
    '::169.254.169.254',  # Legacy IPv4-compatible.
])
def test_provider_refuses_transition_addresses(monkeypatch, allow_private, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [dns_result(address)])
    with pytest.raises(UpstreamError, match='restricted address'):
        _addresses('provider.test.invalid', 443, allow_private)


@pytest.mark.parametrize('address', ['10.20.30.40', '::ffff:10.20.30.40', 'fd12:3456:789a::40'])
def test_direct_trusted_private_provider_addresses_still_work(monkeypatch, address):
    result = dns_result(address)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [result])
    assert _addresses('provider.test.invalid', 443, True) == [result[:3] + (result[4],)]
    with pytest.raises(UpstreamError, match='restricted address'):
        _addresses('provider.test.invalid', 443, False)


@pytest.mark.parametrize('address', ['8.8.8.8', '::ffff:8.8.8.8', '2606:4700:4700::1111'])
def test_direct_public_provider_addresses_still_work(monkeypatch, address):
    result = dns_result(address)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [result])
    assert _addresses('provider.test.invalid', 443, False) == [result[:3] + (result[4],)]


def test_mixed_dns_answer_cannot_hide_a_transition_route(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        dns_result('8.8.8.8'), dns_result('2002:a9fe:a9fe::1')])
    with pytest.raises(UpstreamError, match='restricted address'):
        _addresses('provider.test.invalid', 443, True)
