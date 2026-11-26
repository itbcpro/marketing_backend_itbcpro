import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from studio.config import ChannelPlan
from studio.slots import History, IdeaView, Slot, assign, open_slots, weekly_pattern
from studio.tests.conftest import make_config

MONDAY = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
TORONTO = ZoneInfo("America/Toronto")
SUPPORTED = {
    "instagram": ["image", "carousel", "video", "story"],
    "linkedin": ["text", "image", "carousel", "video"],
}


def idea(name: str, **fields) -> IdeaView:
    defaults = {"pillar": "education", "urgency": "evergreen", "score": 0.5, "expires_at": None}
    defaults.update(fields)
    return IdeaView(id=uuid.uuid5(uuid.NAMESPACE_DNS, name), **defaults)


def test_a_weekly_pattern_spreads_posts_over_the_allowed_days():
    plan = ChannelPlan(enabled=True, posts_per_week=3, days=[0, 1, 2, 3, 4], times=["10:00"])
    days = [d for d, _ in weekly_pattern(plan)]
    assert days == [0, 1, 3] or (len(days) == 3 and days == sorted(days))
    assert weekly_pattern(ChannelPlan(posts_per_week=0)) == []
    assert weekly_pattern(ChannelPlan(days=[], posts_per_week=3)) == []


def test_more_posts_than_slots_uses_every_slot_once():
    plan = ChannelPlan(enabled=True, posts_per_week=10, days=[0, 4], times=["09:00", "17:00"])
    assert len(weekly_pattern(plan)) == 4


def test_open_slots_follow_the_workspace_time_zone():
    config = make_config()
    slots = open_slots(config, TORONTO, MONDAY, 6, [])
    ig = [s for s in slots if s.channel == "instagram"]
    # Wednesday 10:00 in Toronto (UTC-4 in October) is 14:00 UTC; Monday's slot was 2 hours ahead.
    assert any(s.at == datetime(2026, 10, 14, 14, 0, tzinfo=UTC) for s in ig)
    assert all(s.at >= MONDAY + timedelta(hours=2) for s in slots)
    assert slots == sorted(slots, key=lambda s: (s.at, s.channel))


def test_taken_times_are_not_offered_again():
    config = make_config()
    taken = [("instagram", datetime(2026, 10, 14, 14, 30, tzinfo=UTC))]
    slots = open_slots(config, TORONTO, MONDAY, 6, taken)
    assert not any(
        s.channel == "instagram" and s.at == datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
        for s in slots
    )
    assert any(s.channel == "linkedin" for s in slots)


def test_disabled_channels_get_no_slots():
    config = make_config()
    config.channels["linkedin"].enabled = False
    assert {s.channel for s in open_slots(config, TORONTO, MONDAY, 13, [])} == {"instagram"}


def test_timely_ideas_take_the_earliest_slots():
    config = make_config()
    slots = [Slot("instagram", MONDAY + timedelta(days=d)) for d in (1, 5, 9)]
    ideas = [
        idea("evergreen-1", score=0.7),
        idea("evergreen-2", score=0.7),
        idea("timely", urgency="timely", score=0.6),
    ]
    made = assign(slots, ideas, config, SUPPORTED, History(), MONDAY)
    assert made[0].idea_id == ideas[2].id


def test_expired_ideas_are_never_scheduled_after_they_expire():
    config = make_config()
    slots = [Slot("instagram", MONDAY + timedelta(days=5))]
    stale = idea("stale", expires_at=MONDAY + timedelta(days=2), score=0.9)
    fresh = idea("fresh", score=0.2)
    made = assign(slots, [stale, fresh], config, SUPPORTED, History(), MONDAY)
    assert [m.idea_id for m in made] == [fresh.id]


def test_pillar_weights_shape_the_mix():
    config = make_config()
    slots = [Slot("instagram", MONDAY + timedelta(days=d)) for d in range(1, 7)]
    ideas = [idea(f"e{n}", pillar="education") for n in range(6)] + [
        idea(f"p{n}", pillar="proof") for n in range(6)
    ]
    made = assign(slots, ideas, config, SUPPORTED, History(), MONDAY)
    counts = Counter(m.pillar for m in made)
    assert counts["education"] == 4 and counts["proof"] == 2


def test_the_same_idea_can_reach_each_channel_once():
    config = make_config()
    slots = [
        Slot("instagram", MONDAY + timedelta(days=1)),
        Slot("linkedin", MONDAY + timedelta(days=2)),
    ]
    only = idea("only")
    made = assign(slots, [only], config, SUPPORTED, History(), MONDAY)
    assert [m.slot.channel for m in made] == ["instagram", "linkedin"]
    again = assign(slots, [only], config, SUPPORTED, History(used={(only.id, "instagram")}), MONDAY)
    assert [m.slot.channel for m in again] == ["linkedin"]


def test_post_types_follow_hints_then_rotate():
    config = make_config()
    slots = [Slot("instagram", MONDAY + timedelta(days=d)) for d in (1, 2, 3, 4)]
    hinted = idea("hinted", hints=(("instagram", "carousel"),))
    plain = [idea(f"plain{n}") for n in range(3)]
    made = assign(slots, [hinted, *plain], config, SUPPORTED, History(), MONDAY)
    kinds = [m.kind for m in made]
    assert "carousel" in kinds
    assert len(set(kinds)) >= 3  # not the same post type over and over


def test_owner_chosen_post_types_limit_the_choice():
    config = make_config()
    config.channels["instagram"].kinds = ["image"]
    slots = [Slot("instagram", MONDAY + timedelta(days=d)) for d in (1, 2)]
    made = assign(slots, [idea("a"), idea("b")], config, SUPPORTED, History(), MONDAY)
    assert {m.kind for m in made} == {"image"}


def test_slots_without_a_fitting_idea_stay_empty():
    config = make_config()
    slots = [Slot("instagram", MONDAY + timedelta(days=d)) for d in (1, 2, 3)]
    assert len(assign(slots, [idea("one")], config, SUPPORTED, History(), MONDAY)) == 1
