import io
import uuid

import pytest
from PIL import Image

from core.errors import RetryableProviderError, ValidationFailedError
from llm.fake import FakeGateway
from llm.service import LlmService, set_llm_service
from media import services as media_services
from media.models import MediaAsset, MediaSource
from social import images
from studio import config_store, writer
from studio.models import ContentIdea, ContentPiece, PieceStatus
from studio.tests.conftest import CAROUSEL, PIECE, VIDEO, brand_of, llm, make_config

pytestmark = pytest.mark.django_db


def new_piece(workspace, owner, channel="instagram", kind="image", **fields) -> ContentPiece:
    return writer.create(
        brand_of(workspace),
        owner.pk,
        channel=channel,
        kind=kind,
        brief="Why backups fail",
        **fields,
    )


def written(workspace, owner, payload, **kw) -> ContentPiece:
    piece = new_piece(workspace, owner, **kw)
    llm(payload)
    assert writer.run(piece.pk) == "done"
    piece.refresh_from_db()
    return piece


def jpeg_bytes(width=1400, height=1800, colour=(120, 130, 140)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, "PNG")
    return buffer.getvalue()


def upload(workspace, owner, name, tags, colour=(120, 130, 140)) -> MediaAsset:
    asset, _ = media_services.create_from_chunks(
        workspace.pk,
        owner.pk,
        [jpeg_bytes(colour=colour)],
        filename=f"{name}.png",
        source=MediaSource.UPLOAD,
        title=name,
        tags=tags,
    )
    return asset


def test_a_brief_becomes_a_post_with_a_text_card(workspace, owner, configured):
    config_store.save(brand_of(workspace), make_config(visuals={"prefer": ["text_card"]}), owner)
    piece = written(workspace, owner, PIECE)
    assert piece.status == PieceStatus.DRAFT and piece.ai_status == "done"
    assert piece.title == "Test your backup"
    assert piece.copy["hashtags"] == ["#itsupport", "#backup"]
    assert piece.copy["alternate_hooks"] and piece.copy["rationale"]
    asset = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert asset.width == 1080 and not asset.ai_generated
    assert not [c for c in piece.checks if c["severity"] == "error"]


def test_a_carousel_gets_one_card_per_slide(workspace, owner, configured):
    piece = written(workspace, owner, CAROUSEL, kind="carousel")
    assert len(piece.media) == 3
    assert [m["alt_text"] for m in piece.media] == ["Slide 1", "Slide 2", "Slide 3"]


def test_a_text_post_needs_no_pictures(workspace, owner, configured):
    piece = written(workspace, owner, PIECE, channel="linkedin", kind="text")
    assert piece.media == [] and piece.status == PieceStatus.DRAFT
    assert not [c for c in piece.checks if c["severity"] == "error"]


def test_a_video_post_gets_a_rendered_clip_and_a_cover(workspace, owner, configured):
    config_store.save(brand_of(workspace), make_config(visuals={"prefer": ["text_card"]}), owner)
    piece = written(workspace, owner, VIDEO, kind="video")
    clip = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert clip.kind == "video" and (clip.width, clip.height) == (1080, 1920)
    assert piece.cover_asset_id is not None
    assert piece.ai_state["video_asset_id"] == str(clip.pk)


def test_a_video_without_a_plan_is_flagged_not_lost(workspace, owner, configured):
    piece = written(workspace, owner, PIECE, kind="video")
    assert piece.media == [] and "video:no_plan" in piece.ai_state["visual_errors"]
    assert any(c["code"] == "media_missing" and c["severity"] == "error" for c in piece.checks)


def test_text_that_reads_as_machine_made_gets_one_repair_pass(workspace, owner, configured):
    robot = {
        **PIECE,
        "body": "Unlock the power of a seamless backup. It is a game-changer for you.",
    }
    gateway = llm(robot, PIECE)
    piece = new_piece(workspace, owner)
    assert writer.run(piece.pk) == "done"
    piece.refresh_from_db()
    assert len(gateway.requests) == 2
    assert "game-changer" in gateway.requests[1][0].system[0].text  # the feedback lists the habits
    assert piece.copy["body"].startswith("Most small offices")
    assert not [c for c in piece.checks if c["code"].startswith("voice_")]


def test_owner_banned_phrases_block_approval_even_after_the_repair(workspace, owner, configured):
    config_store.save(brand_of(workspace), make_config(voice={"banned_phrases": ["backup"]}), owner)
    piece = new_piece(workspace, owner)
    llm(PIECE, PIECE)
    writer.run(piece.pk)
    piece.refresh_from_db()
    assert any(
        c["code"] == "voice_banned_phrase" and c["severity"] == "error" for c in piece.checks
    )


