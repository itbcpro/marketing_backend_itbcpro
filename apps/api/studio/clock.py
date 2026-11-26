"""Time zone of a workspace. All studio times are stored in UTC and shown in this zone."""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workspaces.models import Workspace


def zone_of(workspace_id: uuid.UUID) -> ZoneInfo:
    name = Workspace.objects.filter(pk=workspace_id).values_list("time_zone", flat=True).first()
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def local_now(workspace_id: uuid.UUID, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(zone_of(workspace_id))
