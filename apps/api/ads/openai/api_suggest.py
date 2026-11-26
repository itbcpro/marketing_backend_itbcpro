"""AI suggestions that pre-fill the add-ad-group and add-ad forms. Read-only: nothing is created."""

import uuid

from django.http import HttpRequest
from ninja import Router, Schema

from ads.openai import suggest
from connections.models import Connection, ConnectionStatus
from core.errors import NotFoundError
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-openai-suggest"], auth=api_auth)


class OpenAiSuggestIn(Schema):
    connection_id: uuid.UUID
    parent_id: str
    instruction: str = ""


def _check(request: HttpRequest, connection_id: uuid.UUID) -> None:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    found = (
        Connection.objects.filter(
            pk=connection_id, workspace_id=membership.workspace_id, provider="openai_ads"
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .exists()
    )
    if not found:
        raise NotFoundError("OpenAI Ads connection not found")


@router.post(
    "/suggest/ad-group",
    response=suggest.NewGroupSuggestion,
    operation_id="openai_ads_suggest_ad_group",
)
def suggest_ad_group(request: HttpRequest, body: OpenAiSuggestIn) -> suggest.NewGroupSuggestion:
    """Propose a new ad group (name, hints, bidding) that fits the campaign and its other groups."""
    _check(request, body.connection_id)
    return suggest.suggest_group(body.parent_id, body.connection_id, body.instruction)


@router.post("/suggest/ad", response=suggest.NewAdSuggestion, operation_id="openai_ads_suggest_ad")
def suggest_ad(request: HttpRequest, body: OpenAiSuggestIn) -> suggest.NewAdSuggestion:
    """Propose a new ad (headline, text, link) that fits its ad group and the campaign."""
    _check(request, body.connection_id)
    return suggest.suggest_ad(body.parent_id, body.connection_id, body.instruction)
