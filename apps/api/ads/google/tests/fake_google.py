"""A small in-memory Google Ads account for tests: one Search campaign and one Performance Max."""

import json
from datetime import timedelta
from typing import Any

import httpx
from django.utils import timezone


def _metrics() -> dict[str, Any]:
    return {
        "impressions": "100",
        "clicks": "5",
        "costMicros": "2500000",
        "conversions": 1.5,
        "conversionsValue": 75.0,
    }


class FakeGoogle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.mutations: list[dict[str, Any]] = []
        self.fail: dict[str, tuple[int, dict[str, Any]]] = {}
        self.headline = "Managed IT for nonprofits"
        self.status = "ENABLED"
        self.created_ids = 0

    # --- rows ------------------------------------------------------------------------------

    def _days(self, count: int = 3) -> list[str]:
        today = timezone.now().date()
        return [(today - timedelta(days=i)).isoformat() for i in range(count)]

    def rows(self, query: str) -> list[dict[str, Any]]:  # noqa: PLR0911, C901, PLR0912
        q = " ".join(query.split())
        metrics = _metrics()
        if "FROM ad_group_ad" in q and "segments.date BETWEEN" in q:
            return [
                {
                    "segments": {"date": d},
                    "adGroupAd": {"ad": {"id": "1000"}},
                    "metrics": metrics,
                }
                for d in self._days()
            ]
        if "FROM asset_group" in q and "segments.date BETWEEN" in q:
            return [
                {"segments": {"date": d}, "assetGroup": {"id": "200"}, "metrics": metrics}
                for d in self._days()
            ]
        if "FROM keyword_view" in q:
            return [
                {
                    "segments": {"date": d},
                    "adGroup": {"id": "100"},
                    "adGroupCriterion": {"criterionId": "7"},
                    "metrics": metrics,
                }
                for d in self._days()
            ]
        if "FROM customer LIMIT 1" in q:
            return [
                {
                    "customer": {
                        "id": "1234567890",
                        "descriptiveName": "Acme",
                        "currencyCode": "CAD",
                        "timeZone": "America/Vancouver",
                        "status": "ENABLED",
                        "manager": False,
                        "testAccount": False,
                    }
                }
            ]
        if "FROM customer_client" in q:
            return [
                {
                    "customerClient": {
                        "id": "111",
                        "descriptiveName": "MCC",
                        "manager": True,
                        "level": "0",
                    }
                },
                {
                    "customerClient": {
                        "id": "1234567890",
                        "descriptiveName": "Acme",
                        "currencyCode": "CAD",
                        "timeZone": "America/Vancouver",
                        "manager": False,
                        "level": "1",
                    }
                },
            ]
        if "FROM campaign WHERE campaign.status != 'REMOVED'" in q:
            return [
                {
                    "campaign": {
                        "id": "10",
                        "name": "Search - IT",
                        "status": self.status,
                        "primaryStatus": "ELIGIBLE",
                        "advertisingChannelType": "SEARCH",
                        "biddingStrategyType": "MANUAL_CPC",
                        "campaignBudget": "customers/1234567890/campaignBudgets/50",
                    },
                    "campaignBudget": {
                        "id": "50",
                        "amountMicros": "20000000",
                        "explicitlyShared": False,
                    },
                },
                {
                    "campaign": {
                        "id": "20",
                        "name": "PMax - Software",
                        "status": "PAUSED",
                        "primaryStatus": "PAUSED",
                        "primaryStatusReasons": ["CAMPAIGN_PAUSED"],
                        "advertisingChannelType": "PERFORMANCE_MAX",
                        "biddingStrategyType": "MAXIMIZE_CONVERSIONS",
                        "campaignBudget": "customers/1234567890/campaignBudgets/60",
                    },
                    "campaignBudget": {"id": "60", "amountMicros": "30000000"},
                },
            ]
        if "FROM ad_group WHERE" in q:
            return [
                {
                    "adGroup": {
                        "id": "100",
                        "name": "Nonprofit IT",
                        "status": "ENABLED",
                        "type": "SEARCH_STANDARD",
                        "cpcBidMicros": "2000000",
                        "primaryStatus": "ELIGIBLE",
                    },
                    "campaign": {"id": "10"},
                }
            ]
        if "FROM ad_group_ad WHERE ad_group_ad.status" in q:
            return [
                {
                    "adGroupAd": {
                        "ad": {
                            "id": "1000",
                            "type": "RESPONSIVE_SEARCH_AD",
                            "finalUrls": ["https://itbcpro.ca/nonprofit"],
                            "responsiveSearchAd": {
                                "headlines": [
                                    {"text": self.headline},
                                    {"text": "Local IT support", "pinnedField": "HEADLINE_1"},
                                    {"text": "Call us today"},
                                ],
                                "descriptions": [
                                    {"text": "Predictable monthly pricing."},
                                    {"text": "On-site help in Metro Vancouver."},
                                ],
                                "path1": "it",
                            },
                        },
                        "status": "ENABLED",
                        "adStrength": "POOR",
                        "policySummary": {"approvalStatus": "APPROVED"},
                    },
                    "adGroup": {"id": "100"},
                }
            ]
        if "FROM ad_group_criterion" in q:
            return [
                {
                    "adGroupCriterion": {
                        "criterionId": "7",
                        "keyword": {"text": "managed it nonprofit", "matchType": "PHRASE"},
                        "status": "ENABLED",
                        "negative": False,
                        "qualityInfo": {"qualityScore": 4},
                    },
                    "adGroup": {"id": "100"},
                },
                {
                    "adGroupCriterion": {
                        "criterionId": "8",
                        "keyword": {"text": "free", "matchType": "BROAD"},
                        "status": "ENABLED",
                        "negative": True,
                    },
                    "adGroup": {"id": "100"},
                },
            ]
        if "FROM asset_group WHERE" in q:
            return [
                {
                    "assetGroup": {
                        "id": "200",
                        "name": "Software assets",
                        "status": "ENABLED",
                        "finalUrls": ["https://itbcpro.ca/software"],
                        "adStrength": "GOOD",
                        "primaryStatus": "ELIGIBLE",
                    },
                    "campaign": {"id": "20"},
                }
            ]
        if "FROM asset_group_asset" in q:
            return [
                {
                    "assetGroup": {"id": "200"},
                    "assetGroupAsset": {"fieldType": "HEADLINE", "primaryStatus": "ELIGIBLE"},
                    "asset": {
                        "id": "900",
                        "type": "TEXT",
                        "textAsset": {"text": "Custom software"},
                    },
                },
                {
                    "assetGroup": {"id": "200"},
                    "assetGroupAsset": {
                        "fieldType": "MARKETING_IMAGE",
                        "primaryStatus": "NOT_ELIGIBLE",
                    },
                    "asset": {
                        "id": "901",
                        "type": "IMAGE",
                        "imageAsset": {
                            "fullSize": {
                                "url": "https://x/i.png",
                                "widthPixels": "1200",
                                "heightPixels": "628",
                            }
                        },
                    },
                },
            ]
        if "search_impression_share" in q:
            return [
                {
                    "campaign": {"id": "10"},
                    "metrics": {
                        "searchImpressionShare": 0.42,
                        "searchBudgetLostImpressionShare": 0.3,
                        "searchRankLostImpressionShare": 0.28,
                    },
                }
            ]
        if "FROM conversion_action" in q:
            return [
                {
                    "conversionAction": {
                        "id": "5",
                        "name": "Lead form",
                        "status": "ENABLED",
                        "type": "WEBPAGE",
                        "category": "SUBMIT_LEAD_FORM",
                        "primaryForGoal": True,
                        "countingType": "ONE_PER_CLICK",
                    }
                }
            ]
        return []

    def _operation_results(self, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            key = next(iter(operation))
            name = key.removesuffix("Operation")
            base = self.created_ids * 1000
            if name == "adGroupAd":
                resource = f"customers/1234567890/adGroupAds/{base + 2}~{base + 3}"
            else:
                resource = f"customers/1234567890/{name}s/{base + index}"
            results.append({f"{name}Result": {"resourceName": resource}})
        return results

    # --- transport -------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        for fragment, (status, payload) in self.fail.items():
            if fragment in path:
                return httpx.Response(status, json=payload)
        if path.endswith("customers:listAccessibleCustomers"):
            return httpx.Response(
                200, json={"resourceNames": ["customers/111", "customers/1234567890"]}
            )
        if path.endswith("/googleAds:search"):
            assert body is not None
            return httpx.Response(200, json={"results": self.rows(body["query"])})
        if path.endswith(":mutate"):
            assert body is not None
            self.mutations.append({"path": path, **body})
            if body.get("validateOnly"):
                return httpx.Response(200, json={})
            self.created_ids += 1
            if "mutateOperations" in body:
                return httpx.Response(
                    200,
                    json={
                        "mutateOperationResponses": self._operation_results(
                            body["mutateOperations"]
                        )
                    },
                )
            if path.endswith("adGroupAds:mutate") and body["operations"][0].get("create"):
                name = f"customers/1234567890/adGroupAds/100~{5000 + self.created_ids}"
            else:
                name = f"customers/1234567890/x/{self.created_ids}"
            return httpx.Response(200, json={"results": [{"resourceName": name}]})
        return httpx.Response(404, json={"error": {"message": "no route", "status": "NOT_FOUND"}})
