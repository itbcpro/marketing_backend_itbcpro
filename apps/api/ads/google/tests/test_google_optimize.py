import json

import httpx
import pytest

from actions.models import ProposedAction
from ads.google import landing
from ads.google import sync as google_sync
from ads.models import AdEntity, AdOptimization
from content import net
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service

pytestmark = pytest.mark.django_db

GOOGLE = "/api/v1/ads/google"
GOOD_PAGE = """<html lang="en"><head><title>Managed IT for nonprofits in Vancouver</title>
<meta name="viewport" content="width=device-width"><meta name="description" content="IT help">
<script async src="https://www.googletagmanager.com/gtag/js?id=AW-123"></script></head>
<body><h1>Managed IT support for nonprofits</h1>
<p>{words}</p><form action="/contact"><input name="email"></form>
<a href="tel:+16045550100">Call</a><a href="/quote">Get a quote</a>
<p>Trusted by 40 nonprofits, 15 years of experience.</p></body></html>"""
BAD_PAGE = "<html><head><title>Home</title></head><body><p>Welcome.</p></body></html>"


def _h(workspace):
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def _post(client, workspace, path, body=None):
    return client.post(
        path, data=json.dumps(body or {}), content_type="application/json", **_h(workspace)
    )


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr("ads.google.optimize.transaction.on_commit", lambda fn: fn())
    monkeypatch.setattr(net, "resolver", lambda host, port: {"93.184.216.34"})
    yield
    set_llm_service(None)
    net.transport = None


def _serve(monkeypatch, html: str, status: int = 200):
    monkeypatch.setattr(
        net,
        "transport",
        httpx.MockTransport(lambda request: httpx.Response(status, text=html)),
    )


@pytest.fixture
def synced(google_connection, fake):
    google_sync.sync_connection(google_connection)
    fake.mutations.clear()
    return google_connection


# ---- landing page audit ----------------------------------------------------------------------


def test_a_good_page_scores_high_and_matches_the_ad(monkeypatch):
    _serve(monkeypatch, GOOD_PAGE.format(words="managed IT support for nonprofits " * 60))
    page = landing.audit("https://itbcpro.ca/nonprofit", ["Managed IT for nonprofits"])
    codes = {i["code"] for i in page["issues"]}
    assert page["reachable"] and page["score"] >= 90
    assert "message_match" in codes and "no_tracking" not in codes
    assert page["facts"]["forms"] == 1 and page["facts"]["phone_link"]
    assert "google_tag" in page["facts"]["tracking"]


def test_a_bare_page_shows_every_bottleneck(monkeypatch):
    _serve(monkeypatch, BAD_PAGE)
    page = landing.audit("https://itbcpro.ca/nonprofit", ["Managed IT for nonprofits"])
    codes = {i["code"] for i in page["issues"]}
    assert {"no_viewport", "no_next_step", "no_tracking", "message_mismatch", "thin_page"} <= codes
    assert page["score"] < 40


def test_insecure_and_unreachable_pages_are_findings_not_errors(monkeypatch):
    _serve(monkeypatch, BAD_PAGE, status=500)
    page = landing.audit("https://itbcpro.ca/x", [])
    assert page["reachable"] is False and page["score"] == 0
    assert page["issues"][0]["code"] == "http_error"
    _serve(monkeypatch, BAD_PAGE)
    plain = landing.audit("http://itbcpro.ca/x", [])
    assert plain["reachable"] and "not_https" in {i["code"] for i in plain["issues"]}
    monkeypatch.setattr(net, "resolver", lambda host, port: {"10.0.0.5"})
    private = landing.audit("https://intranet.example/x", [])
    assert private["issues"][0]["code"] == "unreachable"


def test_the_final_urls_of_a_campaign_are_collected_once(synced):
    ads = list(AdEntity.objects.filter(level="ad"))
    urls = landing.final_urls(ads)
    assert list(urls) == ["https://itbcpro.ca/nonprofit"]
    assert "Local IT support" in urls["https://itbcpro.ca/nonprofit"]


