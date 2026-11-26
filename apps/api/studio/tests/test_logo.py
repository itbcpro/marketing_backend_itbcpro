import io
import uuid

import pytest
from PIL import Image

from media.models import MediaAsset
from social import images
from studio import writer
from studio.config import VisualConfig
from studio.tests.conftest import PIECE, brand_of, llm
from studio.tests.test_designed import GOOD, HEADLINE_PIECE, make, setup, three_tries  # noqa: F401
from studio.tests.test_writer import upload, written
from studio.visuals import logo

pytestmark = pytest.mark.django_db


def png(size, colour, alpha=255) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (*colour, alpha)).save(buffer, "PNG")
    return buffer.getvalue()


def mark(colour=(20, 30, 90)) -> bytes:
    """A small logo: a coloured block on a transparent background."""
    image = Image.new("RGBA", (400, 160), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (300, 100), (*colour, 255)), (50, 30))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def pixel(data: bytes, xy) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB").getpixel(xy)


def test_the_logo_goes_into_the_asked_corner_and_nowhere_else():
    picture = png((1000, 1000), (240, 240, 240))
    for corner, inside, outside in (
        ("bottom-right", (900, 940), (100, 100)),
        ("top-left", (100, 60), (900, 940)),
    ):
        out = logo.place(picture, mark(), corner)
        assert pixel(out, inside) != (240, 240, 240)
        assert pixel(out, outside) == (240, 240, 240)


def test_the_logo_is_scaled_to_a_small_share_of_the_picture():
    out = logo.place(png((1000, 1000), (255, 255, 255)), mark(), "top-left")
    dark = [x for x in range(1000) if pixel(out, (x, 60)) != (255, 255, 255)]
    assert dark and max(dark) - min(dark) < 1000 * (logo.MAX_WIDTH + 0.02)


def test_a_logo_that_would_vanish_gets_a_plate_behind_it():
    dark_picture = png((1000, 1000), (18, 26, 80))
    out = logo.place(dark_picture, mark((20, 30, 90)), "top-left")
    plate = pixel(out, (28, 28))  # in the plate's padding, just outside the mark
    assert sum(plate) > 600
    light = logo.place(png((1000, 1000), (250, 250, 250)), mark(), "top-left")
    assert pixel(light, (20, 20)) == (250, 250, 250)  # enough contrast: no plate


def test_the_corner_varies_between_posts_but_not_for_one_post():
    keys = [uuid.uuid4() for _ in range(200)]
    assert {logo.corner_for(k) for k in keys} == set(logo.CORNERS)
    assert logo.corner_for(keys[0]) == logo.corner_for(keys[0])


def test_the_logo_is_the_chosen_file_else_an_upload_named_or_tagged_logo(workspace, owner):
    style = VisualConfig()
    assert logo.find(workspace.pk, style) is None
    generated = images.store_picture(
        workspace.pk,
        owner.pk,
        png((300, 300), (1, 2, 3)),
        name="logo generated",
        alt_text="",
        ai_generated=True,
    )
    assert logo.find(workspace.pk, style) is None  # a generated picture is never the logo
    mine = upload(workspace, owner, "ITBC brand mark", ["Logo"])
    assert logo.find(workspace.pk, style).pk == mine.pk
    chosen = VisualConfig(logo_asset_id=str(generated.pk))
    assert logo.find(workspace.pk, chosen).pk == generated.pk


def test_stamping_keeps_the_original_and_records_where_it_came_from(workspace, owner):
    original = images.store_picture(
        workspace.pk,
        owner.pk,
        png((800, 800), (200, 200, 200)),
        name="p",
        alt_text="A",
        ai_generated=True,
    )
    brand = upload(workspace, owner, "logo", ["logo"])
    entry = logo.stamp(
        workspace.pk, owner.pk, {"asset_id": str(original.pk), "alt_text": "A"}, brand, "top-left"
    )
    made = MediaAsset.objects.get(pk=entry["asset_id"])
    assert made.variant == "logo" and made.derived_from_id == original.pk and made.ai_generated
    assert MediaAsset.objects.filter(pk=original.pk).exists()


def test_every_picture_of_a_post_carries_the_logo(workspace, owner, configured):
    setup(owner, workspace, logo_on_pictures=True, prefer=["text_card"])
    upload(workspace, owner, "ITBC logo", ["logo"])
    piece = written(workspace, owner, PIECE)
    final = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert final.variant == "logo"
    assert not any(c["code"] == "logo_missing" for c in piece.checks)


def test_the_designed_picture_leaves_a_corner_free_for_the_logo(workspace, owner, configured):
    generator = setup(owner, workspace, logo_on_pictures=True)
    upload(workspace, owner, "ITBC logo", ["logo"])
    piece, _ = make(workspace, owner, HEADLINE_PIECE, GOOD)
    prompt = generator.prompts[0][0]
    assert "corner clean and empty" in prompt and "Do not draw any logo" in prompt
    final = MediaAsset.objects.get(pk=piece.media[0]["asset_id"])
    assert final.variant == "logo" and final.derived_from.ai_generated


def test_no_logo_is_a_warning_that_holds_the_post_back(workspace, owner, configured):
    setup(owner, workspace, logo_on_pictures=True, prefer=["text_card"])
    piece = written(workspace, owner, PIECE)
    assert any(c["code"] == "logo_missing" and c["severity"] == "warning" for c in piece.checks)


def test_the_logo_can_be_turned_off(workspace, owner, configured):
    generator = setup(owner, workspace, logo_on_pictures=False)
    upload(workspace, owner, "ITBC logo", ["logo"])
    piece, _ = make(workspace, owner, HEADLINE_PIECE, GOOD)
    assert "corner clean and empty" not in generator.prompts[0][0]
    assert MediaAsset.objects.get(pk=piece.media[0]["asset_id"]).variant != "logo"


