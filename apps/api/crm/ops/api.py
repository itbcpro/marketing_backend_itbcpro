"""A health summary for the workspace's own CRM, for the reports screen."""

from __future__ import annotations

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from crm.ops import health
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-ops"], auth=api_auth)


class HealthSignal(Schema):
    key: str
    value: float
    limit: float
    status: str


@router.get("/health", response=list[HealthSignal], operation_id="crm_health")
def crm_health(request: HttpRequest) -> list[HealthSignal]:
    m = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    return [HealthSignal(**s) for s in health.snapshot(m.workspace_id, timezone.now())]
