import json
from datetime import timedelta

import pytest
from django.utils import timezone

from conftest import make_user
from social.tests.conftest import headers
from studio import net, runs, writer
from studio.models import ContentIdea, ContentPiece, PieceStatus, RunKind, TrendItem, TrendSource
from studio.tests.conftest import PIECE, brand_of, feed_transport, ideas, llm
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

BASE = "/api/v1/studio"
FEED = b"""<rss version="2.0"><channel><item><title>Printers jam again</title>
<link>https://n.example.com/1</link></item></channel></rss>"""


def get(client, workspace, path, query=None):
    return client.get(f"{BASE}{path}", query or {}, **headers(workspace))


def send(client, workspace, path, body=None, method="post"):
    return getattr(client, method)(
        f"{BASE}{path}",
        data=json.dumps(body if body is not None else {}),
        content_type="application/json",
        **headers(workspace),
    )


def config_body(**channels):
    return {
        "goals": "More consultations",
        "pillars": [{"name": "education", "weight": 2}],
        "channels": channels or {"linkedin": {"enabled": True, "posts_per_week": 2}},
    }


# --- meta and settings --------------------------------------------------------------------------------


def test_meta_lists_channels_providers_and_connected_accounts(logged_in, workspace, account):
    body = get(logged_in, workspace, "/meta").json()
    channels = {c["key"]: c for c in body["channels"]}
    assert set(channels) == {"instagram", "facebook", "linkedin"}
    assert (
        channels["instagram"]["publisher"]
        and channels["instagram"]["accounts"][0]["handle"] == "@acme_it"
    )
    assert not channels["linkedin"]["publisher"]
    assert {k["kind"] for k in body["source_kinds"]} >= {"rss", "news_search", "own_searches"}
    assert body["picture_providers"] == ["library", "ai_image", "text_card"]
    assert body["video_providers"] == ["cards"]


def test_settings_round_trip_and_defaults(logged_in, workspace):
    assert get(logged_in, workspace, "/config").json()["channels"] == {}
    saved = send(logged_in, workspace, "/config", config_body(), method="put")
    assert saved.status_code == 200
    again = get(logged_in, workspace, "/config").json()
    assert again["channels"]["linkedin"]["posts_per_week"] == 2
    assert again["voice"]["language"] == "English"


def test_unknown_channels_and_post_types_are_refused(logged_in, workspace):
    bad_channel = send(
        logged_in, workspace, "/config", config_body(myspace={"enabled": True}), method="put"
    )
    assert bad_channel.status_code in (400, 422)
    bad_kind = send(
        logged_in,
        workspace,
        "/config",
        config_body(linkedin={"enabled": True, "kinds": ["story"]}),
        method="put",
    )
    assert bad_kind.status_code in (400, 422)


def test_turning_on_auto_needs_the_autonomy_right(client, workspace, owner):
    editor = make_user("ed@example.com", workspace, Role.EDITOR)
    client.force_login(editor)
    review = config_body(linkedin={"enabled": True, "publish_mode": "review"})
    auto = config_body(linkedin={"enabled": True, "publish_mode": "auto"})
    assert send(client, workspace, "/config", review, method="put").status_code == 200
    assert send(client, workspace, "/config", auto, method="put").status_code == 403
    client.force_login(owner)
    assert send(client, workspace, "/config", auto, method="put").status_code == 200


def test_the_panic_button_turns_everything_automatic_into_review(logged_in, workspace):
    body = config_body(linkedin={"enabled": True, "publish_mode": "auto"})
    body["automation"] = {"enabled": True}
    send(logged_in, workspace, "/config", body, method="put")
    out = send(logged_in, workspace, "/config/disable-automation").json()
    assert (
        out["automation"]["enabled"] is False
        and out["channels"]["linkedin"]["publish_mode"] == "review"
    )


def test_viewers_can_look_but_not_change(client, workspace):
    viewer = make_user("v@example.com", workspace, Role.VIEWER)
    client.force_login(viewer)
    assert get(client, workspace, "/config").status_code == 200
    assert get(client, workspace, "/overview").status_code == 200
    assert send(client, workspace, "/config", config_body(), method="put").status_code == 403
    assert (
        send(
            client, workspace, "/posts", {"channel": "linkedin", "kind": "text", "brief": "x"}
        ).status_code
        == 403
    )
    assert send(client, workspace, "/run").status_code == 403


def test_the_overview_counts_what_needs_attention(logged_in, workspace, owner, configured):
    piece = writer.create(brand_of(workspace), owner.pk, channel="linkedin", kind="text", brief="x")
    llm(PIECE)
    writer.run(piece.pk)
    body = get(logged_in, workspace, "/overview").json()
    assert body["configured"] and body["counts"]["draft"] == 1 and body["needs_you"] == 1


# --- sources, signals and ideas ----------------------------------------------------------------------


