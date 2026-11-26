import json

import pytest

from ads.google import practice
from ads.google.schemas import SearchCampaign

pytestmark = pytest.mark.django_db

GOOGLE = "/api/v1/ads/google"
THEME = "managed it services"


def _draft(**over):
    base = {
        "name": "Managed IT Toronto",
        "daily_budget_micros": 50_000_000,
        "bidding": "maximize_clicks",
        "targeting": {"countries": ["CA"], "languages": ["en"]},
        "ad_group_name": "Managed IT",
        "keywords": [
            {"text": THEME, "match_type": "phrase"},
            {"text": "it support for business", "match_type": "phrase"},
            {"text": "managed it provider", "match_type": "exact"},
            {"text": "outsourced it department", "match_type": "phrase"},
            {"text": "business it support toronto", "match_type": "phrase"},
        ],
        "negative_keywords": [{"text": "jobs", "match_type": "phrase"}],
        "final_url": "https://example.com/it",
        "headlines": [f"Managed IT Services {i}" for i in range(15)],
        "descriptions": [f"Get a plan that fits {i}. Book a call today." for i in range(4)],
        "path1": "it",
        "path2": "managed",
    }
    return {**base, **over}


def _codes(draft, tracking=True):
    plan = SearchCampaign(connection_id="00000000-0000-0000-0000-000000000001", **draft)
    return {c["code"]: c["level"] for c in practice.review_search(plan, tracking=tracking)}


def test_a_well_formed_search_draft_has_no_failures_or_warnings():
    codes = _codes(_draft())
    assert "fail" not in codes.values() and "warn" not in codes.values()


def test_missing_negatives_few_headlines_and_broad_keywords_are_warned():
    draft = _draft(
        negative_keywords=[],
        headlines=["Managed IT Services", "IT Support", "Call Us Now"],
        keywords=[
            {"text": "managed it", "match_type": "broad"},
            {"text": "support", "match_type": "broad"},
            {"text": "help", "match_type": "broad"},
        ],
    )
    codes = _codes(draft)
    for code in ("negatives_missing", "rsa_headlines_few", "keywords_mostly_broad",
                 "keywords_single_words", "rsa_theme_missing"):  # fmt: skip
        assert codes[code] == "warn", code


def test_tracking_is_required_for_conversion_bidding_and_a_negative_must_not_block_a_keyword():
    codes = _codes(_draft(bidding="maximize_conversions"), tracking=False)
    assert codes["no_conversion_tracking"] == "fail"
    blocked = _codes(_draft(negative_keywords=[{"text": THEME, "match_type": "phrase"}]))
    assert blocked["negative_blocks_keyword"] == "fail"


def test_copy_rules_flag_phone_numbers_exclamations_and_keyword_tokens():
    draft = _draft()
    draft["headlines"][0] = "Call 416-555-0199"
    draft["headlines"][1] = "Best IT Help!"
    draft["headlines"][2] = "{KeyWord:IT Help}"
    codes = _codes(draft)
    assert codes["copy_phone_or_emoji"] == "fail"
    assert codes["copy_exclamation"] == "warn"
    assert codes["copy_keyword_insertion"] == "warn"


def test_the_endpoint_scores_a_complete_draft_and_calls_a_partial_one_incomplete(
    logged_in, workspace, google_connection
):
    def post(draft):
        return logged_in.post(
            f"{GOOGLE}/practice",
            data=json.dumps(
                {"connection_id": str(google_connection.pk), "kind": "search", "draft": draft}
            ),
            content_type="application/json",
            HTTP_X_WORKSPACE_ID=str(workspace.pk),
        )

    done = post(_draft()).json()
    assert done["complete"] and 0 < done["score"] <= 100
    assert post({"name": "x"}).json() == {"complete": False, "score": 0, "checks": []}


# ---- AI draft from a plain description -------------------------------------------------------

GOOD_HEADLINES = [
    "Managed IT Services",
    "managed it services",
    "x" * 31,
    "IT Support Toronto",
    "Local IT Team",
]
GOOD_DESCRIPTIONS = ["Get a plan that fits.", "y" * 91, "Talk to a local engineer today."]

