from datetime import UTC, datetime

import httpx
import pytest
from django.utils import timezone

from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from social import sync
from social.instagram.client import raise_for_response
from social.models import (
    AccountInsight,
    CommentStatus,
    ConversationStatus,
    SocialAccount,
    SocialComment,
    SocialConversation,
    SocialMedia,
)
from social.tests.fake_instagram import ACCOUNT_ID

pytestmark = pytest.mark.django_db

# The fake server serves posts and comments dated September 2026, so the clock is pinned to then.
FROZEN_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: FROZEN_NOW)


def _populate(fake):
    fake.add_media("m1", caption="Image post")
    fake.add_media(
        "m2", media_type="VIDEO", media_product_type="REELS", caption="A reel", comments_count=2
    )
    fake.add_media(
        "m3",
        media_type="CAROUSEL_ALBUM",
        caption="Carousel",
        children={"data": [{"id": "c1", "media_type": "IMAGE", "media_url": "https://x/1.jpg"}]},
    )
    fake.insights["m2"].update(
        {"ig_reels_avg_watch_time": 4200, "ig_reels_video_view_total_time": 90000}
    )
    fake.comments["m2"] = [
        {
            "id": "c-100",
            "text": "How much does it cost?",
            "username": "alice",
            "timestamp": "2026-09-21T09:00:00+0000",
            "like_count": 1,
            "from": {"id": "u1", "username": "alice"},
        },
        {
            "id": "c-101",
            "text": "Love it",
            "username": "bob",
            "timestamp": "2026-09-21T09:30:00+0000",
            "from": {"id": "u2", "username": "bob"},
            "replies": {
                "data": [
                    {
                        "id": "c-102",
                        "text": "Thank you!",
                        "username": "acme_it",
                        "timestamp": "2026-09-21T10:00:00+0000",
                        "from": {"id": ACCOUNT_ID, "username": "acme_it"},
                    }
                ]
            },
        },
    ]
    fake.conversations = [
        {
            "id": "t_1",
            "participants": {
                "data": [
                    {"id": "u1", "username": "alice"},
                    {"id": ACCOUNT_ID, "username": "acme_it"},
                ]
            },
            "messages": {
                "data": [
                    {
                        "id": "mid-2",
                        "created_time": "2026-09-21T11:00:00+0000",
                        "from": {"id": "u1"},
                        "message": "Do you do onboarding?",
                    },
                    {
                        "id": "mid-1",
                        "created_time": "2026-09-21T10:00:00+0000",
                        "from": {"id": ACCOUNT_ID},
                        "message": "Hello",
                    },
                ]
            },
        }
    ]


def test_sync_builds_the_local_copy(connection, fake):
    _populate(fake)
    counts = sync.sync_connection(connection)
    account = SocialAccount.objects.get(connection=connection)
    assert account.username == "acme_it" and account.followers_count == 1200
    kinds = dict(SocialMedia.objects.values_list("external_id", "kind"))
    assert kinds == {"m1": "image", "m2": "reel", "m3": "carousel"}
    assert counts["media"] == 3 and counts["metrics"] == 3 and counts["insights"] == 1
    reel = SocialMedia.objects.get(external_id="m2")
    assert reel.metrics["reach"] == 400
    assert reel.metrics["interactions"] == 20
    assert reel.metrics["saves"] == 5
    assert reel.metrics["avg_watch_seconds"] == 4.2
    assert reel.metrics["engagement_rate"] == 0.05
    assert reel.snapshots.count() == 1
    assert SocialMedia.objects.get(external_id="m3").children[0]["id"] == "c1"
    assert account.last_error == "" and account.webhooks_subscribed


def test_sync_is_repeatable_without_duplicates(connection, fake):
    _populate(fake)
    sync.sync_connection(connection)
    sync.sync_connection(connection)
    assert SocialMedia.objects.count() == 3
    assert SocialComment.objects.count() == 3
    assert AccountInsight.objects.count() == 1


