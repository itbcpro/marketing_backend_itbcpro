import json
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from conftest import make_user
from core.errors import NotFoundError, ValidationFailedError
from media.models import MediaAsset
from social.models import PostDraft
from social.tests.conftest import headers
from studio import brands, config_store, design, planner, runs, services, writer
from studio.config import VisualConfig
from studio.models import ContentIdea, ContentPiece, PieceStatus, TrendItem, TrendSource
from studio.publishers.ports import account_for
from studio.tests.conftest import PIECE, ideas, llm, make_config
from studio.tests.test_writer import upload
from studio.visuals import director, logo
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

BASE = "/api/v1/studio"
MONDAY = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


@pytest.fixture
def two(workspace, owner):
    """Two companies in one workspace, each with its own strategy."""
    first = brands.create(workspace.pk, "ITBCPro")
    second = brands.create(workspace.pk, "ShiftVale")
    config_store.save(first, make_config(goals="Managed IT for offices"), owner)
    config_store.save(second, make_config(goals="Seasonal pop-up events"), owner)
    return first, second


def get(client, workspace, path, brand=None, query=None):
    extra = headers(workspace)
    if brand is not None:
        extra["HTTP_X_STUDIO_BRAND"] = str(brand.pk)
    return client.get(f"{BASE}{path}", query or {}, **extra)


def send(client, workspace, path, body=None, brand=None, method="post"):
    extra = headers(workspace)
    if brand is not None:
        extra["HTTP_X_STUDIO_BRAND"] = str(brand.pk)
    return getattr(client, method)(
        f"{BASE}{path}",
        data=json.dumps(body if body is not None else {}),
        content_type="application/json",
        **extra,
    )


# --- the companies themselves -----------------------------------------------------------------


def test_a_workspace_starts_with_one_company_named_after_itself(workspace):
    (only,) = brands.listing(workspace.pk)
    assert only.name == workspace.name
    assert brands.listing(workspace.pk) == [only]  # not created twice


def test_companies_can_be_added_renamed_hidden_and_brought_back(workspace):
    brands.listing(workspace.pk)
    made = brands.create(workspace.pk, "  ITBC   Software ")
    assert made.name == "ITBC Software" and made.slug == "itbc-software"
    assert brands.create(workspace.pk, "ITBC Software").slug == "itbc-software-2"
    brands.rename(made, "ITBCSoftware")
    brands.archive(made)
    assert made.pk not in [b.pk for b in brands.listing(workspace.pk)]
    brands.restore(made)
    assert made.pk in [b.pk for b in brands.listing(workspace.pk)]
    with pytest.raises(ValidationFailedError):
        brands.create(workspace.pk, "   ")


def test_a_request_must_name_the_company_when_there_are_several(workspace, two):
    first, _ = two
    with pytest.raises(ValidationFailedError):
        brands.resolve(workspace.pk, "")
    assert brands.resolve(workspace.pk, str(first.pk)) == first
    with pytest.raises(ValidationFailedError):
        brands.resolve(workspace.pk, "not-an-id")


def test_a_company_of_another_workspace_cannot_be_used(workspace, two):
    other = create_workspace(name="Other Co", owner=make_user("o@example.com"))
    stranger = brands.create(other.pk, "Stranger")
    with pytest.raises(NotFoundError):
        brands.get(workspace.pk, stranger.pk)


# --- everything is kept apart ------------------------------------------------------------------


def test_each_company_has_its_own_strategy(workspace, two):
    first, second = two
    assert config_store.load(first.pk).goals == "Managed IT for offices"
    assert config_store.load(second.pk).goals == "Seasonal pop-up events"
    assert config_store.load(brands.create(workspace.pk, "New").pk).goals == ""


