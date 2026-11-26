import io
import uuid

import pytest
from django.test import override_settings
from PIL import Image

from actions import services
from actions.models import ActionStatus, Guardrail, RiskClass
from actions.registry import get_action_kind
from ads.google import actions, builders, schemas
from ads.google import sync as google_sync
from ads.models import AdChange, AdEntity
from conftest import make_user
from core.errors import ValidationFailedError
from media import services as media_services
from workspaces.models import Role

pytestmark = pytest.mark.django_db

CAMPAIGN_ID = "10"


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture
def synced(google_connection, fake):
    google_sync.sync_connection(google_connection)
    fake.mutations.clear()
    return google_connection


def _propose(workspace, connection, suffix, payload):
    return services.propose(
        workspace_id=workspace.pk,
        kind=actions.PREFIX + suffix,
        payload={"connection_id": str(connection.pk), **payload},
        requested_by="google-strategist",
    )


def _approve(workspace, action, capture):
    approver = make_user(f"appr-{action.pk}@example.com", workspace, Role.OWNER)
    with capture(execute=True):
        services.approve(workspace_id=workspace.pk, action_id=action.pk, user=approver)
    action.refresh_from_db()
    return action


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, "PNG")
    return buffer.getvalue()


SEARCH = {
    "name": "Search - Managed IT",
    "daily_budget_micros": 25_000_000,
    "bidding": "maximize_clicks",
    "targeting": {"countries": ["ca"], "languages": ["en"]},
    "ad_group_name": "Managed IT nonprofits",
    "keywords": [
        {"text": "managed it for nonprofits", "match_type": "phrase"},
        {"text": "nonprofit it support", "match_type": "exact"},
    ],
    "negative_keywords": [{"text": "free", "match_type": "broad"}],
    "final_url": "https://itbcpro.ca/nonprofit",
    "headlines": ["Managed IT for Nonprofits", "Local IT Support", "Predictable Pricing"],
    "descriptions": [
        "Managed IT with predictable monthly pricing.",
        "On-site help across Metro Vancouver.",
    ],
}


# ---- payload rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            schemas.UpdateRsa,
            {
                "connection_id": uuid.uuid4(),
                "ad_id": "1",
                "headlines": ["a" * 31, "b", "c"],
                "descriptions": ["d", "e"],
            },
        ),
        (
            schemas.UpdateRsa,
            {
                "connection_id": uuid.uuid4(),
                "ad_id": "1",
                "headlines": ["Same", "same", "Other"],
                "descriptions": ["d", "e"],
            },
        ),
        (schemas.Keyword, {"text": "bad!keyword"}),
        (schemas.Targeting, {"countries": ["XX"]}),
        (
            schemas.UpdateBudget,
            {"connection_id": uuid.uuid4(), "campaign_id": "1", "daily_spend_limit_micros": 10},
        ),
        (schemas.AddExtensions, {"connection_id": uuid.uuid4(), "campaign_id": "1"}),
    ],
)
def test_bad_payloads_are_rejected_before_approval(model, payload):
    with pytest.raises(ValueError):
        model(**payload)


def test_a_performance_max_group_needs_landscape_square_and_logo_images():
    content = {
        "name": "Assets",
        "final_url": "https://x.test/",
        "headlines": ["One", "Two", "Three"],
        "long_headlines": ["A long headline"],
        "descriptions": ["Desc one", "Desc two"],
        "business_name": "ITBCPro",
        "images": [{"media_id": uuid.uuid4(), "role": "marketing_image"}] * 3,
    }
    with pytest.raises(ValueError, match="missing image roles"):
        schemas.AssetGroupContent(**content)


# ---- builders ------------------------------------------------------------------------------


