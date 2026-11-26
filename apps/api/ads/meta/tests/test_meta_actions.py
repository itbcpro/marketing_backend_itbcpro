import io

import pytest
from django.test import override_settings
from PIL import Image

from actions import services
from actions.models import ActionStatus, Guardrail, RiskClass
from actions.registry import get_action_kind
from ads.meta import actions, schemas
from ads.meta import sync as meta_sync
from ads.models import AdChange, AdEntity
from conftest import make_user
from core.errors import ValidationFailedError
from media import services as media_services
from workspaces.models import Role

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture
def synced(meta_connection, fake):
    meta_sync.sync_connection(meta_connection)
    fake.posts.clear()
    return meta_connection


def _propose(workspace, connection, suffix, payload):
    return services.propose(
        workspace_id=workspace.pk,
        kind=actions.PREFIX + suffix,
        payload={"connection_id": str(connection.pk), **payload},
        requested_by="meta-strategist",
    )


def _approve(workspace, action, capture):
    approver = make_user(f"appr-{action.pk}@example.com", workspace, Role.OWNER)
    with capture(execute=True):
        services.approve(workspace_id=workspace.pk, action_id=action.pk, user=approver)
    action.refresh_from_db()
    return action


def _picture(workspace, width=1080, height=1350):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, "JPEG")
    asset, _ = media_services.create_from_chunks(
        workspace.pk, None, [buffer.getvalue()], filename="ad.jpg", source="upload"
    )
    return asset


def _plan(workspace, **changes):
    return {
        "name": "Spring leads",
        "objective": "OUTCOME_TRAFFIC",
        "daily_budget_micros": 25_000_000,
        "countries": ["ca"],
        "page_id": "1234567890",
        "link": "https://acme.example/offer",
        "headline": "Backups you can trust",
        "primary_text": "We test your backups every week so you never find out the hard way.",
        "call_to_action": "LEARN_MORE",
        "image_media_id": str(_picture(workspace).pk),
        **changes,
    }


def test_risk_classes_follow_the_safety_shape():
    risk = {
        n: get_action_kind(actions.PREFIX + n).risk
        for n in ("pause", "activate", "campaign.create")
    }
    assert risk == {
        "pause": RiskClass.R1,
        "activate": RiskClass.R2,
        "campaign.create": RiskClass.R2,
    }


def test_pausing_an_ad_set_updates_meta_and_the_copy(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(workspace, synced, "pause", {"resource": "ad_groups", "resource_id": "20"})
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.SUCCEEDED
    assert fake.posts == [("/20", {"status": "PAUSED"})]
    assert AdEntity.objects.get(level="ad_group").status == "paused"
    assert AdChange.objects.filter(diff__status__to="paused").exists()


def test_activation_carries_spend_and_a_cap_blocks_it(workspace, synced):
    Guardrail.objects.create(
        workspace=workspace, key="max_single_spend_micros", config={"amount": 10_000_000}
    )
    action = _propose(
        workspace,
        synced,
        "activate",
        {"resource": "campaigns", "resource_id": "10", "daily_spend_micros": 20_000_000},
    )
    assert action.status == ActionStatus.POLICY_BLOCKED


def test_a_budget_change_uses_minor_units_and_can_be_rolled_back(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(
        workspace,
        synced,
        "update_budget",
        {"level": "campaign", "entity_id": "10", "daily_spend_limit_micros": 40_000_000},
    )
    assert action.before == {"daily_spend_limit_micros": 20_000_000}
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.SUCCEEDED
    assert fake.posts == [("/10", {"daily_budget": "4000"})]
    campaign = AdEntity.objects.get(level="campaign")
    assert campaign.data["budget"] == {"daily_spend_limit_micros": 40_000_000}


def test_a_budget_on_an_item_without_a_daily_budget_is_refused(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(
        workspace,
        synced,
        "update_budget",
        {"level": "ad_set", "entity_id": "20", "daily_spend_limit_micros": 5_000_000},
    )
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.FAILED_FINAL and not fake.posts


def test_a_campaign_is_created_paused_in_order_and_recorded(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    action = _propose(workspace, synced, "campaign.create", _plan(workspace))
    assert action.after["validation"] == {"ok": True}
    assert action.after["structure"]["ad_set"]["ages"] == "18-65"
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.SUCCEEDED
    order = [path.rsplit("/", 1)[-1] for path, _ in fake.posts if "execution_options" not in _]
    assert order == ["adimages", "campaigns", "adsets", "adcreatives", "ads"]
    bodies = {path.rsplit("/", 1)[-1]: body for path, body in fake.posts}
    assert (
        bodies["campaigns"]["status"] == "PAUSED" and bodies["campaigns"]["daily_budget"] == "2500"
    )
    assert bodies["adsets"]["status"] == "PAUSED" and bodies["ads"]["status"] == "PAUSED"
    assert bodies["adsets"]["targeting"]["geo_locations"] == {"countries": ["CA"]}
    story = bodies["adcreatives"]["object_story_spec"]
    assert story["link_data"]["image_hash"] == "hash123" and story["page_id"] == "1234567890"
    assert "utm_source=meta" in bodies["adcreatives"]["url_tags"]
    assert done.result["campaign"] and done.result["resource"] == "campaigns"
    assert AdEntity.objects.filter(level="campaign", name="Spring leads").exists()


def test_a_failure_halfway_deletes_what_was_created(
    workspace, synced, fake, django_capture_on_commit_callbacks
):
    fake.fail["adcreatives"] = (400, {"error": {"code": 100, "message": "bad creative"}})
    action = _propose(workspace, synced, "campaign.create", _plan(workspace))
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.FAILED_FINAL
    # the ad set and the campaign are removed again, the newest first
    assert fake.deleted == ["902", "901"]


def test_the_preview_reports_a_picture_that_does_not_fit(workspace, synced):
    wide = _picture(workspace, 1000, 300)
    action = _propose(
        workspace, synced, "campaign.create", _plan(workspace, image_media_id=str(wide.pk))
    )
    assert action.after["validation"]["ok"] is False


def test_a_duplicate_name_is_reported_before_anything_is_created(workspace, synced):
    action = _propose(workspace, synced, "campaign.create", _plan(workspace, name="Spring offer"))
    assert "already exists" in action.after["validation"]["error"]


@pytest.mark.parametrize(
    "changes",
    [
        {"link": "http://acme.example"},
        {"age_min": 40, "age_max": 30},
        {"objective": "OUTCOME_LEADS"},
        {"countries": ["ca", "CA"]},
        {"headline": "x" * 41},
        {"daily_budget_micros": 100},
    ],
)
def test_bad_plans_are_rejected_before_a_person_sees_them(workspace, changes):
    with pytest.raises(ValueError):
        schemas.CampaignPlan.model_validate(
            {"connection_id": "00000000-0000-0000-0000-000000000001", **_plan(workspace, **changes)}
        )


def test_the_validation_error_type_is_a_domain_error_for_actions(workspace, synced):
    with pytest.raises(ValidationFailedError):
        _propose(workspace, synced, "campaign.create", {"name": "x"})