def test_sources_are_validated_and_managed(logged_in, workspace):
    bad = send(logged_in, workspace, "/sources", {"kind": "rss", "params": {"url": "ftp://x"}})
    assert bad.status_code in (400, 422)
    made = send(
        logged_in, workspace, "/sources", {"kind": "news_search", "params": {"query": "managed IT"}}
    ).json()
    assert made["name"] == "News: managed IT" and made["params"]["country"] == "US"
    patched = send(
        logged_in, workspace, f"/sources/{made['id']}", {"enabled": False}, method="patch"
    ).json()
    assert patched["enabled"] is False
    assert len(get(logged_in, workspace, "/sources").json()) == 1
    assert logged_in.delete(f"{BASE}/sources/{made['id']}", **headers(workspace)).status_code == 204
    assert get(logged_in, workspace, "/sources").json() == []


def test_suggested_sources_come_from_the_topics_and_are_not_added_twice(logged_in, workspace):
    body = config_body()
    body["trends"] = {"topics": ["managed IT", "cybersecurity"], "geo": "CA"}
    send(logged_in, workspace, "/config", body, method="put")
    first = send(logged_in, workspace, "/sources/suggest").json()
    assert {s["kind"] for s in first} == {"news_search", "trending_searches", "own_searches"}
    assert send(logged_in, workspace, "/sources/suggest").json() == []


def test_a_source_can_be_tested_on_demand(logged_in, workspace, monkeypatch):
    monkeypatch.setattr(net, "transport", feed_transport({"https://feeds.example.com/": FEED}))
    source = TrendSource.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        name="Blog",
        params={"url": "https://feeds.example.com/a.xml"},
    )
    out = send(logged_in, workspace, f"/sources/{source.pk}/refresh").json()
    assert out["last_count"] == 1 and out["last_error"] == ""
    trends = get(logged_in, workspace, "/trends").json()
    assert [t["title"] for t in trends] == ["Printers jam again"]
    hidden = send(
        logged_in, workspace, f"/trends/{trends[0]['id']}", {"ignored": True}, method="patch"
    ).json()
    assert hidden["ignored"] and get(logged_in, workspace, "/trends").json() == []


def test_a_broken_source_reports_its_error_instead_of_failing_the_request(
    logged_in, workspace, monkeypatch
):
    monkeypatch.setattr(net, "transport", feed_transport({"https://down.example.com/": 500}))
    source = TrendSource.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        name="Down",
        params={"url": "https://down.example.com/a.xml"},
    )
    out = send(logged_in, workspace, f"/sources/{source.pk}/refresh")
    assert out.status_code == 200 and out.json()["last_error"]


def test_ideas_are_requested_then_reviewed_and_turned_into_posts(logged_in, workspace, configured):
    llm(ideas(2))
    queued = send(
        logged_in, workspace, "/ideas/generate", {"count": 2, "instruction": "security"}
    ).json()
    assert queued["status"] == "running"
    runs.execute(queued["id"])
    assert get(logged_in, workspace, f"/runs/{queued['id']}").json()["stats"] == {"ideas_made": 2}
    listed = get(logged_in, workspace, "/ideas").json()
    assert len(listed) == 2 and listed[0]["pieces"] == 0
    idea_id = listed[0]["id"]
    accepted = send(
        logged_in, workspace, f"/ideas/{idea_id}", {"status": "accepted"}, method="patch"
    ).json()
    assert accepted["status"] == "accepted"
    pieces = send(
        logged_in,
        workspace,
        f"/ideas/{idea_id}/pieces",
        {
            "targets": [
                {"channel": "instagram", "kind": "carousel"},
                {"channel": "linkedin", "kind": "text"},
            ]
        },
    ).json()
    assert [p["channel"] for p in pieces] == ["instagram", "linkedin"]
    assert (
        next(i for i in get(logged_in, workspace, "/ideas").json() if i["id"] == idea_id)["pieces"]
        == 2
    )
    rejected = send(
        logged_in, workspace, f"/ideas/{idea_id}", {"status": "rejected"}, method="patch"
    )
    assert rejected.status_code == 200
    assert idea_id not in [i["id"] for i in get(logged_in, workspace, "/ideas").json()]


def test_post_types_are_checked_when_an_idea_becomes_posts(logged_in, workspace, configured):
    idea = ContentIdea.objects.create(
        workspace_id=workspace.pk, brand=brand_of(workspace), title="x"
    )
    bad = send(
        logged_in,
        workspace,
        f"/ideas/{idea.pk}/pieces",
        {"targets": [{"channel": "linkedin", "kind": "story"}]},
    )
    assert bad.status_code in (400, 422)
    assert send(logged_in, workspace, f"/ideas/{idea.pk}/pieces", {"targets": []}).status_code in (
        400,
        422,
    )


# --- posts --------------------------------------------------------------------------------------------


