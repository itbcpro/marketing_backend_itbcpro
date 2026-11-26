"""Locale value handling."""

from typing import NewType

Locale = NewType("Locale", str)


def negotiate_locale(requested: str | None, supported: list[str], default: str) -> Locale:
    """Pick the best supported locale for a requested tag, falling back to the default."""
    if requested:
        wanted = requested.lower()
        base = wanted.split("-")[0]
        for candidate in supported:
            if candidate.lower() in {wanted, base}:
                return Locale(candidate)
    return Locale(default)
