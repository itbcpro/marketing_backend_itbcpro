"""The workspace's plan and how much of it has been used."""

from __future__ import annotations

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.plans import onboarding, services
from crm.workflows import definitions
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-plans"], auth=api_auth)


class PlanLimitOut(Schema):
    key: str
    limit: int | None
    used: int


class PlanOut(Schema):
    plan_key: str
    plan_name: str
    price_micros: int
    limits: list[PlanLimitOut]


class OnboardingStepOut(Schema):
    key: str
    done: bool
    path: str


@router.get("/onboarding", response=list[OnboardingStepOut], operation_id="crm_onboarding")
def onboarding_steps(request: HttpRequest) -> list[OnboardingStepOut]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    return [
        OnboardingStepOut(key=s.key, done=s.done, path=s.path)
        for s in onboarding.checklist(m.workspace_id)
    ]


@router.get("/plan", response=PlanOut, operation_id="crm_plan")
def plan(request: HttpRequest) -> dict[str, object]:
    m = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = {
        services.MESSAGES: inbox.outbound_count_since(m.workspace_id, month_start),
        services.CONTACTS: contacts.count_active(m.workspace_id),
        services.WORKFLOWS: definitions.count_published(m.workspace_id),
    }
    return services.summary(m.workspace_id, used)