def test_ai_photos_are_art_directed_to_look_real(workspace, owner, configured):
    config_store.save(
        brand_of(workspace),
        make_config(
            visuals={
                "prefer": ["ai_image"],
                "cover_style": "headline",
                "style": "warm office, 35mm",
                "avoid": ["stock smiles"],
            }
        ),
        owner,
    )
    generator = images.FakeImageGenerator()
    images.set_image_generator(generator)
    piece = written(workspace, owner, PIECE)
    prompt, orientation = generator.prompts[0]
    assert "external drive" in prompt and "warm office, 35mm" in prompt and "stock smiles" in prompt
    assert "Do not include: any text" in prompt and orientation == "portrait"
    assert MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).ai_generated


def test_the_next_source_is_tried_when_a_service_is_not_connected(workspace, owner, configured):
    config_store.save(
        brand_of(workspace), make_config(visuals={"prefer": ["ai_image", "text_card"]}), owner
    )
    piece = written(workspace, owner, PIECE)
    assert "ai_image:unavailable" in piece.ai_state["visual_errors"]
    assert (
        len(piece.media) == 1
        and not MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).ai_generated
    )


def test_the_media_library_comes_first_and_pictures_are_not_reused(workspace, owner, configured):
    config_store.save(
        brand_of(workspace), make_config(visuals={"prefer": ["library", "text_card"]}), owner
    )
    desk = upload(workspace, owner, "Backup desk", ["backup", "desk"])
    first = written(workspace, owner, PIECE)
    picked = MediaAsset.objects.get(pk=first.media[0]["asset_id"])
    assert picked.variant == "headline" and picked.derived_from.derived_from_id == desk.pk
    second = written(workspace, owner, PIECE)
    assert MediaAsset.objects.get(pk=second.media[0]["asset_id"]).derived_from_id != desk.pk


def test_a_failed_model_call_marks_the_post_failed(workspace, owner, configured):
    piece = new_piece(workspace, owner)
    set_llm_service(LlmService(FakeGateway([RetryableProviderError("down", provider="x")])))
    assert writer.run(piece.pk) == "failed"
    piece.refresh_from_db()
    assert piece.status == PieceStatus.FAILED and piece.ai_error_code


def test_feedback_revises_the_current_text(workspace, owner, configured):
    piece = written(workspace, owner, PIECE)
    writer.revise(piece, "Make it shorter and add a number")
    piece.refresh_from_db()
    assert piece.status == PieceStatus.DRAFTING and piece.ai_status == "thinking"
    gateway = llm({**PIECE, "body": "Short version. Restore one file today."})
    assert writer.run(piece.pk) == "done"
    prompt = gateway.requests[0][0].system[0].text
    assert "Make it shorter" in prompt and "Most small offices" in prompt
    piece.refresh_from_db()
    assert piece.copy["body"].startswith("Short version") and piece.ai_state["feedback"] == ""


def test_a_post_can_only_be_created_for_a_real_channel_and_type(workspace, owner):
    with pytest.raises(ValidationFailedError):
        writer.create(brand_of(workspace), owner.pk, channel="myspace", kind="image", brief="x")
    with pytest.raises(ValidationFailedError):
        writer.create(brand_of(workspace), owner.pk, channel="instagram", kind="text", brief="x")
    with pytest.raises(ValidationFailedError):
        writer.create(brand_of(workspace), owner.pk, channel="instagram", kind="image", brief="  ")


def test_an_idea_can_stand_in_for_a_brief(workspace, owner, configured):
    idea = ContentIdea.objects.create(
        workspace_id=workspace.pk, brand=brand_of(workspace), title="Idea", angle="An angle"
    )
    piece = writer.create(
        brand_of(workspace), owner.pk, channel="linkedin", kind="text", idea_id=idea.pk
    )
    gateway = llm(PIECE)
    assert writer.run(piece.pk) == "done"
    assert "An angle" in gateway.requests[0][0].system[0].text


def test_a_thinking_post_that_never_finishes_is_marked_failed(workspace, owner, configured):
    from datetime import timedelta

    from django.utils import timezone

    piece = new_piece(workspace, owner)
    ContentPiece.objects.filter(pk=piece.pk).update(ai_since=timezone.now() - timedelta(minutes=30))
    piece.refresh_from_db()
    assert writer.refresh_status(piece).status == PieceStatus.FAILED