# ---- the optimiser ---------------------------------------------------------------------------

PLAN = {
    "summary": "The page has no tracking and two search terms waste money.",
    "expected_effect": "Lower cost per lead once tracking works.",
    "findings": [
        {
            "area": "tracking",
            "severity": "high",
            "title": "No conversion tag on the page",
            "detail": "Score 28.",
        }
    ],
    "negatives": [
        {
            "text": "jobs",
            "match_type": "phrase",
            "scope": "campaign",
            "ad_group_id": "",
            "reason": "9 clicks, no lead",
        },
        {
            "text": "diy",
            "match_type": "phrase",
            "scope": "ad_group",
            "ad_group_id": "100",
            "reason": "wasted",
        },
        {"text": "free", "match_type": "phrase", "scope": "campaign", "reason": "already blocked"},
        {"text": "bad!!", "match_type": "phrase", "scope": "campaign", "reason": "symbols"},
    ],
    "additions": [
        {
            "ad_group_id": "100",
            "text": "nonprofit it support",
            "match_type": "exact",
            "reason": "3 leads",
        },
        {
            "ad_group_id": "999",
            "text": "ghost group",
            "match_type": "exact",
            "reason": "no such group",
        },
    ],
    "pauses": [
        {"ad_group_id": "100", "criterion_id": "7", "text": "managed it", "reason": "no leads"},
        {"ad_group_id": "100", "criterion_id": "424242", "text": "ghost", "reason": "invented"},
    ],
    "rsa_rewrites": [
        {
            "ad_id": "1000",
            "headlines": ["Managed IT Nonprofits", "Vancouver IT Support", "Fast Local Help"],
            "descriptions": ["Local IT support for Vancouver nonprofits.", "Talk to us today."],
            "reason": "weak strength",
        },
        {
            "ad_id": "1000",
            "headlines": ["x" * 40, "b", "c"],
            "descriptions": ["a", "b"],
            "reason": "too long headline",
        },
        {
            "ad_id": "55",
            "headlines": ["One", "Two", "Three"],
            "descriptions": ["Four", "Five"],
            "reason": "not an ad of this campaign",
        },
    ],
    "budget_change": {"daily_budget": 80.0, "reason": "too big a jump"},
    "landing_tasks": [
        {
            "url": "https://itbcpro.ca/nonprofit",
            "problem": "No tracking tag",
            "fix": "Add the Google tag",
            "priority": "high",
        },
        {"url": "https://elsewhere.example/", "problem": "x", "fix": "y", "priority": "low"},
    ],
    "notes": "",
}


def _run(logged_in, workspace, fake, monkeypatch, plan=None):
    _serve(monkeypatch, BAD_PAGE)
    gateway = FakeGateway([text_response(json.dumps(plan or PLAN))])
    set_llm_service(LlmService(gateway))
    campaign = AdEntity.objects.get(level="campaign", external_id="10")
    started = _post(
        logged_in, workspace, f"{GOOGLE}/optimize/campaigns/{campaign.pk}", {"goal": "Cut cost"}
    )
    assert started.status_code == 200, started.content
    return gateway, logged_in.get(
        f"{GOOGLE}/optimize/{started.json()['id']}", **_h(workspace)
    ).json()


def test_the_plan_is_checked_against_real_data_before_anyone_sees_it(
    logged_in, workspace, synced, fake, monkeypatch
):
    gateway, run = _run(logged_in, workspace, fake, monkeypatch)
    plan = run["plan"]
    assert run["status"] == "done" and plan["findings"][0]["area"] == "tracking"
    assert [n["text"] for n in plan["negatives"]] == [
        "jobs",
        "diy",
    ]  # symbols and existing negatives dropped
    assert [a["text"] for a in plan["additions"]] == ["nonprofit it support"]
    assert [p["criterion_id"] for p in plan["pauses"]] == ["7"]
    assert len(plan["rsa_rewrites"]) == 1 and plan["rsa_rewrites"][0]["ad_id"] == "1000"
    assert plan["budget_change"] is None  # beyond 30 percent of the current budget
    assert [t["url"] for t in plan["landing_tasks"]] == ["https://itbcpro.ca/nonprofit"]
    assert any("budget" in d for d in plan["dropped"])
    assert run["landing"][0]["score"] < 40
    system = gateway.requests[-1][0].system[0].text
    assert "Cut cost" in system and "no_tracking" in system


