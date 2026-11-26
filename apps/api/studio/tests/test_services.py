from datetime import timedelta

import pytest
from django.utils import timezone

from conftest import make_user
from core.errors import ConflictError, PermissionDeniedError, ValidationFailedError
from social.models import DraftStatus, PostDraft
from studio import config_store, services, writer
from studio.models import ContentPiece, PieceStatus
from studio.tests.conftest import PIECE, brand_of, llm, make_config
from workspaces.models import Role

pytestmark = pytest.mark.django_db


def written(workspace, owner, channel="instagram", kind="image", payload=PIECE, **fields):
    piece = writer.create(
        brand_of(workspace),
        owner.pk,
        channel=channel,
        kind=kind,
        brief="Why backups fail",
        **fields,
    )
    llm(payload)
    assert writer.run(piece.pk) == "done"
    piece.refresh_from_db()
    return piece


def soon():
    return timezone.now() + timedelta(hours=3)


# --- posting by hand (a channel without a connected publisher) -------------------------------------


def test_a_linkedin_post_becomes_a_kit_that_is_due_at_its_time(workspace, owner, configured):
    piece = written(workspace, owner, "linkedin", "text")
    services.approve(piece, owner, soon())
    piece.refresh_from_db()
    assert piece.status == PieceStatus.SCHEDULED and piece.external == {"manual": True}
    assert piece.approved_by_id == owner.pk
    assert services.dispatch_due()["due"] == 0  # not yet
    ContentPiece.objects.filter(pk=piece.pk).update(
        scheduled_at=timezone.now() - timedelta(minutes=1)
    )
    assert services.dispatch_due()["due"] == 1
    piece.refresh_from_db()
    assert piece.status == PieceStatus.MANUAL_DUE
    posted = services.mark_posted(piece, "https://linkedin.com/posts/abc")
    assert posted.status == PieceStatus.PUBLISHED and posted.permalink.endswith("abc")
    assert posted.published_at is not None


def test_the_kit_holds_the_text_to_paste_with_hashtags_once(workspace, owner, configured):
    piece = written(workspace, owner, "linkedin", "text")
    kit = services.kit(piece)
    assert kit["text"].startswith("Most small offices") and kit["text"].endswith(
        "#itsupport #backup"
    )
    assert kit["text"].count("#itsupport") == 1


def test_a_channel_set_to_manual_never_uses_the_publisher(workspace, owner, account):
    config_store.save(
        brand_of(workspace),
        make_config(channels={"instagram": {"enabled": True, "publish_mode": "manual"}}),
        owner,
    )
    piece = written(workspace, owner)
    services.approve(piece, owner, soon())
    piece.refresh_from_db()
    assert piece.external == {"manual": True} and not PostDraft.objects.exists()


# --- Instagram goes through the existing pipeline -----------------------------------------------------


def test_an_instagram_post_is_handed_to_the_post_pipeline(workspace, owner, configured, account):
    piece = written(workspace, owner)
    when = soon()
    services.approve(piece, owner, when)
    piece.refresh_from_db()
    draft = PostDraft.objects.get(pk=piece.external["draft_id"])
    assert draft.status == DraftStatus.SCHEDULED and draft.scheduled_at == when
    assert draft.kind == "image" and draft.caption.startswith("Most small offices")
    assert draft.hashtags == ["#itsupport", "#backup"] and draft.approved_by_id == owner.pk
    assert draft.media == piece.media and draft.account_id == account.pk
    assert piece.status == PieceStatus.SCHEDULED and "manual" not in piece.external


def test_video_posts_become_reels(workspace, owner, configured, account):
    from studio.tests.conftest import VIDEO

    piece = written(workspace, owner, kind="video", payload=VIDEO)
    services.approve(piece, owner, soon())
    piece.refresh_from_db()
    assert PostDraft.objects.get(pk=piece.external["draft_id"]).kind == "reel"


