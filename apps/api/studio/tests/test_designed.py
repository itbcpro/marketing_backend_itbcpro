import pytest

from core.errors import RetryableProviderError
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service
from media.models import MediaAsset
from social import images
from studio import config_store, writer
from studio.tests.conftest import CAROUSEL, PIECE, brand_of, llm, make_config
from studio.tests.test_writer import upload
from studio.visuals import designer

pytestmark = pytest.mark.django_db

HEADLINE_PIECE = {
    **PIECE,
    "visual": {
        **PIECE["visual"],
        "on_image_text": "Is your backup real?",
        "support_line": "Most are never tested",
    },
}
GOOD = {"read_text": ["Is your backup real?", "Most are never tested"], "ok": True, "problems": []}
BAD = {
    "read_text": ["Is your backap real?"],
    "ok": False,
    "problems": ['"backup" is spelled "backap"'],
}


def setup(owner, workspace, **visuals):
    config_store.save(
        brand_of(workspace), make_config(visuals={"prefer": ["ai_image"], **visuals}), owner
    )
    generator = images.FakeImageGenerator()
    images.set_image_generator(generator)
    return generator


def make(workspace, owner, *payloads, kind="image"):
    piece = writer.create(brand_of(workspace), owner.pk, channel="instagram", kind=kind, brief="x")
    gateway = llm(*payloads)
    assert writer.run(piece.pk) == "done"
    piece.refresh_from_db()
    return piece, gateway


@pytest.fixture(autouse=True)
def three_tries(monkeypatch):
    """The studio tries once to save money; these tests cover the retry logic itself."""
    monkeypatch.setattr(designer, "MAX_ATTEMPTS", 3)


def ai_assets(workspace):
    return MediaAsset.objects.filter(workspace_id=workspace.pk, ai_generated=True)


def test_the_image_model_designs_the_whole_cover_and_it_is_proofread(workspace, owner, configured):
    generator = setup(owner, workspace)
    piece, gateway = make(workspace, owner, HEADLINE_PIECE, GOOD)
    prompt, orientation = generator.prompts[0]
    assert '1. "Is your backup real?"' in prompt and '2. "Most are never tested"' in prompt
    assert "no other words anywhere" in prompt and orientation == "square"
    final = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert final.variant == "meta_square" and final.ai_generated  # no overlay was added
    assert len(generator.prompts) == 1
    sent = gateway.requests[1][0]  # the proofreader saw the picture and the expected text
    assert sent.messages[0].content[0].raw["type"] == "image"
    assert "Is your backup real?" in sent.system[0].text


def test_a_misspelled_cover_is_drawn_again_with_the_mistakes_pointed_out(
    workspace, owner, configured
):
    generator = setup(owner, workspace)
    piece, _ = make(workspace, owner, HEADLINE_PIECE, BAD, GOOD)
    assert len(generator.prompts) == 2
    assert "backap" in generator.prompts[1][0] and "text errors" in generator.prompts[1][0]
    assert ai_assets(workspace).count() == 2  # the rejected attempt was deleted
    assert MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).variant == "meta_square"


def test_when_the_model_never_gets_the_text_right_the_studio_sets_the_headline(
    workspace, owner, configured
):
    generator = setup(owner, workspace)
    piece, _ = make(workspace, owner, HEADLINE_PIECE, BAD, BAD, BAD)
    assert len(generator.prompts) == 4  # three designed attempts, then one plain photograph
    final = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert final.variant == "headline"
    assert ai_assets(workspace).count() == 3  # photo, its crop, and the exact-text copy


def test_a_proofreader_that_cannot_be_asked_does_not_block_the_cover(workspace, owner, configured):
    generator = setup(owner, workspace)
    piece = writer.create(
        brand_of(workspace), owner.pk, channel="instagram", kind="image", brief="x"
    )
    set_llm_service(
        LlmService(
            FakeGateway(
                [
                    text_response(__import__("json").dumps(HEADLINE_PIECE)),
                    RetryableProviderError("down", provider="x"),
                ]
            )
        )
    )
    assert writer.run(piece.pk) == "done"
    piece.refresh_from_db()
    assert len(generator.prompts) == 1
    assert MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).variant == "meta_square"


def test_a_real_photo_from_the_library_still_gets_a_headline(workspace, owner, configured):
    config_store.save(
        brand_of(workspace), make_config(visuals={"prefer": ["library", "ai_image"]}), owner
    )
    upload(workspace, owner, "Backup desk", ["backup", "desk"])
    piece, _ = make(workspace, owner, HEADLINE_PIECE)
    final = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert final.variant == "headline" and not final.ai_generated


def test_a_plain_cover_style_adds_no_text_at_all(workspace, owner, configured):
    generator = setup(owner, workspace, cover_style="plain")
    piece, gateway = make(workspace, owner, HEADLINE_PIECE)
    assert len(gateway.requests) == 1 and "Text on the image" not in generator.prompts[0][0]
    assert MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).variant == "meta_portrait"


