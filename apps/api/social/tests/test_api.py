import hashlib
import hmac
import io
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone
from PIL import Image

from actions.models import ProposedAction
from conftest import make_user
from media import services as media_services
from social import public_media
from social.models import (
    ConversationStatus,
    PostDraft,
    SocialComment,
    SocialConversation,
    SocialMessage,
    WebhookEvent,
)
from social.tests.conftest import headers, llm, make_comment, make_media
from social.tests.fake_instagram import ACCOUNT_ID
from workspaces.models import Role

pytestmark = pytest.mark.django_db

BASE = "/api/v1/social/instagram"


def get(client, workspace, path, query=None):
    return client.get(f"{BASE}{path}", query or {}, **headers(workspace))


def post(client, workspace, path, body=None, method="post"):
    return getattr(client, method)(
        f"{BASE}{path}",
        data=json.dumps(body or {}),
        content_type="application/json",
        **headers(workspace),
    )


def test_setup_status_lists_what_is_missing(logged_in, workspace, settings):
    settings.INSTAGRAM_APP_ID = settings.INSTAGRAM_APP_SECRET = ""
    settings.PUBLIC_BASE_URL = "http://localhost:3000"
    body = get(logged_in, workspace, "/status").json()
    assert body["configured"] is False and body["missing"] == ["app_id", "app_secret"]
    assert (
        body["public_url_ready"] is False and "instagram_business_content_publish" in body["scopes"]
    )
    assert body["webhook_url"].endswith("/bff/api/v1/social/instagram/webhook")
    settings.INSTAGRAM_APP_ID, settings.INSTAGRAM_APP_SECRET = "id", "secret"
    assert get(logged_in, workspace, "/status").json()["configured"] is True


def test_the_accounts_list_includes_connections_that_have_not_synced_yet(
    logged_in, workspace, connection
):
    row = get(logged_in, workspace, "/accounts").json()[0]
    assert (
        row["id"] is None
        and row["connection_id"] == str(connection.pk)
        and row["missing_scopes"] == []
    )


def test_sync_now_fills_the_account(logged_in, workspace, connection, fake):
    fake.add_media("m1")
    body = post(logged_in, workspace, f"/connections/{connection.pk}/sync").json()
    assert body["id"] and body["username"] == "acme_it" and body["counts"]["media"] == 1
    assert get(logged_in, workspace, "/accounts").json()[0]["followers_count"] == 1200


def test_posts_can_be_listed_filtered_and_sorted_by_any_metric(logged_in, workspace, account):
    make_media(
        account, "a", kind="reel", metrics={"reach": 100, "engagement_rate": 0.1}, caption="Backups"
    )
    make_media(
        account, "b", kind="reel", metrics={"reach": 900, "engagement_rate": 0.05}, caption="Phones"
    )
    make_media(account, "c", kind="image", metrics={}, caption="Phones too")
    base = f"/accounts/{account.pk}/media"
    rows = get(logged_in, workspace, base, {"kind": "reel", "sort": "reach"}).json()
    assert [m["external_id"] for m in rows["items"]] == ["b", "a"] and rows["total"] == 2
    assert [
        m["external_id"]
        for m in get(logged_in, workspace, base, {"sort": "engagement_rate"}).json()["items"]
    ][:2] == ["a", "b"]
    assert get(logged_in, workspace, base, {"q": "phones"}).json()["total"] == 2


def test_post_detail_returns_snapshots_and_baseline(logged_in, workspace, account):
    media = make_media(account, "a", kind="reel", metrics={"reach": 100})
    body = get(logged_in, workspace, f"/media/{media.pk}").json()
    assert body["media"]["external_id"] == "a" and "same_format" in body["baseline"]


def test_other_workspaces_cannot_see_an_account(logged_in, workspace, account, owner):
    from workspaces.services import create_workspace

    other = create_workspace(name="Other", owner=owner)
    assert get(logged_in, other, f"/accounts/{account.pk}").status_code == 404


