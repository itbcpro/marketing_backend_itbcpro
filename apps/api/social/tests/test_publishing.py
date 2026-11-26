import io
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone
from PIL import Image

from actions import services as action_services
from actions.models import ActionStatus, ProposedAction
from core.errors import (
    PermissionDeniedError,
    RateLimitedError,
    RetryableProviderError,
    ValidationFailedError,
)
from media import services as media_services
from media.models import MediaAsset, MediaKind, MediaSource
from social import drafts, publishing
from social.models import DraftStatus, PostDraft, SocialMedia
from social.tests.fake_instagram import ACCOUNT_ID

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _public_url(settings):
    settings.PUBLIC_BASE_URL = "https://marketing.example"


def picture(workspace, owner, size=(1080, 1350), fmt="JPEG", color=(30, 80, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    asset, _ = media_services.create_from_chunks(
        workspace.pk,
        owner.pk,
        [buffer.getvalue()],
        filename=f"p-{color[0]}.{fmt.lower()}",
        source=MediaSource.UPLOAD,
    )
    return asset


def video(workspace, seconds=20.0, ratio=(1080, 1920)):
    return MediaAsset.objects.create(
        workspace_id=workspace.pk,
        kind=MediaKind.VIDEO,
        source=MediaSource.UPLOAD,
        name="clip",
        storage_key="x/clip.mp4",
        mime_type="video/mp4",
        file_format="MP4",
        width=ratio[0],
        height=ratio[1],
        duration_seconds=seconds,
        size_bytes=1000,
    )


def draft_of(account, kind="image", media=None, **fields):
    defaults = {
        "workspace_id": account.workspace_id,
        "account": account,
        "kind": kind,
        "status": DraftStatus.DRAFTED,
        "caption": "Five signs your backups are not working.",
        "hashtags": ["#managedit", "#backup"],
        "media": media or [],
    }
    defaults.update(fields)
    return PostDraft.objects.create(**defaults)


def go(draft, owner, when=None):
    drafts.approve(draft, owner, when)
    draft.refresh_from_db()
    assert drafts.publish_due()["launched"] == 1
    draft.refresh_from_db()
    action = ProposedAction.objects.get(pk=draft.action_id)
    assert action.status == ActionStatus.APPROVED
    return action


def test_an_image_post_is_published_and_verified(workspace, owner, account, connection, fake):
    asset = picture(workspace, owner)
    draft = draft_of(
        account,
        media=[{"asset_id": str(asset.pk), "alt_text": "Server rack"}],
        first_comment="More tips in our bio",
    )
    action = go(draft, owner)
    action_services.execute(action.pk)
    action.refresh_from_db()
    draft.refresh_from_db()
    assert action.status == ActionStatus.VERIFIED
    assert draft.status == DraftStatus.PUBLISHED and draft.external_media_id in fake.published
    assert draft.permalink.startswith("https://instagram.com/p/")
    container = next(iter(fake.containers.values()))
    assert container["image_url"].startswith(
        "https://marketing.example/bff/api/v1/social/public-media/"
    )
    assert (
        container["caption"].endswith("#managedit #backup")
        and container["alt_text"] == "Server rack"
    )
    assert fake.calls_to("POST", r"/comments$")[0]["message"] == "More tips in our bio"
    assert SocialMedia.objects.filter(external_id=draft.external_media_id).exists()


def test_png_images_are_converted_to_jpeg_before_publishing(workspace, owner, account, fake):
    asset = picture(workspace, owner, fmt="PNG")
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    action_services.execute(go(draft, owner).pk)
    assert MediaAsset.objects.filter(variant="instagram_jpeg", derived_from=asset).exists()


def test_a_carousel_creates_children_then_the_parent(workspace, owner, account, fake):
    assets = [picture(workspace, owner, color=(i * 40, 50, 90)) for i in range(1, 4)]
    draft = draft_of(
        account,
        "carousel",
        media=[{"asset_id": str(a.pk), "alt_text": f"s{i}"} for i, a in enumerate(assets)],
    )
    action_services.execute(go(draft, owner).pk)
    created = fake.calls_to("POST", rf"^/{ACCOUNT_ID}/media$")
    children = [c for c in created if c.get("is_carousel_item")]
    parent = next(c for c in created if c.get("media_type") == "CAROUSEL")
    assert len(children) == 3 and len(parent["children"].split(",")) == 3
    draft.refresh_from_db()
    assert draft.status == DraftStatus.PUBLISHED


def test_a_reel_waits_for_processing_and_uses_the_cover(workspace, owner, account, fake):
    clip = video(workspace)
    cover = picture(workspace, owner, size=(1080, 1920))
    draft = draft_of(
        account,
        "reel",
        media=[{"asset_id": str(clip.pk)}],
        cover_asset_id=cover.pk,
        share_to_feed=False,
    )
    fake.default_container_states = ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]
    action_services.execute(go(draft, owner).pk)
    container = next(iter(fake.containers.values()))
    assert container["media_type"] == "REELS" and container["video_url"].startswith("https://")
    assert container["cover_url"].startswith("https://") and container["share_to_feed"] is False
    draft.refresh_from_db()
    assert draft.status == DraftStatus.PUBLISHED


def test_a_reel_still_processing_resumes_without_a_second_container(
    workspace, owner, account, fake, monkeypatch
):
    clip = video(workspace)
    draft = draft_of(account, "reel", media=[{"asset_id": str(clip.pk)}])
    fake.default_container_states = ["IN_PROGRESS"]
    monkeypatch.setattr(publishing, "WAIT_SECONDS", 0)
    action = go(draft, owner)
    with pytest.raises(RetryableProviderError):
        action_services.execute(action.pk)
    assert ProposedAction.objects.get(pk=action.pk).status == ActionStatus.FAILED_RETRYABLE
    draft.refresh_from_db()
    assert draft.status == DraftStatus.PUBLISHING and draft.publish_state["container_id"]
    container_id = draft.publish_state["container_id"]
    fake.container_states[container_id] = ["FINISHED"]
    monkeypatch.setattr(publishing, "WAIT_SECONDS", 30)
    action_services.execute(action.pk)
    draft.refresh_from_db()
    assert (
        draft.status == DraftStatus.PUBLISHED
        and len(fake.containers) == 1
        and len(fake.published) == 1
    )


def test_a_processing_error_fails_the_post_with_the_reason(workspace, owner, account, fake):
    clip = video(workspace)
    draft = draft_of(account, "reel", media=[{"asset_id": str(clip.pk)}])
    fake.default_container_states = ["ERROR"]
    action = go(draft, owner)
    action_services.execute(action.pk)
    draft.refresh_from_db()
    assert ProposedAction.objects.get(pk=action.pk).status == ActionStatus.FAILED_FINAL
    assert draft.status == DraftStatus.FAILED and "could not process" in draft.error_detail
    assert not fake.published


def test_nothing_is_created_when_the_daily_limit_is_nearly_reached(workspace, owner, account, fake):
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    fake.quota = {"quota_usage": 49, "config": {"quota_total": 50}}
    action = go(draft, owner)
    with pytest.raises(RateLimitedError):
        action_services.execute(action.pk)
    assert not fake.containers


def test_a_second_run_of_a_published_post_does_nothing(workspace, owner, account, fake):
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    action = go(draft, owner)
    action_services.execute(action.pk)
    calls = len(fake.calls)
    result = publishing.execute(ProposedAction.objects.get(pk=action.pk))
    assert result["media_id"] and len(fake.calls) == calls


def test_a_publish_that_timed_out_is_recovered_by_its_caption(workspace, owner, account, fake):
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    action = go(draft, owner)
    action_services.execute(action.pk)
    draft.refresh_from_db()
    published = draft.external_media_id
    PostDraft.objects.filter(pk=draft.pk).update(
        status=DraftStatus.PUBLISHING,
        external_media_id="",
        publish_state={**draft.publish_state, "media_id": "", "publish_started": True},
    )
    ProposedAction.objects.filter(pk=action.pk).update(status=ActionStatus.FAILED_RETRYABLE)
    action_services.execute(action.pk)
    draft.refresh_from_db()
    assert draft.external_media_id == published and len(fake.published) == 1


def test_an_unreachable_public_address_is_explained(workspace, owner, account, fake, settings):
    settings.PUBLIC_BASE_URL = "http://localhost:3000"
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    action = go(draft, owner)
    action_services.execute(action.pk)
    draft.refresh_from_db()
    assert draft.status == DraftStatus.FAILED and "APP_PUBLIC_BASE_URL" in draft.error_detail


# --- approval and scheduling -------------------------------------------------------------------


def test_errors_block_approval(workspace, owner, account):
    draft = draft_of(account, media=[])
    with pytest.raises(ValidationFailedError):
        drafts.approve(draft, owner, None)
    draft.refresh_from_db()
    assert draft.status == DraftStatus.DRAFTED
    assert any(c["code"] == "no_media" for c in draft.checks)


def test_only_someone_who_may_approve_posts_can_approve(workspace, account, owner):
    from conftest import make_user
    from workspaces.models import Role

    editor = make_user("editor@example.com", workspace, Role.EDITOR)
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    with pytest.raises(PermissionDeniedError):
        drafts.approve(draft, editor, None)


def test_a_future_post_waits_for_its_time(workspace, owner, account, fake):
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    drafts.approve(draft, owner, timezone.now() + timedelta(hours=2))
    assert drafts.publish_due() == {"launched": 0, "missed": 0, "paused": 0, "failed": 0}
    PostDraft.objects.filter(pk=draft.pk).update(scheduled_at=timezone.now() - timedelta(minutes=1))
    assert drafts.publish_due()["launched"] == 1


def test_a_slot_missed_by_hours_is_not_published_late(workspace, owner, account, fake):
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    drafts.approve(draft, owner, timezone.now() + timedelta(hours=1))
    PostDraft.objects.filter(pk=draft.pk).update(scheduled_at=timezone.now() - timedelta(hours=8))
    assert drafts.publish_due()["missed"] == 1
    draft.refresh_from_db()
    assert draft.status == DraftStatus.FAILED and draft.error_code == "missed_slot"


def test_the_kill_switch_pauses_scheduled_posts_without_losing_them(
    workspace, owner, account, fake
):
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    drafts.approve(draft, owner, timezone.now() - timedelta(minutes=1))
    action_services.set_kill_switch(workspace_id=workspace.pk, enabled=True, user=owner)
    assert drafts.publish_due()["paused"] == 1
    draft.refresh_from_db()
    assert draft.status == DraftStatus.SCHEDULED
    action_services.set_kill_switch(workspace_id=workspace.pk, enabled=False, user=owner)
    assert drafts.publish_due()["launched"] == 1


def test_editing_a_scheduled_post_takes_the_approval_back(workspace, owner, account):
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    drafts.approve(draft, owner, timezone.now() + timedelta(days=1))
    drafts.update(draft, {"caption": "New caption"})
    draft.refresh_from_db()
    assert draft.status == DraftStatus.DRAFTED and draft.approved_by is None


def test_blocked_terms_stop_a_post(workspace, owner, account):
    from actions.models import Guardrail

    Guardrail.objects.create(
        workspace=workspace, key="blocked_terms", config={"terms": ["guaranteed"]}
    )
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}], caption="Guaranteed uptime!")
    with pytest.raises(ValidationFailedError):
        drafts.approve(draft, owner, None)
    assert any(c["code"] == "blocked_terms" for c in draft.checks)


def test_a_permanent_failure_marks_the_post_failed(workspace, owner, account, fake):
    asset = picture(workspace, owner)
    draft = draft_of(account, media=[{"asset_id": str(asset.pk)}])
    action = go(draft, owner)
    fake.fail_next = [
        httpx.Response(200, json={"data": [fake.quota]}),
        httpx.Response(400, json={"error": {"code": 100, "message": "Invalid image"}}),
    ]
    action_services.execute(action.pk)
    draft.refresh_from_db()
    assert draft.status == DraftStatus.FAILED and draft.error_code == "provider_rejected"
