"""Notification use cases (public surface of the module)."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import QuerySet
from django.utils import timezone

from core.errors import NotFoundError, ValidationFailedError
from crm.notifications.models import Notification, NotificationKind, Preference

DEFAULT_IN_APP = True
DEFAULT_EMAIL_DIGEST = False


@dataclass(frozen=True)
class Setting:
    kind: str
    in_app: bool
    email_digest: bool


def preferences(workspace_id: uuid.UUID, user_id: uuid.UUID) -> list[Setting]:
    stored = {
        p.kind: p for p in Preference.objects.for_workspace(workspace_id).filter(user_id=user_id)
    }
    out: list[Setting] = []
    for kind in NotificationKind.values:
        row = stored.get(kind)
        out.append(
            Setting(
                kind,
                row.in_app if row else DEFAULT_IN_APP,
                row.email_digest if row else DEFAULT_EMAIL_DIGEST,
            )
        )
    return out


def set_preference(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str,
    *,
    in_app: bool,
    email_digest: bool,
) -> Setting:
    if kind not in NotificationKind.values:
        raise ValidationFailedError("invalid_kind")
    Preference.objects.update_or_create(
        workspace_id=workspace_id,
        user_id=user_id,
        kind=kind,
        defaults={"in_app": in_app, "email_digest": email_digest},
    )
    return Setting(kind, in_app, email_digest)


def notify(
    workspace_id: uuid.UUID,
    *,
    recipient: uuid.UUID,
    actor_id: str | None,
    kind: str,
    data: dict[str, Any],
) -> Notification | None:
    """Tell one person about something. Never tell people about their own actions."""
    if actor_id is not None and str(recipient) == str(actor_id):
        return None
    wanted = next(s for s in preferences(workspace_id, recipient) if s.kind == kind)
    if not wanted.in_app and not wanted.email_digest:
        return None
    return Notification.objects.create(
        workspace_id=workspace_id,
        user_id=recipient,
        kind=kind,
        data={k: str(v) for k, v in data.items()},
        # Someone who only wants email gets no bell entry: it is born read.
        read_at=None if wanted.in_app else timezone.now(),
    )


def _mine(workspace_id: uuid.UUID, user_id: uuid.UUID) -> QuerySet[Notification]:
    return Notification.objects.for_workspace(workspace_id).filter(user_id=user_id)


def list_for(
    workspace_id: uuid.UUID, user_id: uuid.UUID, *, unread_only: bool = False, limit: int = 50
) -> list[Notification]:
    rows = _mine(workspace_id, user_id)
    if unread_only:
        rows = rows.filter(read_at__isnull=True)
    return list(rows[: max(1, min(limit, 200))])


def unread_count(workspace_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return _mine(workspace_id, user_id).filter(read_at__isnull=True).count()


def mark_read(workspace_id: uuid.UUID, user_id: uuid.UUID, notification_id: uuid.UUID) -> None:
    updated = (
        _mine(workspace_id, user_id)
        .filter(pk=notification_id, read_at__isnull=True)
        .update(read_at=timezone.now())
    )
    if not updated and not _mine(workspace_id, user_id).filter(pk=notification_id).exists():
        raise NotFoundError("notification not found")


def mark_all_read(workspace_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return _mine(workspace_id, user_id).filter(read_at__isnull=True).update(read_at=timezone.now())


# ---- email digest ---------------------------------------------------------------------


@dataclass(frozen=True)
class Digest:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    counts: dict[str, int]
    notification_ids: list[uuid.UUID]


def due_digests() -> list[Digest]:
    """Unread, not yet emailed notifications of people who asked for email, old enough to batch."""
    cutoff = timezone.now() - timedelta(minutes=settings.CRM_DIGEST_MIN_AGE_MINUTES)
    wanting = {
        (p.workspace_id, p.user_id, p.kind) for p in Preference.objects.filter(email_digest=True)
    }
    pending = Notification.objects.filter(
        read_at__isnull=True, emailed_at__isnull=True, created_at__lte=cutoff
    ).order_by("created_at")
    groups: dict[tuple[uuid.UUID, uuid.UUID], list[Notification]] = {}
    for row in pending:
        if (row.workspace_id, row.user_id, row.kind) in wanting:
            groups.setdefault((row.workspace_id, row.user_id), []).append(row)
    return [
        Digest(w, u, dict(Counter(r.kind for r in rows)), [r.pk for r in rows])
        for (w, u), rows in groups.items()
    ]


def mark_emailed(workspace_id: uuid.UUID, notification_ids: list[uuid.UUID]) -> None:
    Notification.objects.for_workspace(workspace_id).filter(pk__in=notification_ids).update(
        emailed_at=timezone.now()
    )
