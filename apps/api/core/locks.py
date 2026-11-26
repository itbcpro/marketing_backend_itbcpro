"""Distributed locks: Redis (cross-process, TTL based) and PostgreSQL advisory (transactional)."""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager

import redis
from django.conf import settings
from django.db import connection

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class LockNotAcquiredError(RuntimeError):
    """Raised when a lock is held by someone else and the caller asked not to wait."""


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2)


@contextmanager
def redis_lock(name: str, *, ttl_seconds: int = 60) -> Iterator[None]:
    """Hold ``name`` for at most ``ttl_seconds``; fail fast if someone else holds it.

    The value is a random token so a lock that expired and was re-acquired elsewhere is never
    released by the previous owner.
    """
    client = _client()
    key = f"lock:{name}"
    token = secrets.token_hex(16)
    try:
        if not client.set(key, token, nx=True, ex=ttl_seconds):
            raise LockNotAcquiredError(name)
        try:
            yield
        finally:
            client.eval(_RELEASE_SCRIPT, 1, key, token)
    finally:
        client.close()


@contextmanager
def advisory_lock(name: str) -> Iterator[bool]:
    """Non-blocking session-level PostgreSQL advisory lock. Yields whether it was acquired."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [name])
        row = cursor.fetchone()
        acquired = bool(row and row[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [name])