def test_a_carousel_cover_is_designed_and_inner_slides_stay_exact_cards(
    workspace, owner, configured
):
    generator = setup(owner, workspace)
    piece, _ = make(
        workspace,
        owner,
        {**CAROUSEL, **{"visual": HEADLINE_PIECE["visual"]}},
        GOOD,
        kind="carousel",
    )
    assets = [MediaAsset.objects.get(pk=m["asset_id"]) for m in piece.media]
    assert assets[0].ai_generated and not any(a.ai_generated for a in assets[1:])
    assert len(generator.prompts) == 1


def test_designed_slides_are_proofread_one_by_one_and_fall_back_to_cards(
    workspace, owner, configured
):
    generator = setup(owner, workspace, slides_style="designed")
    payloads = [
        {**CAROUSEL, "visual": HEADLINE_PIECE["visual"]},
        {"read_text": [], "ok": True, "problems": []},  # slide 1
        {"read_text": [], "ok": True, "problems": []},  # slide 2
        BAD,
        BAD,
        BAD,  # slide 3 never works
        GOOD,  # then the cover
    ]
    piece, _ = make(workspace, owner, *payloads, kind="carousel")
    assets = [MediaAsset.objects.get(pk=m["asset_id"]) for m in piece.media]
    assert [a.ai_generated for a in assets] == [True, True, False]
    assert any("inner slide" in prompt for prompt, _ in generator.prompts[1:])


def test_the_writer_is_told_to_put_the_message_in_the_picture():
    from llm import prompts

    body = prompts.get_prompt("studio/piece-writer").body
    assert "on_image_text" in body and "support_line" in body


def test_every_image_prompt_and_verdict_is_kept_on_the_post(workspace, owner, configured):
    setup(owner, workspace)
    piece, _ = make(workspace, owner, HEADLINE_PIECE, BAD, GOOD)
    trace = piece.ai_state["image_trace"]
    assert [t["kind"] for t in trace] == ["designed", "designed"]
    assert trace[0]["ok"] is False and "backap" in trace[0]["problems"][0]
    assert trace[0]["read_text"] == ["Is your backap real?"] and trace[1]["ok"] is True
    assert '1. "Is your backup real?"' in trace[0]["prompt"]
    assert "text errors" in trace[1]["prompt"]


def test_the_trace_shows_the_fallback_to_a_plain_photo(workspace, owner, configured):
    setup(owner, workspace)
    piece, _ = make(workspace, owner, HEADLINE_PIECE, BAD, BAD, BAD)
    kinds = [t["kind"] for t in piece.ai_state["image_trace"]]
    assert kinds == ["designed", "designed", "designed", "plain"]
    assert "Do not include: any text" in piece.ai_state["image_trace"][-1]["prompt"]


def test_designed_feed_pictures_are_square_and_never_cropped(workspace, owner, configured):
    generator = setup(owner, workspace)
    piece, _ = make(workspace, owner, HEADLINE_PIECE, GOOD)
    prompt, orientation = generator.prompts[0]
    assert orientation == "square" and "nothing is cropped" in prompt
    final = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert final.width == final.height
    assert "no other words anywhere" in prompt


def test_a_vertical_story_is_told_how_much_the_side_crop_removes(workspace, owner, configured):
    generator = setup(owner, workspace)
    make(workspace, owner, HEADLINE_PIECE, GOOD, kind="story")
    prompt = generator.prompts[0][0]
    assert "1024x1536 canvas" in prompt and "outer 8 percent on the left and right" in prompt


def test_every_try_keeps_the_designed_look_and_every_line(workspace, owner, configured):
    from studio import design

    generator = setup(owner, workspace)
    piece, _ = make(workspace, owner, HEADLINE_PIECE, BAD, BAD, GOOD)
    prompts_ = [p for p, _ in generator.prompts]
    plain = design.find("bold typographic poster").brief
    assert all('2. "Most are never tested"' in p for p in prompts_)
    assert all(plain not in p for p in prompts_)
    assert piece.ai_state["image_trace"][2]["expected"] == [
        "Is your backup real?",
        "Most are never tested",
    ]


def test_a_rate_limited_image_service_is_waited_for_not_given_up_on(monkeypatch):
    from core.errors import RateLimitedError
    from studio.visuals import designer

    waits = []
    monkeypatch.setattr(designer.time, "sleep", waits.append)

    class Busy:
        calls = 0

        def generate(self, prompt, *, orientation):
            self.calls += 1
            if self.calls < 3:
                raise RateLimitedError("slow down", provider="openai")
            return b"png"

    busy = Busy()
    assert designer._draw(busy, "p", "square") == b"png"
    assert waits == [20, 45]


def test_one_try_is_the_default_so_a_bad_cover_costs_one_picture(
    workspace, owner, configured, monkeypatch
):
    monkeypatch.setattr(designer, "MAX_ATTEMPTS", 1)
    generator = setup(owner, workspace)
    make(workspace, owner, HEADLINE_PIECE, BAD)
    assert len(generator.prompts) == 2  # one designed attempt, then one plain photograph
