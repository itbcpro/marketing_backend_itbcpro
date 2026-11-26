"""Request bodies for Meta's create endpoints, built from a validated plan. Pure functions."""

from typing import Any

from ads.meta import money
from ads.meta.schemas import CampaignPlan

# objective -> (optimisation goal, billing event, destination type)
OPTIMISATION: dict[str, tuple[str, str, str]] = {
    "OUTCOME_TRAFFIC": ("LINK_CLICKS", "IMPRESSIONS", "WEBSITE"),
    "OUTCOME_AWARENESS": ("REACH", "IMPRESSIONS", "WEBSITE"),
    "OUTCOME_ENGAGEMENT": ("LINK_CLICKS", "IMPRESSIONS", "WEBSITE"),
    "OUTCOME_LEADS": ("OFFSITE_CONVERSIONS", "IMPRESSIONS", "WEBSITE"),
    "OUTCOME_SALES": ("OFFSITE_CONVERSIONS", "IMPRESSIONS", "WEBSITE"),
}
CONVERSION_EVENT = {"OUTCOME_LEADS": "LEAD", "OUTCOME_SALES": "PURCHASE"}


def campaign_body(plan: CampaignPlan, currency: str) -> dict[str, Any]:
    return {
        "name": plan.name,
        "objective": plan.objective,
        "status": "PAUSED",
        "special_ad_categories": [],
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "daily_budget": str(money.to_minor(plan.daily_budget_micros, currency)),
    }


def ad_set_body(plan: CampaignPlan, campaign_id: str) -> dict[str, Any]:
    goal, billing, destination = OPTIMISATION[plan.objective]
    body: dict[str, Any] = {
        "name": f"{plan.name} - audience",
        "campaign_id": campaign_id,
        "status": "PAUSED",
        "optimization_goal": goal,
        "billing_event": billing,
        "destination_type": destination,
        "targeting": {
            "geo_locations": {"countries": plan.countries},
            "age_min": plan.age_min,
            "age_max": plan.age_max,
            "targeting_automation": {"advantage_audience": 1},
        },
    }
    event = CONVERSION_EVENT.get(plan.objective)
    if event:
        body["promoted_object"] = {"pixel_id": plan.pixel_id, "custom_event_type": event}
    return body


def creative_body(plan: CampaignPlan, image_hash: str) -> dict[str, Any]:
    link_data: dict[str, Any] = {
        "image_hash": image_hash,
        "link": plan.link,
        "message": plan.primary_text,
        "name": plan.headline,
        "call_to_action": {"type": plan.call_to_action, "value": {"link": plan.link}},
    }
    if plan.description:
        link_data["description"] = plan.description
    story: dict[str, Any] = {"page_id": plan.page_id, "link_data": link_data}
    if plan.instagram_user_id:
        story["instagram_user_id"] = plan.instagram_user_id
    campaign_tag = plan.utm_campaign or plan.name.lower().replace(" ", "-")[:100]
    return {
        "name": f"{plan.name} - creative",
        "object_story_spec": story,
        "url_tags": f"utm_source=meta&utm_medium=paid&utm_campaign={campaign_tag}",
    }


def ad_body(plan: CampaignPlan, ad_set_id: str, creative_id: str) -> dict[str, Any]:
    return {
        "name": f"{plan.name} - ad",
        "adset_id": ad_set_id,
        "creative": {"creative_id": creative_id},
        "status": "PAUSED",
    }


def summarize(plan: CampaignPlan, currency: str) -> dict[str, Any]:
    """What the approver sees: the structure that will be created, all paused."""
    goal, _, _ = OPTIMISATION[plan.objective]
    return {
        "campaign": {"name": plan.name, "objective": plan.objective, "status": "paused"},
        "ad_set": {
            "countries": plan.countries,
            "ages": f"{plan.age_min}-{plan.age_max}",
            "optimisation": goal,
            "audience": "Advantage+ audience",
            "placements": "automatic",
        },
        "ad": {
            "headline": plan.headline,
            "primary_text": plan.primary_text,
            "call_to_action": plan.call_to_action,
            "link": plan.link,
        },
        "budget": {"daily_micros": plan.daily_budget_micros, "currency": currency},
    }