def test_pictures_can_be_redrawn_from_the_current_text(workspace, owner, configured):
    piece = written(workspace, owner, PIECE)
    before = piece.media[0]["asset_id"]
    config_store.save(brand_of(workspace), make_config(visuals={"prefer": ["text_card"]}), owner)
    piece.copy = {
        **piece.copy,
        "visual": {**piece.copy["visual"], "on_image_text": "Test it today"},
    }
    piece.save()
    again = writer.redraw(piece, owner.pk)
    assert again.media[0]["asset_id"] != before
    assert uuid.UUID(again.media[0]["asset_id"])


def photo_config(owner, workspace, **visuals):
    config_store.save(brand_of(workspace), make_config(visuals=visuals), owner)


def test_a_photo_gets_its_headline_written_on_it(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["ai_image"], cover_style="headline")
    images.set_image_generator(images.FakeImageGenerator())
    piece = written(
        workspace,
        owner,
        {**PIECE, "visual": {**PIECE["visual"], "on_image_text": "Test your backup today"}},
    )
    final = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert final.variant == "headline" and final.ai_generated
    assert (final.width, final.height) == (1024, 1280)  # the 4:5 crop of the generated photo
    assert final.derived_from.derived_from.ai_generated  # the plain photo is kept


def test_the_headline_falls_back_to_the_hook(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["ai_image"], cover_style="headline")
    images.set_image_generator(images.FakeImageGenerator())
    piece = written(workspace, owner, PIECE)
    assert MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).variant == "headline"


def test_text_on_pictures_can_be_turned_off(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["ai_image"], cover_style="plain")
    images.set_image_generator(images.FakeImageGenerator())
    piece = written(workspace, owner, PIECE)
    assert MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).variant == "meta_portrait"


def test_text_cards_are_not_covered_twice(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["text_card"])
    piece = written(workspace, owner, PIECE)
    assert MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).variant != "headline"


def test_a_carousel_opens_with_a_photo_and_continues_with_cards(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["ai_image", "text_card"], cover_style="headline")
    images.set_image_generator(images.FakeImageGenerator())
    piece = written(workspace, owner, CAROUSEL, kind="carousel")
    assets = [MediaAsset.objects.get(pk=m["asset_id"]) for m in piece.media]
    assert len(assets) == 3 and assets[0].variant == "headline" and assets[0].ai_generated
    assert all(not a.ai_generated for a in assets[1:])
    assert piece.media[0]["alt_text"]


def test_a_carousel_without_a_photo_source_stays_all_cards(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["text_card"])
    piece = written(workspace, owner, CAROUSEL, kind="carousel")
    assert not any(
        MediaAsset.objects.get(pk=m["asset_id"]).variant == "headline" for m in piece.media
    )


def test_a_video_cover_carries_the_headline(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["ai_image"], cover_style="headline")
    images.set_image_generator(images.FakeImageGenerator())
    piece = written(workspace, owner, VIDEO, kind="video")
    assert MediaAsset.objects.get(pk=piece.cover_asset_id).variant == "headline"


def test_realistic_ai_pictures_raise_the_label_warning(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["ai_image"], cover_style="headline")
    images.set_image_generator(images.FakeImageGenerator())
    piece = written(workspace, owner, PIECE)
    warning = next(c for c in piece.checks if c["code"] == "ai_generated_media")
    assert warning["severity"] == "warning" and warning["params"]["count"] == 1


def test_text_cards_and_real_photos_need_no_label(workspace, owner, configured):
    photo_config(owner, workspace, prefer=["text_card"])
    piece = written(workspace, owner, PIECE)
    assert not any(c["code"] == "ai_generated_media" for c in piece.checks)


def test_the_label_warning_also_holds_a_post_back_from_automatic_approval(
    workspace, owner, account
):
    from studio.models import PieceStatus

    config_store.save(
        brand_of(workspace),
        make_config(
            visuals={"prefer": ["ai_image"], "cover_style": "headline"},
            automation={"enabled": True},
            channels={"instagram": {"enabled": True, "publish_mode": "auto"}},
        ),
        owner,
    )
    images.set_image_generator(images.FakeImageGenerator())
    piece = writer.create(
        brand_of(workspace), None, channel="instagram", kind="image", brief="x", origin="ai"
    )
    llm(PIECE)
    writer.run(piece.pk)
    piece.refresh_from_db()
    assert piece.status == PieceStatus.DRAFT and piece.ai_state["held_reason"] == "needs_review"
