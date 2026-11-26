import json

import pytest
from django.test import override_settings

from actions.models import ProposedAction
from ads import alerts
from ads.google import conversions
from ads.google import sync as google_sync
from ads.models import AdAlert, AdEntity, AdMetricDaily
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service

pytestmark = pytest.mark.django_db

STORE = "/api/v1/ads/openai/store"
GOOGLE = "/api/v1/ads/google"


def _h(workspace):
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr("ads.openai.variants.transaction.on_commit", lambda fn: fn())
    monkeypatch.setattr("ads.openai.review.transaction.on_commit", lambda fn: fn())
    monkeypatch.setattr("ads.google.keywords.transaction.on_commit", lambda fn: fn())
    yield
    set_llm_service(None)


@pytest.fixture
def synced(google_connection, fake):
    google_sync.sync_connection(google_connection)
    fake.mutations.clear()
    return google_connection


def _entity(level, external_id):
    return AdEntity.objects.get(level=level, external_id=external_id)


def _post(client, workspace, path, body=None):
    return client.post(
        path, data=json.dumps(body or {}), content_type="application/json", **_h(workspace)
    )


# ---- setup ----------------------------------------------------------------------------------


def test_the_setup_status_lists_missing_app_keys(logged_in, workspace):
    with override_settings(
        GOOGLE_OAUTH_CLIENT_ID="id", GOOGLE_OAUTH_CLIENT_SECRET="", GOOGLE_ADS_DEVELOPER_TOKEN=""
    ):
        body = logged_in.get(f"{GOOGLE}/status", **_h(workspace)).json()
    assert body["configured"] is False
    assert body["missing"] == ["client_secret"]  # the developer token is optional
    assert body["redirect_uri"].endswith("/bff/api/v1/connections/oauth/google_ads/callback")


def test_app_keys_added_as_a_connection_complete_the_setup(logged_in, workspace):
    with override_settings(
        GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="", GOOGLE_ADS_DEVELOPER_TOKEN=""
    ):
        created = _post(
            logged_in,
            workspace,
            "/api/v1/connections/",
            {
                "provider": "google_ads_app",
                "external_account_id": "google-ads-app",
                "display_name": "Google Ads app keys",
                "fields": {"client_id": "cid", "client_secret": "sec", "developer_token": "dev"},
            },
        )
        assert created.status_code == 200, created.content
        assert created.json()["status"] == "active"
        body = logged_in.get(f"{GOOGLE}/status", **_h(workspace)).json()
        assert body["configured"] is True and body["missing"] == []
        from ads.google.credentials import app_credentials

        keys = app_credentials(workspace.pk)
        assert (keys.client_id, keys.client_secret, keys.developer_token) == ("cid", "sec", "dev")


def test_accounts_under_a_manager_are_listed_and_one_can_be_selected(
    logged_in, workspace, google_connection, fake
):
    listing = logged_in.get(
        f"{GOOGLE}/accounts", {"connection_id": str(google_connection.pk)}, **_h(workspace)
    ).json()
    by_id = {a["id"]: a for a in listing["accounts"]}
    assert (
        by_id["1234567890"]["name"] == "Acme" and by_id["1234567890"]["login_customer_id"] == "111"
    )
    assert by_id["111"]["manager"] is True
    result = _post(
        logged_in,
        workspace,
        f"{GOOGLE}/accounts/select",
        {
            "connection_id": str(google_connection.pk),
            "customer_id": "123-456-7890",
            "login_customer_id": "111",
        },
    )
    assert result.status_code == 200, result.content
    google_connection.granted_scopes = ["https://www.googleapis.com/auth/adwords"]
    google_connection.save()
    from connections import lifecycle

    lifecycle.check_connection(google_connection)
    google_connection.refresh_from_db()
    assert (
        google_connection.external_account_id == "1234567890"
        and google_connection.config["login_customer_id"] == "111"
    )
    assert google_connection.status == "active", google_connection.health_detail
    assert _post(
        logged_in,
        workspace,
        f"{GOOGLE}/accounts/select",
        {"connection_id": str(google_connection.pk), "customer_id": "12"},
    ).status_code in {400, 422}


