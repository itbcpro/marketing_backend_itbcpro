"""SSRF-safe outbound fetching.

Any server-side request to a URL that did not come from configuration must use ``safe_fetch``.
It resolves DNS itself, rejects private and special-purpose addresses, connects to the validated
IP (defeating DNS rebinding), re-validates every redirect hop, and caps time and size.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from core.errors import DomainError, ErrorCode

Resolver = Callable[[str, int], Iterable[str]]

METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})
MAX_REDIRECTS = 3
DEFAULT_MAX_BYTES = 2_000_000
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class UnsafeUrlError(DomainError):
    code = ErrorCode.VALIDATION_FAILED
    http_status = 422


def _default_resolver(host: str, port: int) -> Iterable[str]:
    return {str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}


def is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not (
        address.is_multicast or address.is_reserved or address.is_loopback or address.is_link_local
    )


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    url: str
    host: str
    port: int
    ip: str


def validate_outbound_url(
    url: str,
    *,
    resolver: Resolver = _default_resolver,
    allowed_hosts: frozenset[str] | None = None,
) -> ValidatedTarget:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeUrlError("only https URLs with a host are allowed")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials in URLs are not allowed")
    host = parts.hostname.lower()
    if host in METADATA_HOSTS or host.endswith((".internal", ".local", ".localhost")):
        raise UnsafeUrlError("host is not allowed")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise UnsafeUrlError("host is not on the allow-list")
    port = parts.port or 443
    try:
        addresses = sorted(resolver(host, port))
    except OSError as exc:
        raise UnsafeUrlError("host could not be resolved") from exc
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise UnsafeUrlError("host resolves to a non-public address")
    return ValidatedTarget(url=url, host=host, port=port, ip=addresses[0])


def safe_fetch(
    url: str,
    *,
    resolver: Resolver = _default_resolver,
    allowed_hosts: frozenset[str] | None = None,
    timeout: float = 10.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    headers: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    method: str = "GET",
    json_body: Any = None,
    content: bytes | None = None,
) -> httpx.Response:
    """Request ``url`` safely (GET by default). ``transport`` is for tests; production pins the
    validated IP. Redirects are only followed for GET, so a write is never replayed elsewhere."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        target = validate_outbound_url(current, resolver=resolver, allowed_hosts=allowed_hosts)
        parts = urlsplit(current)
        pinned = parts._replace(netloc=f"{target.ip}:{target.port}").geturl()
        request_headers = {
            "Host": target.host,
            "User-Agent": "itbcpro-fetch/1.0",
            **(headers or {}),
        }
        with httpx.Client(
            timeout=timeout, follow_redirects=False, transport=transport, verify=True
        ) as client:
            request = client.build_request(
                method,
                pinned if transport is None else current,
                headers=request_headers,
                json=json_body,
                content=content,
                extensions={"sni_hostname": target.host},
            )
            response = client.send(request, stream=True)
            try:
                if response.status_code in REDIRECT_STATUSES and "location" in response.headers:
                    if method != "GET":
                        raise UnsafeUrlError("redirects are not followed for writes")
                    current = urljoin(current, response.headers["location"])
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise UnsafeUrlError("response is too large")
                # The body is already decoded, so the encoding and length headers no longer apply.
                clean = httpx.Headers(response.headers)
                for stale in ("content-encoding", "content-length", "transfer-encoding"):
                    clean.pop(stale, None)
                return httpx.Response(response.status_code, headers=clean, content=bytes(body))
            finally:
                response.close()
    raise UnsafeUrlError("too many redirects")