def test_account_stats_and_reports_endpoints(logged_in, workspace, account):
    make_media(
        account,
        "a",
        kind="reel",
        metrics={"reach": 100, "engagement_rate": 0.1},
        metrics_synced_at=timezone.now(),
    )
    assert get(logged_in, workspace, f"/accounts/{account.pk}/stats").json()["overall"]["n"] == 1
    started = post(
        logged_in, workspace, f"/accounts/{account.pk}/reports", {"scope": "account"}
    ).json()
    assert started["status"] == "thinking"
    assert get(logged_in, workspace, f"/reports/{started['id']}").json()["id"] == started["id"]
    assert len(get(logged_in, workspace, f"/accounts/{account.pk}/reports").json()) == 1


# --- settings and permissions --------------------------------------------------------------------


def test_settings_round_trip(logged_in, workspace, account):
    body = {
        "comments": {"rules": {"question": "draft"}, "tone": "Warm"},
        "messages": {"enabled": True, "mode": "draft"},
    }
    saved = post(
        logged_in, workspace, f"/accounts/{account.pk}/settings", body, method="put"
    ).json()
    assert saved["comments"]["tone"] == "Warm" and saved["messages"]["enabled"] is True
    assert (
        get(logged_in, workspace, f"/accounts/{account.pk}/settings").json()["comments"]["tone"]
        == "Warm"
    )


def test_allowing_auto_needs_the_autonomy_right(client, workspace, account, owner):
    editor = make_user("ed@example.com", workspace, Role.EDITOR)
    client.force_login(editor)
    path = f"/accounts/{account.pk}/settings"
    assert (
        post(
            client, workspace, path, {"comments": {"rules": {"question": "draft"}}}, method="put"
        ).status_code
        == 200
    )
    assert (
        post(
            client, workspace, path, {"comments": {"rules": {"question": "auto"}}}, method="put"
        ).status_code
        == 403
    )
    client.force_login(owner)
    assert (
        post(
            client, workspace, path, {"comments": {"rules": {"question": "auto"}}}, method="put"
        ).status_code
        == 200
    )


def test_viewers_cannot_change_anything(client, workspace, account):
    viewer = make_user("v@example.com", workspace, Role.VIEWER)
    client.force_login(viewer)
    assert get(client, workspace, f"/accounts/{account.pk}/settings").status_code == 200
    assert (
        post(client, workspace, f"/accounts/{account.pk}/settings", {}, method="put").status_code
        == 403
    )
    assert (
        post(client, workspace, f"/accounts/{account.pk}/drafts", {"brief": "x"}).status_code == 403
    )


def test_disable_automation_is_a_panic_button(logged_in, workspace, account):
    post(
        logged_in,
        workspace,
        f"/accounts/{account.pk}/settings",
        {"comments": {"rules": {"question": "auto"}}},
        method="put",
    )
    body = post(logged_in, workspace, f"/accounts/{account.pk}/settings/disable-automation").json()
    assert body["comments"]["rules"]["question"] == "draft"


# --- inbox -------------------------------------------------------------------------------------------


def test_comments_are_listed_open_first_and_can_be_answered(logged_in, workspace, account):
    media = make_media(account, "m1")
    comment = make_comment(
        account, "c1", "How much?", media=media, status="draft_ready", draft="Send us a DM!"
    )
    make_comment(account, "c2", "Thanks", media=media, status="replied")
    listing = get(
        logged_in, workspace, f"/accounts/{account.pk}/comments", {"status": "open"}
    ).json()
    assert (
        listing["total"] == 1
        and listing["open_count"] == 1
        and listing["items"][0]["draft"] == "Send us a DM!"
    )
    answered = post(
        logged_in, workspace, f"/comments/{comment.pk}/reply", {"text": "Send us a DM!"}
    ).json()
    assert answered["applied"] is True
    action = ProposedAction.objects.get(pk=answered["action_id"])
    assert action.kind == "social.instagram.reply_comment" and action.status == "approved"
    assert action.payload["comment_id"] == "c1"


def test_a_reply_needs_text_and_cannot_be_sent_twice(logged_in, workspace, account):
    comment = make_comment(account, "c1", "Hi")
    assert (
        post(logged_in, workspace, f"/comments/{comment.pk}/reply", {"text": "  "}).status_code
        == 422
    )
    SocialComment.objects.filter(pk=comment.pk).update(status="replied")
    assert (
        post(logged_in, workspace, f"/comments/{comment.pk}/reply", {"text": "Hi"}).status_code
        == 409
    )


