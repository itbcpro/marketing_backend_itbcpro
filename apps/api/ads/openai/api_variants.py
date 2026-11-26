"""Three AI-written versions of an ad, and choosing one to replace the current text."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from ads.models import AdEntity, AdHintProposal, AdVariantSet
from ads.openai import hints, variants
from core.errors import NotFoundError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-openai-variants"], auth=api_auth)
RECENT = 5


class VariantsIn(Schema):
    instruction: str = ""


class ChooseIn(Schema):
    index: int


class VariantSetOut(Schema):
    id: uuid.UUID
    entity_id: uuid.UUID
    instruction: str
    status: str
    error_code: str
    thinking_since: datetime | None
    variants: list[dict[str, Any]]
    chosen_index: int | None
    action_id: uuid.UUID | None
    applied: bool
    created_at: datetime


def _out(row: AdVariantSet) -> VariantSetOut:
    return VariantSetOut(
        id=row.pk,
        entity_id=row.entity_id,
        instruction=row.instruction,
        status=row.status,
        error_code=row.error_code,
        thinking_since=row.thinking_since,
        variants=row.variants,
        chosen_index=row.chosen_index,
        action_id=row.action_id,
        applied=row.applied,
        created_at=row.created_at,
    )


def _entity(request: HttpRequest, entity_id: uuid.UUID, permission: Permission) -> AdEntity:
    membership = resolve_workspace(request, permission)
    entity = (
        AdEntity.objects.filter(pk=entity_id, workspace_id=membership.workspace_id)
        .select_related("connection", "parent__parent")
        .first()
    )
    if entity is None:
        raise NotFoundError("ad not found")
    return entity


@router.get(
    "/store/entities/{entity_id}/variants",
    response=list[VariantSetOut],
    operation_id="openai_ads_variants_list",
)
def list_variants(request: HttpRequest, entity_id: uuid.UUID) -> list[VariantSetOut]:
    entity = _entity(request, entity_id, Permission.VIEW)
    return [_out(r) for r in AdVariantSet.objects.filter(entity=entity)[:RECENT]]


@router.post(
    "/store/entities/{entity_id}/variants",
    response=VariantSetOut,
    operation_id="openai_ads_variants_start",
)
def start_variants(request: HttpRequest, entity_id: uuid.UUID, body: VariantsIn) -> VariantSetOut:
    entity = _entity(request, entity_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    return _out(variants.start(entity.connection, user.pk, entity, body.instruction))


@router.get(
    "/store/variants/{set_id}", response=VariantSetOut, operation_id="openai_ads_variants_get"
)
def get_variants(request: HttpRequest, set_id: uuid.UUID) -> VariantSetOut:
    membership = resolve_workspace(request, Permission.VIEW)
    return _out(variants.get(membership.workspace_id, set_id))


@router.post(
    "/store/variants/{set_id}/choose",
    response=VariantSetOut,
    operation_id="openai_ads_variants_choose",
)
def choose_variant(request: HttpRequest, set_id: uuid.UUID, body: ChooseIn) -> VariantSetOut:
    """Replace the ad's text with the chosen version (published unless approval is needed)."""
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    row = variants.get(membership.workspace_id, set_id)
    updated, _ = variants.choose(row, user, body.index)
    return _out(updated)


class HintProposalOut(Schema):
    id: uuid.UUID
    entity_id: uuid.UUID
    instruction: str
    status: str
    error_code: str
    thinking_since: datetime | None
    before: list[str]
    after: list[str]
    summary: str
    notes: str
    decided: bool
    applied: bool
    action_id: uuid.UUID | None
    created_at: datetime


def _hint_out(row: AdHintProposal) -> HintProposalOut:
    return HintProposalOut(
        id=row.pk,
        entity_id=row.entity_id,
        instruction=row.instruction,
        status=row.status,
        error_code=row.error_code,
        thinking_since=row.thinking_since,
        before=list(row.before),
        after=list(row.after),
        summary=row.summary,
        notes=row.notes,
        decided=row.decided,
        applied=row.applied,
        action_id=row.action_id,
        created_at=row.created_at,
    )


@router.get(
    "/store/entities/{entity_id}/hints",
    response=list[HintProposalOut],
    operation_id="openai_ads_hints_list",
)
def list_hints(request: HttpRequest, entity_id: uuid.UUID) -> list[HintProposalOut]:
    entity = _entity(request, entity_id, Permission.VIEW)
    return [_hint_out(r) for r in AdHintProposal.objects.filter(entity=entity)[:RECENT]]


@router.post(
    "/store/entities/{entity_id}/hints",
    response=HintProposalOut,
    operation_id="openai_ads_hints_start",
)
def start_hints(request: HttpRequest, entity_id: uuid.UUID, body: VariantsIn) -> HintProposalOut:
    """AI cleans up this ad group's context hints; nothing changes until the owner applies it."""
    entity = _entity(request, entity_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    return _hint_out(hints.start(entity.connection, user.pk, entity, body.instruction))


@router.post(
    "/store/hints/{proposal_id}/apply",
    response=HintProposalOut,
    operation_id="openai_ads_hints_apply",
)
def apply_hints(request: HttpRequest, proposal_id: uuid.UUID) -> HintProposalOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    row = hints.get(membership.workspace_id, proposal_id)
    updated, _ = hints.apply(row, user)
    return _hint_out(updated)
