"""Action kinds for OpenAI Ads.

Safety shape: everything is created paused; delivery starts only through ``activate`` (R2, spend
guarded) and stops through ``pause`` (R1). Archiving cannot be undone by the API, so it is R3 and
never auto-approved. Updates store the previous state so a rollback can restore it.
"""

from collections.abc import Callable
from typing import Any

from actions.models import ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind
from ads.openai import schemas
from ads.openai.client import PROVIDER, OpenAiAdsClient
from connections.models import Connection, ConnectionStatus
from core.errors import DomainError, NotFoundError
from core.logging import get_logger

log = get_logger(__name__)

PREFIX = "ads.openai."
BUDGET_KEYS = ("daily_spend_limit_micros", "lifetime_spend_limit_micros")

ClientFactory = Callable[[Connection], OpenAiAdsClient]


def _default_factory(connection: Connection) -> OpenAiAdsClient:
    return OpenAiAdsClient(connection)


client_factory: ClientFactory = _default_factory
"""Replaced in tests; production always builds a real client."""


def _connection(action: ProposedAction) -> Connection:
    connection_id = action.effective_payload["connection_id"]
    connection = Connection.objects.filter(
        pk=str(connection_id), workspace_id=action.workspace_id, provider=PROVIDER
    ).exclude(status=ConnectionStatus.REVOKED)
    found = connection.first()
    if found is None:
        raise NotFoundError("OpenAI Ads connection not found for this workspace")
    return found


LEVEL_BY_RESOURCE = {"campaigns": "campaign", "ad_groups": "ad_group", "ads": "ad"}


def _mirror(action: ProposedAction, resource: str, row: dict[str, Any]) -> None:
    """Keep the local copy in step with a change we just made; never fails the action."""
    from ads.openai import sync

    try:
        sync.mirror_row(_connection(action), LEVEL_BY_RESOURCE[resource], row)
    except Exception:
        log.exception("openai_ads_mirror_failed", resource=resource)


def _open(action: ProposedAction) -> OpenAiAdsClient:
    return client_factory(_connection(action))


def _key(action: ProposedAction) -> str:
    return f"act-{action.pk}"


def _model[M: schemas.Connected](model: type[M], action: ProposedAction) -> M:
    return model.model_validate(action.effective_payload)


def _base(action: ProposedAction) -> dict[str, Any]:
    payload = action.effective_payload
    return {"connection_id": payload["connection_id"], "summary": ""}


# --- creation -------------------------------------------------------------------------------


def _creator(model: type[Any], resource: str) -> Callable[[ProposedAction], dict[str, Any]]:
    def execute(action: ProposedAction) -> dict[str, Any]:
        payload = _model(model, action)
        with _open(action) as client:
            created = client.create(resource, payload.api_body(), idempotency_key=_key(action))
        return {"resource": resource, "resource_id": created["id"], "status": created.get("status")}

    return execute


def _create_preview(
    model: type[Any],
) -> Callable[[ProposedAction], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    def preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return None, {"request": _model(model, action).api_body()}

    return preview


def _verify_exists(action: ProposedAction) -> bool:
    result = action.result or {}
    with _open(action) as client:
        row = client.get_resource(result["resource"], result["resource_id"])
    return bool(row.get("id") == result["resource_id"])


def _archive_created(action: ProposedAction) -> dict[str, Any]:
    result = action.result or {}
    return {**_base(action), "resource": result["resource"], "resource_id": result["resource_id"]}


# --- state ----------------------------------------------------------------------------------


def _state_executor(state: str) -> Callable[[ProposedAction], dict[str, Any]]:
    def execute(action: ProposedAction) -> dict[str, Any]:
        ref = _model(schemas.ResourceRef, action)
        with _open(action) as client:
            row = client.set_state(ref.resource, ref.resource_id, state)
        _mirror(action, ref.resource, row)
        return {
            "resource": ref.resource,
            "resource_id": ref.resource_id,
            "status": row.get("status"),
        }

    return execute


def _state_verifier(expected: str) -> Callable[[ProposedAction], bool]:
    def verify(action: ProposedAction) -> bool:
        result = action.result or {}
        with _open(action) as client:
            row = client.get_resource(result["resource"], result["resource_id"])
        return bool(row.get("status") == expected)

    return verify


def _state_rollback(action: ProposedAction) -> dict[str, Any]:
    result = action.result or {}
    return {**_base(action), "resource": result["resource"], "resource_id": result["resource_id"]}


def _current(action: ProposedAction, resource: str, resource_id: str) -> dict[str, Any] | None:
    """Best-effort read for the approver's before/after view; approval must not depend on it."""
    try:
        with _open(action) as client:
            return client.get_resource(resource, resource_id)
    except DomainError:
        log.warning("openai_ads_preview_read_failed", resource=resource)
        return None


# --- updates --------------------------------------------------------------------------------


def _budget_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = _model(schemas.UpdateBudget, action)
    row = _current(action, "campaigns", payload.campaign_id)
    before = {"budget": row.get("budget")} if row else None
    return before, {"budget": payload.budget.as_api()}


def _budget_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateBudget, action)
    with _open(action) as client:
        row = client.update(
            "campaigns",
            payload.campaign_id,
            {"budget": payload.budget.as_api()},
            idempotency_key=_key(action),
        )
    return {
        "resource": "campaigns",
        "resource_id": payload.campaign_id,
        "budget": row.get("budget"),
    }