def test_hide_delete_and_ignore(logged_in, workspace, account):
    comment = make_comment(account, "c1", "Spam")
    hide = post(logged_in, workspace, f"/comments/{comment.pk}/hide", {"hidden": True}).json()
    assert ProposedAction.objects.get(pk=hide["action_id"]).kind.endswith("hide_comment")
    deleted = post(logged_in, workspace, f"/comments/{comment.pk}/delete").json()
    assert ProposedAction.objects.get(pk=deleted["action_id"]).risk == "r3"
    assert (
        post(logged_in, workspace, f"/comments/{comment.pk}/ignore").json()["status"] == "ignored"
    )


def test_an_editor_cannot_apply_irreversible_actions_but_can_queue_them(client, workspace, account):
    editor = make_user("ed2@example.com", workspace, Role.EDITOR)
    client.force_login(editor)
    comment = make_comment(account, "c1", "Spam")
    result = post(client, workspace, f"/comments/{comment.pk}/delete").json()
    assert result["applied"] is False and result["status"] == "awaiting_approval"


def test_triage_endpoint_runs_the_ai(logged_in, workspace, account):
    make_comment(account, "c1", "How much?", media=make_media(account, "m1"))
    llm(
        {
            "items": [
                {
                    "index": 0,
                    "category": "question",
                    "sentiment": "neutral",
                    "language": "English",
                    "priority": 1,
                    "confidence": 0.9,
                    "action": "reply",
                    "reason": "price",
                    "reply": "Send us a DM.",
                }
            ]
        }
    )
    assert post(logged_in, workspace, f"/accounts/{account.pk}/comments/triage").json() == {
        "triaged": 1,
        "sent": 0,
    }


def conversation(account, hours_ago=1, external_id="t1"):
    when = timezone.now() - timedelta(hours=hours_ago)
    convo = SocialConversation.objects.create(
        workspace_id=account.workspace_id,
        account=account,
        external_id=external_id,
        participant_id="u1",
        participant_username="alice",
        last_message_at=when,
        last_inbound_at=when,
        needs_reply=True,
        unread=True,
    )
    SocialMessage.objects.create(
        workspace_id=account.workspace_id,
        conversation=convo,
        external_id=f"m-{external_id}",
        inbound=True,
        text="Hello",
        sent_at=when,
    )
    return convo


def test_conversations_list_detail_reply_and_handoff(logged_in, workspace, account):
    convo = conversation(account)
    listing = get(
        logged_in, workspace, f"/accounts/{account.pk}/conversations", {"status": "waiting"}
    ).json()
    assert listing["waiting"] == 1 and listing["items"][0]["reply_window_open"] is True
    detail = get(logged_in, workspace, f"/conversations/{convo.pk}").json()
    assert [m["text"] for m in detail["messages"]] == ["Hello"]
    convo.refresh_from_db()
    assert convo.unread is False
    sent = post(
        logged_in, workspace, f"/conversations/{convo.pk}/reply", {"text": "Hi Alice"}
    ).json()
    assert ProposedAction.objects.get(pk=sent["action_id"]).payload["recipient_id"] == "u1"
    assert (
        post(logged_in, workspace, f"/conversations/{convo.pk}/take-over").json()["status"]
        == "handoff"
    )
    assert (
        post(logged_in, workspace, f"/conversations/{convo.pk}/hand-back").json()["bot_paused"]
        is False
    )
    assert (
        post(logged_in, workspace, f"/conversations/{convo.pk}/close").json()["status"] == "closed"
    )


def test_the_24_hour_window_and_opt_out_block_manual_replies_too(logged_in, workspace, account):
    old = conversation(account, hours_ago=30)
    assert (
        post(logged_in, workspace, f"/conversations/{old.pk}/reply", {"text": "Hi"}).status_code
        == 409
    )
    fresh = conversation(account, external_id="t2")
    SocialConversation.objects.filter(pk=fresh.pk).update(status=ConversationStatus.OPTED_OUT)
    assert (
        post(logged_in, workspace, f"/conversations/{fresh.pk}/reply", {"text": "Hi"}).status_code
        == 409
    )