def test_nothing_changes_until_items_are_applied_and_each_becomes_an_action(
    logged_in, workspace, synced, fake, monkeypatch, django_capture_on_commit_callbacks
):
    _, run = _run(logged_in, workspace, fake, monkeypatch)
    assert ProposedAction.objects.count() == 0 and not fake.mutations
    with django_capture_on_commit_callbacks(execute=True):
        applied = _post(
            logged_in,
            workspace,
            f"{GOOGLE}/optimize/{run['id']}/apply",
            {"keys": ["neg:0", "neg:1", "add:0", "pause:0", "rsa:0"], "apply_now": False},
        )
    assert applied.status_code == 200, applied.content
    kinds = sorted(ProposedAction.objects.values_list("kind", flat=True))
    assert kinds == [
        "ads.google.ad.update_rsa",
        "ads.google.keyword.pause",
        "ads.google.keywords.add",
        "ads.google.negatives.add",
        "ads.google.negatives.add",
    ]  # campaign and ad group negatives are separate actions
    assert all(a.status == "awaiting_approval" for a in ProposedAction.objects.all())
    assert not fake.mutations  # sending for approval changes nothing in Google Ads
    assert set(applied.json()["applied"]) == {"neg:0", "neg:1", "add:0", "pause:0", "rsa:0"}
    again = _post(logged_in, workspace, f"{GOOGLE}/optimize/{run['id']}/apply", {"keys": ["neg:0"]})
    assert again.status_code == 409


def test_applying_now_runs_the_actions_as_the_approver(
    logged_in, workspace, synced, fake, monkeypatch, django_capture_on_commit_callbacks
):
    _, run = _run(logged_in, workspace, fake, monkeypatch)
    with django_capture_on_commit_callbacks(execute=True):
        applied = _post(
            logged_in,
            workspace,
            f"{GOOGLE}/optimize/{run['id']}/apply",
            {"keys": ["neg:0"], "apply_now": True},
        )
    assert applied.status_code == 200, applied.content
    assert fake.mutations
    assert applied.json()["applied"]["neg:0"]["status"] in {"succeeded", "verified"}


def test_a_failed_model_call_leaves_the_run_failed_not_stuck(
    logged_in, workspace, synced, fake, monkeypatch
):
    from core.errors import RetryableProviderError

    _serve(monkeypatch, BAD_PAGE)
    set_llm_service(LlmService(FakeGateway([RetryableProviderError("down", provider="x")])))
    campaign = AdEntity.objects.get(level="campaign", external_id="10")
    started = _post(logged_in, workspace, f"{GOOGLE}/optimize/campaigns/{campaign.pk}", {})
    run = AdOptimization.objects.get(pk=started.json()["id"])
    assert run.status == "failed" and run.thinking_since is None


def test_a_campaign_budget_change_inside_the_limit_is_kept(
    logged_in, workspace, synced, fake, monkeypatch
):
    plan = {
        **PLAN,
        "budget_change": {"daily_budget": 24.0, "reason": "loses impressions to budget"},
    }
    _, run = _run(logged_in, workspace, fake, monkeypatch, plan)
    assert run["plan"]["budget_change"] == {
        "from_micros": 20_000_000,
        "to_micros": 24_000_000,
        "reason": "loses impressions to budget",
    }