def _budget_verify(action: ProposedAction) -> bool:
    payload = _model(schemas.UpdateBudget, action)
    with _open(action) as client:
        row = client.get_resource("campaigns", payload.campaign_id)
    return bool(row.get("budget") == payload.budget.as_api())


def _budget_rollback(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateBudget, action)
    previous = {
        k: v for k, v in ((action.before or {}).get("budget") or {}).items() if k in BUDGET_KEYS
    }
    return {**_base(action), "campaign_id": payload.campaign_id, "budget": previous}


def _bid_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = _model(schemas.UpdateBid, action)
    row = _current(action, "ad_groups", payload.ad_group_id)
    before = {"bidding": row.get("bidding_config")} if row else None
    return before, {"bidding": payload.bidding.as_api()}


def _bid_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateBid, action)
    with _open(action) as client:
        row = client.update(
            "ad_groups",
            payload.ad_group_id,
            {"bidding_config": payload.bidding.as_api()},
            idempotency_key=_key(action),
        )
    return {
        "resource": "ad_groups",
        "resource_id": payload.ad_group_id,
        "bidding": row.get("bidding_config"),
    }


def _bid_rollback(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateBid, action)
    previous = dict((action.before or {}).get("bidding") or {})
    previous.setdefault("strategy", "fixed_bid")
    return {**_base(action), "ad_group_id": payload.ad_group_id, "bidding": previous}


def _upload_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.ImageUpload, action)
    with _open(action) as client:
        uploaded = client.upload_image(payload.image_url, idempotency_key=_key(action))
    return {"file_id": uploaded["file_id"]}


# --- content edits --------------------------------------------------------------------------


def _editor(
    model: type[Any], resource: str, id_field: str
) -> Callable[[ProposedAction], dict[str, Any]]:
    def execute(action: ProposedAction) -> dict[str, Any]:
        payload = _model(model, action)
        resource_id = getattr(payload, id_field)
        with _open(action) as client:
            row = client.update(
                resource, resource_id, payload.api_body(), idempotency_key=_key(action)
            )
        _mirror(action, resource, row)
        return {"resource": resource, "resource_id": resource_id, "status": row.get("status")}

    return execute


