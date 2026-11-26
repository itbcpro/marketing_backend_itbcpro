import json

import httpx
import pytest
from django.test import override_settings

from ads.google import client as google_client
from ads.google.client import GoogleAdsClient
from ads.models import AdChange, AdEntity, AdMetricDaily, AdSyncState
from ads.openai import rows
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    PolicyRejectedError,
    RateLimitedError,
    RetryableProviderError,
)

pytestmark = pytest.mark.django_db


def _sync(connection):
    from ads.google import sync

    return sync.sync_connection(connection)


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (401, {"error": {"status": "UNAUTHENTICATED", "message": "expired"}}, AuthExpiredError),
        (
            429,
            {"error": {"status": "RESOURCE_EXHAUSTED", "message": "slow down"}},
            RateLimitedError,
        ),
        (503, {"error": {"status": "UNAVAILABLE", "message": "try later"}}, RetryableProviderError),
        (
            400,
            {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "Request contains an invalid argument.",
                    "details": [
                        {
                            "errors": [
                                {
                                    "errorCode": {"policyViolationError": "POLICY_ERROR"},
                                    "message": "Ad violates a policy.",
                                    "location": {
                                        "fieldPathElements": [{"fieldName": "operations"}]
                                    },
                                }
                            ]
                        }
                    ],
                }
            },
            PolicyRejectedError,
        ),
        (
            400,
            {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "bad",
                    "details": [
                        {
                            "errors": [
                                {
                                    "errorCode": {"fieldError": "REQUIRED"},
                                    "message": "Required field missing.",
                                    "location": {
                                        "fieldPathElements": [
                                            {"fieldName": "campaign"},
                                            {"fieldName": "name"},
                                        ]
                                    },
                                }
                            ]
                        }
                    ],
                }
            },
            PermanentProviderError,
        ),
    ],
)
def test_google_errors_are_mapped_to_domain_errors(google_connection, status, payload, error):
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json=payload))
    client = GoogleAdsClient(
        google_connection,
        access_token="t",
        developer_token="d",
        transport=transport,
        throttle=lambda _: None,
        refresher=lambda _: False,
    )
    with pytest.raises(error) as caught:
        client.search("SELECT customer.id FROM customer")
    if error is PermanentProviderError:
        assert "campaign.name" in str(caught.value)


def test_requests_carry_the_tokens_and_the_manager_account(google_connection, fake):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return fake.handler(request)

    client = GoogleAdsClient(
        google_connection,
        access_token="abc",
        developer_token="dev",
        transport=httpx.MockTransport(handler),
        throttle=lambda _: None,
    )
    client.search("SELECT customer.id FROM customer")
    headers = seen[0].headers
    assert headers["authorization"] == "Bearer abc" and headers["developer-token"] == "dev"
    assert headers["login-customer-id"] == "111"
    assert seen[0].url.path == "/v25/customers/1234567890/googleAds:search"


def test_search_follows_page_tokens(google_connection):
    pages = [
        {"results": [{"a": 1}], "nextPageToken": "next"},
        {"results": [{"a": 2}]},
    ]
    bodies: list[dict] = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=pages.pop(0))

    client = GoogleAdsClient(
        google_connection,
        access_token="t",
        developer_token="d",
        transport=httpx.MockTransport(handler),
        throttle=lambda _: None,
    )
    assert client.search("SELECT 1") == [{"a": 1}, {"a": 2}]
    assert "pageToken" not in bodies[0] and bodies[1]["pageToken"] == "next"


def test_an_expired_token_is_refreshed_once_and_the_call_repeated(google_connection):
    answers = [
        httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED", "message": "expired"}}),
        httpx.Response(200, json={"results": [{"ok": True}]}),
    ]
    refreshed: list[int] = []
    client = GoogleAdsClient(
        google_connection,
        access_token="old",
        developer_token="d",
        transport=httpx.MockTransport(lambda r: answers.pop(0)),
        throttle=lambda _: None,
        refresher=lambda c: bool(refreshed.append(1) or True),
    )
    client._token = "old"
    client._headers = lambda: {"Authorization": "Bearer new", "developer-token": "d"}  # type: ignore[method-assign]
    assert client.search("SELECT 1") == [{"ok": True}] and refreshed == [1]


def test_no_developer_token_is_needed_and_none_is_sent_without_one(google_connection):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    with override_settings(GOOGLE_ADS_DEVELOPER_TOKEN=""):
        client = GoogleAdsClient(
            google_connection, access_token="t", transport=httpx.MockTransport(handler),
            throttle=lambda _: None,
        )  # fmt: skip
        assert client.search("SELECT 1") == []
    assert "developer-token" not in seen[0].headers
    with override_settings(GOOGLE_ADS_DEVELOPER_TOKEN="legacy"):
        client = GoogleAdsClient(
            google_connection, access_token="t", transport=httpx.MockTransport(handler),
            throttle=lambda _: None,
        )  # fmt: skip
        client.search("SELECT 1")
    assert seen[1].headers["developer-token"] == "legacy"


def test_customer_ids_lose_their_dashes():
    assert google_client.customer_id("123-456-7890") == "1234567890"