def test_lead_status_updates_only_for_real_leads(logged_in, workspace, account):
    convo = conversation(account)
    assert (
        post(logged_in, workspace, f"/conversations/{convo.pk}/lead", {"status": "won"}).status_code
        == 422
    )
    SocialConversation.objects.filter(pk=convo.pk).update(lead={"need": "IT", "status": "new"})
    assert (
        post(logged_in, workspace, f"/conversations/{convo.pk}/lead", {"status": "won"}).json()[
            "lead"
        ]["status"]
        == "won"
    )


# --- studio -----------------------------------------------------------------------------------------


def test_drafts_can_be_created_edited_approved_and_cancelled(logged_in, workspace, account, owner):
    asset, _ = media_services.create_from_chunks(
        workspace.pk, owner.pk, [_jpeg()], filename="p.jpg", source="upload"
    )
    made = post(
        logged_in,
        workspace,
        f"/accounts/{account.pk}/drafts/manual",
        {"kind": "image", "caption": "Hello world"},
    ).json()
    assert made["status"] == "drafted" and any(c["code"] == "no_media" for c in made["checks"])
    updated = post(
        logged_in,
        workspace,
        f"/drafts/{made['id']}",
        {"media": [{"asset_id": str(asset.pk), "alt_text": "A picture"}], "hashtags": ["#a"]},
        method="patch",
    ).json()
    assert not [c for c in updated["checks"] if c["severity"] == "error"]
    when = (timezone.now() + timedelta(days=1)).isoformat()
    approved = post(
        logged_in, workspace, f"/drafts/{made['id']}/approve", {"scheduled_at": when}
    ).json()
    assert approved["status"] == "scheduled"
    calendar = get(
        logged_in, workspace, f"/accounts/{account.pk}/drafts", {"status": "scheduled"}
    ).json()
    assert [d["id"] for d in calendar] == [made["id"]]
    assert (
        post(logged_in, workspace, f"/drafts/{made['id']}/unschedule").json()["status"] == "drafted"
    )
    assert (
        post(logged_in, workspace, f"/drafts/{made['id']}/cancel").json()["status"] == "cancelled"
    )
    assert get(logged_in, workspace, f"/accounts/{account.pk}/drafts").json() == []


def test_approving_in_the_past_or_with_errors_is_refused(logged_in, workspace, account):
    made = post(
        logged_in, workspace, f"/accounts/{account.pk}/drafts/manual", {"caption": "x"}
    ).json()
    assert (
        post(
            logged_in,
            workspace,
            f"/drafts/{made['id']}/approve",
            {"scheduled_at": (timezone.now() - timedelta(days=1)).isoformat()},
        ).status_code
        == 422
    )
    assert post(logged_in, workspace, f"/drafts/{made['id']}/approve").status_code == 422


def test_the_composer_endpoint_starts_the_ai_job(logged_in, workspace, account):
    body = post(
        logged_in,
        workspace,
        f"/accounts/{account.pk}/drafts",
        {"kind": "carousel", "brief": "Backups", "visuals": "text_cards"},
    ).json()
    assert body["ai_status"] == "thinking" and body["kind"] == "carousel"
    assert PostDraft.objects.get(pk=body["id"]).origin == "ai"
    assert (
        post(logged_in, workspace, f"/drafts/{body['id']}/revise", {"feedback": "  "}).status_code
        == 422
    )


def test_plans_can_be_started_and_applied(logged_in, workspace, account):
    from social import analysis
    from social.tests.test_analysis import PLAN

    started = post(
        logged_in, workspace, f"/accounts/{account.pk}/reports", {"scope": "plan"}
    ).json()
    llm(PLAN)
    analysis.run(uuid.UUID(started["id"]))
    applied = post(logged_in, workspace, f"/reports/{started['id']}/apply-plan").json()
    assert len(applied["draft_ids"]) == 2
    assert post(logged_in, workspace, f"/reports/{started['id']}/apply-plan").status_code == 409


