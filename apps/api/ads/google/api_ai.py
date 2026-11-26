"""Google Ads extras: conversion audit and AI keyword suggestions from real search terms."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from ads.google import conversions, keywords
from ads.google.api import _connection
from ads.models import AdEntity, AdKeywordProposal
from core.errors import NotFoundError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-google"], auth=api_auth)
RECENT = 5


class ConversionsOut(Schema):
    actions: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    totals_30d: dict[str, float]


class InstructionIn(Schema):
    instruction: str = ""


class KeywordProposalOut(Schema):
    id: uuid.UUID
    entity_id: uuid.UUID
    instruction: str
    status: str
    error_code: str
    thinking_since: datetime | None
    search_terms: list[dict[str, Any]]
    negatives: list[dict[str, Any]]
    additions: list[dict[str, Any]]
    pauses: list[dict[str, Any]]
    summary: str
    notes: str
    applied: dict[str, Any]
    created_at: datetime


class ApplyKeywordsIn(Schema):
    negatives: list[int] = []
    additions: list[int] = []
    pauses: list[int] = []


def _out(row: AdKeywordProposal) -> KeywordProposalOut:
    return KeywordProposalOut(
        id=row.pk,
        entity_id=row.entity_id,
        instruction=row.instruction,
        status=row.status,
        error_code=row.error_code,
        thinking_since=row.thinking_since,
        search_terms=list(row.search_terms),
        negatives=list(row.negatives),
        additions=list(row.additions),
        pauses=list(row.pauses),
        summary=row.summary,
        notes=row.notes,
        applied=row.applied,
        created_at=row.created_at,
    )


def _entity(request: HttpRequest, entity_id: uuid.UUID, permission: Permission) -> AdEntity:
    membership = resolve_workspace(request, permission)
    found = (
        AdEntity.objects.filter(pk=entity_id, workspace_id=membership.workspace_id)
        .select_related("connection", "parent")
        .first()
    )
    if found is None:
        raise NotFoundError("ad group not found")
    return found


@router.get("/conversions", response=ConversionsOut, operation_id="google_ads_conversions")
def conversion_audit(request: HttpRequest, connection_id: uuid.UUID) -> ConversionsOut:
    connection = _connection(request, connection_id, Permission.VIEW)
    return ConversionsOut(**conversions.audit(connection))


@router.get(
    "/keywords/entities/{entity_id}",
    response=list[KeywordProposalOut],
    operation_id="google_ads_keywords_list",
)
def list_keyword_proposals(request: HttpRequest, entity_id: uuid.UUID) -> list[KeywordProposalOut]:
    entity = _entity(request, entity_id, Permission.VIEW)
    return [_out(r) for r in AdKeywordProposal.objects.filter(entity=entity)[:RECENT]]


@router.post(
    "/keywords/entities/{entity_id}",
    response=KeywordProposalOut,
    operation_id="google_ads_keywords_start",
)
def start_keyword_proposal(
    request: HttpRequest, entity_id: uuid.UUID, body: InstructionIn
) -> KeywordProposalOut:
    entity = _entity(request, entity_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    return _out(keywords.start(entity.connection, user.pk, entity, body.instruction))


@router.post(
    "/keywords/{proposal_id}/apply",
    response=KeywordProposalOut,
    operation_id="google_ads_keywords_apply",
)
def apply_keyword_proposal(
    request: HttpRequest, proposal_id: uuid.UUID, body: ApplyKeywordsIn
) -> KeywordProposalOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    row = keywords.get(membership.workspace_id, proposal_id)
    updated, _ = keywords.apply(
        row, user, negatives=body.negatives, additions=body.additions, pauses=body.pauses
    )
    return _out(updated)
