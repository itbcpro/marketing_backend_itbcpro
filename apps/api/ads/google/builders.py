"""Mutate operations for Google Ads, built as plain data so they can be checked without a network.

Creation uses one all-or-nothing ``googleAds:mutate`` request with temporary negative ids, so a
failure leaves nothing half-built. Everything is created paused.
"""

import base64
from typing import Any

from ads.google.schemas import (
    COUNTRY_IDS,
    LANGUAGE_IDS,
    AddAssets,
    AddExtensions,
    AdGroupPlan,
    AssetGroupContent,
    Keyword,
    PmaxCampaign,
    SearchCampaign,
    Targeting,
)

EU_POLITICAL = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
MATCH = {"exact": "EXACT", "phrase": "PHRASE", "broad": "BROAD"}
IMAGE_FIELD = {
    "marketing_image": "MARKETING_IMAGE",
    "square_marketing_image": "SQUARE_MARKETING_IMAGE",
    "portrait_marketing_image": "PORTRAIT_MARKETING_IMAGE",
    "logo": "LOGO",
    "landscape_logo": "LANDSCAPE_LOGO",
}
IMAGE_SPEC = {
    "marketing_image": "google_landscape",
    "square_marketing_image": "google_square",
    "portrait_marketing_image": "google_portrait",
    "logo": "google_logo_square",
    "landscape_logo": "google_logo_landscape",
}
BIDDING_FIELDS: dict[str, dict[str, Any]] = {
    "manual_cpc": {"manualCpc": {"enhancedCpcEnabled": False}},
    "maximize_clicks": {"targetSpend": {}},
    "maximize_conversions": {"maximizeConversions": {}},
}


class Temp:
    """Hands out temporary resource names (negative ids) for one request."""

    def __init__(self, customer: str) -> None:
        self._customer = customer
        self._next = 0

    def name(self, collection: str) -> str:
        self._next -= 1
        return f"customers/{self._customer}/{collection}/{self._next}"


def resource(customer: str, collection: str, resource_id: str | int) -> str:
    return f"customers/{customer}/{collection}/{resource_id}"


def _budget_op(temp: Temp, name: str, micros: int) -> tuple[str, dict[str, Any]]:
    budget = temp.name("campaignBudgets")
    return budget, {
        "campaignBudgetOperation": {
            "create": {
                "resourceName": budget,
                "name": f"{name} budget {abs(int(budget.rsplit('/', 1)[-1]))}",
                "amountMicros": str(micros),
                "deliveryMethod": "STANDARD",
                "explicitlyShared": False,
            }
        }
    }


def _targeting_ops(campaign: str, targeting: Targeting) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": campaign,
                    "location": {"geoTargetConstant": f"geoTargetConstants/{COUNTRY_IDS[code]}"},
                }
            }
        }
        for code in targeting.countries
    ]
    ops += [
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": campaign,
                    "language": {"languageConstant": f"languageConstants/{LANGUAGE_IDS[code]}"},
                }
            }
        }
        for code in targeting.languages
    ]
    return ops


def _keyword(ad_group: str, keyword: Keyword, *, negative: bool = False) -> dict[str, Any]:
    create: dict[str, Any] = {
        "adGroup": ad_group,
        "status": "ENABLED",
        "keyword": {"text": keyword.text, "matchType": MATCH[keyword.match_type]},
    }
    if negative:
        create["negative"] = True
    elif keyword.max_cpc_micros:
        create["cpcBidMicros"] = str(keyword.max_cpc_micros)
    return {"adGroupCriterionOperation": {"create": create}}


