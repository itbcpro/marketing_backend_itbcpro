"""Per-connection outbound rate limiting (token bucket in Redis) with provider feedback.

Every adapter asks ``acquire`` before calling a platform and reports throttling responses with
``penalize`` so all workers share one view of a connection's remaining budget.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Any

import redis
from django.conf import settings

_LUA_ACQUIRE = """
local key = KEYS[1]
local blocked_key = KEYS[2]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local blocked_until = tonumber(redis.call('get', blocked_key) or '0')
if blocked_until > now then
    return {0, blocked_until - now}
end

local data = redis.call('hmget', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
    tokens = capacity
    ts = now
end
tokens = math.min(capacity, tokens + (now - ts) * refill / 1000.0)
local allowed = 0
local wait_ms = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
else
    wait_ms = math.ceil((cost - tokens) * 1000.0 / refill)
end
redis.call('hset', key, 'tokens', tokens, 'ts', now)
redis.call('pexpire', key, math.ceil(capacity * 1000.0 / refill) + 60000)
return {allowed, wait_ms}
"""


@dataclass(frozen=True, slots=True)
class BucketSpec:
    """``capacity`` tokens, refilled at ``refill_per_second`` tokens per second."""

    capacity: int
    refill_per_second: float


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    retry_after_seconds: float


def _client() -> Any:
    return redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2)


def _keys(connection_id: uuid.UUID, bucket: str) -> tuple[str, str]:
    return f"rl:{connection_id}:{bucket}", f"rl:{connection_id}:{bucket}:blocked"


def acquire(
    connection_id: uuid.UUID,
    bucket: str,
    spec: BucketSpec,
    *,
    cost: int = 1,
    now_ms: int | None = None,
) -> Decision:
    key, blocked_key = _keys(connection_id, bucket)
    client = _client()
    try:
        allowed, wait_ms = client.eval(
            _LUA_ACQUIRE,
            2,
            key,
            blocked_key,
            spec.capacity,
            spec.refill_per_second,
            now_ms if now_ms is not None else int(time.time() * 1000),
            cost,
        )
    finally:
        client.close()
    return Decision(allowed=bool(allowed), retry_after_seconds=int(wait_ms) / 1000)


def penalize(
    connection_id: uuid.UUID, bucket: str, *, seconds: float, now_ms: int | None = None
) -> None:
    """Block a bucket after the provider reported throttling (for example a Retry-After header)."""
    _, blocked_key = _keys(connection_id, bucket)
    until = (now_ms if now_ms is not None else int(time.time() * 1000)) + int(seconds * 1000)
    client = _client()
    try:
        client.set(blocked_key, until, px=int(seconds * 1000) + 1000)
    finally:
        client.close()


def snapshot(connection_id: uuid.UUID, bucket: str) -> dict[str, float]:
    """Current state for dashboards."""
    key, blocked_key = _keys(connection_id, bucket)
    client = _client()
    try:
        data = client.hgetall(key)
        blocked = client.get(blocked_key)
    finally:
        client.close()
    return {
        "tokens": float(data.get(b"tokens", 0)),
        "blocked_until_ms": float(blocked or 0),
    }
