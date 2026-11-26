"""Action kinds for Google Ads.

Safety shape: campaigns are created paused in one all-or-nothing request that is validated by Google
first (``validateOnly``) and shown to the approver; delivery starts only through ``activate`` (R2,
spend guarded); archiving is R3. Edits keep the previous state so a rollback can restore it.
"""

import re
from collections.abc import Callable
from typing import Any

from actions.models import ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind
from ads.google import builders, factory, schemas
from ads.google import sync as google_sync
from ads.google.client import PROVIDER, GoogleAdsClient
from ads.models import AdEntity, AdLevel
from connections.models import Connection, ConnectionStatus
from core.errors import DomainError, NotFoundError, PermanentProviderError, ValidationFailedError
from core.logging import get_logger
from media.models import MediaAsset
from media.specs import SPEC_BY_KEY, check_asset
from media.storage import get_storage

log = get_logger(__name__)

PREFIX = "ads.google."
STATE = {"activate": "ENABLED", "pause": "PAUSED", "archive": "REMOVED"}
LOCAL_STATUS = {"ENABLED": "active", "PAUSED": "paused", "REMOVED": "archived"}
SERVICE = {
    "campaigns": "campaigns",
    "ad_groups": "adGroups",
    "ads": "adGroupAds",
    "asset_groups": "assetGroups",
}
LEVEL = {
    "campaigns": AdLevel.CAMPAIGN,
    "ad_groups": AdLevel.AD_GROUP,
    "ads": AdLevel.AD,
    "asset_groups": AdLevel.ASSET_GROUP,
}
RESULT_KEYS = {
    "campaign": "campaignResult",
    "ad_group": "adGroupResult",
    "ad": "adGroupAdResult",
    "asset_group": "assetGroupResult",
}


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
        raise NotFoundError("Google Ads connection not found for this workspace")
    return found


def _open(action: ProposedAction) -> GoogleAdsClient:
    return factory.open_client(_connection(action))


def _base(action: ProposedAction) -> dict[str, Any]:
    return {"connection_id": action.effective_payload["connection_id"], "summary": ""}


def _entity(connection: Connection, level: str, external_id: str) -> AdEntity:
    found = AdEntity.objects.filter(
        connection=connection, level=level, external_id=external_id
    ).first()
    if found is None:
        raise NotFoundError("sync the account first: this item is not in the local copy")
    return found


def _ids(response: dict[str, Any]) -> dict[str, str]:
    """Ids of the created resources from a ``googleAds:mutate`` response, keyed by kind."""
    found: dict[str, str] = {}
    for item in response.get("mutateOperationResponses", []):
        for kind, key in RESULT_KEYS.items():
            name = (item.get(key) or {}).get("resourceName")
            if name and kind not in found:
                found[kind] = re.sub(r"^.*/", "", name).split("~")[-1]
    return found


def _refresh_copy(action: ProposedAction) -> None:
    """After a creation the local copy is refreshed so the new items show up; never fails."""
    try:
        google_sync.sync_connection(_connection(action))
    except DomainError:
        log.warning("google_ads_refresh_after_create_failed")


def _resource_name(client: GoogleAdsClient, connection: Connection, resource: str, rid: str) -> str:
    if resource == "ads":
        ad = _entity(connection, AdLevel.AD, rid)
        group = ad.parent.external_id if ad.parent else ""
        return builders.resource(client.customer, "adGroupAds", f"{group}~{rid}")
    return builders.resource(client.customer, SERVICE[resource], rid)


# --- status -------------------------------------------------------------------------------