def test_a_manager_account_is_reported_as_needing_a_client_account(google_connection, fake):
    from ads.google import provider

    fake_rows = fake.rows
    fake.rows = lambda q: (
        [{"customer": {"id": "111", "status": "ENABLED", "manager": True}}]
        if "FROM customer LIMIT 1" in q
        else fake_rows(q)
    )  # type: ignore[method-assign]
    result = provider.health_check(google_connection)
    assert result.status == "degraded" and "manager" in result.detail


# ---- shared screens work for Google entities -------------------------------------------------


def test_the_shared_tree_and_dashboards_serve_google_entities(logged_in, workspace, synced):
    tree = logged_in.get(f"{STORE}/tree", {"connection_id": str(synced.pk)}, **_h(workspace)).json()
    names = {c["name"]: c for c in tree["campaigns"]}
    assert (
        names["Search - IT"]["row"]["clicks_7d"] == 15
        and names["PMax - Software"]["row"]["ads_count"] == 1
    )

    ad = logged_in.get(f"{STORE}/entities/{_entity('ad', '1000').pk}", **_h(workspace)).json()
    codes = {c["code"]: c for c in ad["checks"]}
    assert codes["rsa_few_headlines"]["params"] == {"count": 3, "target": 15}
    assert (
        codes["rsa_few_descriptions"]["level"] == "warn"
        and codes["ad_strength_low"]["params"]["strength"] == "poor"
    )
    assert (
        ad["platform"]["ad_strength"] == "POOR"
        and ad["totals"]["last_7_days"]["conversions"] == 4.5
    )

    group = logged_in.get(
        f"{STORE}/entities/{_entity('ad_group', '100').pk}", **_h(workspace)
    ).json()
    assert {c["level"] for c in group["children"]} == {"ad", "keyword"}
    assert any(c["code"] == "low_quality_keywords" for c in group["checks"])

    campaign = logged_in.get(
        f"{STORE}/entities/{_entity('campaign', '10').pk}", **_h(workspace)
    ).json()
    codes = {c["code"]: c for c in campaign["checks"]}
    assert codes["budget_limited"]["params"] == {"lost": 30} and "rank_limited" not in codes
    assert campaign["platform"]["impression_share"]["share"] == 0.42

    pmax = logged_in.get(
        f"{STORE}/entities/{_entity('asset_group', '200').pk}", **_h(workspace)
    ).json()
    missing = {c["params"]["field"] for c in pmax["checks"] if c["code"] == "assets_missing"}
    assert missing == {"headline", "description", "square_marketing_image", "logo", "long_headline"}
    assert pmax["level"] == "asset_group" and pmax["row"]["clicks_7d"] == 15


