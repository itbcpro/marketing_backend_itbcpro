"""Action kinds for Meta Ads.

Safety shape: a campaign is created paused, with its ad set and ad, in one approved step that
shows the approver the whole structure first. Delivery starts only through ``activate`` (R2, spend
guarded). Anything half-created when a step fails is deleted again. Edits keep the previous state
so a rollback can restore it.
"""

import base64
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from actions.models import ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind
from ads.meta import builders, factory, money, schemas
from ads.meta import sync as meta_sync
from ads.meta.client import PROVIDER, MetaAdsClient
from ads.models import AdEntity, AdLevel, AdSyncState
from connections.models import Connection, ConnectionStatus
from core.errors import DomainError, NotFoundError, PermanentProviderError, ValidationFailedError
from core.logging import get_logger
from media.models import MediaAsset
from media.specs import SPEC_BY_KEY, check_asset
from media.storage import get_storage

log = get_logger(__name__)

PREFIX = "ads.meta."
STATE = {"activate": "ACTIVE", "pause": "PAUSED", "archive": "ARCHIVED"}
LOCAL_STATUS = {"ACTIVE": "active", "PAUSED": "paused", "ARCHIVED": "archived"}
LEVEL = {"campaigns": AdLevel.CAMPAIGN, "ad_groups": AdLevel.AD_GROUP, "ads": AdLevel.AD}
BUDGET_LEVEL = {"campaign": AdLevel.CAMPAIGN, "ad_set": AdLevel.AD_GROUP}
IMAGE_SPECS = ("meta_square", "meta_portrait", "meta_landscape")


def _model[M: schemas.Connected](model: type[M], action: ProposedAction) -> M:
    return model.model_validate(action.effective_payload)


