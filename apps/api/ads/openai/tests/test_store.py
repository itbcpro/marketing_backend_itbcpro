# ruff: noqa: F811 - pytest fixtures are imported and re-declared as arguments
import json
from datetime import timedelta

import httpx
import pytest
from django.test import Client
from django.utils import timezone

from ads.models import AdChange, AdEntity, AdMetricDaily, AdReview
from ads.openai import actions
from ads.openai.client import OpenAiAdsClient
from ads.openai.tests.test_openai_ads import connection  # noqa: F401 - fixture
from conftest import make_user
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service
from workspaces.models import Role, Workspace

pytestmark = pytest.mark.django_db

BASE = "/api/v1/ads/openai/store"


class Platform:
    """A tiny ad account: one campaign, one ad group, one ad and three days of numbers."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ad_title = "Managed IT for nonprofits"
        self.status = "active"

    def _ad(self) -> dict:
        return {
            "id": "ad_1",
            "name": "Nonprofit IT A",
            "status": self.status,
            "review_status": "approved",
            "creative": {
                "type": "chat_card",
                "title": self.ad_title,
                "body": "Predictable monthly pricing.",
                "target_url": "https://itbcpro.ca/x",
                "file_id": "file_1",
                "image_url": "https://cdn/x.png",
            },
        }

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        path = request.url.path.removeprefix("/v1")
        self.calls.append(path)
        listing = {"object": "list", "has_more": False}
        if path == "/ad_account":
            return httpx.Response(
                200, json={"id": "adacct_1", "currency_code": "USD", "name": "Acme"}
            )
        if path == "/campaigns":
            row = {
                "id": "cmp_1",
                "name": "Spring",
                "status": "active",
                "budget": {"daily_spend_limit_micros": 10_000_000},
            }
            return httpx.Response(200, json={**listing, "data": [row]})
        if path == "/ad_groups":
            row = {
                "id": "grp_1",
                "name": "Nonprofits",
                "status": "active",
                "bid_too_low": True,
                "serving_issues": [{"code": "campaign_not_active"}],
                "bidding_config": {
                    "strategy": "fixed_bid",
                    "billing_event_type": "click",
                    "max_bid_micros": 2_000_000,
                },
                "context_hints": [
                    "it support",
                    "or comparing managed providers",
                    "and also pricing",
                ],
            }
            return httpx.Response(200, json={**listing, "data": [row]})
        if path == "/ads":
            return httpx.Response(200, json={**listing, "data": [self._ad()]})
        if path == "/ad_account/insights":
            today = timezone.now().date()
            rows = [
                {
                    "ad_id": "ad_1",
                    "readable_time": (today - timedelta(days=i)).isoformat(),
                    "impressions": 100,
                    "clicks": 5,
                    "spend": 2.5,
                }
                for i in range(3)
            ]
            return httpx.Response(200, json={**listing, "data": rows})
        if request.method == "POST" and path == "/ads/ad_1":
            creative = (json.loads(request.content) or {}).get("creative") or {}
            self.ad_title = creative.get("title", self.ad_title)
            return httpx.Response(200, json=self._ad())
        return httpx.Response(404, text="nope")


@pytest.fixture
def platform(monkeypatch):
    fake = Platform()
    monkeypatch.setattr(
        actions,
        "client_factory",
        lambda c: OpenAiAdsClient(
            c, api_key="k", transport=httpx.MockTransport(fake.handler), throttle=lambda _: None
        ),
    )
    monkeypatch.setattr("ads.openai.review.transaction.on_commit", lambda fn: fn())
    yield fake
    set_llm_service(None)


def _h(workspace):
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def _sync(client, workspace, connection):
    return client.post(f"{BASE}/sync?connection_id={connection.pk}", **_h(workspace))


def _verdict(verdict="needs_attention", since="first_review"):
    return {
        "verdict": verdict,
        "score": 62,
        "summary": "Delivers, but the headline is generic.",
        "strengths": ["Clear offer"],
        "issues": [
            {
                "severity": "medium",
                "area": "creative",
                "problem": "Headline is generic",
                "evidence": "CTR 5%",
                "suggestion": "Name the place",
                "fix": {
                    "type": "ad_text",
                    "new_title": "Managed IT in Vancouver",
                    "new_body": None,
                    "new_hints": None,
                    "new_max_bid": None,
                },
            }
        ],
        "since_last_review": {"assessment": since, "explanation": "First look.", "regressions": []},
        "next_steps": ["Wait a week"],
    }


def _ad_id(connection):
    return AdEntity.objects.get(connection=connection, level="ad").pk


def test_sync_stores_the_account_and_the_tree_reads_it_without_calling_the_platform(
    logged_in, workspace, connection, platform
) -> None:
    response = _sync(logged_in, workspace, connection)
    assert response.status_code == 200, response.content
    assert response.json()["counts"] == {"campaigns": 1, "ad_groups": 1, "ads": 1, "metric_days": 3}
    assert AdMetricDaily.objects.count() == 3

    platform.calls.clear()
    tree = logged_in.get(
        f"{BASE}/tree", {"connection_id": str(connection.pk)}, **_h(workspace)
    ).json()
    assert platform.calls == []
    ad = tree["campaigns"][0]["ad_groups"][0]["ads"][0]
    assert ad["name"] == "Nonprofit IT A" and ad["entity_id"]
    assert tree["sync"]["currency"] == "USD" and tree["sync"]["synced_at"]


def test_a_second_sync_records_what_changed_and_does_not_duplicate(
    logged_in, workspace, connection, platform
) -> None:
    _sync(logged_in, workspace, connection)
    platform.ad_title = "Managed IT in Vancouver"
    platform.status = "paused"
    _sync(logged_in, workspace, connection)
    assert AdEntity.objects.count() == 3 and AdMetricDaily.objects.count() == 3
    diff = AdChange.objects.get().diff
    assert diff["status"] == {"from": "active", "to": "paused"}
    assert diff["creative"]["to"]["title"] == "Managed IT in Vancouver"


def test_the_ad_page_data_comes_from_the_database(
    logged_in, workspace, connection, platform
) -> None:
    _sync(logged_in, workspace, connection)
    platform.calls.clear()
    detail = logged_in.get(
        f"{BASE}/entities/{_ad_id(connection)}", {"days": 30}, **_h(workspace)
    ).json()
    assert platform.calls == []
    assert detail["campaign"]["name"] == "Spring" and detail["ad_group"]["name"] == "Nonprofits"
    assert len(detail["days"]) == 3
    assert detail["totals"]["last_7_days"]["clicks"] == 15
    assert detail["totals"]["last_7_days"]["ctr"] == 0.05


def test_a_review_is_stored_and_the_next_one_is_compared_with_it(
    logged_in, workspace, connection, platform
) -> None:
    gateway = FakeGateway(
        [
            text_response(json.dumps(_verdict())),
            text_response(json.dumps(_verdict("healthy", "improved"))),
        ]
    )
    set_llm_service(LlmService(gateway))
    _sync(logged_in, workspace, connection)
    ad_id = _ad_id(connection)

    first = logged_in.post(f"{BASE}/entities/{ad_id}/reviews", **_h(workspace))
    assert first.status_code == 200, first.content
    stored = logged_in.get(f"{BASE}/reviews/{first.json()['id']}", **_h(workspace)).json()
    assert stored["status"] == "done" and stored["result"]["verdict"] == "needs_attention"
    assert stored["comparison"] is None

    platform.ad_title = "Managed IT in Vancouver"
    _sync(logged_in, workspace, connection)
    second = logged_in.post(f"{BASE}/entities/{ad_id}/reviews", **_h(workspace)).json()
    again = logged_in.get(f"{BASE}/reviews/{second['id']}", **_h(workspace)).json()
    assert again["previous_id"] == first.json()["id"]
    assert (
        again["comparison"]["changes"][0]["diff"]["creative"]["to"]["title"]
        == "Managed IT in Vancouver"
    )
    assert "Managed IT in Vancouver" in gateway.requests[-1][0].system[0].text
    assert "Headline is generic" in gateway.requests[-1][0].system[0].text  # the previous verdict
    assert AdReview.objects.filter(status="done").count() == 2


def test_a_review_needs_a_sync_first_and_survives_model_failure(
    logged_in, workspace, connection, platform
) -> None:
    set_llm_service(LlmService(FakeGateway([text_response("not json")] * 3)))
    _sync(logged_in, workspace, connection)
    ad_id = _ad_id(connection)
    review = logged_in.post(f"{BASE}/entities/{ad_id}/reviews", **_h(workspace)).json()
    stored = logged_in.get(f"{BASE}/reviews/{review['id']}", **_h(workspace)).json()
    assert stored["status"] == "failed" and stored["error_code"]


def test_review_before_any_sync_is_a_conflict(logged_in, workspace, connection, platform) -> None:
    from ads.models import AdEntity as Entity

    Entity.objects.create(
        workspace=workspace, connection=connection, level="ad", external_id="ad_9", name="x"
    )
    ad = Entity.objects.get(external_id="ad_9")
    response = logged_in.post(f"{BASE}/entities/{ad.pk}/reviews", **_h(workspace))
    assert response.status_code == 409


def test_account_review_covers_all_ads(logged_in, workspace, connection, platform) -> None:
    gateway = FakeGateway([text_response(json.dumps(_verdict("healthy")))])
    set_llm_service(LlmService(gateway))
    _sync(logged_in, workspace, connection)
    response = logged_in.post(
        f"{BASE}/account-review?connection_id={connection.pk}", **_h(workspace)
    )
    assert response.status_code == 200, response.content
    listing = logged_in.get(
        f"{BASE}/account-reviews", {"connection_id": str(connection.pk)}, **_h(workspace)
    ).json()
    assert listing[0]["status"] == "done" and listing[0]["verdict"] == "healthy"
    assert "Nonprofit IT A" in gateway.requests[-1][0].system[0].text


def test_another_workspace_cannot_see_the_ad(logged_in, workspace, connection, platform) -> None:
    _sync(logged_in, workspace, connection)
    other = Workspace.objects.create(name="Other", slug="other-store")
    stranger = Client()
    stranger.force_login(make_user("s@example.com", other, Role.OWNER))
    response = stranger.get(f"{BASE}/entities/{_ad_id(connection)}", **_h(other))
    assert response.status_code == 404


def test_the_ads_list_combines_numbers_platform_flags_and_the_last_verdict(
    logged_in, workspace, connection, platform
) -> None:
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(_verdict()))])))
    _sync(logged_in, workspace, connection)
    logged_in.post(f"{BASE}/entities/{_ad_id(connection)}/reviews", **_h(workspace))
    platform.calls.clear()
    out = logged_in.get(
        f"{BASE}/ads", {"connection_id": str(connection.pk)}, **_h(workspace)
    ).json()
    assert platform.calls == []
    row = out["ads"][0]
    assert row["title"] == "Managed IT for nonprofits" and row["campaign"] == "Spring"
    assert row["clicks_7d"] == 15 and row["spend_7d"] == 7.5 and row["ctr_7d"] == 0.05
    assert row["bid_too_low"] is True and row["issue_count"] == 1
    assert row["verdict"] == "needs_attention" and row["score"] == 62
    assert out["totals_7d"]["clicks"] == 15 and out["currency"] == "USD"


def test_the_detail_shows_the_platform_analysis_and_automatic_checks(
    logged_in, workspace, connection, platform
) -> None:
    _sync(logged_in, workspace, connection)
    detail = logged_in.get(f"{BASE}/entities/{_ad_id(connection)}", **_h(workspace)).json()
    assert detail["platform"]["bid_too_low"] is True
    assert detail["platform"]["serving_issues"] == [
        {"level": "ad_group", "code": "campaign_not_active"}
    ]
    codes = {c["code"]: c for c in detail["checks"]}
    assert codes["serving_issues"]["level"] == "fail"
    assert codes["bid_too_low"]["level"] == "warn"
    assert codes["hints_few"]["params"]["count"] == 3
    assert codes["hints_fragmented"]["params"]["count"] == 2
    assert codes["review_approved"]["level"] == "ok"


def _review_with_fix(logged_in, workspace, connection, platform, fix):
    verdict = _verdict()
    verdict["issues"][0]["fix"] = fix
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(verdict))])))
    _sync(logged_in, workspace, connection)
    review = logged_in.post(f"{BASE}/entities/{_ad_id(connection)}/reviews", **_h(workspace)).json()
    return review["id"]


def _apply(client, workspace, review_id, apply_now=False):
    return client.post(
        f"{BASE}/reviews/{review_id}/issues/0/apply",
        data=json.dumps({"apply_now": apply_now}),
        content_type="application/json",
        **_h(workspace),
    )


FIX = {
    "type": "ad_text",
    "new_title": "Managed IT in Vancouver",
    "new_body": None,
    "new_hints": None,
    "new_max_bid": None,
}


def test_applying_a_suggestion_creates_one_proposed_action_with_the_full_creative(
    logged_in, workspace, connection, platform
) -> None:
    from actions.models import ProposedAction

    review_id = _review_with_fix(logged_in, workspace, connection, platform, FIX)
    response = _apply(logged_in, workspace, review_id)
    assert response.status_code == 200, response.content
    action = ProposedAction.objects.get(pk=response.json()["action_id"])
    assert action.kind == "ads.openai.ad.update" and action.status == "awaiting_approval"
    creative = action.payload["creative"]
    assert creative["title"] == "Managed IT in Vancouver"
    assert creative["body"] == "Predictable monthly pricing." and creative["file_id"] == "file_1"
    assert _apply(logged_in, workspace, review_id).status_code == 409
    stored = logged_in.get(f"{BASE}/reviews/{review_id}", **_h(workspace)).json()
    assert "0" in stored["applied"]


def test_hint_bid_and_pause_suggestions_become_the_right_actions(
    logged_in, workspace, connection, platform
) -> None:
    from actions.models import ProposedAction

    cases = [
        (
            {
                "type": "context_hints",
                "new_title": None,
                "new_body": None,
                "new_max_bid": None,
                "new_hints": ["managed IT for nonprofits", "IT support for charities in Vancouver"],
            },
            "ads.openai.ad_group.update",
        ),
        (
            {
                "type": "max_bid",
                "new_title": None,
                "new_body": None,
                "new_hints": None,
                "new_max_bid": 2.5,
            },
            "ads.openai.ad_group.update_bid",
        ),
        (
            {
                "type": "pause_ad",
                "new_title": None,
                "new_body": None,
                "new_hints": None,
                "new_max_bid": None,
            },
            "ads.openai.pause",
        ),
    ]
    for fix, kind in cases:
        AdReview.objects.all().delete()
        review_id = _review_with_fix(logged_in, workspace, connection, platform, fix)
        action = ProposedAction.objects.get(
            pk=_apply(logged_in, workspace, review_id).json()["action_id"]
        )
        assert action.kind == kind
    assert action.payload["resource_id"] == "ad_1"


def test_an_editor_can_propose_a_suggestion_but_not_apply_it_directly(
    logged_in, workspace, connection, platform
) -> None:
    review_id = _review_with_fix(logged_in, workspace, connection, platform, FIX)
    editor = Client()
    editor.force_login(make_user("ed2@example.com", workspace, Role.EDITOR))
    result = _apply(editor, workspace, review_id, apply_now=True).json()
    assert result["applied"] is False and result["status"] == "awaiting_approval"


def test_a_suggestion_without_a_fix_cannot_be_applied(
    logged_in, workspace, connection, platform
) -> None:
    review_id = _review_with_fix(logged_in, workspace, connection, platform, None)
    assert _apply(logged_in, workspace, review_id).status_code in {400, 422}


def _entity_id(connection, level):
    return AdEntity.objects.get(connection=connection, level=level).pk


def test_a_campaign_dashboard_lists_its_ad_groups_and_rolls_numbers_up(
    logged_in, workspace, connection, platform
) -> None:
    _sync(logged_in, workspace, connection)
    detail = logged_in.get(
        f"{BASE}/entities/{_entity_id(connection, 'campaign')}", **_h(workspace)
    ).json()
    assert detail["level"] == "campaign" and detail["campaign"]["name"] == "Spring"
    assert [c["name"] for c in detail["children"]] == ["Nonprofits"]
    assert detail["children"][0]["clicks_7d"] == 15 and detail["children"][0]["ads_count"] == 1
    assert detail["totals"]["last_7_days"]["clicks"] == 15
    assert [a["title"] for a in detail["ads"]] == ["Managed IT for nonprofits"]
    assert detail["row"]["bid_too_low"] is True  # a group beneath it has a low bid
    assert detail["platform"]["statuses"]["campaign"] == "active"


def test_an_ad_group_dashboard_lists_its_ads_and_checks_the_group(
    logged_in, workspace, connection, platform
) -> None:
    _sync(logged_in, workspace, connection)
    detail = logged_in.get(
        f"{BASE}/entities/{_entity_id(connection, 'ad_group')}", **_h(workspace)
    ).json()
    assert detail["level"] == "ad_group" and detail["ad_group"]["name"] == "Nonprofits"
    assert [a["title"] for a in detail["children"]] == ["Managed IT for nonprofits"]
    codes = {c["code"] for c in detail["checks"]}
    assert {"bid_too_low", "hints_few", "hints_fragmented", "single_ad"} <= codes
    assert "link_ok" not in codes  # ad-only check


def test_reviewing_an_ad_group_uses_all_its_ads_and_its_hints_fix_applies_to_the_group(
    logged_in, workspace, connection, platform
) -> None:
    from actions.models import ProposedAction

    verdict = _verdict()
    verdict["issues"][0]["fix"] = {
        "type": "context_hints",
        "new_title": None,
        "new_body": None,
        "new_max_bid": None,
        "new_hints": ["managed IT for nonprofits", "IT support for charities"],
    }
    gateway = FakeGateway([text_response(json.dumps(verdict))])
    set_llm_service(LlmService(gateway))
    _sync(logged_in, workspace, connection)
    group_id = _entity_id(connection, "ad_group")
    review = logged_in.post(f"{BASE}/entities/{group_id}/reviews", **_h(workspace)).json()
    stored = logged_in.get(f"{BASE}/reviews/{review['id']}", **_h(workspace)).json()
    assert stored["scope"] == "ad_group" and stored["status"] == "done"
    prompt = gateway.requests[-1][0].system[0].text
    assert '"children"' in prompt
    assert "Managed IT for nonprofits" in prompt  # the ad's headline is visible to the reviewer
    assert "or comparing managed providers" in prompt  # and so are the group's hints
    assert "looking at all of its ads together" in prompt

    response = _apply(logged_in, workspace, review["id"])
    assert response.status_code == 200, response.content
    action = ProposedAction.objects.get(pk=response.json()["action_id"])
    assert action.kind == "ads.openai.ad_group.update"
    assert action.payload["ad_group_id"] == "grp_1"


def test_the_tree_carries_numbers_for_every_level(
    logged_in, workspace, connection, platform
) -> None:
    _sync(logged_in, workspace, connection)
    tree = logged_in.get(
        f"{BASE}/tree", {"connection_id": str(connection.pk)}, **_h(workspace)
    ).json()
    campaign = tree["campaigns"][0]
    group = campaign["ad_groups"][0]
    assert campaign["row"]["clicks_7d"] == 15 and campaign["row"]["ads_count"] == 1
    assert group["row"]["children_count"] == 1 and group["ads"][0]["row"]["spend_7d"] == 7.5