def _status_executor(verb: str) -> Callable[[ProposedAction], dict[str, Any]]:
    def execute(action: ProposedAction) -> dict[str, Any]:
        ref = _model(schemas.ResourceRef, action)
        connection = _connection(action)
        with factory.open_client(connection) as client:
            name = _resource_name(client, connection, ref.resource, ref.resource_id)
            state = STATE[verb]
            operation = (
                {"remove": name}
                if state == "REMOVED"
                else {"updateMask": "status", "update": {"resourceName": name, "status": state}}
            )
            client.mutate(SERVICE[ref.resource], [operation])
        google_sync.mirror_patch(
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


# --- budget and bid -----------------------------------------------------------------------


def _budget_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = _model(schemas.UpdateBudget, action)
    entity = AdEntity.objects.filter(
        connection=_connection(action), level=AdLevel.CAMPAIGN, external_id=payload.campaign_id
    ).first()
    before = {"budget": entity.data.get("budget")} if entity else None
    return before, {"budget": {"daily_spend_limit_micros": payload.daily_spend_limit_micros}}


def _budget_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateBudget, action)
    connection = _connection(action)
    campaign = _entity(connection, AdLevel.CAMPAIGN, payload.campaign_id)
    if campaign.data.get("budget_shared"):
        raise PermanentProviderError(
            "this campaign uses a shared budget; change it in Google Ads", provider=PROVIDER
        )
    name = campaign.data.get("budget_resource")
    if not name:
        raise PermanentProviderError(
            "the campaign budget is unknown; sync first", provider=PROVIDER
        )
    with factory.open_client(connection) as client:
        client.mutate(
            "campaignBudgets",
            [
                {
                    "updateMask": "amount_micros",
                    "update": {
                        "resourceName": name,
                        "amountMicros": str(payload.daily_spend_limit_micros),
                    },
                }
            ],
        )
    google_sync.mirror_patch(
        connection,
        AdLevel.CAMPAIGN,
        payload.campaign_id,
        {"budget": {"daily_spend_limit_micros": payload.daily_spend_limit_micros}},
    )
    return {"resource": "campaigns", "resource_id": payload.campaign_id}


def _budget_rollback(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateBudget, action)
    previous = ((action.before or {}).get("budget") or {}).get("daily_spend_limit_micros")
    if not previous:
        raise ValidationFailedError("the previous budget is unknown")
    return {
        **_base(action),
        "campaign_id": payload.campaign_id,
        "daily_spend_limit_micros": previous,
    }


def _bid_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = _model(schemas.UpdateCpcBid, action)
    entity = AdEntity.objects.filter(
        connection=_connection(action), level=AdLevel.AD_GROUP, external_id=payload.ad_group_id
    ).first()
    before = (
        {"max_bid_micros": (entity.data.get("bidding_config") or {}).get("max_bid_micros")}
        if entity
        else None
    )
    return before, {"max_bid_micros": payload.max_bid_micros}


def _bid_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateCpcBid, action)
    connection = _connection(action)
    with factory.open_client(connection) as client:
        name = builders.resource(client.customer, "adGroups", payload.ad_group_id)
        client.mutate(
            "adGroups",
            [
                {
                    "updateMask": "cpc_bid_micros",
                    "update": {"resourceName": name, "cpcBidMicros": str(payload.max_bid_micros)},
                }
            ],
        )
    google_sync.mirror_patch(
        connection,
        AdLevel.AD_GROUP,
        payload.ad_group_id,
        {"bidding_config": {"max_bid_micros": payload.max_bid_micros}},
    )
    return {"resource": "ad_groups", "resource_id": payload.ad_group_id}


def _bid_rollback(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateCpcBid, action)
    previous = (action.before or {}).get("max_bid_micros")
    if not previous:
        raise ValidationFailedError("the previous bid is unknown")
    return {**_base(action), "ad_group_id": payload.ad_group_id, "max_bid_micros": previous}


# --- responsive search ad text --------------------------------------------------------------


def _rsa_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = _model(schemas.UpdateRsa, action)
    entity = AdEntity.objects.filter(
        connection=_connection(action), level=AdLevel.AD, external_id=payload.ad_id
    ).first()
    creative = (entity.data.get("creative") or {}) if entity else {}
    before = (
        {"headlines": creative.get("headlines"), "descriptions": creative.get("descriptions")}
        if entity
        else None
    )
    return before, {"headlines": payload.headlines, "descriptions": payload.descriptions}