def test_published_and_failed_states_are_copied_back(workspace, owner, configured, account):
    piece = written(workspace, owner)
    services.approve(piece, owner, soon())
    piece.refresh_from_db()
    draft = PostDraft.objects.get(pk=piece.external["draft_id"])
    PostDraft.objects.filter(pk=draft.pk).update(
        status=DraftStatus.PUBLISHED,
        permalink="https://instagram.com/p/x",
        published_at=timezone.now(),
    )
    assert services.sync_external()["changed"] == 1
    piece.refresh_from_db()
    assert piece.status == PieceStatus.PUBLISHED and piece.permalink.endswith("/p/x")

    other = written(workspace, owner)
    services.approve(other, owner, soon())
    other.refresh_from_db()
    PostDraft.objects.filter(pk=other.external["draft_id"]).update(
        status=DraftStatus.FAILED, error_code="missed_slot", error_detail="late"
    )
    services.sync_external()
    other.refresh_from_db()
    assert other.status == PieceStatus.FAILED and other.error_code == "missed_slot"


def test_a_post_that_fails_the_instagram_checks_is_not_approved(
    workspace, owner, configured, account
):
    piece = written(workspace, owner)
    piece.media = []
    piece.save()
    with pytest.raises(ValidationFailedError):
        services.approve(piece, owner, soon())
    assert not PostDraft.objects.exists()
    piece.refresh_from_db()
    assert piece.status == PieceStatus.DRAFT and piece.checks


def test_taking_a_post_back_cancels_the_instagram_side(workspace, owner, configured, account):
    piece = written(workspace, owner)
    services.approve(piece, owner, soon())
    piece.refresh_from_db()
    draft_id = piece.external["draft_id"]
    services.unschedule(piece)
    piece.refresh_from_db()
    assert (
        piece.status == PieceStatus.DRAFT and piece.external == {} and piece.approved_by_id is None
    )
    assert PostDraft.objects.get(pk=draft_id).status == DraftStatus.CANCELLED


# --- rules ------------------------------------------------------------------------------------------


def test_only_people_who_may_approve_can_approve(workspace, owner, configured):
    piece = written(workspace, owner, "linkedin", "text")
    viewer = make_user("v@example.com", workspace, Role.VIEWER)
    with pytest.raises(PermissionDeniedError):
        services.approve(piece, viewer)


def test_banned_words_and_missing_pictures_block_approval(workspace, owner, configured):
    piece = written(workspace, owner)
    piece.media = []
    piece.save()
    with pytest.raises(ValidationFailedError):
        services.approve(piece, owner)


def test_editing_a_scheduled_post_takes_back_the_approval(workspace, owner, configured):
    piece = written(workspace, owner, "linkedin", "text")
    services.approve(piece, owner, soon())
    edited = services.update(piece, {"body": "A different post body that is long enough."})
    assert edited.status == PieceStatus.DRAFT and edited.approved_by_id is None
    assert edited.copy["body"].startswith("A different")


def test_edits_are_checked_against_the_channel_limits(workspace, owner, configured):
    piece = written(workspace, owner, "linkedin", "text")
    edited = services.update(piece, {"body": "x" * 3100})
    assert any(c["code"] == "body_too_long" and c["severity"] == "error" for c in edited.checks)
    with pytest.raises(ValidationFailedError):
        services.update(piece, {"status": "published"})


def test_published_posts_cannot_change(workspace, owner, configured):
    piece = written(workspace, owner, "linkedin", "text")
    services.mark_posted(piece)
    with pytest.raises(ConflictError):
        services.update(piece, {"title": "x"})
    with pytest.raises(ConflictError):
        services.cancel(piece)


def test_cancelling_a_scheduled_post_withdraws_it(workspace, owner, configured, account):
    piece = written(workspace, owner)
    services.approve(piece, owner, soon())
    piece.refresh_from_db()
    draft_id = piece.external["draft_id"]
    services.cancel(piece)
    assert piece.status == PieceStatus.CANCELLED
    assert PostDraft.objects.get(pk=draft_id).status == DraftStatus.CANCELLED
