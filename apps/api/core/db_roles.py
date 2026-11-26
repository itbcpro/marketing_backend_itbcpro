"""Database roles for row level security: a runtime role that cannot bypass policies."""

from typing import Any

from django.conf import settings
from django.db import transaction
from psycopg import sql

ADVISORY_LOCK_KEY = 7_340_001
TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
SEQUENCE_PRIVILEGES = "USAGE, SELECT"


def ensure_runtime_role(cursor: Any, *, password: str | None = None) -> str:
    """Create the runtime role if missing and grant it DML on the public schema (idempotent)."""
    with transaction.atomic():
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [ADVISORY_LOCK_KEY])
        return _ensure(cursor, password)


def _ensure(cursor: Any, password: str | None) -> str:
    name = settings.DB_RUNTIME_ROLE
    role = sql.Identifier(name)
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [name])
    if cursor.fetchone() is None:
        cursor.execute(
            sql.SQL(
                "CREATE ROLE {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT"
            ).format(role)
        )
    if password:
        cursor.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD %s").format(role), [password])
    for statement in (
        "GRANT USAGE ON SCHEMA public TO {}",
        f"GRANT {TABLE_PRIVILEGES} ON ALL TABLES IN SCHEMA public TO {{}}",
        f"GRANT {SEQUENCE_PRIVILEGES} ON ALL SEQUENCES IN SCHEMA public TO {{}}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT {TABLE_PRIVILEGES} ON TABLES TO {{}}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT {SEQUENCE_PRIVILEGES} "
        "ON SEQUENCES TO {}",
    ):
        cursor.execute(sql.SQL(statement).format(role))
    return name