def _connection(action: ProposedAction) -> Connection:
    found = (
        Connection.objects.filter(
            pk=str(action.effective_payload["connection_id"]),
            workspace_id=action.workspace_id,
            provider=PROVIDER,
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    if found is None:
        raise NotFoundError("Meta Ads connection not found for this workspace")
    return found


def _base(action: ProposedAction) -> dict[str, Any]:
    return {"connection_id": action.effective_payload["connection_id"], "summary": ""}


def _entity(connection: Connection, level: str, external_id: str) -> AdEntity:
    found = AdEntity.objects.filter(
        connection=connection, level=level, external_id=external_id
    ).first()
    if found is None:
        raise NotFoundError("sync the account first: this item is not in the local copy")
    return found


def _currency(connection: Connection) -> str:
    state = AdSyncState.objects.filter(connection=connection).first()
    code = str((state.account if state else {}).get("currency_code") or "")
    if code:
        return code
    with factory.open_client(connection) as client:
        return str(client.get_account().get("currency") or "USD")


def _refresh_copy(action: ProposedAction) -> None:
    """After a creation the local copy is refreshed so the new items show up; never fails."""
    try:
        meta_sync.sync_connection(_connection(action))
    except DomainError:
        log.warning("meta_ads_refresh_after_create_failed")


# --- status -------------------------------------------------------------------------------


def _status_executor(verb: str) -> Callable[[ProposedAction], dict[str, Any]]:
    def execute(action: ProposedAction) -> dict[str, Any]:
        ref = _model(schemas.ResourceRef, action)
        connection = _connection(action)
        state = STATE[verb]
        with factory.open_client(connection) as client:
            client.update(ref.resource_id, {"status": state})
        meta_sync.mirror_patch(
            connection, LEVEL[ref.resource], ref.resource_id, {"status": LOCAL_STATUS[state]}
        )
        return {
            "resource": ref.resource,
            "resource_id": ref.resource_id,
            "status": LOCAL_STATUS[state],
        }

    return execute


def _state_rollback(action: ProposedAction) -> dict[str, Any]:
    result = action.result or {}
    return {**_base(action), "resource": result["resource"], "resource_id": result["resource_id"]}


# --- budget -------------------------------------------------------------------------------


def _budget_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = _model(schemas.UpdateBudget, action)
    entity = AdEntity.objects.filter(
        connection=_connection(action),
        level=BUDGET_LEVEL[payload.level],
        external_id=payload.entity_id,
    ).first()
    before = {"daily_spend_limit_micros": _daily_budget(entity)} if entity else None
    return before, {"daily_spend_limit_micros": payload.daily_spend_limit_micros}


def _daily_budget(entity: AdEntity) -> int | None:
    key = "budget" if entity.level == AdLevel.CAMPAIGN else "bidding_config"
    value = (entity.data.get(key) or {}).get("daily_spend_limit_micros")
    return int(value) if value else None


def _budget_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateBudget, action)
    connection = _connection(action)
    entity = _entity(connection, BUDGET_LEVEL[payload.level], payload.entity_id)
    if _daily_budget(entity) is None:
        raise PermanentProviderError(
            "this item has no daily budget of its own (the budget is set on its campaign or ad "
            "set, or it is a lifetime budget); change it in Meta Ads Manager",
            provider=PROVIDER,
        )
    minor = money.to_minor(payload.daily_spend_limit_micros, _currency(connection))
    with factory.open_client(connection) as client:
        client.update(payload.entity_id, {"daily_budget": str(minor)})
    patch = (
        {"budget": {"daily_spend_limit_micros": payload.daily_spend_limit_micros}}
        if payload.level == "campaign"
        else {
            "bidding_config": {
                **(entity.data.get("bidding_config") or {}),
                "daily_spend_limit_micros": payload.daily_spend_limit_micros,
            }
        }
    )
    meta_sync.mirror_patch(connection, BUDGET_LEVEL[payload.level], payload.entity_id, patch)
    return {"level": payload.level, "entity_id": payload.entity_id}


def _budget_rollback(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateBudget, action)
    previous = (action.before or {}).get("daily_spend_limit_micros")
    if not previous:
        raise ValidationFailedError("the previous budget is unknown")
    return {
        **_base(action),
        "level": payload.level,
        "entity_id": payload.entity_id,
        "daily_spend_limit_micros": previous,
    }


# --- campaign creation ----------------------------------------------------------------------


def load_image(plan: schemas.CampaignPlan, workspace_id: Any) -> tuple[str, bytes]:
    """The library picture for the ad, checked against Meta's feed formats."""
    asset = MediaAsset.objects.filter(pk=plan.image_media_id, workspace_id=workspace_id).first()
    if asset is None or not asset.storage_key:
        raise ValidationFailedError("the picture is not in the asset library")
    outcomes = [check_asset(asset, SPEC_BY_KEY[key]) for key in IMAGE_SPECS]
    if not any(o["ok"] for o in outcomes):
        reasons = ", ".join(sorted({r["code"] for o in outcomes for r in o["reasons"]}))
        raise ValidationFailedError(f"the picture does not fit a Meta feed format: {reasons}")
    with get_storage().open(asset.storage_key) as handle:
        return asset.name, handle.read()


def _create_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    plan = _model(schemas.CampaignPlan, action)
    connection = _connection(action)
    currency = _currency(connection)
    validation: dict[str, Any] = {"ok": True}
    try:
        load_image(plan, action.workspace_id)
        with factory.open_client(connection) as client:
            if client.find_by_name("campaigns", plan.name):
                raise ValidationFailedError("a campaign with this name already exists")
            client.create("campaigns", builders.campaign_body(plan, currency), validate_only=True)
    except DomainError as exc:
        validation = {"ok": False, "error": str(exc)[:400]}
    return None, {"structure": builders.summarize(plan, currency), "validation": validation}


def _create_execute(action: ProposedAction) -> dict[str, Any]:
    plan = _model(schemas.CampaignPlan, action)
    connection = _connection(action)
    currency = _currency(connection)
    name, data = load_image(plan, action.workspace_id)
    created: list[str] = []
    with factory.open_client(connection) as client:
        if client.find_by_name("campaigns", plan.name):
            raise ValidationFailedError("a campaign with this name already exists")
        try:
            ids = _create_all(client, plan, currency, name, data, created)
        except DomainError:
            _clean_up(client, created)
            raise
    _refresh_copy(action)
    return {"resource": "campaigns", "resource_id": ids["campaign"], **ids}


def _create_all(
    client: MetaAdsClient,
    plan: schemas.CampaignPlan,
    currency: str,
    image_name: str,
    image: bytes,
    created: list[str],
) -> dict[str, str]:
    """Creation order matters; every id is recorded at once so a failure can undo the steps."""
    uploaded = client.upload_image(image_name, base64.b64encode(image).decode())
    hashes = [str(v.get("hash") or "") for v in (uploaded.get("images") or {}).values()]
    image_hash = hashes[0] if hashes else ""
    if not image_hash:
        raise PermanentProviderError("Meta did not accept the picture", provider=PROVIDER)
    campaign = str(client.create("campaigns", builders.campaign_body(plan, currency))["id"])
    created.append(campaign)
    ad_set = str(client.create("adsets", builders.ad_set_body(plan, campaign))["id"])
    created.append(ad_set)
    creative = str(client.create("adcreatives", builders.creative_body(plan, image_hash))["id"])
    ad = str(client.create("ads", builders.ad_body(plan, ad_set, creative))["id"])
    created.append(ad)
    return {"campaign": campaign, "ad_set": ad_set, "creative": creative, "ad": ad}


def _clean_up(client: MetaAdsClient, created: list[str]) -> None:
    for object_id in reversed(created):
        with suppress(DomainError):
            client.delete(object_id)


def _archive_created(action: ProposedAction) -> dict[str, Any]:
    result = action.result or {}
    return {**_base(action), "resource": "campaigns", "resource_id": result["resource_id"]}


def register_action_kinds() -> None:
    def add(name: str, model: type[Any], risk: RiskClass, executor: Any, **extra: Any) -> None:
        register_action_kind(
            ActionKind(
                kind=PREFIX + name, payload_model=model, risk=risk, executor=executor, **extra
            )
        )

    add("activate", schemas.Activate, RiskClass.R2, _status_executor("activate"),
        description="Start delivery of a campaign, ad set or ad",
        spend_micros=lambda p: p.daily_spend_micros,
        rollback_kind=PREFIX + "pause", rollback_payload=_state_rollback)  # fmt: skip
    add("pause", schemas.ResourceRef, RiskClass.R1, _status_executor("pause"),
        description="Stop delivery of a campaign, ad set or ad")  # fmt: skip
    add("archive", schemas.ResourceRef, RiskClass.R2, _status_executor("archive"),
        description="Archive a campaign, ad set or ad (can be restored)")  # fmt: skip
    add("update_budget", schemas.UpdateBudget, RiskClass.R2, _budget_execute,
        description="Change a daily budget on a campaign or an ad set", preview=_budget_preview,
        spend_micros=lambda p: p.daily_spend_limit_micros,
        rollback_kind=PREFIX + "update_budget", rollback_payload=_budget_rollback)  # fmt: skip
    add("campaign.create", schemas.CampaignPlan, RiskClass.R2, _create_execute,
        description="Create a paused campaign with one ad set and one image ad",
        preview=_create_preview, spend_micros=lambda p: p.daily_budget_micros,
        rollback_kind=PREFIX + "archive", rollback_payload=_archive_created)  # fmt: skip