def test_sync_copies_every_level_and_maps_google_fields(google_connection, fake):
    counts = _sync(google_connection)
    assert counts == {
        "campaigns": 2,
        "ad_groups": 1,
        "ads": 1,
        "asset_groups": 1,
        "keywords": 2,
        "metric_days": 9,
    }
    campaign = AdEntity.objects.get(level="campaign", external_id="10")
    assert campaign.status == "active" and campaign.data["budget"] == {
        "daily_spend_limit_micros": 20_000_000
    }
    assert campaign.data["impression_share"] == {
        "share": 0.42,
        "lost_budget": 0.3,
        "lost_rank": 0.28,
    }
    paused = AdEntity.objects.get(level="campaign", external_id="20")
    assert paused.status == "paused" and paused.data["serving_issues"] == [
        {"code": "campaign_paused"}
    ]
    ad = AdEntity.objects.get(level="ad", external_id="1000")
    assert (
        ad.review_status == "approved"
        and ad.data["creative"]["title"] == "Managed IT for nonprofits"
    )
    assert ad.data["creative"]["headlines"][1] == "Local IT support"
    assert ad.data["creative"]["pinned_headlines"] == {"Local IT support": "HEADLINE_1"}
    assert ad.data["ad_strength"] == "POOR" and ad.parent.external_id == "100"
    keyword = AdEntity.objects.get(level="keyword", external_id="100~7")
    assert keyword.name == "managed it nonprofit" and keyword.data["quality_score"] == 4
    assert AdEntity.objects.get(level="keyword", external_id="100~8").data["negative"] is True
    group = AdEntity.objects.get(level="ad_group", external_id="100")
    assert group.data["keyword_count"] == 1 and group.data["bidding_config"] == {
        "max_bid_micros": 2_000_000
    }
    asset_group = AdEntity.objects.get(level="asset_group", external_id="200")
    assert asset_group.parent.external_id == "20" and len(asset_group.data["assets"]) == 2
    assert asset_group.data["asset_keys"] == ["HEADLINE:900", "MARKETING_IMAGE:901"]
    state = AdSyncState.objects.get(connection=google_connection)
    assert (
        state.account["currency_code"] == "CAD"
        and state.account["conversion_actions"][0]["primary"] is True
    )


def test_sync_stores_daily_numbers_including_conversions_and_keeps_keywords_out_of_totals(
    google_connection, fake
):
    _sync(google_connection)
    ad = AdEntity.objects.get(level="ad", external_id="1000")
    day = AdMetricDaily.objects.filter(entity=ad).first()
    assert (day.impressions, day.clicks, day.spend_micros) == (100, 5, 2_500_000)
    assert day.conversions == 1.5 and day.conversion_value == 75.0
    assert AdMetricDaily.objects.filter(entity__level="keyword").count() == 3

    everything = list(AdEntity.objects.filter(connection=google_connection))
    built = rows.build_rows(everything)
    search = AdEntity.objects.get(level="campaign", external_id="10")
    pmax = AdEntity.objects.get(level="campaign", external_id="20")
    assert built[search.pk]["clicks_7d"] == 15  # the ad only, keywords are not added on top
    assert built[pmax.pk]["clicks_7d"] == 15  # the asset group carries Performance Max numbers
    assert built[search.pk]["conversions_7d"] == 4.5
    keyword = AdEntity.objects.get(level="keyword", external_id="100~7")
    assert built[keyword.pk]["clicks_7d"] == 15


def test_a_second_sync_records_changes_and_marks_removed_items(google_connection, fake):
    _sync(google_connection)
    fake.headline = "Managed IT in Vancouver"
    fake.status = "PAUSED"
    _sync(google_connection)
    diffs = [c.diff for c in AdChange.objects.all()]
    assert any(d.get("status") == {"from": "active", "to": "paused"} for d in diffs)
    assert any(
        "creative" in d and d["creative"]["to"]["title"] == "Managed IT in Vancouver" for d in diffs
    )
    original = fake.rows
    fake.rows = lambda q: [] if "FROM asset_group_asset" in q else original(q)  # type: ignore[method-assign]
    _sync(google_connection)
    assert AdEntity.objects.get(level="asset_group").data["asset_keys"] == []


def test_a_failed_sync_is_recorded_and_raised(google_connection, fake):
    fake.fail["googleAds:search"] = (503, {"error": {"status": "UNAVAILABLE", "message": "down"}})
    with pytest.raises(RetryableProviderError):
        _sync(google_connection)
    assert "down" in AdSyncState.objects.get(connection=google_connection).last_error


def test_search_requests_never_set_a_page_size(google_connection, fake):
    """Google rejects ``pageSize`` on recent API versions (the page size is fixed)."""
    from ads.google import sync

    sync.sync_connection(google_connection)
    assert fake.calls
    assert all("pageSize" not in (body or {}) for _, _, body in fake.calls)


def test_parallel_edit_conflicts_are_retryable_not_final():
    response = httpx.Response(
        400,
        json={
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": "Multiple requests were attempting to modify the same resource at once. Retry the request.",
            }
        },
    )
    with pytest.raises(RetryableProviderError):
        google_client.raise_for_response(response)
