"""Brand profile, memory items and feedback."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema

from audit.actors import Actor
from core.api.filtering import FilterSpec, apply_filters
from core.api.pagination import DEFAULT_LIMIT, Page, paginate
from identity.authentication import api_auth
from llm.service import get_llm_service
from memory import reflection, services
from memory.models import FeedbackEvent, MemoryItem
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["memory"], auth=api_auth)
ITEM_FILTERS = {
    "status": FilterSpec("status"),
    "kind": FilterSpec("kind"),
    "scope": FilterSpec("scope"),
}


class ProfileIn(Schema):
    content: dict[str, Any]


class ProfileOut(Schema):
    version: int
    content: dict[str, Any]
    created_at: datetime


class ItemIn(Schema):
    text: str
    kind: str = "rule"
    scope: str = "workspace"
    scope_ref: str = ""
    is_hard_rule: bool = False
    activate: bool = True


class ItemOut(Schema):
    id: uuid.UUID
    kind: str
    scope: str
    scope_ref: str
    text: str
    status: str
    is_hard_rule: bool
    source: str
    confidence: float
    evidence: list[str]
    usage_count: int
    created_at: datetime


class ItemFilters(Schema):
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    status: str | None = None
    kind: str | None = None
    scope: str | None = None


class FeedbackOut(Schema):
    id: uuid.UUID
    agent: str
    action_kind: str
    verdict: str
    reason: str
    note: str
    changed_fields: list[str]
    processed: bool
    created_at: datetime


@router.get("/profile", response=ProfileOut | None, operation_id="memory_profile_get")
def get_profile(request: HttpRequest) -> Any:
    membership = resolve_workspace(request, Permission.VIEW)
    return services.current_brand_profile(membership.workspace_id)


@router.put("/profile", response=ProfileOut, operation_id="memory_profile_put")
def put_profile(request: HttpRequest, payload: ProfileIn) -> Any:
    membership = resolve_workspace(request, Permission.EDIT_MEMORY)
    return services.save_brand_profile(
        workspace_id=membership.workspace_id, content=payload.content, user=membership.user
    )


@router.get("/items", response=Page[ItemOut], operation_id="memory_items_list")
def list_items(request: HttpRequest, filters: Query[ItemFilters]) -> Page[ItemOut]:
    membership = resolve_workspace(request, Permission.VIEW)
    params = {k: v for k in ITEM_FILTERS if (v := getattr(filters, k)) is not None}
    queryset = apply_filters(
        MemoryItem.objects.filter(workspace_id=membership.workspace_id), params, ITEM_FILTERS
    )
    rows, next_cursor = paginate(queryset, cursor=filters.cursor, limit=filters.limit)
    return Page[ItemOut](
        items=[ItemOut.model_validate(r, from_attributes=True) for r in rows],
        next_cursor=next_cursor,
    )


@router.post("/items", response=ItemOut, operation_id="memory_items_create")
def create_item(request: HttpRequest, payload: ItemIn) -> Any:
    membership = resolve_workspace(request, Permission.EDIT_MEMORY)
    return services.remember(
        workspace_id=membership.workspace_id,
        text=payload.text,
        kind=payload.kind,
        scope=payload.scope,
        scope_ref=payload.scope_ref,
        status="active" if payload.activate else "proposed",
        is_hard_rule=payload.is_hard_rule,
        source="user",
        confidence=1.0,
        actor=Actor.human(membership.user_id),
    )


@router.post("/items/{item_id}/activate", response=ItemOut, operation_id="memory_items_activate")
def activate_item(request: HttpRequest, item_id: uuid.UUID) -> Any:
    membership = resolve_workspace(request, Permission.EDIT_MEMORY)
    return services.activate(
        workspace_id=membership.workspace_id, item_id=item_id, user=membership.user
    )


@router.post("/items/{item_id}/retire", response=ItemOut, operation_id="memory_items_retire")
def retire_item(request: HttpRequest, item_id: uuid.UUID, reject: bool = False) -> Any:
    membership = resolve_workspace(request, Permission.EDIT_MEMORY)
    return services.retire(
        workspace_id=membership.workspace_id, item_id=item_id, user=membership.user, reject=reject
    )


@router.get("/feedback", response=list[FeedbackOut], operation_id="memory_feedback_list")
def list_feedback(request: HttpRequest) -> Any:
    membership = resolve_workspace(request, Permission.VIEW)
    return list(
        FeedbackEvent.objects.filter(workspace_id=membership.workspace_id).order_by("-created_at")[
            :100
        ]
    )


@router.post("/reflect", response=list[ItemOut], operation_id="memory_reflect")
def reflect(request: HttpRequest) -> Any:
    membership = resolve_workspace(request, Permission.EDIT_MEMORY)
    return reflection.run_reflection(
        membership.workspace_id, get_llm_service(membership.workspace_id)
    )