# --- the whole message on the picture, and a different design each time ---------------------------


POSTER = {
    **PIECE,
    "visual": {
        **PIECE["visual"],
        "poster_lines": [
            "3 questions to test any software vendor",
            "What would you remove from this scope?",
            "How will we know it works after the first release?",
            "Book a free discovery call",
        ],
        "design_direction": "Editorial split layout, large numerals, deep navy with one warm accent.",
    },
}
POSTER_OK = {"read_text": [], "ok": True, "problems": []}


def test_all_the_poster_lines_and_the_art_direction_reach_the_image_model(
    workspace, owner, configured
):
    generator = setup(owner, workspace)
    piece, _ = make(workspace, owner, POSTER, POSTER_OK)
    prompt = generator.prompts[0][0]
    for number, line in enumerate(POSTER["visual"]["poster_lines"], start=1):
        assert f'{number}. "{line}"' in prompt
    assert "Editorial split layout" in prompt and "Hierarchy: line 1 is the hero" in prompt
    assert piece.ai_state["image_trace"][0]["expected"] == POSTER["visual"]["poster_lines"]


def test_the_last_try_keeps_every_line_and_the_chosen_layout(workspace, owner, configured):
    generator = setup(owner, workspace)
    bad = {"read_text": [], "ok": False, "problems": ["a word is misspelled"]}
    make(workspace, owner, POSTER, bad, bad, POSTER_OK)
    last = generator.prompts[2][0]
    assert all(f'{n}. "' in last for n in (1, 2, 3, 4))
    assert "Editorial split layout" in last and "Type is the hero" not in last


def test_the_writer_sees_recent_designs_and_must_differ(workspace, owner, configured):
    first = written(workspace, owner, POSTER)
    assert "Editorial split layout" in first.copy["visual"]["design_direction"]
    piece = writer.create(
        brand_of(workspace), owner.pk, channel="instagram", kind="image", brief="another"
    )
    gateway = llm(
        {**PIECE, "visual": {**PIECE["visual"], "design_direction": "Bold flat illustration."}}
    )
    assert writer.run(piece.pk) == "done"
    system = gateway.requests[0][0].system[0].text
    assert "Editorial split layout" in system  # listed as a recent design to avoid
    assert "clearly different in layout and look" in system


def test_without_poster_lines_the_headline_and_support_line_are_used(workspace, owner, configured):
    generator = setup(owner, workspace)
    make(workspace, owner, HEADLINE_PIECE, GOOD)
    assert '1. "Is your backup real?"' in generator.prompts[0][0]


def white_logo() -> Image.Image:
    """A logo saved on an opaque white background, with empty margins."""
    image = Image.new("RGB", (1000, 400), (255, 255, 255))
    image.paste(Image.new("RGB", (300, 120), (0, 0, 0)), (200, 100))
    image.paste(Image.new("RGB", (300, 120), (70, 210, 255)), (500, 100))
    return image


def test_a_logo_on_white_loses_the_white_and_the_empty_margin():
    tight = logo.prepare(white_logo())
    assert tight.size == (600, 120)
    assert tight.getpixel((10, 10))[3] == 255 and tight.getpixel((450, 60))[:3] == (70, 210, 255)
    corner = Image.new("RGBA", (50, 50), (255, 255, 255, 255))
    assert logo.prepare(corner.convert("RGB")).size[0] >= 1  # an all-white file does not crash


def test_the_white_of_a_logo_file_is_not_painted_on_the_picture():
    buffer = io.BytesIO()
    white_logo().save(buffer, "PNG")
    picture = png((1000, 1000), (40, 60, 120))
    out = logo.place(picture, buffer.getvalue(), "top-left")
    width = round(1000 * logo.MAX_WIDTH)
    height = round(width * 120 / 600)
    margin = round(1000 * logo.MARGIN)
    assert pixel(out, (margin + width + 40, margin + 2)) == (40, 60, 120)  # beside the mark
    assert (
        max(x for x in range(1000) if pixel(out, (x, margin + height // 2)) != (40, 60, 120))
        < margin + width + 30
    )


def test_the_logo_stays_small_in_its_corner():
    assert logo.MAX_WIDTH <= 0.13 and logo.MAX_HEIGHT <= 0.07


def busy(size=(1000, 1000)) -> Image.Image:
    import random

    random.seed(1)
    image = Image.new("RGB", size, (128, 128, 128))
    for x in range(0, size[0], 10):
        for y in range(0, size[1], 10):
            shade = random.randint(0, 255)
            image.paste((shade, 255 - shade, shade // 2), (x, y, x + 10, y + 10))
    return image


def test_the_logo_goes_where_the_picture_is_quietest():
    picture = busy()
    picture.paste(Image.new("RGB", (250, 250), (235, 235, 235)), (0, 750))  # calm bottom-left
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    out = logo.place_in_calmest_corner(buffer.getvalue(), mark(), "top-right")
    with Image.open(io.BytesIO(out)) as result:
        changed = [
            c
            for c in logo.CORNERS
            if result.convert("RGB").crop(_box(c)).tobytes() != picture.crop(_box(c)).tobytes()
        ]
    assert changed == ["bottom-left"]


def _box(corner: str) -> tuple[int, int, int, int]:
    x = 0 if corner.endswith("left") else 750
    y = 0 if corner.startswith("top") else 750
    return x, y, x + 250, y + 250


def test_on_a_close_call_the_preferred_corner_wins():
    flat = png((1000, 1000), (230, 230, 230))
    out = logo.place_in_calmest_corner(flat, mark(), "top-right")
    assert pixel(out, (900, 70)) != (230, 230, 230) and pixel(out, (100, 70)) == (230, 230, 230)