def _rsa_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateRsa, action)
    connection = _connection(action)
    ad = _entity(connection, AdLevel.AD, payload.ad_id)
    pins: dict[str, str] = (ad.data.get("creative") or {}).get("pinned_headlines") or {}

    def headline(text: str) -> dict[str, Any]:
        return {"text": text, **({"pinnedField": pins[text]} if text in pins else {})}

    with factory.open_client(connection) as client:
        client.mutate(
            "ads",
            [
                {
                    "updateMask": "responsive_search_ad.headlines,responsive_search_ad.descriptions",
                    "update": {
                        "resourceName": builders.resource(client.customer, "ads", payload.ad_id),
                        "responsiveSearchAd": {
                            "headlines": [headline(h) for h in payload.headlines],
                            "descriptions": [{"text": d} for d in payload.descriptions],
                        },
                    },
                }
            ],
        )
    creative = {
        **(ad.data.get("creative") or {}),
        "headlines": payload.headlines,
        "descriptions": payload.descriptions,
        "title": payload.headlines[0],
        "body": payload.descriptions[0],
    }
    google_sync.mirror_patch(
        connection, AdLevel.AD, payload.ad_id, {"creative": creative, "review_status": "in_review"}
    )
    return {"resource": "ads", "resource_id": payload.ad_id}


def _rsa_rollback(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.UpdateRsa, action)
    before = action.before or {}
    if not before.get("headlines") or not before.get("descriptions"):
        raise ValidationFailedError("the previous text is unknown")
    return {
        **_base(action),
        "ad_id": payload.ad_id,
        "headlines": before["headlines"],
        "descriptions": before["descriptions"],
    }


# --- a new responsive search ad ---------------------------------------------------------------


def _rsa_operation(customer: str, plan: schemas.AddRsa) -> dict[str, Any]:
    return {
        "create": {
            "adGroup": builders.resource(customer, "adGroups", plan.ad_group_id),
            "status": "ENABLED",
            "ad": {
                "finalUrls": [plan.final_url],
                "responsiveSearchAd": {
                    "headlines": [{"text": h} for h in plan.headlines],
                    "descriptions": [{"text": d} for d in plan.descriptions],
                    **({"path1": plan.path1} if plan.path1 else {}),
                    **({"path2": plan.path2} if plan.path2 else {}),
                },
            },
        }
    }


def _add_rsa_preview(
    action: ProposedAction,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    plan = _model(schemas.AddRsa, action)
    validation: dict[str, Any] = {"ok": True, "error": ""}
    with _open(action) as client:
        try:
            client.mutate("adGroupAds", [_rsa_operation(client.customer, plan)], validate_only=True)
        except DomainError as exc:
            validation = {"ok": False, "error": str(exc)[:400]}
    return None, {
        "ad_group_id": plan.ad_group_id,
        "headlines": plan.headlines,
        "descriptions": plan.descriptions,
        "validation": validation,
    }


def _add_rsa_execute(action: ProposedAction) -> dict[str, Any]:
    plan = _model(schemas.AddRsa, action)
    with _open(action) as client:
        response = client.mutate("adGroupAds", [_rsa_operation(client.customer, plan)])
    name = str(((response.get("results") or [{}])[0]).get("resourceName", ""))
    ad_id = name.rsplit("~", 1)[-1] if "~" in name else ""
    _refresh_copy(action)
    return {"resource": "ads", "resource_id": ad_id, "ad_group_id": plan.ad_group_id}


def _add_rsa_rollback(action: ProposedAction) -> dict[str, Any]:
    result = action.result or {}
    if not result.get("resource_id"):
        raise ValidationFailedError("the new ad id is unknown")
    return {**_base(action), "resource": "ads", "resource_id": result["resource_id"]}


# --- keywords -------------------------------------------------------------------------------


def _criterion(
    keyword: schemas.Keyword, parent_key: str, parent: str, negative: bool
) -> dict[str, Any]:
    create: dict[str, Any] = {
        parent_key: parent,
        "status": "ENABLED",
        "keyword": {"text": keyword.text, "matchType": builders.MATCH[keyword.match_type]},
    }
    if negative:
        create["negative"] = True
    elif keyword.max_cpc_micros:
        create["cpcBidMicros"] = str(keyword.max_cpc_micros)
    return {"create": create}


def _keywords_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.AddKeywords, action)
    with _open(action) as client:
        parent = builders.resource(client.customer, "adGroups", payload.ad_group_id)
        client.mutate(
            "adGroupCriteria", [_criterion(k, "adGroup", parent, False) for k in payload.keywords]
        )
    _refresh_copy(action)
    return {"added": len(payload.keywords), "ad_group_id": payload.ad_group_id}


