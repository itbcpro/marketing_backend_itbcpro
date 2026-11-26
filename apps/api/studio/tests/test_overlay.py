import io

from PIL import Image

from social.images import Palette
from studio.visuals.overlay import headline_on_photo, short_headline

PALETTE = Palette("#0F172A", "#2563EB", "#F8FAFC")


def photo(colour=(240, 240, 240), size=(1024, 1536)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


def render(headline="Is your backup real?", size=(1080, 1350), **kw) -> Image.Image:
    data = headline_on_photo(photo(), headline, palette=PALETTE, size=size, **kw)
    return Image.open(io.BytesIO(data))


def test_the_photo_is_cropped_to_the_post_shape():
    for size in ((1080, 1350), (1080, 1920), (1200, 628)):
        assert render(size=size).size == size


def test_text_stays_readable_on_a_white_photo():
    # A white photo is the worst case: the gradient must turn the text area dark.
    image = render()
    bottom = image.getpixel((540, 1300))
    top = image.getpixel((540, 40))
    assert sum(bottom) < 300 and sum(top) > 600
    assert any(
        image.getpixel((x, y)) == (255, 255, 255)
        for x in range(80, 600, 3)
        for y in range(900, 1250, 3)
    )


def test_long_headlines_still_fit_inside_the_margins():
    image = render(
        "A backup nobody has ever restored is a hope and not a plan for your company " * 2
    )
    margin = round(1080 * 0.08)
    right_strip = [
        image.getpixel((x, y)) for x in range(1080 - margin + 8, 1080) for y in range(0, 1350, 25)
    ]
    assert not any(
        p == (255, 255, 255) for p in right_strip
    )  # nothing white spills into the right margin


def test_story_text_keeps_clear_of_the_bottom_buttons():
    image = render(size=(1080, 1920))
    reserved = [image.getpixel((x, y)) for x in range(80, 1000, 7) for y in range(1760, 1900, 7)]
    assert not any(p == (255, 255, 255) for p in reserved)


def test_a_handle_is_added_when_given():
    plain = render()
    named = render(handle="@itbcpro")
    assert plain.tobytes() != named.tobytes()


def test_the_headline_is_cut_to_a_short_line():
    assert short_headline("Is your backup real? Most are not tested.") == "Is your backup real?"
    long = "one two three four five six seven eight nine ten eleven twelve"
    assert short_headline(long).split() == long.split()[:10]
    assert short_headline("No full stop here.") == "No full stop here"
