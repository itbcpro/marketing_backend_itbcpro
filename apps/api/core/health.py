"""Readiness checks. Each dependency registers its own check (open for extension)."""

from collections.abc import Callable

import redis
from django.conf import settings
from django.db import connection

ReadinessCheck = Callable[[], None]

_checks: dict[str, ReadinessCheck] = {}


def register_check(name: str, check: ReadinessCheck) -> None:
    _checks[name] = check


def run_checks() -> dict[str, str]:
    """Run every registered check; a value of ``ok`` means healthy."""
    results: dict[str, str] = {}
    for name, check in _checks.items():
        try:
            check()
        except Exception as exc:
            results[name] = f"error: {type(exc).__name__}"
        else:
            results[name] = "ok"
    return results


def _check_database() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def _check_redis() -> None:
    client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2)
    try:
        client.ping()
    finally:
        client.close()


register_check("database", _check_database)
register_check("redis", _check_redis)