def _negatives_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.AddNegatives, action)
    with _open(action) as client:
        if payload.scope == "campaign":
            parent = builders.resource(client.customer, "campaigns", payload.parent_id)
            client.mutate(
                "campaignCriteria",
                [_criterion(k, "campaign", parent, True) for k in payload.keywords],
            )
        else:
            parent = builders.resource(client.customer, "adGroups", payload.parent_id)
            client.mutate(
                "adGroupCriteria",
                [_criterion(k, "adGroup", parent, True) for k in payload.keywords],
            )
    _refresh_copy(action)
    return {"added": len(payload.keywords), "scope": payload.scope}


def _pause_keyword_execute(action: ProposedAction) -> dict[str, Any]:
    payload = _model(schemas.PauseKeyword, action)
    connection = _connection(action)
    with factory.open_client(connection) as client:
        name = builders.resource(
            client.customer, "adGroupCriteria", f"{payload.ad_group_id}~{payload.criterion_id}"
        )
        client.mutate(
            "adGroupCriteria",
            [{"updateMask": "status", "update": {"resourceName": name, "status": "PAUSED"}}],
        )
    google_sync.mirror_patch(
        connection,
        AdLevel.KEYWORD,
        f"{payload.ad_group_id}~{payload.criterion_id}",
        {"status": "paused"},
    )
    return {"ad_group_id": payload.ad_group_id, "criterion_id": payload.criterion_id}


# --- creation -------------------------------------------------------------------------------


