"""SSRF guard for user-supplied BYOK endpoint URLs.

A model connection carries a customer-provided ``base_url`` that the SERVER then
calls — a classic SSRF surface. Validate it before storing and before every
outbound use: https-only and never pointing at loopback / private / link-local /
cloud-metadata addresses. Hostnames that resolve to such addresses are also
rejected (DNS-rebind-resistant at call time).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """Raised when a BYOK base_url is not a safe public https endpoint."""


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → treat as unsafe
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local  # 169.254.0.0/16 incl. cloud metadata
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def validate_base_url(url: str, *, resolve: bool = True) -> str:
    """Return the URL if it is a safe public https endpoint, else raise
    ``UnsafeUrlError``. Set ``resolve=False`` to skip DNS (validation-only, e.g.
    unit tests or when the network isn't reachable)."""
    if not url or not isinstance(url, str):
        raise UnsafeUrlError("base_url is required")
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        raise UnsafeUrlError("base_url must use https")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("base_url has no host")
    low = host.lower()
    if low == "localhost" or low.endswith(".localhost") or low.endswith(".internal"):
        raise UnsafeUrlError("base_url host is not allowed")
    # Literal IP hosts: block directly. (Detect IP-ness separately — UnsafeUrlError
    # subclasses ValueError, so it must NOT be raised inside an except-ValueError.)
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        if _is_blocked_ip(host):
            raise UnsafeUrlError("base_url points at a non-public address")
        return url
    if resolve:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"base_url host does not resolve: {host}") from exc
        for info in infos:
            if _is_blocked_ip(str(info[4][0])):
                raise UnsafeUrlError("base_url resolves to a non-public address")
    return url