def test_each_company_has_its_own_ideas_signals_and_sources(workspace, two):
    first, second = two
    for brand, title in ((first, "IT idea"), (second, "Event idea")):
        ContentIdea.objects.create(workspace_id=workspace.pk, brand=brand, title=title)
        TrendSource.objects.create(
            workspace_id=workspace.pk, brand=brand, kind="rss", name=title, params={}
        )
        TrendItem.objects.create(
            workspace_id=workspace.pk, brand=brand, kind="rss", title=title, fingerprint=title
        )
    from studio import strategist
    from studio.trends import collect

    assert [i.title for i in strategist.usable_ideas(first.pk)] == ["IT idea"]
    assert [i.title for i in collect.recent_items(second.pk, 7, 10)] == ["Event idea"]


def test_the_same_headline_can_be_a_signal_for_both_companies(workspace, two):
    for brand in two:
        TrendItem.objects.create(
            workspace_id=workspace.pk,
            brand=brand,
            kind="rss",
            title="Shared news",
            fingerprint="same",
        )
    assert TrendItem.objects.filter(fingerprint="same").count() == 2


def test_the_calendar_of_one_company_does_not_fill_the_slots_of_another(workspace, two):
    first, second = two
    llm(ideas(5), ideas(5))
    a = planner.fill_calendar(first, now=MONDAY, horizon_days=6)
    b = planner.fill_calendar(second, now=MONDAY, horizon_days=6)
    assert a.created == b.created == 5  # the second company still gets its full week
    assert ContentPiece.objects.filter(brand=first).count() == 5
    assert ContentPiece.objects.filter(brand=second).count() == 5


def test_design_families_are_rotated_per_company(workspace, two):
    first, second = two
    used = [
        writer.create(first, None, channel="instagram", kind="image", brief="x").ai_state[
            design.KEY
        ]
        for _ in range(3)
    ]
    # the second company has used nothing yet, so any family is open to it
    assert design.pick(second.pk, "instagram", workspace.pk).name in {
        f.name for f in design.families()
    }
    assert len(set(used)) == 3


def test_the_daily_routine_is_scheduled_per_company(workspace, owner, two):
    first, _ = two
    config_store.save(first, make_config(automation={"enabled": True, "run_hour": 6}), owner)
    assert runs.due_brands(MONDAY) == [first.pk]
    run = runs.start(first, "daily", now=MONDAY)
    from studio.models import RunStatus, StudioRun

    StudioRun.objects.filter(pk=run.pk).update(status=RunStatus.DONE)
    assert (
        runs.due_brands(MONDAY + timedelta(hours=3)) == []
    )  # done for the first, off for the second


def test_a_piece_is_found_only_through_its_own_company(workspace, owner, two):
    first, second = two
    piece = writer.create(first, owner.pk, channel="linkedin", kind="text", brief="x")
    assert services.get(first.pk, piece.pk).pk == piece.pk
    with pytest.raises(NotFoundError):
        services.get(second.pk, piece.pk)


def test_each_company_uses_its_own_prompt_name_and_strategy(workspace, owner, two):
    _, second = two
    piece = writer.create(second, owner.pk, channel="linkedin", kind="text", brief="x")
    gateway = llm(PIECE)
    assert writer.run(piece.pk) == "done"
    system = gateway.requests[0][0].system[0].text
    assert "ShiftVale" in system and "Seasonal pop-up events" in system
    assert "Managed IT for offices" not in system
    assert "runs several companies" in system  # the workspace-wide profile is not used


# --- publishing never crosses companies ---------------------------------------------------------


def test_with_several_companies_an_account_is_never_guessed(workspace, owner, account, two):
    assert account_for(workspace.pk, "instagram", "") is None
    assert account_for(workspace.pk, "instagram", "someone-else") is None
    assert account_for(workspace.pk, "instagram", str(account.pk)).id == str(account.pk)


def test_a_lone_company_still_uses_the_only_account(workspace, account):
    brands.listing(workspace.pk)
    assert account_for(workspace.pk, "instagram", "").id == str(account.pk)