def test_comments_are_stored_and_owner_replies_close_the_thread(connection, fake):
    _populate(fake)
    sync.sync_connection(connection)
    question = SocialComment.objects.get(external_id="c-100")
    assert question.status == CommentStatus.NEW and not question.from_owner
    thanked = SocialComment.objects.get(external_id="c-101")
    assert thanked.status == CommentStatus.REPLIED
    own = SocialComment.objects.get(external_id="c-102")
    assert own.from_owner and own.status == CommentStatus.IGNORED
    assert own.parent_external_id == "c-101"


def test_conversations_track_the_reply_window_and_who_waits(connection, fake):
    _populate(fake)
    sync.sync_connection(connection)
    conversation = SocialConversation.objects.get(external_id="t_1")
    assert conversation.participant_id == "u1" and conversation.participant_username == "alice"
    assert conversation.messages.count() == 2
    assert conversation.needs_reply and conversation.unread
    assert conversation.status == ConversationStatus.OPEN
    inbound = conversation.messages.get(external_id="mid-2")
    assert inbound.inbound and not conversation.messages.get(external_id="mid-1").inbound


def test_unsupported_metrics_fall_back_to_a_smaller_set(connection, fake):
    fake.add_media("m1")
    fake.unsupported_metrics = {"views", "total_interactions"}
    sync.sync_connection(connection)
    media = SocialMedia.objects.get(external_id="m1")
    assert media.metrics["reach"] == 400 and media.metrics["saves"] == 5
    assert "views" not in media.metrics


def test_a_broken_post_does_not_stop_the_sync(connection, fake):
    fake.add_media("m1")
    fake.add_media("m2")
    fake.unsupported_metrics = {"reach"}
    sync.sync_connection(connection)
    assert SocialMedia.objects.count() == 2
    assert SocialMedia.objects.filter(metrics_synced_at__isnull=False).count() == 0


def test_posts_that_disappear_are_marked_removed(connection, fake):
    fake.add_media("m1")
    fake.add_media("m2", timestamp="2026-09-21T10:00:00+0000")
    sync.sync_connection(connection)
    fake.media = [m for m in fake.media if m["id"] != "m2"]
    sync.sync_connection(connection)
    assert SocialMedia.objects.get(external_id="m2").removed_at is not None
    assert SocialMedia.objects.get(external_id="m1").removed_at is None


def test_auth_failure_is_recorded_and_raised(connection, fake):
    fake.fail_next = [httpx.Response(401, json={"error": {"code": 190, "message": "expired"}})]
    with pytest.raises(AuthExpiredError):
        sync.sync_connection(connection)


def test_inbox_only_skips_insights(connection, fake):
    _populate(fake)
    counts = sync.sync_connection(connection, inbox_only=True)
    assert "metrics" not in counts and AccountInsight.objects.count() == 0
    assert counts["comments"] == 3


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"error": {"code": 190}}, AuthExpiredError),
        (400, {"error": {"code": 190, "message": "token"}}, AuthExpiredError),
        (429, {"error": {"code": 4}}, RateLimitedError),
        (400, {"error": {"code": 80002}}, RateLimitedError),
        (500, {"error": {"code": 2}}, RetryableProviderError),
        (400, {"error": {"code": 1, "message": "retry"}}, RetryableProviderError),
        (400, {"error": {"code": 100, "error_subcode": 2207027}}, RetryableProviderError),
        (400, {"error": {"code": 100, "message": "bad"}}, PermanentProviderError),
    ],
)
def test_platform_errors_become_domain_errors(status, body, error):
    with pytest.raises(error):
        raise_for_response(httpx.Response(status, json=body))


def test_timestamps_with_and_without_a_colon_parse():
    assert sync.parse_time("2026-09-21T10:00:00+0000").year == 2026
    assert sync.parse_time("2026-09-21T10:00:00+00:00").hour == 10
    assert sync.parse_time(None).tzinfo is not None
