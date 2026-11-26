"""Audit log viewer, export and integrity endpoints (workspace scoped, VIEW_AUDIT only)."""

import csv
import io
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from django.http import HttpRequest, StreamingHttpResponse
from ninja import Query, Router, Schema

from audit import services
from audit.models import AuditEvent
from core.api.filtering import FilterSpec, apply_filters
from core.api.pagination import DEFAULT_LIMIT, Page, paginate
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["audit"], auth=api_auth)

ALLOWED_FILTERS = {
    "action": FilterSpec("action"),
    "target_type": FilterSpec("target_type"),
    "target_id": FilterSpec("target_id"),
    "actor_type": FilterSpec("actor_type"),
    "actor_id": FilterSpec("actor_id"),
}
EXPORT_COLUMNS = (
    "seq",
    "occurred_at",
    "actor_type",
    "actor_id",
    "action",
    "target_type",
    "target_id",
    "request_id",
    "hash",
)


class AuditEventOut(Schema):
    id: uuid.UUID
    seq: int
    occurred_at: datetime
    actor_type: str
    actor_id: str
    action: str
    target_type: str
    target_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str


class EventFilters(Schema):
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    action: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None

    def filter_params(self) -> dict[str, str]:
        return {
            name: value for name in ALLOWED_FILTERS if (value := getattr(self, name)) is not None
        }


class IntegrityOut(Schema):
    intact: bool
    first_broken_seq: int | None


def _events_for(request: HttpRequest, filters: EventFilters) -> Any:
    membership = resolve_workspace(request, Permission.VIEW_AUDIT)
    queryset = AuditEvent.objects.filter(chain=membership.workspace_id)
    return apply_filters(queryset, filters.filter_params(), ALLOWED_FILTERS)


@router.get("/events", response=Page[AuditEventOut], operation_id="audit_events")
def list_events(request: HttpRequest, filters: Query[EventFilters]) -> Page[AuditEventOut]:
    rows, next_cursor = paginate(
        _events_for(request, filters), cursor=filters.cursor, limit=filters.limit
    )
    return Page[AuditEventOut](
        items=[AuditEventOut.model_validate(row, from_attributes=True) for row in rows],
        next_cursor=next_cursor,
    )


def _csv_rows(events: Any) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    yield buffer.getvalue()
    for event in events.order_by("seq").iterator(chunk_size=500):
        buffer.seek(0)
        buffer.truncate()
        writer.writerow([_cell(getattr(event, column)) for column in EXPORT_COLUMNS])
        yield buffer.getvalue()


def _cell(value: object) -> str:
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    # Neutralise spreadsheet formula injection in exported cells.
    return f"'{text}" if text[:1] in {"=", "+", "-", "@"} else text


@router.get("/events/export", operation_id="audit_export")
def export_events(request: HttpRequest, filters: Query[EventFilters]) -> StreamingHttpResponse:
    response = StreamingHttpResponse(
        _csv_rows(_events_for(request, filters)), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = 'attachment; filename="audit-events.csv"'
    return response


@router.get("/integrity", response=IntegrityOut, operation_id="audit_integrity")
def integrity(request: HttpRequest) -> IntegrityOut:
    membership = resolve_workspace(request, Permission.VIEW_AUDIT)
    broken = services.verify_chain(membership.workspace_id)
    return IntegrityOut(intact=broken is None, first_broken_seq=broken)