def test_a_post_waits_for_hand_posting_until_its_company_has_an_account(
    workspace, owner, account, two
):
    first, _ = two
    config_store.save(first, make_config(channels={"instagram": {"enabled": True}}), owner)
    piece = writer.create(first, owner.pk, channel="instagram", kind="image", brief="x")
    llm(PIECE)
    writer.run(piece.pk)
    piece.refresh_from_db()
    services.approve(piece, owner, timezone.now() + timedelta(hours=3))
    piece.refresh_from_db()
    assert piece.external == {"manual": True} and not PostDraft.objects.exists()

    config_store.save(
        first,
        make_config(channels={"instagram": {"enabled": True, "account_id": str(account.pk)}}),
        owner,
    )
    other = writer.create(first, owner.pk, channel="instagram", kind="image", brief="y")
    llm(PIECE)
    writer.run(other.pk)
    other.refresh_from_db()
    services.approve(other, owner, timezone.now() + timedelta(hours=3))
    other.refresh_from_db()
    assert other.status == PieceStatus.SCHEDULED and "draft_id" in other.external


# --- each company's own look ---------------------------------------------------------------------


def test_each_company_finds_its_own_logo(workspace, owner, two):
    first, second = two
    itbc = upload(workspace, owner, "ITBCPro logo", ["logo"])
    shift = upload(workspace, owner, "ShiftVale mark", ["logo"], (10, 20, 30))
    unrelated = upload(workspace, owner, "Team photo", [], (40, 50, 60))
    style = VisualConfig()
    assert logo.find(workspace.pk, style, first.pk).pk == itbc.pk
    assert logo.find(workspace.pk, style, second.pk).pk == shift.pk
    assert unrelated.pk not in {logo.find(workspace.pk, style, b.pk).pk for b in two}


def test_a_company_without_its_own_logo_gets_none_when_there_are_several(workspace, owner, two):
    _, second = two
    upload(workspace, owner, "ITBCPro logo", ["logo"])
    assert logo.find(workspace.pk, VisualConfig(), second.pk) is None  # never another company's


def test_a_lone_company_may_use_any_logo(workspace, owner):
    only = brands.listing(workspace.pk)[0]
    mark = upload(workspace, owner, "our mark", ["logo"])
    assert logo.find(workspace.pk, VisualConfig(), only.pk).pk == mark.pk


def test_a_logo_can_be_chosen_explicitly(workspace, owner, two):
    first, _ = two
    picked = upload(workspace, owner, "anything", [], (70, 80, 90))
    assert (
        logo.find(workspace.pk, VisualConfig(logo_asset_id=str(picked.pk)), first.pk).pk
        == picked.pk
    )
    assert isinstance(picked, MediaAsset)


def test_each_company_has_its_own_colours(workspace, owner, two):
    _, second = two
    config_store.save(second, make_config(visuals={"brand_colours": ["#112233", "#ff8800"]}), owner)
    piece = ContentPiece.objects.create(
        workspace_id=workspace.pk, brand=second, channel="instagram", kind="image"
    )
    palette = director._palette(piece, config_store.load(second.pk).visuals)
    assert (palette.background, palette.accent) == ("#112233", "#ff8800")
    assert palette.text in {"#FFFFFF", "#000000"}


# --- the API ---------------------------------------------------------------------------------------


def test_the_company_list_and_management_endpoints(logged_in, workspace):
    listed = get(logged_in, workspace, "/brands").json()
    assert [b["name"] for b in listed] == [workspace.name]
    made = send(logged_in, workspace, "/brands", {"name": "ITBCSoftware"}).json()
    assert made["slug"] == "itbcsoftware"
    renamed = send(
        logged_in, workspace, f"/brands/{made['id']}", {"name": "ITBC Software"}, method="patch"
    )
    assert renamed.json()["name"] == "ITBC Software"
    hidden = send(
        logged_in, workspace, f"/brands/{made['id']}", {"archived": True}, method="patch"
    ).json()
    assert hidden["archived_at"]
    assert [b["name"] for b in get(logged_in, workspace, "/brands").json()] == [workspace.name]


