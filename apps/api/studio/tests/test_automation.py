import pytest

from conftest import make_user
from social.models import PostDraft
from studio import config_store, writer
from studio.models import PieceStatus, StudioProfile
from studio.tests.conftest import PIECE, brand_of, llm, make_config
from workspaces.models import Role

pytestmark = pytest.mark.django_db

ROBOT = {**PIECE, "body": "Unlock the power of a seamless backup. It is a game-changer for you."}


def auto_config(**overrides):
    return make_config(
        automation={"enabled": True},
        channels={"instagram": {"enabled": True, "publish_mode": "auto"}},
        **overrides,
    )


def ai_piece(workspace, channel="instagram", kind="image"):
    return writer.create(
        brand_of(workspace), None, channel=channel, kind=kind, brief="Why backups fail", origin="ai"
    )


def run_piece(piece, *payloads):
    llm(*payloads)
    assert writer.run(piece.pk) == "done"
    piece.refresh_from_db()
    return piece


def test_a_clean_post_on_an_auto_channel_is_approved_by_the_automation_user(
    workspace, owner, account
):
    config_store.save(brand_of(workspace), auto_config(), owner)
    piece = run_piece(ai_piece(workspace), PIECE)
    assert piece.status == PieceStatus.SCHEDULED and piece.approved_by_id == owner.pk
    assert PostDraft.objects.filter(pk=piece.external["draft_id"]).exists()


def test_a_post_with_remaining_warnings_waits_for_a_person(workspace, owner, account):
    config_store.save(brand_of(workspace), auto_config(), owner)
    piece = run_piece(ai_piece(workspace), ROBOT, ROBOT)  # the repair pass fails to fix it
    assert piece.status == PieceStatus.DRAFT
    assert piece.ai_state["held_reason"] == "needs_review"
    assert not PostDraft.objects.exists()


def test_review_channels_always_wait_for_a_person(workspace, owner, account):
    review = make_config(
        automation={"enabled": True},
        channels={"instagram": {"enabled": True, "publish_mode": "review"}},
    )
    config_store.save(brand_of(workspace), review, owner)
    assert run_piece(ai_piece(workspace), PIECE).status == PieceStatus.DRAFT


def test_nothing_is_approved_when_automation_is_off(workspace, owner, account):
    off = auto_config()
    off.automation.enabled = False
    config_store.save(brand_of(workspace), off, owner)
    assert run_piece(ai_piece(workspace), PIECE).status == PieceStatus.DRAFT


def test_posts_a_person_asked_for_are_never_auto_approved(workspace, owner, account):
    config_store.save(brand_of(workspace), auto_config(), owner)
    piece = writer.create(
        brand_of(workspace), owner.pk, channel="instagram", kind="image", brief="x"
    )
    assert run_piece(piece, PIECE).status == PieceStatus.DRAFT


def test_the_right_to_approve_is_checked_when_the_post_is_ready(workspace, owner, account):
    config_store.save(brand_of(workspace), auto_config(), owner)
    viewer = make_user("v@example.com", workspace, Role.VIEWER)
    StudioProfile.objects.filter(workspace_id=workspace.pk).update(automation_user=viewer)
    piece = run_piece(ai_piece(workspace), PIECE)
    assert piece.status == PieceStatus.DRAFT and piece.ai_state["held_reason"]
    assert not PostDraft.objects.exists()


def test_the_automation_user_is_remembered_only_while_something_runs_alone(workspace, owner):
    config_store.save(brand_of(workspace), auto_config(), owner)
    assert StudioProfile.objects.get(workspace_id=workspace.pk).automation_user_id == owner.pk
    config_store.save(brand_of(workspace), make_config(), owner)
    assert StudioProfile.objects.get(workspace_id=workspace.pk).automation_user_id is None


def test_a_post_with_a_banned_word_is_never_auto_approved(workspace, owner, account):
    config_store.save(brand_of(workspace), auto_config(voice={"banned_phrases": ["backup"]}), owner)
    piece = run_piece(ai_piece(workspace), PIECE, PIECE)
    assert piece.status == PieceStatus.DRAFT