def search_operations(customer: str, plan: SearchCampaign) -> list[dict[str, Any]]:
    temp = Temp(customer)
    budget, budget_op = _budget_op(temp, plan.name, plan.daily_budget_micros)
    campaign = temp.name("campaigns")
    campaign_create: dict[str, Any] = {
        "resourceName": campaign,
        "name": plan.name,
        "status": "PAUSED",
        "advertisingChannelType": "SEARCH",
        "campaignBudget": budget,
        **BIDDING_FIELDS[plan.bidding],
        "networkSettings": {
            "targetGoogleSearch": True,
            "targetSearchNetwork": plan.search_partners,
            "targetContentNetwork": False,
            "targetPartnerSearchNetwork": False,
        },
        "containsEuPoliticalAdvertising": EU_POLITICAL,
    }
    operations: list[dict[str, Any]] = [
        budget_op,
        {"campaignOperation": {"create": campaign_create}},
        *_targeting_ops(campaign, plan.targeting),
        *(_campaign_negative(campaign, k) for k in plan.negative_keywords),
    ]
    for item in plan.ad_groups:
        operations += _ad_group_operations(temp, campaign, item, plan.max_cpc_micros)
    return operations


def add_ad_group_operations(
    customer: str, campaign_id: str, item: AdGroupPlan
) -> list[dict[str, Any]]:
    """One ad group with its keywords and ads, added to an existing campaign in one request."""
    campaign = resource(customer, "campaigns", campaign_id)
    return _ad_group_operations(Temp(customer), campaign, item, None)


def _campaign_negative(campaign: str, keyword: Keyword) -> dict[str, Any]:
    return {
        "campaignCriterionOperation": {
            "create": {
                "campaign": campaign,
                "negative": True,
                "keyword": {"text": keyword.text, "matchType": MATCH[keyword.match_type]},
            }
        }
    }


def _ad_group_operations(
    temp: Temp, campaign: str, item: AdGroupPlan, default_cpc: int | None
) -> list[dict[str, Any]]:
    group = temp.name("adGroups")
    group_create: dict[str, Any] = {
        "resourceName": group,
        "name": item.name,
        "campaign": campaign,
        "status": "ENABLED",
        "type": "SEARCH_STANDARD",
    }
    cpc = item.max_cpc_micros or default_cpc
    if cpc:
        group_create["cpcBidMicros"] = str(cpc)
    ops: list[dict[str, Any]] = [
        {"adGroupOperation": {"create": group_create}},
        *(_keyword(group, k) for k in item.keywords),
        *(_keyword(group, k, negative=True) for k in item.negative_keywords),
    ]
    for copy in item.ads:
        ad = {
            "finalUrls": [copy.final_url],
            "responsiveSearchAd": {
                "headlines": [{"text": h} for h in copy.headlines],
                "descriptions": [{"text": d} for d in copy.descriptions],
                **({"path1": copy.path1} if copy.path1 else {}),
                **({"path2": copy.path2} if copy.path2 else {}),
            },
        }
        ops.append(
            {"adGroupAdOperation": {"create": {"adGroup": group, "status": "ENABLED", "ad": ad}}}
        )
    return ops


def _text_asset(temp: Temp, text: str) -> tuple[str, dict[str, Any]]:
    name = temp.name("assets")
    return name, {"assetOperation": {"create": {"resourceName": name, "textAsset": {"text": text}}}}


def _link(asset_group: str, asset: str, field_type: str) -> dict[str, Any]:
    return {
        "assetGroupAssetOperation": {
            "create": {"assetGroup": asset_group, "asset": asset, "fieldType": field_type}
        }
    }


