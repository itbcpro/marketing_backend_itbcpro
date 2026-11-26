"""The signed-in person's notifications and preferences."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from crm.notifications import services
from crm.notifications.models import Notification
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-notifications"], auth=api_auth)


class NotificationOut(Schema):
    id: uuid.UUID
    kind: str
    data: dict[str, str]
    read: bool
    created_at: datetime


class UnreadOut(Schema):
    unread: int


class MarkedOut(Schema):
    marked: int


class PreferenceIn(Schema):
    in_app: bool
    email_digest: bool


class PreferenceOut(PreferenceIn):
    kind: str


def _out(row: Notification) -> NotificationOut:
    return NotificationOut(
        id=row.pk,
        kind=row.kind,
        data={k: str(v) for k, v in row.data.items()},
        read=row.read_at is not None,
        created_at=row.created_at,
    )


@router.get("/notifications", response=list[NotificationOut], operation_id="crm_notifications_list")
def list_notifications(
    request: HttpRequest, unread_only: bool = False, limit: int = 50
) -> list[NotificationOut]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    rows = services.list_for(m.workspace_id, m.user_id, unread_only=unread_only, limit=limit)
    return [_out(r) for r in rows]


@router.get("/notifications/unread", response=UnreadOut, operation_id="crm_notifications_unread")
def unread(request: HttpRequest) -> UnreadOut:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    return UnreadOut(unread=services.unread_count(m.workspace_id, m.user_id))


@router.post(
    "/notifications/read-all", response=MarkedOut, operation_id="crm_notifications_read_all"
)
def read_all(request: HttpRequest) -> MarkedOut:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    return MarkedOut(marked=services.mark_all_read(m.workspace_id, m.user_id))


@router.post(
    "/notifications/{notification_id}/read",
    response={204: None},
    operation_id="crm_notifications_read",
)
def read(request: HttpRequest, notification_id: uuid.UUID) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    services.mark_read(m.workspace_id, m.user_id, notification_id)
    return Status(204, None)


@router.get(
    "/notification-preferences",
    response=list[PreferenceOut],
    operation_id="crm_notification_preferences",
)
def preferences(request: HttpRequest) -> list[PreferenceOut]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    return [
        PreferenceOut(kind=s.kind, in_app=s.in_app, email_digest=s.email_digest)
        for s in services.preferences(m.workspace_id, m.user_id)
    ]


@router.put(
    "/notification-preferences/{kind}",
    response=PreferenceOut,
    operation_id="crm_notification_preference_set",
)
def set_preference(request: HttpRequest, kind: str, payload: PreferenceIn) -> PreferenceOut:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    s = services.set_preference(
        m.workspace_id,
        m.user_id,
        kind,
        in_app=payload.in_app,
        email_digest=payload.email_digest,
    )
    return PreferenceOut(kind=s.kind, in_app=s.in_app, email_digest=s.email_digest)
