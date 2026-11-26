"""Cursor pagination over a time-ordered UUID primary key."""

import base64
import binascii
import uuid

from django.db.models import Model, QuerySet
from ninja import Schema

from core.errors import ValidationFailedError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class Page[T](Schema):
    items: list[T]
    next_cursor: str | None


def encode_cursor(value: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode().rstrip("=")


def decode_cursor(cursor: str) -> uuid.UUID:
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailedError("invalid cursor") from exc


def paginate[M: Model](
    queryset: QuerySet[M], *, cursor: str | None, limit: int = DEFAULT_LIMIT
) -> tuple[list[M], str | None]:
    """Return one page ordered by id (UUIDv7 = creation order) and the next cursor."""
    limit = max(1, min(limit, MAX_LIMIT))
    ordered = queryset.order_by("pk")
    if cursor:
        ordered = ordered.filter(pk__gt=decode_cursor(cursor))
    rows = list(ordered[: limit + 1])
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_cursor(rows[-1].pk)
    return rows, None
