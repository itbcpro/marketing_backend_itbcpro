"""Server-sent events over Redis Streams.

Every event is appended to a capped per-channel stream, so a client that reconnects with
``Last-Event-ID`` resumes exactly where it left off (within the retention window).
"""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import redis
import redis.asyncio as aioredis
from django.conf import settings

STREAM_MAXLEN = 1000
BLOCK_MILLISECONDS = 15_000
HEARTBEAT = ": ping\n\n"


def workspace_channel(workspace_id: uuid.UUID) -> str:
    return f"ws:{workspace_id}"


def _key(channel: str) -> str:
    return f"sse:{channel}"


def publish(channel: str, event_type: str, data: dict[str, Any]) -> str:
    """Append an event; returns its id (a Redis stream id such as ``1700000000000-0``)."""
    client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2)
    try:
        entry_id = client.xadd(
            _key(channel),
            {"type": event_type, "data": json.dumps(data, default=str)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    finally:
        client.close()
    return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)


def format_event(entry_id: str, event_type: str, payload: str) -> str:
    return f"id: {entry_id}\nevent: {event_type}\ndata: {payload}\n\n"


def valid_last_event_id(value: str | None) -> str:
    """Accept only well-formed stream ids; anything else means "from now"."""
    if value and all(part.isdigit() for part in value.split("-", 1)) and value.count("-") <= 1:
        return value
    return "$"


async def stream(
    channel: str,
    *,
    last_event_id: str | None = None,
    block_ms: int = BLOCK_MILLISECONDS,
    should_stop: Callable[[], bool] = lambda: False,
) -> AsyncIterator[str]:
    """Yield SSE frames for ``channel`` until ``should_stop`` or the consumer goes away."""
    client = aioredis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2)
    cursor = valid_last_event_id(last_event_id)
    try:
        yield "retry: 3000\n\n"
        while not should_stop():
            batches = await client.xread({_key(channel): cursor}, block=block_ms, count=100)
            if not batches:
                yield HEARTBEAT
                continue
            for _key_name, entries in batches:
                for entry_id, fields in entries:
                    cursor = entry_id.decode()
                    yield format_event(cursor, fields[b"type"].decode(), fields[b"data"].decode())
    finally:
        await client.aclose()