def test_the_shared_change_endpoint_uses_the_google_action_kinds(
    logged_in, workspace, synced, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        result = _post(
            logged_in,
            workspace,
            "/api/v1/ads/openai/changes",
            {
                "connection_id": str(synced.pk),
                "kind": "pause",
                "payload": {"resource": "campaigns", "resource_id": "10"},
                "apply_now": True,
            },
        )
    assert result.status_code == 200, result.content
    action = ProposedAction.objects.get(pk=result.json()["action_id"])
    assert action.kind == "ads.google.pause" and result.json()["applied"] is True
    assert _entity("campaign", "10").status == "paused"


# ---- AI ---------------------------------------------------------------------------------------


def _rsa(n):
    return {
        "headlines": [f"Headline number {i} here" for i in range(n)],
        "descriptions": [
            "Managed IT with predictable pricing.",
            "On-site help across Metro Vancouver.",
            "Call for a free quote today.",
            "Secure, reliable IT for teams.",
        ],
        "angle": "Place first",
        "rationale": "Names the service area.",
    }


def test_rsa_versions_are_written_and_choosing_one_updates_the_google_ad(
    logged_in, workspace, synced, fake, django_capture_on_commit_callbacks
):
    gateway = FakeGateway([text_response(json.dumps({"variants": [_rsa(12), _rsa(13), _rsa(14)]}))])
    set_llm_service(LlmService(gateway))
    ad = _entity("ad", "1000")
    started = _post(
        logged_in,
        workspace,
        f"{STORE}/entities/{ad.pk}/variants",
        {"instruction": "Mention Vancouver"},
    )
    assert started.status_code == 200, started.content
    stored = logged_in.get(f"{STORE}/entities/{ad.pk}/variants", **_h(workspace)).json()[0]
    assert stored["status"] == "done" and len(stored["variants"][0]["headlines"]) == 12
    prompt = gateway.requests[-1][0].system[0].text
    assert "responsive search ad" in prompt.lower() and "Mention Vancouver" in prompt

    with django_capture_on_commit_callbacks(execute=True):
        chosen = _post(
            logged_in, workspace, f"{STORE}/variants/{stored['id']}/choose", {"index": 1}
        )
    assert chosen.status_code == 200, chosen.content
    action = ProposedAction.objects.get(pk=chosen.json()["action_id"])
    assert action.kind == "ads.google.ad.update_rsa" and len(action.payload["headlines"]) == 13
    assert _entity("ad", "1000").data["creative"]["headlines"][0] == "Headline number 0 here"


def test_rsa_versions_with_a_headline_over_30_characters_are_repaired(logged_in, workspace, synced):
    bad = _rsa(12)
    bad["headlines"][0] = "x" * 40
    set_llm_service(
        LlmService(
            FakeGateway(
                [
                    text_response(json.dumps({"variants": [bad, _rsa(12), _rsa(12)]})),
                    text_response(json.dumps({"variants": [_rsa(12), _rsa(12), _rsa(12)]})),
                ]
            )
        )
    )
    ad = _entity("ad", "1000")
    _post(logged_in, workspace, f"{STORE}/entities/{ad.pk}/variants", {})
    assert (
        logged_in.get(f"{STORE}/entities/{ad.pk}/variants", **_h(workspace)).json()[0]["status"]
        == "done"
    )


def test_the_ad_review_names_the_platform_and_sees_google_diagnostics(logged_in, workspace, synced):
    verdict = {
        "verdict": "needs_attention",
        "score": 55,
        "summary": "Weak ad strength.",
        "strengths": [],
        "issues": [],
        "next_steps": ["Add headlines"],
        "since_last_review": {
            "assessment": "first_review",
            "explanation": "First.",
            "regressions": [],
        },
    }
    gateway = FakeGateway([text_response(json.dumps(verdict))])
    set_llm_service(LlmService(gateway))
    ad = _entity("ad", "1000")
    response = _post(logged_in, workspace, f"{STORE}/entities/{ad.pk}/reviews")
    assert response.status_code == 200, response.content
    prompt = gateway.requests[-1][0].system[0].text
    assert "Google Ads" in prompt and "responsive search ads" in prompt
    assert "rsa_few_headlines" in prompt and "POOR" in prompt


def test_keyword_suggestions_come_from_real_search_terms_and_apply_as_actions(
    logged_in, workspace, synced, fake, django_capture_on_commit_callbacks
):
    original = fake.rows
    fake.rows = lambda q: (
        [  # type: ignore[method-assign]
            {
                "searchTermView": {"searchTerm": "free it jobs", "status": "NONE"},
                "metrics": {
                    "impressions": "50",
                    "clicks": "9",
                    "costMicros": "31000000",
                    "conversions": 0,
                },
            },
            {
                "searchTermView": {
                    "searchTerm": "managed it nonprofit vancouver",
                    "status": "NONE",
                },
                "metrics": {
                    "impressions": "80",
                    "clicks": "12",
                    "costMicros": "40000000",
                    "conversions": 3,
                },
            },
        ]
        if "search_term_view" in q
        else original(q)
    )
    result = {
        "negatives": [
            {
                "text": "jobs",
                "match_type": "phrase",
                "scope": "campaign",
                "reason": "9 clicks, no sale",
            }
        ],
        "additions": [
            {
                "text": "managed it nonprofit vancouver",
                "match_type": "exact",
                "reason": "3 conversions",
            },
            {"text": "managed it nonprofit", "match_type": "phrase", "reason": "already a keyword"},
        ],
        "pauses": [
            {"criterion_id": "7", "text": "managed it nonprofit", "reason": "low quality"},
            {"criterion_id": "999", "text": "ghost", "reason": "not real"},
        ],
        "summary": "Two issues.",
        "notes": "",
    }
    gateway = FakeGateway([text_response(json.dumps(result))])
    set_llm_service(LlmService(gateway))
    group = _entity("ad_group", "100")
    started = _post(logged_in, workspace, f"{GOOGLE}/keywords/entities/{group.pk}", {})
    assert started.status_code == 200, started.content
    proposal = logged_in.get(f"{GOOGLE}/keywords/entities/{group.pk}", **_h(workspace)).json()[0]
    assert proposal["status"] == "done"
    assert [a["text"] for a in proposal["additions"]] == [
        "managed it nonprofit vancouver"
    ]  # duplicates dropped
    assert [p["criterion_id"] for p in proposal["pauses"]] == ["7"]  # invented ids dropped
    assert "free it jobs" in gateway.requests[-1][0].system[0].text

    with django_capture_on_commit_callbacks(execute=True):
        applied = _post(
            logged_in,
            workspace,
            f"{GOOGLE}/keywords/{proposal['id']}/apply",
            {"negatives": [0], "additions": [0], "pauses": [0]},
        )
    assert applied.status_code == 200, applied.content
    kinds = sorted(ProposedAction.objects.values_list("kind", flat=True))
    assert kinds == [
        "ads.google.keyword.pause",
        "ads.google.keywords.add",
        "ads.google.negatives.add",
    ]
    assert (
        _post(
            logged_in, workspace, f"{GOOGLE}/keywords/{proposal['id']}/apply", {"negatives": [0]}
        ).status_code
        == 409
    )


# ---- conversions and alerts --------------------------------------------------------------------


def test_the_conversion_audit_finds_untracked_clicks_and_setup_problems(synced, google_connection):
    from ads.models import AdSyncState

    state = AdSyncState.objects.get(connection=google_connection)
    state.account = {
        **state.account,
        "conversion_actions": [
            {
                "id": "1",
                "name": "Lead form",
                "status": "enabled",
                "category": "SUBMIT_LEAD_FORM",
                "primary": True,
                "counting": "MANY_PER_CLICK",
                "attribution": "GOOGLE_ADS_LAST_CLICK",
            },
            {
                "id": "2",
                "name": "Contact page",
                "status": "enabled",
                "category": "SUBMIT_LEAD_FORM",
                "primary": True,
                "counting": "ONE_PER_CLICK",
            },
        ],
    }
    state.save()
    AdMetricDaily.objects.filter(entity__level__in=["ad", "asset_group"]).update(
        conversions=0, clicks=200
    )
    codes = {f["code"] for f in conversions.audit(google_connection)["findings"]}
    assert {"duplicate_primary", "many_per_click_leads", "last_click", "tracking_silent"} <= codes


def test_alerts_open_stay_and_resolve_as_the_account_changes(synced, google_connection):
    ad = _entity("ad", "1000")
    ad.review_status = "rejected"
    ad.save()
    first = alerts.evaluate(google_connection)
    assert first["opened"] >= 1
    assert AdAlert.objects.filter(
        code="ad_rejected", entity=ad, resolved_at__isnull=True, severity="high"
    ).exists()
    assert AdAlert.objects.filter(
        code="budget_limited"
    ).exists()  # 30% of impressions lost to budget
    again = alerts.evaluate(google_connection)
    assert again["opened"] == 0
    ad.review_status = "approved"
    ad.save()
    assert alerts.evaluate(google_connection)["resolved"] >= 1
    assert not AdAlert.objects.filter(code="ad_rejected", resolved_at__isnull=True).exists()


def test_alerts_are_listed_and_can_be_acknowledged(logged_in, workspace, synced, google_connection):
    ad = _entity("ad", "1000")
    ad.review_status = "rejected"
    ad.save()
    alerts.evaluate(google_connection)
    listing = logged_in.get(
        f"{STORE}/alerts", {"connection_id": str(synced.pk)}, **_h(workspace)
    ).json()
    assert listing[0]["severity"] == "high" and listing[0]["code"] == "ad_rejected"
    acknowledged = _post(logged_in, workspace, f"{STORE}/alerts/{listing[0]['id']}/acknowledge")
    assert acknowledged.json()["acknowledged"] is True