def test_viewers_can_list_companies_but_not_change_them(client, workspace):
    viewer = make_user("v@example.com", workspace, Role.VIEWER)
    client.force_login(viewer)
    assert get(client, workspace, "/brands").status_code == 200
    assert send(client, workspace, "/brands", {"name": "X"}).status_code == 403


def test_the_api_needs_to_know_which_company_when_there_are_several(logged_in, workspace, two):
    assert get(logged_in, workspace, "/config").status_code in (400, 422)
    assert get(logged_in, workspace, "/overview").status_code in (400, 422)
    first, second = two
    assert get(logged_in, workspace, "/config", first).json()["goals"] == "Managed IT for offices"
    assert get(logged_in, workspace, "/config", second).json()["goals"] == "Seasonal pop-up events"


def test_saving_a_strategy_changes_only_the_chosen_company(logged_in, workspace, two):
    first, second = two
    body = {"goals": "New goal for the first", "channels": {"linkedin": {"enabled": True}}}
    assert send(logged_in, workspace, "/config", body, first, method="put").status_code == 200
    assert get(logged_in, workspace, "/config", first).json()["goals"] == "New goal for the first"
    assert get(logged_in, workspace, "/config", second).json()["goals"] == "Seasonal pop-up events"


def test_posts_ideas_and_sources_are_listed_per_company(logged_in, workspace, owner, two):
    first, second = two
    for brand, title in ((first, "one"), (second, "two")):
        ContentPiece.objects.create(
            workspace_id=workspace.pk,
            brand=brand,
            channel="linkedin",
            kind="text",
            title=title,
            status=PieceStatus.DRAFT,
        )
        ContentIdea.objects.create(workspace_id=workspace.pk, brand=brand, title=title)
    assert [p["title"] for p in get(logged_in, workspace, "/posts", first).json()] == ["one"]
    assert [p["title"] for p in get(logged_in, workspace, "/posts", second).json()] == ["two"]
    assert [i["title"] for i in get(logged_in, workspace, "/ideas", second).json()] == ["two"]
    own = send(
        logged_in,
        workspace,
        "/sources",
        {"kind": "news_search", "params": {"query": "events"}},
        second,
    ).json()
    assert [s["id"] for s in get(logged_in, workspace, "/sources", second).json()] == [own["id"]]
    assert get(logged_in, workspace, "/sources", first).json() == []


def test_a_post_of_another_company_cannot_be_opened_or_changed(logged_in, workspace, two):
    first, second = two
    piece = ContentPiece.objects.create(
        workspace_id=workspace.pk,
        brand=first,
        channel="linkedin",
        kind="text",
        status=PieceStatus.DRAFT,
    )
    assert get(logged_in, workspace, f"/posts/{piece.pk}", first).status_code == 200
    assert get(logged_in, workspace, f"/posts/{piece.pk}", second).status_code == 404
    assert send(logged_in, workspace, f"/posts/{piece.pk}/cancel", None, second).status_code == 404


def test_the_overview_and_runs_are_per_company(logged_in, workspace, two):
    first, second = two
    ContentPiece.objects.create(
        workspace_id=workspace.pk,
        brand=first,
        channel="linkedin",
        kind="text",
        status=PieceStatus.DRAFT,
    )
    assert get(logged_in, workspace, "/overview", first).json()["counts"] == {"draft": 1}
    assert get(logged_in, workspace, "/overview", second).json()["counts"] == {}
    run = send(logged_in, workspace, "/run", None, second).json()
    assert [r["id"] for r in get(logged_in, workspace, "/runs", second).json()] == [run["id"]]
    assert get(logged_in, workspace, "/runs", first).json() == []


def test_a_company_of_another_workspace_is_refused_in_the_header(client, workspace, owner, two):
    stranger = make_user("s@example.com")
    other = create_workspace(name="Other Co", owner=stranger)
    foreign = brands.create(other.pk, "Foreign")
    client.force_login(owner)
    assert get(client, workspace, "/config", foreign).status_code == 404