CAMPAIGN = {
    "name": "Managed IT Toronto",
    "daily_budget": 40,
    "bidding": "maximize_clicks",
    "max_cpc": None,
    "countries": ["ca", "XX"],
    "languages": ["en", "zz"],
    "negative_keywords": ["jobs", "managed it services", "free"],
    "ad_groups": [
        {
            "name": "Managed IT",
            "keywords": [
                {"text": "managed it services", "match_type": "phrase"},
                {"text": "Managed IT Services", "match_type": "phrase"},
                {"text": "it support (toronto)", "match_type": "exact"},
            ],
            "negative_keywords": ["cabling"],
            "ads": [
                {
                    "final_url": "https://wrong.example.com/",
                    "headlines": GOOD_HEADLINES,
                    "descriptions": GOOD_DESCRIPTIONS,
                    "path1": "managed-it",
                    "path2": "this-is-way-too-long-for-a-path",
                },
                {"final_url": "", "headlines": ["Only one"], "descriptions": ["Too few"]},
                {
                    "final_url": "",
                    "headlines": ["Co-managed IT", "Fixed monthly IT cost", "Cut IT downtime"],
                    "descriptions": ["Predictable pricing.", "Book a free IT review."],
                },
            ],
        },
        {
            "name": "Network Cabling",
            "keywords": [
                {"text": "managed it services", "match_type": "phrase"},
                {"text": "network cabling toronto", "match_type": "phrase"},
            ],
            "negative_keywords": [],
            "ads": [
                {"final_url": "", "headlines": GOOD_HEADLINES, "descriptions": GOOD_DESCRIPTIONS}
            ],
        },
        {
            "name": "Has no usable ad",
            "keywords": [{"text": "it help desk", "match_type": "phrase"}],
            "negative_keywords": [],
            "ads": [{"final_url": "", "headlines": ["One"], "descriptions": ["Two"]}],
        },
    ],
}


SECOND_CAMPAIGN = {
    "name": "Cabling Toronto",
    "daily_budget": 15,
    "bidding": "maximize_clicks",
    "countries": ["CA"],
    "languages": ["en"],
    "negative_keywords": [],
    "ad_groups": [
        {
            "name": "Office cabling",
            "keywords": [{"text": "office cabling toronto", "match_type": "phrase"}],
            "negative_keywords": [],
            "ads": [
                {"final_url": "", "headlines": GOOD_HEADLINES, "descriptions": GOOD_DESCRIPTIONS}
            ],
        }
    ],
}
EMPTY_CAMPAIGN = {**SECOND_CAMPAIGN, "name": "No groups at all", "ad_groups": []}

DRAFT = {
    "summary": "Two campaigns for Toronto firms.",
    "notes": "Budget is an assumption.",
    "campaigns": [CAMPAIGN, SECOND_CAMPAIGN, EMPTY_CAMPAIGN],
}


def _draft_request(client, workspace, connection, **body):
    return client.post(
        f"{GOOGLE}/draft",
        data=json.dumps({"connection_id": str(connection.pk), "currency": "CAD", **body}),
        content_type="application/json",
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )


def test_a_plain_description_becomes_a_clean_form_ready_draft(
    logged_in, workspace, google_connection, monkeypatch
):
    import httpx

    from content import net
    from llm.fake import FakeGateway, text_response
    from llm.service import LlmService, set_llm_service

    monkeypatch.setattr(
        net,
        "transport",
        httpx.MockTransport(
            lambda r: httpx.Response(200, text="<html><title>IT</title><h1>Managed IT</h1></html>")
        ),
    )
    gateway = FakeGateway([text_response(json.dumps(DRAFT))])
    set_llm_service(LlmService(gateway))
    try:
        response = _draft_request(
            logged_in, workspace, google_connection,
            brief="Managed IT for Toronto businesses", website="https://itbcpro.ca/it",
        )  # fmt: skip
    finally:
        set_llm_service(None)
    body = response.json()
    assert response.status_code == 200, body
    assert [c["name"] for c in body["campaigns"]] == ["Managed IT Toronto", "Cabling Toronto"]
    assert body["campaigns"][1]["daily_budget_micros"] == 15_000_000
    out = body["campaigns"][0]
    assert out["daily_budget_micros"] == 40_000_000
    assert out["countries"] == ["CA"] and out["languages"] == ["en"]
    assert [k["text"] for k in out["negative_keywords"]] == ["jobs", "free"]
    first, second = out["ad_groups"]  # the group without a usable ad is dropped
    assert [g["name"] for g in out["ad_groups"]] == ["Managed IT", "Network Cabling"]
    assert [k["text"] for k in first["keywords"]] == ["managed it services"]  # dupes, specials
    assert [k["text"] for k in second["keywords"]] == ["network cabling toronto"]  # no repeats
    assert [k["text"] for k in first["negative_keywords"]] == ["cabling"]
    assert len(first["ads"]) == 2  # the ad with too little text is dropped
    ad = first["ads"][0]
    assert ad["final_url"] == "https://itbcpro.ca/it"  # the owner's page wins over the model's
    assert ad["headlines"] == ["Managed IT Services", "IT Support Toronto", "Local IT Team"]
    assert ad["descriptions"] == ["Get a plan that fits.", "Talk to a local engineer today."]
    assert ad["path1"] == "managed-it" and ad["path2"] == ""
    assert "Managed IT for Toronto businesses" in gateway.requests[0][0].system[0].text


def test_an_empty_description_or_insecure_website_is_refused(
    logged_in, workspace, google_connection
):
    assert _draft_request(logged_in, workspace, google_connection, brief="  ").status_code == 422
    bad = _draft_request(
        logged_in, workspace, google_connection, brief="x", website="http://itbcpro.ca"
    )
    assert bad.status_code == 422


# ---- one more ad group for an existing campaign ---------------------------------------------

NEW_GROUP = {
    "summary": "Targets cabling jobs.",
    "notes": "",
    "ad_group": {
        "name": "Network Cabling",
        "keywords": [
            {"text": "network cabling toronto", "match_type": "phrase"},
            {"text": "existing keyword", "match_type": "phrase"},
        ],
        "negative_keywords": ["jobs"],
        "ads": [
            {"final_url": "", "headlines": GOOD_HEADLINES, "descriptions": GOOD_DESCRIPTIONS},
            {"final_url": "", "headlines": GOOD_HEADLINES[:3], "descriptions": GOOD_DESCRIPTIONS},
        ],
    },
}


def test_a_new_ad_group_is_written_for_an_existing_campaign_without_repeating_keywords(
    logged_in, workspace, google_connection
):
    from ads.models import AdEntity
    from llm.fake import FakeGateway, text_response
    from llm.service import LlmService, set_llm_service

    campaign = AdEntity.objects.create(
        workspace=workspace, connection=google_connection, level="campaign",
        external_id="42", name="Managed IT",
    )  # fmt: skip
    group = AdEntity.objects.create(
        workspace=workspace, connection=google_connection, level="ad_group",
        external_id="7", name="Managed IT", parent=campaign,
    )  # fmt: skip
    AdEntity.objects.create(
        workspace=workspace, connection=google_connection, level="keyword",
        external_id="7~1", name="existing keyword", parent=group,
    )  # fmt: skip
    gateway = FakeGateway([text_response(json.dumps(NEW_GROUP))])
    set_llm_service(LlmService(gateway))
    try:
        response = logged_in.post(
            f"{GOOGLE}/draft-ad-group",
            data=json.dumps(
                {
                    "connection_id": str(google_connection.pk),
                    "campaign_id": "42",
                    "brief": "Add network cabling",
                    "website": "https://itbcpro.ca/cabling",
                }
            ),
            content_type="application/json",
            HTTP_X_WORKSPACE_ID=str(workspace.pk),
        )
    finally:
        set_llm_service(None)
    out = response.json()
    assert response.status_code == 200, out
    new = out["ad_group"]
    assert [k["text"] for k in new["keywords"]] == ["network cabling toronto"]
    assert [k["text"] for k in new["negative_keywords"]] == ["jobs"]
    assert len(new["ads"]) == 1 and new["ads"][0]["final_url"] == "https://itbcpro.ca/cabling"
    system = gateway.requests[0][0].system[0].text
    assert "existing keyword" in system and "Managed IT" in system
