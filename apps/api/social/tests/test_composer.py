import io

import pytest
from PIL import Image

from core.errors import ValidationFailedError
from media.models import MediaAsset
from social import checks, composer, images
from social.models import DraftStatus, PostDraft
from social.tests.conftest import llm, make_media

pytestmark = pytest.mark.django_db

COPY = {
    "title": "Is your backup real?",
    "caption": "Is your backup real? Most are not tested.\n\nHere is how to check in five minutes.\n\nSave this for Friday.",
    "alternates": ["Backups fail quietly.", "When did you last restore?"],
    "hashtags": ["#managedit", "backup", "#smallbusiness"],
    "first_comment": "Want a checklist? Message us.",
    "slides": [],
    "reel": None,
    "image_prompt": "A tidy server room, soft light, vertical 4:5, blue accents",
    "alt_text": "A tidy server room with blue lighting",
    "rationale": "Opens with the pain, ends with a save prompt.",
}
CAROUSEL = {
    **COPY,
    "slides": [
        {
            "headline": "Test your backups",
            "body": "Restore one file today.",
            "alt_text": "Slide one",
        },
        {
            "headline": "Check the date",
            "body": "Backups older than a week are a risk.",
            "alt_text": "",
        },
        {
            "headline": "Save this post",
            "body": "Send it to your IT person.",
            "alt_text": "Last slide",
        },
    ],
}
REEL = {
    **COPY,
    "reel": {
        "hook": "Your backup is probably broken",
        "beats": ["Show a failed restore", "Explain why", "Show the fix"],
        "on_screen_text": ["Probably broken", "Test it"],
        "shot_list": ["Screen recording", "Talking head"],
        "duration_seconds": 30,
        "audio_note": "Original voice",
        "cta": "Save this",
    },
}


def write(account, owner, payload, **kwargs):
    draft = composer.create_draft(account, owner.pk, brief="Why backups fail", **kwargs)
    llm(payload)
    assert composer.run(draft.pk) == "done"
    draft.refresh_from_db()
    return draft


def test_a_brief_becomes_a_post_with_a_text_card(account, owner):
    draft = write(account, owner, COPY)
    assert (
        draft.caption.startswith("Is your backup real?") and draft.title == "Is your backup real?"
    )
    assert draft.hashtags == ["#managedit", "#backup", "#smallbusiness"]
    assert draft.ai["alternates"] == COPY["alternates"] and draft.ai["rationale"]
    assert draft.ai_status == "done" and draft.status == DraftStatus.DRAFTED
    asset = MediaAsset.objects.get(pk=draft.media[0]["asset_id"])
    assert (asset.width, asset.height) == (1080, 1350) and not asset.ai_generated
    assert draft.media[0]["alt_text"] == COPY["alt_text"]
    assert not [c for c in draft.checks if c["severity"] == "error"]


def test_a_carousel_gets_one_exact_text_card_per_slide(account, owner):
    draft = write(account, owner, CAROUSEL, kind="carousel")
    assert len(draft.media) == 3
    assert [m["alt_text"] for m in draft.media] == ["Slide one", "Check the date", "Last slide"]
    assert all(MediaAsset.objects.get(pk=m["asset_id"]).width == 1080 for m in draft.media)


def test_a_reel_plan_becomes_a_rendered_vertical_video_with_a_cover(account, owner):
    draft = write(account, owner, REEL, kind="reel")
    assert draft.ai["reel"]["hook"] == "Your backup is probably broken"
    cover = MediaAsset.objects.get(pk=draft.cover_asset_id)
    assert (cover.width, cover.height) == (1080, 1920)
    movie = MediaAsset.objects.get(pk=draft.media[0]["asset_id"])
    assert movie.kind == "video" and (movie.width, movie.height) == (1080, 1920)
    assert movie.file_format == "MP4" and not movie.ai_generated
    assert 10 <= movie.duration_seconds <= 31
    assert draft.ai["video_asset_id"] == str(movie.pk)
    assert not [c for c in draft.checks if c["severity"] == "error"]


def test_a_video_the_person_attached_is_never_replaced(account, owner):
    draft = write(account, owner, REEL, kind="reel")
    own = MediaAsset.objects.get(pk=draft.media[0]["asset_id"])
    draft.ai = {**draft.ai, "video_asset_id": ""}
    draft.save()
    composer.regenerate_visuals(draft, owner.pk, "text_cards")
    draft.refresh_from_db()
    assert draft.media[0]["asset_id"] == str(own.pk)


def test_regenerating_pictures_redraws_a_generated_reel(account, owner):
    draft = write(account, owner, REEL, kind="reel")
    first = draft.media[0]["asset_id"]
    draft.ai = {**draft.ai, "reel": {**draft.ai["reel"], "on_screen_text": ["A new line"]}}
    draft.save()
    composer.regenerate_visuals(draft, owner.pk, "text_cards")
    draft.refresh_from_db()
    assert draft.media[0]["asset_id"] != first


def test_an_ai_image_is_generated_cropped_and_flagged(account, owner):
    fake = images.FakeImageGenerator()
    images.set_image_generator(fake)
    draft = write(account, owner, COPY, visuals="ai_image")
    asset = MediaAsset.objects.get(pk=draft.media[0]["asset_id"])
    assert asset.ai_generated and (asset.width, asset.height) == (1024, 1280)
    assert fake.prompts[0][0].startswith("A tidy server room")
    assert any(c["code"] == "ai_generated" for c in draft.checks)


def test_missing_image_service_keeps_the_text_and_says_why(account, owner):
    draft = write(account, owner, COPY, visuals="ai_image")
    assert draft.caption and draft.media == []
    assert draft.ai["visual_error"] == "validation_failed"


