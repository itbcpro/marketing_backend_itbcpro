"""Identifier generation."""

import uuid


def new_id() -> uuid.UUID:
    """Return a time-ordered UUIDv7, sortable by creation time."""
    return uuid.uuid7()
