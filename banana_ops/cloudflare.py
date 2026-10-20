"""Cloudflare's edge address ranges, for servers behind proxied (orange-cloud) records.

The Caddy configuration trusts ``CF-Connecting-IP`` only from these peers and
serves Cloudflare "Flexible" requests instead of redirecting them in a loop;
``proxy`` also uses them to tell whether the domain is proxied (a proxied record
resolves to these addresses, not to the server's).

Standard library only: the ``banana`` controller runs with the system Python.
Source: https://www.cloudflare.com/ips-v4 and /ips-v6 (also
``https://api.cloudflare.com/client/v4/ips``); refresh the lists when
Cloudflare announces a change.
"""

import ipaddress
import socket
from functools import cache

UPDATED = "2026-09-30"

IPV4 = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
)
IPV6 = (
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
)
RANGES = IPV4 + IPV6


@cache
def networks():
    return tuple(ipaddress.ip_network(value) for value in RANGES)


def is_cloudflare_address(value):
    """Whether *value* (an IPv4 or IPv6 address) belongs to Cloudflare's edge."""
    try:
        address = ipaddress.ip_address(str(value).strip().strip("[]"))
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address.version == network.version and address in network for network in networks())


def all_cloudflare(addresses):
    """True when there is at least one address and every one is a Cloudflare edge address."""
    values = list(addresses)
    return bool(values) and all(is_cloudflare_address(value) for value in values)


def proxied(domain):
    """Whether *domain* resolves only to Cloudflare's edge; a lookup that fails counts as no."""
    try:
        found = socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    return all_cloudflare(entry[4][0] for entry in found)