def test_feedback_revises_the_current_draft(account, owner):
    draft = write(account, owner, COPY)
    composer.revise(draft, "Make the hook shorter")
    gateway = llm({**COPY, "caption": "Short hook.\n\nBody.", "title": "Short"})
    assert composer.run(draft.pk) == "done"
    draft.refresh_from_db()
    assert draft.caption.startswith("Short hook")
    prompt = gateway.requests[0][0].system[0].text
    assert "Make the hook shorter" in prompt and "Is your backup real?" in prompt


def test_a_failed_model_call_leaves_the_draft_usable(account, owner):
    from core.errors import RetryableProviderError
    from llm.fake import FakeGateway
    from llm.service import LlmService, set_llm_service

    draft = composer.create_draft(account, owner.pk, brief="Why backups fail")
    set_llm_service(LlmService(FakeGateway([RetryableProviderError("down", provider="x")])))
    assert composer.run(draft.pk) == "failed"
    draft.refresh_from_db()
    assert draft.ai_status == "failed" and draft.status == DraftStatus.DRAFTED


def test_bad_briefs_are_refused(account, owner):
    for brief in ("", "  ", "x" * 5000):
        with pytest.raises(ValidationFailedError):
            composer.create_draft(account, owner.pk, brief=brief)
    with pytest.raises(ValidationFailedError):
        composer.create_draft(account, owner.pk, brief="ok", kind="podcast")


def test_the_top_posts_inform_the_writer(account, owner):
    make_media(
        account, "best", caption="Our best hook", metrics={"engagement_rate": 0.2, "reach": 500}
    )
    draft = composer.create_draft(account, owner.pk, brief="Another post")
    gateway = llm(COPY)
    composer.run(draft.pk)
    assert "Our best hook" in gateway.requests[0][0].system[0].text


def test_pictures_can_be_redrawn_in_another_style(account, owner):
    draft = write(account, owner, COPY)
    images.set_image_generator(images.FakeImageGenerator())
    composer.regenerate_visuals(draft, owner.pk, "ai_image")
    draft.refresh_from_db()
    assert MediaAsset.objects.get(pk=draft.media[0]["asset_id"]).ai_generated


# --- text cards and colours ---------------------------------------------------------------------


def test_cards_are_the_requested_size_and_readable():
    palette = images.Palette("#0F172A", "#2563EB", images.readable_on("#0F172A"))
    data = images.render_card(
        "A headline that wraps over several lines for sure", "Body text", palette=palette
    )
    image = Image.open(io.BytesIO(data))
    assert image.size == (1080, 1350) and images.contrast(palette.background, palette.text) >= 4.5


@pytest.mark.parametrize(
    "colour", ["#FFFFFF", "#000000", "#2563EB", "#F59E0B", "#10B981", "#7C3AED"]
)
def test_text_colour_always_has_aa_contrast(colour):
    assert images.contrast(colour, images.readable_on(colour)) >= 4.5


def test_brand_colours_are_read_from_the_profile():
    palette = images.palette_from({"brand_colors": "Primary #112233, accent #FF8800"})
    assert (palette.background, palette.accent) == ("#112233", "#FF8800")
    assert images.palette_from({}).background == images.DEFAULT_PALETTE[0]


# --- checks ---------------------------------------------------------------------------------


def draft_with(account, **fields):
    return PostDraft(workspace_id=account.workspace_id, account=account, kind="image", **fields)


def codes(draft):
    return {c["code"] for c in checks.run_checks(draft)}


def test_caption_rules(account):
    assert "caption_too_long" in codes(draft_with(account, caption="x" * 2300))
    assert "hook_too_long" in codes(draft_with(account, caption="y" * 200))
    assert "hashtags_too_many" in codes(
        draft_with(account, caption="a", hashtags=[f"#t{i}" for i in range(31)])
    )
    assert "hashtags_many" in codes(
        draft_with(account, caption="a", hashtags=[f"#t{i}" for i in range(12)])
    )
    assert "mentions_too_many" in codes(
        draft_with(account, caption=" ".join(f"@u{i}" for i in range(21)))
    )
    assert "link_in_caption" in codes(draft_with(account, caption="See https://acme.example"))
    assert "caption_empty" in codes(draft_with(account, caption=""))


def test_media_rules(account, workspace, owner):
    from media import services

    buffer = io.BytesIO()
    Image.new("RGB", (2000, 500), "red").save(buffer, "JPEG")
    wide, _ = services.create_from_chunks(
        workspace.pk, owner.pk, [buffer.getvalue()], filename="w.jpg", source="upload"
    )
    entry = [{"asset_id": str(wide.pk)}]
    assert "ratio_feed" in codes(draft_with(account, caption="a", media=entry))
    assert "ratio_vertical" in codes(
        PostDraft(workspace_id=account.workspace_id, account=account, kind="story", media=entry)
    )
    assert "carousel_count" in codes(
        PostDraft(
            workspace_id=account.workspace_id,
            account=account,
            kind="carousel",
            caption="a",
            media=entry,
        )
    )
    assert "reel_needs_video" in codes(
        PostDraft(
            workspace_id=account.workspace_id,
            account=account,
            kind="reel",
            caption="a",
            media=entry,
        )
    )
    assert "alt_missing" in codes(draft_with(account, caption="a", media=entry))
    assert "no_media" in codes(draft_with(account, caption="a"))
    ordered = [
        c["severity"]
        for c in checks.run_checks(draft_with(account, caption="x" * 2300, media=entry))
    ]
    assert ordered == sorted(ordered, key={"error": 0, "warning": 1, "info": 2}.get)