def test_a_post_goes_from_brief_to_posted(logged_in, workspace, configured):
    created = send(
        logged_in,
        workspace,
        "/posts",
        {"channel": "linkedin", "kind": "text", "brief": "Why backups fail"},
    ).json()
    assert created["status"] == "drafting" and created["ai_status"] == "thinking"
    llm(PIECE)
    writer.run(created["id"])
    post = get(logged_in, workspace, f"/posts/{created['id']}").json()
    assert post["status"] == "draft" and post["content"]["body"].startswith("Most small offices")

    edited = send(
        logged_in,
        workspace,
        f"/posts/{post['id']}",
        {"body": "Shorter body that still has enough words."},
        method="patch",
    ).json()
    assert edited["content"]["body"].startswith("Shorter")

    when = (timezone.now() + timedelta(hours=5)).isoformat()
    approved = send(
        logged_in, workspace, f"/posts/{post['id']}/approve", {"scheduled_at": when}
    ).json()
    assert approved["status"] == "scheduled" and approved["manual"] is True

    kit = get(logged_in, workspace, f"/posts/{post['id']}/kit").json()
    assert kit["text"].startswith("Shorter body") and "#itsupport" in kit["text"]

    posted = send(
        logged_in,
        workspace,
        f"/posts/{post['id']}/mark-posted",
        {"url": "https://linkedin.com/p/1"},
    ).json()
    assert posted["status"] == "published" and posted["permalink"].endswith("/p/1")


def test_the_calendar_filters_by_range_channel_and_backlog(logged_in, workspace, owner):
    now = timezone.now()
    for channel, kind, delta in (
        ("linkedin", "text", 1),
        ("instagram", "image", 2),
        ("linkedin", "text", None),
    ):
        ContentPiece.objects.create(
            workspace_id=workspace.pk,
            brand=brand_of(workspace),
            channel=channel,
            kind=kind,
            status=PieceStatus.DRAFT,
            scheduled_at=now + timedelta(days=delta) if delta else None,
        )
    window = {"start": now.isoformat(), "end": (now + timedelta(days=3)).isoformat()}
    assert len(get(logged_in, workspace, "/posts", window).json()) == 2
    assert len(get(logged_in, workspace, "/posts", {**window, "channel": "instagram"}).json()) == 1
    assert len(get(logged_in, workspace, "/posts", {"undated": True}).json()) == 1
    too_wide = {"start": now.isoformat(), "end": (now + timedelta(days=400)).isoformat()}
    assert get(logged_in, workspace, "/posts", too_wide).status_code in (400, 422)


def test_a_post_in_the_past_cannot_be_scheduled(logged_in, workspace, owner, configured):
    piece = ContentPiece.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        channel="linkedin",
        kind="text",
        status=PieceStatus.DRAFT,
        copy={"body": "A body with enough characters to pass."},
    )
    past = (timezone.now() - timedelta(days=1)).isoformat()
    assert send(
        logged_in, workspace, f"/posts/{piece.pk}/approve", {"scheduled_at": past}
    ).status_code in (400, 422)


def test_revising_and_cancelling(logged_in, workspace, owner, configured):
    created = send(
        logged_in, workspace, "/posts", {"channel": "linkedin", "kind": "text", "brief": "x"}
    ).json()
    llm(PIECE)
    writer.run(created["id"])
    revised = send(
        logged_in, workspace, f"/posts/{created['id']}/revise", {"feedback": "Shorter"}
    ).json()
    assert revised["status"] == "drafting"
    assert send(
        logged_in, workspace, f"/posts/{created['id']}/revise", {"feedback": ""}
    ).status_code in (400, 422)
    cancelled = send(logged_in, workspace, f"/posts/{created['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"


def test_workspaces_cannot_see_each_others_posts(client, workspace, owner):
    piece = ContentPiece.objects.create(
        workspace_id=workspace.pk, brand=brand_of(workspace), channel="linkedin", kind="text"
    )
    stranger = make_user("s@example.com")
    other = create_workspace(name="Other Co", owner=stranger)
    client.force_login(stranger)
    assert get(client, other, f"/posts/{piece.pk}").status_code == 404
    assert get(client, other, "/posts").json() == []
    assert send(client, other, f"/posts/{piece.pk}/cancel").status_code == 404


def test_manual_runs_are_queued_and_listed(logged_in, workspace, configured):
    run = send(logged_in, workspace, "/run").json()
    assert run["kind"] == RunKind.MANUAL and run["status"] == "running"
    assert [r["id"] for r in get(logged_in, workspace, "/runs").json()] == [run["id"]]


def test_signals_older_than_the_window_are_not_listed(logged_in, workspace):
    old = TrendItem.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        title="Old",
        fingerprint="a" * 40,
    )
    TrendItem.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=30))
    TrendItem.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        title="New",
        fingerprint="b" * 40,
    )
    assert [t["title"] for t in get(logged_in, workspace, "/trends", {"days": 7}).json()] == ["New"]
