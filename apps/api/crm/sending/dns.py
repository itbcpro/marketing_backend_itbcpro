"""DNS lookups behind a small port, so verification can be tested without a network."""

from __future__ import annotations

from typing import Any, Protocol

import dns.exception
import dns.resolver
from django.conf import settings


class ResolverUnavailableError(Exception):
    """DNS could not be reached or timed out. This is not the same as the record being absent."""


class DnsResolver(Protocol):
    def txt(self, name: str) -> list[str]: ...

    def cname(self, name: str) -> list[str]: ...

    def mx(self, name: str) -> list[str]: ...


def _clean(host: str) -> str:
    return host.rstrip(".").lower()


class SystemResolver:
    """Real lookups through ``dnspython`` with a hard time limit."""

    def __init__(self) -> None:
        self._resolver = dns.resolver.Resolver()
        self._resolver.lifetime = settings.CRM_DNS_TIMEOUT_SECONDS
        self._resolver.timeout = settings.CRM_DNS_TIMEOUT_SECONDS

    def _query(self, name: str, kind: str) -> list[Any]:
        try:
            return list(self._resolver.resolve(name, kind))
        except dns.resolver.NXDOMAIN, dns.resolver.NoAnswer:
            return []
        except (dns.exception.Timeout, dns.resolver.NoNameservers, OSError) as exc:
            raise ResolverUnavailableError(str(exc)) from exc

    def txt(self, name: str) -> list[str]:
        return [b"".join(r.strings).decode("utf-8", "replace") for r in self._query(name, "TXT")]

    def cname(self, name: str) -> list[str]:
        return [_clean(str(r.target)) for r in self._query(name, "CNAME")]

    def mx(self, name: str) -> list[str]:
        return [_clean(str(r.exchange)) for r in self._query(name, "MX")]