def test_several_ad_groups_each_get_their_own_keywords_and_ads():
    ad = {
        "final_url": "https://itbcpro.ca/a",
        "headlines": ["One headline", "Two headline", "Three headline"],
        "descriptions": ["First description.", "Second description."],
    }
    other = {**ad, "headlines": ["Four headline", "Five headline", "Six headline"]}
    plan = schemas.SearchCampaign(
        connection_id=uuid.uuid4(),
        name="Search - Two groups",
        daily_budget_micros=25_000_000,
        targeting={"countries": ["ca"]},
        negative_keywords=[{"text": "jobs", "match_type": "phrase"}],
        ad_groups=[
            {"name": "Group A", "keywords": [{"text": "alpha"}], "ads": [ad, other]},
            {"name": "Group B", "keywords": [{"text": "beta"}], "ads": [ad]},
        ],
    )
    ops = builders.search_operations("1234567890", plan)
    assert builders.summarize(ops) == {
        "campaignBudget": 1, "campaign": 1, "campaignCriterion": 3, "adGroup": 2,
        "adGroupCriterion": 2, "adGroupAd": 3,
    }  # fmt: skip
    groups = [
        o["adGroupOperation"]["create"]["resourceName"] for o in ops if "adGroupOperation" in o
    ]
    assert groups[0] != groups[1]
    with pytest.raises(ValueError, match="ad group names"):
        schemas.SearchCampaign(
            **{**plan.model_dump(), "ad_groups": [plan.ad_groups[0].model_dump()] * 2}
        )


def test_an_old_single_ad_group_payload_is_still_accepted():
    plan = schemas.SearchCampaign(connection_id=uuid.uuid4(), **SEARCH)
    assert len(plan.ad_groups) == 1 and plan.ad_groups[0].name == "Managed IT nonprofits"
    assert [k.text for k in plan.ad_groups[0].negative_keywords] == ["free"]


def test_a_search_campaign_is_one_request_with_temporary_ids_everything_paused():
    plan = schemas.SearchCampaign(connection_id=uuid.uuid4(), **SEARCH)
    ops = builders.search_operations("1234567890", plan)
    assert builders.summarize(ops) == {
        "campaignBudget": 1, "campaign": 1, "campaignCriterion": 2, "adGroup": 1,
        "adGroupCriterion": 3, "adGroupAd": 1,
    }  # fmt: skip
    campaign = ops[1]["campaignOperation"]["create"]
    assert campaign["status"] == "PAUSED" and campaign["advertisingChannelType"] == "SEARCH"
    assert campaign["campaignBudget"].endswith("/campaignBudgets/-1")
    assert campaign["containsEuPoliticalAdvertising"] == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    assert (
        campaign["targetSpend"] == {}
        and campaign["networkSettings"]["targetContentNetwork"] is False
    )
    assert ops[0]["campaignBudgetOperation"]["create"]["amountMicros"] == "25000000"
    assert (
        ops[2]["campaignCriterionOperation"]["create"]["location"]["geoTargetConstant"]
        == "geoTargetConstants/2124"
    )
    keywords = [
        o["adGroupCriterionOperation"]["create"] for o in ops if "adGroupCriterionOperation" in o
    ]
    assert [k["keyword"]["matchType"] for k in keywords] == ["PHRASE", "EXACT", "BROAD"]
    assert keywords[2]["negative"] is True


def test_a_performance_max_campaign_creates_assets_and_links_each_by_field_type():
    media_id = uuid.uuid4()
    content = {
        "name": "PMax assets",
        "final_url": "https://itbcpro.ca/software",
        "headlines": ["One", "Two", "Three"],
        "long_headlines": ["Custom software for your business"],
        "descriptions": ["Description one", "Description two"],
        "business_name": "ITBCPro",
        "youtube_video_ids": ["dQw4w9WgXcQ"],
        "images": [
            {"media_id": media_id, "role": r}
            for r in ("marketing_image", "square_marketing_image", "logo")
        ],
    }
    plan = schemas.PmaxCampaign(
        connection_id=uuid.uuid4(),
        name="PMax",
        daily_budget_micros=30_000_000,
        targeting={"countries": ["CA"]},
        asset_group=content,
    )
    images = [
        ("marketing_image", "a", b"1"),
        ("square_marketing_image", "b", b"2"),
        ("logo", "c", b"3"),
    ]
    ops = builders.pmax_operations("1234567890", plan, images)
    campaign = ops[1]["campaignOperation"]["create"]
    assert (
        campaign["advertisingChannelType"] == "PERFORMANCE_MAX" and campaign["status"] == "PAUSED"
    )
    assert campaign["maximizeConversions"] == {}
    links = [
        o["assetGroupAssetOperation"]["create"]["fieldType"]
        for o in ops
        if "assetGroupAssetOperation" in o
    ]
    assert sorted(links) == sorted(
        ["HEADLINE"] * 3
        + [
            "LONG_HEADLINE",
            "DESCRIPTION",
            "DESCRIPTION",
            "BUSINESS_NAME",
            "MARKETING_IMAGE",
            "SQUARE_MARKETING_IMAGE",
            "LOGO",
            "YOUTUBE_VIDEO",
        ]
    )
    assert all(
        n < 0
        for n in (
            int(o["assetOperation"]["create"]["resourceName"].rsplit("/", 1)[1])
            for o in ops
            if "assetOperation" in o
        )
    )