def _edit_preview(
    model: type[Any], resource: str, id_field: str, fields: tuple[str, ...]
) -> Callable[[ProposedAction], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    def preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        payload = _model(model, action)
        row = _current(action, resource, getattr(payload, id_field))
        before = {f: row.get(f) for f in fields if f in payload.api_body()} if row else None
        return before, payload.api_body()

    return preview


def _edit_rollback(model: type[Any], id_field: str) -> Callable[[ProposedAction], dict[str, Any]]:
    def plan(action: ProposedAction) -> dict[str, Any]:
        payload = _model(model, action)
        previous = dict(action.before or {})
        creative = previous.get("creative")
        if isinstance(creative, dict):
            allowed = schemas.ChatCard.model_fields
            previous["creative"] = {k: v for k, v in creative.items() if k in allowed}
        return {**_base(action), id_field: getattr(payload, id_field), **previous}

    return plan


def register_action_kinds() -> None:
    def add(name: str, model: type[Any], risk: RiskClass, executor: Any, **extra: Any) -> None:
        register_action_kind(
            ActionKind(
                kind=PREFIX + name, payload_model=model, risk=risk, executor=executor, **extra
            )
        )

    add(
        "campaign.create",
        schemas.CampaignCreate,
        RiskClass.R2,
        _creator(schemas.CampaignCreate, "campaigns"),
        description="Create a paused OpenAI Ads campaign with a budget",
        preview=_create_preview(schemas.CampaignCreate),
        verify=_verify_exists,
        spend_micros=lambda p: p.budget.amount_micros,
        rollback_kind=PREFIX + "archive",
        rollback_payload=_archive_created,
    )
    add(
        "ad_group.create",
        schemas.AdGroupCreate,
        RiskClass.R1,
        _creator(schemas.AdGroupCreate, "ad_groups"),
        description="Create a paused ad group with a bidding configuration",
        preview=_create_preview(schemas.AdGroupCreate),
        verify=_verify_exists,
        rollback_kind=PREFIX + "archive",
        rollback_payload=_archive_created,
    )
    add(
        "ad.create",
        schemas.AdCreate,
        RiskClass.R2,
        _creator(schemas.AdCreate, "ads"),
        description="Create a paused chat-card ad",
        preview=_create_preview(schemas.AdCreate),
        verify=_verify_exists,
        rollback_kind=PREFIX + "archive",
        rollback_payload=_archive_created,
    )
    add(
        "image.upload",
        schemas.ImageUpload,
        RiskClass.R1,
        _upload_execute,
        description="Upload a creative image from a public HTTPS URL",
    )
    add(
        "activate",
        schemas.Activate,
        RiskClass.R2,
        _state_executor("activate"),
        description="Start delivery of a campaign, ad group or ad",
        verify=_state_verifier("active"),
        spend_micros=lambda p: p.daily_spend_micros,
        rollback_kind=PREFIX + "pause",
        rollback_payload=_state_rollback,
    )
    add(
        "pause",
        schemas.ResourceRef,
        RiskClass.R1,
        _state_executor("pause"),
        description="Stop delivery of a campaign, ad group or ad",
        verify=_state_verifier("paused"),
    )
    add(
        "archive",
        schemas.ResourceRef,
        RiskClass.R3,
        _state_executor("archive"),
        description="Archive a campaign, ad group or ad permanently (cannot be undone)",
        verify=_state_verifier("archived"),
    )
    add(
        "campaign.update_budget",
        schemas.UpdateBudget,
        RiskClass.R2,
        _budget_execute,
        description="Change a campaign's budget",
        preview=_budget_preview,
        verify=_budget_verify,
        spend_micros=lambda p: p.budget.amount_micros,
        rollback_kind=PREFIX + "campaign.update_budget",
        rollback_payload=_budget_rollback,
    )
    add(
        "ad_group.update_bid",
        schemas.UpdateBid,
        RiskClass.R2,
        _bid_execute,
        description="Change an ad group's bidding configuration",
        preview=_bid_preview,
        rollback_kind=PREFIX + "ad_group.update_bid",
        rollback_payload=_bid_rollback,
    )
    add(
        "campaign.update",
        schemas.CampaignUpdate,
        RiskClass.R1,
        _editor(schemas.CampaignUpdate, "campaigns", "campaign_id"),
        description="Rename a campaign or change its description or end time",
        preview=_edit_preview(
            schemas.CampaignUpdate,
            "campaigns",
            "campaign_id",
            ("name", "description", "end_time"),
        ),
        rollback_kind=PREFIX + "campaign.update",
        rollback_payload=_edit_rollback(schemas.CampaignUpdate, "campaign_id"),
    )
    add(
        "ad_group.update",
        schemas.AdGroupUpdate,
        RiskClass.R1,
        _editor(schemas.AdGroupUpdate, "ad_groups", "ad_group_id"),
        description="Rename an ad group or replace its context hints",
        preview=_edit_preview(
            schemas.AdGroupUpdate, "ad_groups", "ad_group_id", ("name", "context_hints")
        ),
        rollback_kind=PREFIX + "ad_group.update",
        rollback_payload=_edit_rollback(schemas.AdGroupUpdate, "ad_group_id"),
    )
    add(
        "ad.update",
        schemas.AdUpdate,
        RiskClass.R2,
        _editor(schemas.AdUpdate, "ads", "ad_id"),
        description="Rename an ad or replace its creative (goes back to review)",
        preview=_edit_preview(schemas.AdUpdate, "ads", "ad_id", ("name", "creative")),
        rollback_kind=PREFIX + "ad.update",
        rollback_payload=_edit_rollback(schemas.AdUpdate, "ad_id"),
    )
