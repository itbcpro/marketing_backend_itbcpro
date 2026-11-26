"""Tenant context: which workspace the current request or task may see (ADR 0009).

The context lives in a ``ContextVar`` and in the PostgreSQL session variable ``app.workspace_id``.
Row level security policies compare ``workspace_id`` with that variable, so an unset context
returns no rows instead of all rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.db import connection
from django.db.backends.signals import connection_created

SESSION_VARIABLE = "app.workspace_id"
POLICY_NAME = "tenant_isolation"

_current: ContextVar[uuid.UUID | None] = ContextVar("current_workspace_id", default=None)


def current_workspace_id() -> uuid.UUID | None:
    return _current.get()


def _push_to_database(workspace_id: uuid.UUID | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config(%s, %s, false)", [SESSION_VARIABLE, str(workspace_id or "")]
        )


def activate(workspace_id: uuid.UUID) -> None:
    """Enter a workspace for the rest of the request. The middleware clears it afterwards."""
    _current.set(workspace_id)
    _push_to_database(workspace_id)


def clear() -> None:
    if _current.get() is not None:
        _current.set(None)
        _push_to_database(None)


def forget() -> None:
    """Drop the in-process context without touching the database (test isolation, process start)."""
    _current.set(None)


@contextmanager
def use_workspace(workspace_id: uuid.UUID) -> Iterator[None]:
    """Run a block inside a workspace and restore the previous context afterwards."""
    previous = _current.get()
    _current.set(workspace_id)
    _push_to_database(workspace_id)
    try:
        yield
    finally:
        _current.set(previous)
        _push_to_database(previous)


@contextmanager
def system_context() -> Iterator[None]:
    """Explicit marker for audited cross-tenant jobs. It grants no rows by itself.

    Cross-tenant work must run as a database role that is allowed to bypass the policies;
    wrapping code in this manager makes every such place easy to find and review.
    """
    yield


def _restore_on_new_connection(sender: Any, connection: Any, **_: Any) -> None:
    workspace_id = _current.get()
    if workspace_id is not None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, false)", [SESSION_VARIABLE, str(workspace_id)]
            )


connection_created.connect(_restore_on_new_connection, dispatch_uid="core.tenancy.restore")
