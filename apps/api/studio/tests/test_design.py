import uuid

import pytest

from studio import design, writer
from studio.tests.conftest import PIECE, brand_of, llm

pytestmark = pytest.mark.django_db


def test_the_family_list_is_clean_and_has_room_to_vary():
    names = [f.name for f in design.families()]
    assert len(names) >= 10 and len(set(names)) == len(names)
    assert all(f.brief.strip() for f in design.families())


def new(workspace, owner, channel="instagram"):
    return writer.create(brand_of(workspace), owner.pk, channel=channel, kind="image", brief="x")


def test_consecutive_posts_of_a_channel_get_different_design_families(workspace, owner, configured):
    names = [new(workspace, owner).ai_state[design.KEY] for _ in range(len(design.families()) * 2)]
    for start in range(len(names) - 8):
        window = names[start : start + 8]
        assert len(set(window)) == 8  # no family twice within eight posts


def test_after_every_family_was_used_a_long_unused_one_comes_back(workspace, owner, configured):
    first = [new(workspace, owner).ai_state[design.KEY] for _ in range(len(design.families()))]
    again = new(workspace, owner).ai_state[design.KEY]
    assert again not in first[-9:]  # never one of the nine most recent


def test_channels_have_their_own_history(workspace, owner, configured):
    a = new(workspace, owner, "instagram").ai_state[design.KEY]
    pick = design.pick(brand_of(workspace).pk, "linkedin", uuid.uuid4())
    assert pick.name in {f.name for f in design.families()} and a


def test_the_writer_is_handed_the_family_and_keeps_it_through_revisions(
    workspace, owner, configured
):
    piece = new(workspace, owner)
    family = design.find(piece.ai_state[design.KEY])
    gateway = llm(PIECE, PIECE)
    assert writer.run(piece.pk) == "done"
    system = gateway.requests[0][0].system[0].text
    assert family.name in system and family.brief in system
    piece.refresh_from_db()
    writer.revise(piece, "shorter")
    piece.refresh_from_db()
    assert piece.ai_state[design.KEY] == family.name


def test_an_unknown_family_name_does_not_break_the_prompt():
    assert "own judgement" in design.describe("something that was removed from the list")