def asset_operations(
    temp: Temp,
    asset_group: str,
    content: AssetGroupContent | AddAssets,
    images: list[tuple[str, str, bytes]],
) -> list[dict[str, Any]]:
    """Create the assets and link them to the asset group. ``images`` is ``(role, name, bytes)``."""
    ops: list[dict[str, Any]] = []
    texts = (
        ("HEADLINE", content.headlines),
        ("LONG_HEADLINE", content.long_headlines),
        ("DESCRIPTION", content.descriptions),
    )
    for field_type, values in texts:
        for text in values:
            name, create = _text_asset(temp, text)
            ops += [create, _link(asset_group, name, field_type)]
    business = getattr(content, "business_name", "")
    if business:
        name, create = _text_asset(temp, business)
        ops += [create, _link(asset_group, name, "BUSINESS_NAME")]
    for role, label, data in images:
        name = temp.name("assets")
        ops += [
            {
                "assetOperation": {
                    "create": {
                        "resourceName": name,
                        "name": label[:120],
                        "imageAsset": {"data": base64.b64encode(data).decode()},
                    }
                }
            },
            _link(asset_group, name, IMAGE_FIELD[role]),
        ]
    for video in content.youtube_video_ids:
        name = temp.name("assets")
        ops += [
            {
                "assetOperation": {
                    "create": {"resourceName": name, "youtubeVideoAsset": {"youtubeVideoId": video}}
                }
            },
            _link(asset_group, name, "YOUTUBE_VIDEO"),
        ]
    return ops


def pmax_operations(
    customer: str, plan: PmaxCampaign, images: list[tuple[str, str, bytes]]
) -> list[dict[str, Any]]:
    temp = Temp(customer)
    budget, budget_op = _budget_op(temp, plan.name, plan.daily_budget_micros)
    campaign = temp.name("campaigns")
    group = temp.name("assetGroups")
    if plan.bidding == "maximize_conversion_value":
        bidding: dict[str, Any] = {
            "maximizeConversionValue": {"targetRoas": plan.target_roas} if plan.target_roas else {}
        }
    else:
        bidding = {
            "maximizeConversions": (
                {"targetCpaMicros": str(plan.target_cpa_micros)} if plan.target_cpa_micros else {}
            )
        }
    return [
        budget_op,
        {
            "campaignOperation": {
                "create": {
                    "resourceName": campaign,
                    "name": plan.name,
                    "status": "PAUSED",
                    "advertisingChannelType": "PERFORMANCE_MAX",
                    "campaignBudget": budget,
                    "urlExpansionOptOut": False,
                    "containsEuPoliticalAdvertising": EU_POLITICAL,
                    **bidding,
                }
            }
        },
        *_targeting_ops(campaign, plan.targeting),
        {
            "assetGroupOperation": {
                "create": {
                    "resourceName": group,
                    "name": plan.asset_group.name,
                    "campaign": campaign,
                    "status": "PAUSED",
                    "finalUrls": [plan.asset_group.final_url],
                }
            }
        },
        *asset_operations(temp, group, plan.asset_group, images),
    ]


def extension_operations(customer: str, plan: AddExtensions) -> list[dict[str, Any]]:
    temp = Temp(customer)
    campaign = resource(customer, "campaigns", plan.campaign_id)
    ops: list[dict[str, Any]] = []

    def add(asset: dict[str, Any], field_type: str) -> None:
        name = temp.name("assets")
        ops.append({"assetOperation": {"create": {"resourceName": name, **asset}}})
        ops.append(
            {
                "campaignAssetOperation": {
                    "create": {"campaign": campaign, "asset": name, "fieldType": field_type}
                }
            }
        )

    for link in plan.sitelinks:
        asset: dict[str, Any] = {
            "sitelinkAsset": {
                "linkText": link.link_text,
                **({"description1": link.description1} if link.description1 else {}),
                **({"description2": link.description2} if link.description2 else {}),
            },
            "finalUrls": [link.final_url],
        }
        add(asset, "SITELINK")
    for text in plan.callouts:
        add({"calloutAsset": {"calloutText": text}}, "CALLOUT")
    for snippet in plan.snippets:
        add(
            {"structuredSnippetAsset": {"header": snippet.header, "values": snippet.values}},
            "STRUCTURED_SNIPPET",
        )
    return ops


def summarize(operations: list[dict[str, Any]]) -> dict[str, int]:
    """How many of each kind of operation a request contains (for the approver's preview)."""
    counts: dict[str, int] = {}
    for op in operations:
        key = next(iter(op)).removesuffix("Operation")
        counts[key] = counts.get(key, 0) + 1
    return counts
