"""Password validators."""

import hashlib

import httpx
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

RANGE_URL = "https://api.pwnedpasswords.com/range/"
TIMEOUT_SECONDS = 2.0


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS, headers={"Add-Padding": "true"})


def is_breached(password: str, *, client: httpx.Client | None = None) -> bool:
    """k-anonymity lookup: only the first five hex characters of the SHA-1 leave the process.

    Fails open (returns False) when the service is unreachable so sign-up is never blocked by an
    outage; the length and common-password validators still apply.
    """
    digest = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    owned = client is None
    http = client or _client()
    try:
        response = http.get(f"{RANGE_URL}{prefix}")
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    finally:
        if owned:
            http.close()
    for line in response.text.splitlines():
        candidate, _sep, count = line.partition(":")
        if candidate.strip() == suffix and int(count.strip() or 0) > 0:
            return True
    return False


class BreachedPasswordValidator:
    def validate(self, password: str, user: object = None) -> None:
        if is_breached(password):
            raise ValidationError(
                _("This password has appeared in a known data breach."), code="password_breached"
            )

    def get_help_text(self) -> str:
        return _("Your password must not appear in known data breaches.")