# ---- status, budget, bid, text ----------------------------------------------------------------


def test_the_kinds_are_registered_with_the_intended_risk():
    risk = {
        k.kind: k.risk
        for k in (
            get_action_kind(actions.PREFIX + n)
            for n in ("activate", "pause", "archive", "search_campaign.create", "negatives.add")
        )
    }
    assert (
        risk[actions.PREFIX + "pause"] == RiskClass.R1
        and risk[actions.PREFIX + "archive"] == RiskClass.R3
    )
    assert (
        risk[actions.PREFIX + "activate"] == RiskClass.R2
        and risk[actions.PREFIX + "negatives.add"] == RiskClass.R1
    )


def test_pausing_an_ad_uses_the_combined_ad_group_ad_resource_and_updates_the_copy(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(workspace, synced, "pause", {"resource": "ads", "resource_id": "1000"})
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.SUCCEEDED
    mutation = fake.mutations[0]
    assert mutation["path"].endswith("/adGroupAds:mutate")
    update = mutation["operations"][0]["update"]
    assert (
        update["resourceName"] == "customers/1234567890/adGroupAds/100~1000"
        and update["status"] == "PAUSED"
    )
    assert AdEntity.objects.get(level="ad", external_id="1000").status == "paused"
    assert AdChange.objects.filter(diff__status__to="paused").exists()


def test_activation_carries_spend_and_a_cap_blocks_it(workspace, synced):
    Guardrail.objects.create(
        workspace=workspace, key="max_single_spend_micros", config={"amount": 10_000_000}
    )
    action = _propose(
        workspace,
        synced,
        "activate",
        {"resource": "campaigns", "resource_id": CAMPAIGN_ID, "daily_spend_micros": 20_000_000},
    )
    assert action.status == ActionStatus.POLICY_BLOCKED


def test_archiving_sends_a_remove_operation(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(
        workspace, synced, "archive", {"resource": "asset_groups", "resource_id": "200"}
    )
    assert action.risk == RiskClass.R3
    _approve(workspace, action, django_capture_on_commit_callbacks)
    assert fake.mutations[0]["operations"] == [{"remove": "customers/1234567890/assetGroups/200"}]


def test_a_budget_change_edits_the_campaign_budget_and_can_be_rolled_back(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(
        workspace,
        synced,
        "campaign.update_budget",
        {"campaign_id": CAMPAIGN_ID, "daily_spend_limit_micros": 40_000_000},
    )
    assert (
        action.before == {"budget": {"daily_spend_limit_micros": 20_000_000}}
        and action.spend_micros == 40_000_000
    )
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    op = fake.mutations[0]["operations"][0]
    assert fake.mutations[0]["path"].endswith("/campaignBudgets:mutate")
    assert op["update"] == {
        "resourceName": "customers/1234567890/campaignBudgets/50",
        "amountMicros": "40000000",
    }
    assert AdEntity.objects.get(level="campaign", external_id=CAMPAIGN_ID).data["budget"] == {
        "daily_spend_limit_micros": 40_000_000
    }
    assert (
        get_action_kind(done.kind).rollback_payload(done)["daily_spend_limit_micros"] == 20_000_000
    )


def test_a_shared_budget_is_not_changed_from_here(
    synced, workspace, django_capture_on_commit_callbacks
):
    campaign = AdEntity.objects.get(level="campaign", external_id=CAMPAIGN_ID)
    campaign.data = {**campaign.data, "budget_shared": True}
    campaign.save()
    action = _propose(
        workspace,
        synced,
        "campaign.update_budget",
        {"campaign_id": CAMPAIGN_ID, "daily_spend_limit_micros": 40_000_000},
    )
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.FAILED_FINAL and "shared budget" in done.error


def test_a_bid_change_sets_the_ad_group_cpc(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(
        workspace,
        synced,
        "ad_group.update_bid",
        {"ad_group_id": "100", "max_bid_micros": 3_000_000},
    )
    assert action.before == {"max_bid_micros": 2_000_000}
    _approve(workspace, action, django_capture_on_commit_callbacks)
    assert fake.mutations[0]["operations"][0]["update"]["cpcBidMicros"] == "3000000"


def test_rewriting_an_rsa_keeps_pinned_headlines_and_remembers_the_old_text(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    payload = {
        "ad_id": "1000",
        "headlines": ["Local IT support", "Managed IT in Vancouver", "Call us today"],
        "descriptions": ["Predictable monthly pricing.", "On-site help in Metro Vancouver."],
    }
    action = _propose(workspace, synced, "ad.update_rsa", payload)
    assert action.before["headlines"][0] == "Managed IT for nonprofits"
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    update = fake.mutations[0]["operations"][0]
    assert (
        update["updateMask"] == "responsive_search_ad.headlines,responsive_search_ad.descriptions"
    )
    headlines = update["update"]["responsiveSearchAd"]["headlines"]
    assert {"text": "Local IT support", "pinnedField": "HEADLINE_1"} in headlines
    ad = AdEntity.objects.get(level="ad", external_id="1000")
    assert ad.data["creative"]["title"] == "Local IT support" and ad.review_status == "in_review"
    assert (
        get_action_kind(done.kind).rollback_payload(done)["headlines"][0]
        == "Managed IT for nonprofits"
    )


# ---- keywords -------------------------------------------------------------------------------


def test_keywords_negatives_and_pausing_send_the_right_criteria(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    add = _propose(
        workspace,
        synced,
        "keywords.add",
        {
            "ad_group_id": "100",
            "keywords": [
                {"text": "it help desk", "match_type": "exact", "max_cpc_micros": 2_500_000}
            ],
        },
    )
    _approve(workspace, add, django_capture_on_commit_callbacks)
    created = fake.mutations[0]["operations"][0]["create"]
    assert fake.mutations[0]["path"].endswith("/adGroupCriteria:mutate")
    assert (
        created["keyword"] == {"text": "it help desk", "matchType": "EXACT"}
        and created["cpcBidMicros"] == "2500000"
    )

    negative = _propose(
        workspace,
        synced,
        "negatives.add",
        {
            "scope": "campaign",
            "parent_id": CAMPAIGN_ID,
            "keywords": [{"text": "jobs", "match_type": "broad"}],
        },
    )
    assert negative.risk == RiskClass.R1
    _approve(workspace, negative, django_capture_on_commit_callbacks)
    neg = next(m for m in fake.mutations if m["path"].endswith("/campaignCriteria:mutate"))
    assert neg["operations"][0]["create"]["negative"] is True

    pause = _propose(
        workspace, synced, "keyword.pause", {"ad_group_id": "100", "criterion_id": "7"}
    )
    _approve(workspace, pause, django_capture_on_commit_callbacks)
    assert AdEntity.objects.get(level="keyword", external_id="100~7").status == "paused"


# ---- creation ---------------------------------------------------------------------------------


def test_a_search_campaign_is_validated_first_then_created_in_one_request(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(workspace, synced, "search_campaign.create", SEARCH)
    assert action.status == ActionStatus.AWAITING_APPROVAL and action.spend_micros == 25_000_000
    assert action.after["validation"] == {"ok": True, "error": ""}
    assert action.after["operations"]["adGroupCriterion"] == 3
    assert (
        fake.mutations[0]["validateOnly"] is True and len(fake.mutations) == 1
    )  # nothing created yet

    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.SUCCEEDED
    real = [m for m in fake.mutations if not m.get("validateOnly")]
    assert len(real) == 1 and real[0]["path"].endswith("/googleAds:mutate")
    assert done.result["resource"] == "campaigns" and done.result["resource_id"]
    assert get_action_kind(done.kind).rollback_kind == actions.PREFIX + "archive"


def test_a_failed_validation_is_shown_to_the_approver_and_creation_is_blocked_by_google(
    workspace, synced, fake
):
    fake.fail[":mutate"] = (
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
                                "location": {"fieldPathElements": [{"fieldName": "campaign"}]},
                            }
                        ]
                    }
                ],
            }
        },
    )
    action = _propose(workspace, synced, "search_campaign.create", SEARCH)
    assert (
        action.after["validation"]["ok"] is False
        and "Required field missing" in action.after["validation"]["error"]
    )


def _library_image(workspace, width, height, name):
    asset, _ = media_services.create_from_chunks(
        workspace.pk,
        None,
        [_png(width, height)],
        filename=f"{name}.png",
        source="upload",
        title=name,
    )
    return asset


def test_performance_max_uploads_library_images_and_rejects_ones_that_do_not_fit(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    landscape = _library_image(workspace, 1200, 628, "landscape")
    square = _library_image(workspace, 1200, 1200, "square")
    logo = _library_image(workspace, 1024, 1024, "logo")
    payload = {
        "name": "PMax - Software",
        "daily_budget_micros": 30_000_000,
        "targeting": {"countries": ["CA"]},
        "asset_group": {
            "name": "Software assets",
            "final_url": "https://itbcpro.ca/software",
            "headlines": ["Custom software", "Web apps", "Mobile apps"],
            "long_headlines": ["Custom software development for your business"],
            "descriptions": ["We build web and mobile apps.", "From idea to launch."],
            "business_name": "ITBCPro",
            "images": [
                {"media_id": str(landscape.pk), "role": "marketing_image"},
                {"media_id": str(square.pk), "role": "square_marketing_image"},
                {"media_id": str(logo.pk), "role": "logo"},
            ],
        },
    }
    action = _propose(workspace, synced, "pmax_campaign.create", payload)
    assert action.after["validation"]["ok"] is True
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.SUCCEEDED
    body = next(m for m in fake.mutations if not m.get("validateOnly"))
    images = [
        o["assetOperation"]["create"]["imageAsset"]
        for o in body["mutateOperations"]
        if "assetOperation" in o and "imageAsset" in o["assetOperation"]["create"]
    ]
    assert len(images) == 3 and all(i["data"] for i in images)

    wrong = {
        **payload,
        "asset_group": {
            **payload["asset_group"],
            "images": [
                {
                    "media_id": str(square.pk),
                    "role": "marketing_image",
                },  # a square is not a landscape
                {"media_id": str(square.pk), "role": "square_marketing_image"},
                {"media_id": str(logo.pk), "role": "logo"},
            ],
        },
        "name": "PMax - Two",
    }
    second = _propose(workspace, synced, "pmax_campaign.create", wrong)
    assert (
        second.after["validation"]["ok"] is False
        and "wrong_ratio" in second.after["validation"]["error"]
    )
    with pytest.raises(ValidationFailedError):
        actions._pmax_execute(second)


def test_extensions_add_assets_then_link_them_to_the_campaign(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(
        workspace,
        synced,
        "campaign.add_extensions",
        {
            "campaign_id": CAMPAIGN_ID,
            "sitelinks": [
                {
                    "link_text": "Contact",
                    "description1": "Talk to us",
                    "final_url": "https://itbcpro.ca/contact",
                }
            ],
            "callouts": ["24/7 support"],
            "snippets": [{"header": "Services", "values": ["Helpdesk", "Security", "Cloud"]}],
        },
    )
    _approve(workspace, action, django_capture_on_commit_callbacks)
    ops = fake.mutations[0]["mutateOperations"]
    fields = [
        o["campaignAssetOperation"]["create"]["fieldType"]
        for o in ops
        if "campaignAssetOperation" in o
    ]
    assert fields == ["SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"]


def test_assets_can_be_added_to_and_removed_from_an_existing_asset_group(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    add = _propose(
        workspace,
        synced,
        "asset_group.add_assets",
        {
            "asset_group_id": "200",
            "headlines": ["Custom apps"],
            "youtube_video_ids": ["dQw4w9WgXcQ"],
        },
    )
    _approve(workspace, add, django_capture_on_commit_callbacks)
    ops = fake.mutations[0]["mutateOperations"]
    assert [
        o["assetGroupAssetOperation"]["create"]["fieldType"]
        for o in ops
        if "assetGroupAssetOperation" in o
    ] == ["HEADLINE", "YOUTUBE_VIDEO"]
    remove = _propose(
        workspace,
        synced,
        "asset_group.remove_asset",
        {"asset_group_id": "200", "asset_id": "901", "field_type": "MARKETING_IMAGE"},
    )
    _approve(workspace, remove, django_capture_on_commit_callbacks)
    last = [m for m in fake.mutations if "mutateOperations" in m][-1]["mutateOperations"][0]
    assert last == {
        "assetGroupAssetOperation": {
            "remove": "customers/1234567890/assetGroupAssets/200~901~MARKETING_IMAGE"
        }
    }


def test_a_new_ad_is_added_beside_the_old_one_and_can_be_paused_again(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(
        workspace,
        synced,
        "ad.add_rsa",
        {
            "ad_group_id": "100",
            "final_url": "https://itbcpro.ca/nonprofit",
            "headlines": ["Managed IT Nonprofits", "Vancouver IT Support", "Fast Local Help"],
            "descriptions": ["Local IT support for Vancouver nonprofits.", "Talk to us today."],
            "path1": "nonprofit",
        },
    )
    assert action.after["validation"] == {"ok": True, "error": ""}
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.SUCCEEDED
    created = [m for m in fake.mutations if m["path"].endswith("adGroupAds:mutate")][-1]
    ad = created["operations"][0]["create"]
    assert ad["adGroup"] == "customers/1234567890/adGroups/100" and ad["status"] == "ENABLED"
    assert ad["ad"]["finalUrls"] == ["https://itbcpro.ca/nonprofit"]
    assert ad["ad"]["responsiveSearchAd"]["path1"] == "nonprofit"
    assert done.result["resource"] == "ads" and done.result["resource_id"].isdigit()
    kind = get_action_kind(actions.PREFIX + "ad.add_rsa")
    assert kind.risk == RiskClass.R2 and kind.rollback_kind == actions.PREFIX + "pause"


def test_a_new_ad_with_bad_copy_is_refused_before_anyone_sees_it(workspace, synced):
    with pytest.raises(ValidationFailedError):
        _propose(
            workspace,
            synced,
            "ad.add_rsa",
            {
                "ad_group_id": "100",
                "final_url": "http://insecure.example",
                "headlines": ["x" * 31, "b", "c"],
                "descriptions": ["a", "b"],
            },
        )


def test_an_ad_group_added_to_an_existing_campaign_is_one_request_inside_it():
    ad = {
        "final_url": "https://itbcpro.ca/a",
        "headlines": ["One headline", "Two headline", "Three headline"],
        "descriptions": ["First description.", "Second description."],
    }
    plan = schemas.AddAdGroup(
        connection_id=uuid.uuid4(),
        campaign_id="42",
        ad_group={"name": "Group C", "keywords": [{"text": "gamma"}], "ads": [ad, ad]},
    )
    ops = builders.add_ad_group_operations("1234567890", plan.campaign_id, plan.ad_group)
    assert builders.summarize(ops) == {"adGroup": 1, "adGroupCriterion": 1, "adGroupAd": 2}
    group = ops[0]["adGroupOperation"]["create"]
    assert group["campaign"] == "customers/1234567890/campaigns/42"