def _validated(client: GoogleAdsClient, operations: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        client.mutate_all(operations, validate_only=True)
    except DomainError as exc:
        return {"ok": False, "error": str(exc)[:400]}
    return {"ok": True, "error": ""}


def _search_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    plan = _model(schemas.SearchCampaign, action)
    with _open(action) as client:
        operations = builders.search_operations(client.customer, plan)
        validation = _validated(client, operations)
    return None, {"operations": builders.summarize(operations), "validation": validation}


def _search_execute(action: ProposedAction) -> dict[str, Any]:
    plan = _model(schemas.SearchCampaign, action)
    with _open(action) as client:
        operations = builders.search_operations(client.customer, plan)
        response = client.mutate_all(operations)
    created = _ids(response)
    _refresh_copy(action)
    return {"resource": "campaigns", "resource_id": created.get("campaign", ""), **created}


def _add_group_preview(
    action: ProposedAction,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    plan = _model(schemas.AddAdGroup, action)
    with _open(action) as client:
        operations = builders.add_ad_group_operations(
            client.customer, plan.campaign_id, plan.ad_group
        )
        validation = _validated(client, operations)
    return None, {"operations": builders.summarize(operations), "validation": validation}


def _add_group_execute(action: ProposedAction) -> dict[str, Any]:
    plan = _model(schemas.AddAdGroup, action)
    with _open(action) as client:
        operations = builders.add_ad_group_operations(
            client.customer, plan.campaign_id, plan.ad_group
        )
        response = client.mutate_all(operations)
    created = _ids(response)
    _refresh_copy(action)
    return {"resource": "ad_groups", "resource_id": created.get("ad_group", ""), **created}


def _add_group_rollback(action: ProposedAction) -> dict[str, Any]:
    result = action.result or {}
    if not result.get("resource_id"):
        raise ValidationFailedError("the new ad group id is unknown")
    return {**_base(action), "resource": "ad_groups", "resource_id": result["resource_id"]}


ROLE_CHECK = {role: SPEC_BY_KEY[spec] for role, spec in builders.IMAGE_SPEC.items()}


def load_images(
    workspace_id: Any, images: list[schemas.ImageAsset]
) -> tuple[list[tuple[str, str, bytes]], list[str]]:
    """Read library images for upload and report the ones that do not fit their role."""
    storage = get_storage()
    loaded: list[tuple[str, str, bytes]] = []
    problems: list[str] = []
    for item in images:
        asset = MediaAsset.objects.filter(pk=item.media_id, workspace_id=workspace_id).first()
        if asset is None or not asset.storage_key:
            problems.append(f"{item.media_id}: not in the asset library")
            continue
        outcome = check_asset(asset, ROLE_CHECK[item.role])
        if not outcome["ok"]:
            codes = ", ".join(r["code"] for r in outcome["reasons"])
            problems.append(f"{asset.name} ({item.role}): {codes}")
            continue
        with storage.open(asset.storage_key) as handle:
            loaded.append((item.role, asset.name, handle.read()))
    return loaded, problems


def _pmax_preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    plan = _model(schemas.PmaxCampaign, action)
    images, problems = load_images(action.workspace_id, plan.asset_group.images)
    if problems:
        return None, {"validation": {"ok": False, "error": "; ".join(problems)[:400]}}
    with _open(action) as client:
        operations = builders.pmax_operations(client.customer, plan, images)
        validation = _validated(client, operations)
    return None, {"operations": builders.summarize(operations), "validation": validation}


def _pmax_execute(action: ProposedAction) -> dict[str, Any]:
    plan = _model(schemas.PmaxCampaign, action)
    images, problems = load_images(action.workspace_id, plan.asset_group.images)
    if problems:
        raise ValidationFailedError("; ".join(problems)[:400])
    with _open(action) as client:
        response = client.mutate_all(builders.pmax_operations(client.customer, plan, images))
    created = _ids(response)
    _refresh_copy(action)
    return {"resource": "campaigns", "resource_id": created.get("campaign", ""), **created}


def _extensions_execute(action: ProposedAction) -> dict[str, Any]:
    plan = _model(schemas.AddExtensions, action)
    with _open(action) as client:
        operations = builders.extension_operations(client.customer, plan)
        client.mutate_all(operations)
    return {"campaign_id": plan.campaign_id, "added": len(operations) // 2}


def _add_assets_execute(action: ProposedAction) -> dict[str, Any]:
    plan = _model(schemas.AddAssets, action)
    images, problems = load_images(action.workspace_id, plan.images)
    if problems:
        raise ValidationFailedError("; ".join(problems)[:400])
    with _open(action) as client:
        group = builders.resource(client.customer, "assetGroups", plan.asset_group_id)
        operations = builders.asset_operations(builders.Temp(client.customer), group, plan, images)
        client.mutate_all(operations)
    _refresh_copy(action)
    return {"asset_group_id": plan.asset_group_id, "added": len(operations) // 2}


def _remove_asset_execute(action: ProposedAction) -> dict[str, Any]:
    plan = _model(schemas.RemoveAsset, action)
    with _open(action) as client:
        name = builders.resource(
            client.customer,
            "assetGroupAssets",
            f"{plan.asset_group_id}~{plan.asset_id}~{plan.field_type}",
        )
        client.mutate_all([{"assetGroupAssetOperation": {"remove": name}}])
    _refresh_copy(action)
    return {"asset_group_id": plan.asset_group_id, "asset_id": plan.asset_id}


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
        description="Start delivery of a campaign, ad group, ad or asset group",
        spend_micros=lambda p: p.daily_spend_micros,
        rollback_kind=PREFIX + "pause", rollback_payload=_state_rollback)  # fmt: skip
    add("pause", schemas.ResourceRef, RiskClass.R1, _status_executor("pause"),
        description="Stop delivery of a campaign, ad group, ad or asset group")  # fmt: skip
    add("archive", schemas.ResourceRef, RiskClass.R3, _status_executor("archive"),
        description="Remove a campaign, ad group, ad or asset group (cannot be undone)")  # fmt: skip
    add("campaign.update_budget", schemas.UpdateBudget, RiskClass.R2, _budget_execute,
        description="Change a campaign's daily budget", preview=_budget_preview,
        spend_micros=lambda p: p.daily_spend_limit_micros,
        rollback_kind=PREFIX + "campaign.update_budget", rollback_payload=_budget_rollback)  # fmt: skip
    add("ad_group.update_bid", schemas.UpdateCpcBid, RiskClass.R2, _bid_execute,
        description="Change an ad group's maximum cost per click", preview=_bid_preview,
        rollback_kind=PREFIX + "ad_group.update_bid", rollback_payload=_bid_rollback)  # fmt: skip
    add("ad.update_rsa", schemas.UpdateRsa, RiskClass.R2, _rsa_execute,
        description="Replace the headlines and descriptions of a responsive search ad",
        preview=_rsa_preview, rollback_kind=PREFIX + "ad.update_rsa",
        rollback_payload=_rsa_rollback)  # fmt: skip
    add("ad.add_rsa", schemas.AddRsa, RiskClass.R2, _add_rsa_execute,
        description="Add a new responsive search ad beside the existing ones in an ad group",
        preview=_add_rsa_preview, rollback_kind=PREFIX + "pause",
        rollback_payload=_add_rsa_rollback)  # fmt: skip
    add("ad_group.create", schemas.AddAdGroup, RiskClass.R2, _add_group_execute,
        description="Add an ad group with keywords and responsive search ads to a campaign",
        preview=_add_group_preview, rollback_kind=PREFIX + "pause",
        rollback_payload=_add_group_rollback)  # fmt: skip
    add("keywords.add", schemas.AddKeywords, RiskClass.R2, _keywords_execute,
        description="Add keywords to an ad group")  # fmt: skip
    add("negatives.add", schemas.AddNegatives, RiskClass.R1, _negatives_execute,
        description="Add negative keywords to a campaign or ad group")  # fmt: skip
    add("keyword.pause", schemas.PauseKeyword, RiskClass.R1, _pause_keyword_execute,
        description="Pause one keyword")  # fmt: skip
    add("search_campaign.create", schemas.SearchCampaign, RiskClass.R2, _search_execute,
        description="Create a paused Search campaign with keywords and a responsive search ad",
        preview=_search_preview, spend_micros=lambda p: p.daily_budget_micros,
        rollback_kind=PREFIX + "archive", rollback_payload=_archive_created)  # fmt: skip
    add("pmax_campaign.create", schemas.PmaxCampaign, RiskClass.R2, _pmax_execute,
        description="Create a paused Performance Max campaign with one asset group",
        preview=_pmax_preview, spend_micros=lambda p: p.daily_budget_micros,
        rollback_kind=PREFIX + "archive", rollback_payload=_archive_created)  # fmt: skip
    add("campaign.add_extensions", schemas.AddExtensions, RiskClass.R2, _extensions_execute,
        description="Add sitelinks, callouts and structured snippets to a campaign")  # fmt: skip
    add("asset_group.add_assets", schemas.AddAssets, RiskClass.R2, _add_assets_execute,
        description="Add texts, library images and YouTube videos to an asset group")  # fmt: skip
    add("asset_group.remove_asset", schemas.RemoveAsset, RiskClass.R2, _remove_asset_execute,
        description="Remove one asset from an asset group")  # fmt: skip
