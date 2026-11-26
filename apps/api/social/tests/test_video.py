import pytest

from core.errors import ValidationFailedError
from social import images, video
from social.schemas import ReelPlan

PALETTE = images.Palette("#0F172A", "#2563EB", "#FFFFFF")


def plan(**changes) -> ReelPlan:
    values = {
        "hook": "Your backup is probably broken",
        "beats": ["Show a failed restore", "Explain why", "Show the fix"],
        "on_screen_text": ["Probably broken", "Test it"],
        "shot_list": [],
        "duration_seconds": 20,
        "cta": "Save this",
    }
    return ReelPlan(**{**values, **changes})


def test_scenes_follow_the_plan_and_fit_the_length():
    scenes = video.scenes_for(plan())
    assert [s.headline for s in scenes] == [
        "Your backup is probably broken",
        "Probably broken",
        "Test it",
        "Save this",
    ]
    assert all(s.body == "" for s in scenes)  # filming notes are never shown to viewers
    assert scenes[0].seconds == video.HOOK_SCENE
    assert all(video.MIN_SCENE <= s.seconds <= video.MAX_SCENE for s in scenes[1:])


def test_beats_are_used_only_when_there_is_no_on_screen_text():
    scenes = video.scenes_for(plan(on_screen_text=[], cta=""))
    assert [s.headline for s in scenes][1:] == [
        "Show a failed restore",
        "Explain why",
        "Show the fix",
    ]


def test_a_long_plan_is_capped_at_eight_scenes():
    scenes = video.scenes_for(plan(on_screen_text=[f"Line {i}" for i in range(10)], beats=[]))
    assert len(scenes) == video.MAX_SCENES


def test_a_plan_without_a_hook_is_refused():
    with pytest.raises(ValidationFailedError):
        video.render(plan(hook="  "), PALETTE)


def test_render_makes_an_mp4_with_a_header_we_can_read(tmp_path):
    data = video.render(plan(duration_seconds=8, on_screen_text=["One"], beats=[], cta=""), PALETTE)
    target = tmp_path / "reel.mp4"
    target.write_bytes(data)
    from media import inspect

    facts = inspect.inspect_video(target)
    assert (facts.width, facts.height, facts.file_format) == (1080, 1920, "MP4")
    assert 4 <= facts.duration_seconds <= 8