def test_the_publishing_limit_is_read_live(logged_in, workspace, account, fake):
    path = f"/accounts/{account.pk}/publishing-limit"
    assert get(logged_in, workspace, path).json() == {
        "used": 3,
        "total": 50,
        "available": True,
        "known": True,
    }
    fake.fail_next = [httpx.Response(401, json={"error": {"code": 190, "message": "expired"}})]
    assert get(logged_in, workspace, path).json()["known"] is False


# --- public endpoints ------------------------------------------------------------------------------


def _jpeg():
    buffer = io.BytesIO()
    Image.new("RGB", (1080, 1350), "navy").save(buffer, "JPEG")
    return buffer.getvalue()


def sign(body: bytes, secret: str = "s3cret") -> str:  # noqa: S107
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_the_webhook_handshake(client, settings):
    settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN = "verify-me"
    url = "/api/v1/social/instagram/webhook"
    ok = client.get(
        url, {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1234"}
    )
    assert ok.status_code == 200 and ok.content == b"1234"
    assert (
        client.get(
            url, {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}
        ).status_code
        == 403
    )
    settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN = ""
    assert (
        client.get(
            url, {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"}
        ).status_code
        == 403
    )


def test_signed_events_are_stored_once_and_unsigned_ones_are_refused(client, settings, account):
    settings.INSTAGRAM_APP_SECRET = "s3cret"
    url = "/api/v1/social/instagram/webhook"
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": ACCOUNT_ID,
                "time": 1,
                "changes": [{"field": "comments", "value": {"id": "c9", "text": "Hi"}}],
            }
        ],
    }
    body = json.dumps(payload).encode()
    ok = client.post(
        url, data=body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sign(body)
    )
    assert ok.status_code == 200
    client.post(
        url, data=body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sign(body)
    )
    assert WebhookEvent.objects.count() == 1 and WebhookEvent.objects.get().field == "comments"
    assert (
        client.post(
            url,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body, "wrong"),
        ).status_code
        == 403
    )
    assert client.post(url, data=body, content_type="application/json").status_code == 403
    unknown = json.dumps(
        {"entry": [{"id": "unknown", "changes": [{"field": "comments", "value": {}}]}]}
    ).encode()
    client.post(
        url, data=unknown, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sign(unknown)
    )
    assert WebhookEvent.objects.count() == 1


def test_messaging_events_are_stored_too(client, settings, account):
    settings.INSTAGRAM_APP_SECRET = "s3cret"
    body = json.dumps(
        {
            "entry": [
                {
                    "id": ACCOUNT_ID,
                    "messaging": [{"sender": {"id": "u1"}, "message": {"mid": "m1", "text": "hi"}}],
                }
            ]
        }
    ).encode()
    client.post(
        "/api/v1/social/instagram/webhook",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=sign(body),
    )
    assert WebhookEvent.objects.get().field == "messages"


def test_public_media_links_serve_one_file_and_expire(client, workspace, owner, settings):
    asset, _ = media_services.create_from_chunks(
        workspace.pk, owner.pk, [_jpeg()], filename="p.jpg", source="upload"
    )
    token = public_media.sign_asset(asset.pk)
    response = client.get(f"/api/v1/social/public-media/{token}")
    assert response.status_code == 200 and response["Content-Type"] == "image/jpeg"
    assert b"".join(response.streaming_content)[:2] == b"\xff\xd8"
    assert client.get("/api/v1/social/public-media/garbage").status_code == 404
    assert client.get(f"/api/v1/social/public-media/{token}x").status_code == 404
    settings.PUBLIC_BASE_URL = "https://m.example"
    assert public_media.public_url(asset).startswith(
        "https://m.example/bff/api/v1/social/public-media/"
    )
    public_media.MAX_AGE_SECONDS = -1
    try:
        assert client.get(f"/api/v1/social/public-media/{token}").status_code == 404
    finally:
        public_media.MAX_AGE_SECONDS = 6 * 3600


def test_public_endpoints_are_not_cut_off_by_the_default_anonymous_limit(client, settings):
    settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN = "verify-me"
    url = "/api/v1/social/instagram/webhook"
    query = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1"}
    statuses = {client.get(url, query).status_code for _ in range(70)}
    assert statuses == {200}