def test_a_failed_item_shows_why_and_can_be_sent_again(
    logged_in, workspace, synced, fake, monkeypatch, django_capture_on_commit_callbacks
):
    _, run = _run(logged_in, workspace, fake, monkeypatch)
    fake.fail[":mutate"] = (
        400,
        {"error": {"status": "INVALID_ARGUMENT", "message": "A policy was violated. policy"}},
    )
    with django_capture_on_commit_callbacks(execute=True):
        sent = _post(
            logged_in,
            workspace,
            f"{GOOGLE}/optimize/{run['id']}/apply",
            {"keys": ["pause:0"], "apply_now": True},
        )
    state = sent.json()["applied"]["pause:0"]
    assert state["status"] in {"failed_final", "failed_retryable"} and state["error"]
    fake.fail.clear()
    with django_capture_on_commit_callbacks(execute=True):
        again = _post(
            logged_in,
            workspace,
            f"{GOOGLE}/optimize/{run['id']}/apply",
            {"keys": ["pause:0"], "apply_now": True},
        )
    assert again.status_code == 200, again.content
    assert again.json()["applied"]["pause:0"]["status"] in {"succeeded", "verified"}


def test_the_model_sees_every_ad_with_its_id_text_and_strength(
    logged_in, workspace, synced, fake, monkeypatch
):
    gateway, _ = _run(logged_in, workspace, fake, monkeypatch)
    system = gateway.requests[-1][0].system[0].text
    assert '"ad_id": "1000"' in system and '"ad_group_id": "100"' in system
    assert "Local IT support" in system and "Call us today" in system
    assert "One ad group should hold one tight theme" in system  # best practice is in the prompt


NEW_ADS_PLAN = {
    **PLAN,
    "new_ads": [
        {
            "ad_group_id": "100",
            "final_url": "https://itbcpro.ca/nonprofit",
            "headlines": ["Managed IT Nonprofits", "Vancouver IT Support", "Fast Local Help"],
            "descriptions": ["Local IT support for Vancouver nonprofits.", "Talk to us today."],
            "angle": "Local and fast",
            "reason": "tests a place-led angle beside the current ad",
        },
        {
            "ad_group_id": "100",
            "final_url": "https://not-our-site.example/x",
            "headlines": ["One", "Two", "Three"],
            "descriptions": ["Four", "Five"],
            "angle": "wrong url",
            "reason": "uses a url no ad of the campaign uses",
        },
        {
            "ad_group_id": "999",
            "final_url": "https://itbcpro.ca/nonprofit",
            "headlines": ["One", "Two", "Three"],
            "descriptions": ["Four", "Five"],
            "angle": "no such group",
            "reason": "invented group",
        },
    ],
    "extensions": {
        "sitelinks": [
            {
                "link_text": "Nonprofit IT",
                "description1": "Managed support",
                "description2": "For Vancouver teams",
                "final_url": "https://itbcpro.ca/nonprofit",
            },
            {"link_text": "Elsewhere", "final_url": "https://other.example/page"},
        ],
        "callouts": ["Local Vancouver team", "A very long callout that Google would refuse"],
        "reason": "no extensions today",
    },
}


def test_new_ads_and_extensions_are_checked_and_become_actions(
    logged_in, workspace, synced, fake, monkeypatch, django_capture_on_commit_callbacks
):
    _, run = _run(logged_in, workspace, fake, monkeypatch, NEW_ADS_PLAN)
    plan = run["plan"]
    assert [a["angle"] for a in plan["new_ads"]] == ["Local and fast"]
    assert [s["link_text"] for s in plan["extensions"]["sitelinks"]] == ["Nonprofit IT"]
    assert plan["extensions"]["callouts"] == ["Local Vancouver team"]
    with django_capture_on_commit_callbacks(execute=True):
        applied = _post(
            logged_in,
            workspace,
            f"{GOOGLE}/optimize/{run['id']}/apply",
            {"keys": ["newad:0", "ext"], "apply_now": False},
        )
    assert applied.status_code == 200, applied.content
    kinds = sorted(ProposedAction.objects.values_list("kind", flat=True))
    assert kinds == ["ads.google.ad.add_rsa", "ads.google.campaign.add_extensions"]
