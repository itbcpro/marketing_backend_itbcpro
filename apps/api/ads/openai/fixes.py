"""Convert an AI-suggested fix into the payload of a platform action."""

from typing import Any

from ads.models import AdEntity, AdLevel
from core.errors import ValidationFailedError

MICROS = 1_000_000


def build_change(entity: AdEntity, fix: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Return ``(action kind suffix, payload, summary)`` for a fix on one ad."""
    group = entity if entity.level == AdLevel.AD_GROUP else entity.parent
    kind = fix.get("type")
    if kind == "ad_text":
        if entity.level != AdLevel.AD:
            raise ValidationFailedError("ad text can only be changed on an ad")
        creative = entity.data.get("creative") or {}
        if creative.get("type") != "chat_card" or not creative.get("file_id"):
            raise ValidationFailedError("this creative type cannot be edited here")
        if not (fix.get("new_title") or fix.get("new_body")):
            raise ValidationFailedError("the suggestion has no new text")
        payload = {
            "ad_id": entity.external_id,
            "creative": {
                "type": "chat_card",
                "title": fix.get("new_title") or creative.get("title"),
                "body": fix.get("new_body") or creative.get("body"),
                "target_url": creative.get("target_url"),
                "file_id": creative.get("file_id"),
            },
        }
        return "ad.update", payload, "Apply AI suggestion: new ad text"
    if kind == "context_hints":
        hints = [str(h).strip() for h in (fix.get("new_hints") or []) if str(h).strip()]
        if group is None or not hints:
            raise ValidationFailedError("the suggestion has no hints")
        payload = {"ad_group_id": group.external_id, "context_hints": hints}
        return "ad_group.update", payload, "Apply AI suggestion: new context hints"
    if kind == "max_bid":
        bidding = (group.data.get("bidding_config") if group else None) or {}
        value = fix.get("new_max_bid")
        if group is None or bidding.get("strategy", "fixed_bid") != "fixed_bid" or not value:
            raise ValidationFailedError("this ad group does not use a fixed bid")
        payload = {
            "ad_group_id": group.external_id,
            "bidding": {
                "strategy": "fixed_bid",
                "billing_event_type": bidding.get("billing_event_type") or "click",
                "max_bid_micros": round(float(value) * MICROS),
            },
        }
        return "ad_group.update_bid", payload, "Apply AI suggestion: new maximum bid"
    if kind == "pause_ad":
        resource = {"ad": "ads", "ad_group": "ad_groups", "campaign": "campaigns"}[entity.level]
        payload = {"resource": resource, "resource_id": entity.external_id}
        return "pause", payload, "Apply AI suggestion: pause this ad"
    raise ValidationFailedError("unknown suggestion")
