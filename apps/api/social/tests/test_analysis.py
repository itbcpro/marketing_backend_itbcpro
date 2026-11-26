import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from core.errors import ConflictError, ValidationFailedError
from social import analysis, analytics
from social.models import DraftStatus, MediaMetricSnapshot, ReportScope, SocialReport
from social.tests.conftest import llm, make_comment, make_media

pytestmark = pytest.mark.django_db

ACCOUNT_REPORT = {
    "verdict": "steady",
    "score": 58,
    "summary": "Reels reach more people than images, based on 6 reels.",
    "what_works": ["Reels"],
    "what_does_not": ["Long captions"],
    "format_mix": ["Add carousels"],
    "timing": ["Too early to say"],
    "audience": ["Mostly 25 to 34"],
    "community": ["3 comments waiting"],
    "priorities": [
        {
            "title": "Post two reels a week",
            "why": "Reach lift",
            "how": "Film on Mondays",
            "impact": "high",
            "effort": "medium",
        }
    ],
    "experiments": [{"hypothesis": "Question hooks win", "test": "Four reels", "metric": "views"}],
    "next_posts": [{"idea": "Backup checklist", "format": "carousel", "why": "Saves"}],
    "risks": ["Gap of 9 days"],
    "since_last_report": {"assessment": "first_report", "explanation": "First report."},
}
MEDIA_REPORT = {
    "verdict": "average",
    "score": 50,
    "summary": "Typical.",
    "why": ["Hook is generic"],
    "learnings": ["Lead with the problem"],
    "improvements": ["Shorten the first line"],
    "repurpose": [{"idea": "Reel version", "format": "reel", "why": "Reach"}],
}
PLAN = {
    "summary": "Exploratory plan.",
    "cadence": "Three posts a week",
    "pillars": ["education", "proof"],
    "items": [
        {
            "day": 1,
            "time": "09:30",
            "kind": "reel",
            "pillar": "education",
            "title": "Backups",
            "hook": "Is your backup real?",
            "brief": "Show a restore test.",
            "rationale": "Reels reach more.",
        },
        {
            "day": 3,
            "time": "17:00",
            "kind": "carousel",
            "pillar": "proof",
            "title": "Case",
            "hook": "From 3 outages to 0",
            "brief": "Slides.",
            "rationale": "Proof.",
        },
    ],
}


def measured(account, external_id, kind, reach, interactions, days_ago=10, **fields):
    metrics = {
        "reach": reach,
        "views": reach * 2,
        "interactions": interactions,
        "saves": interactions // 4,
        "shares": interactions // 5,
        "engagement_rate": round(interactions / reach, 4),
    }
    return make_media(
        account,
        external_id,
        kind=kind,
        metrics=metrics,
        metrics_synced_at=timezone.now(),
        posted_at=timezone.now() - timedelta(days=days_ago),
        **fields,
    )


def seed_posts(account):
    for i in range(6):
        measured(
            account,
            f"r{i}",
            "reel",
            1000 + i * 100,
            80 + i * 5,
            days_ago=5 + i * 3,
            caption="Reel #tip #it",
        )
    for i in range(3):
        measured(account, f"i{i}", "image", 300 + i * 10, 12, days_ago=7 + i * 5, caption="x" * 400)


def test_small_samples_are_marked_unreliable_and_get_no_lift(account):
    seed_posts(account)
    stats = analytics.compute_stats(account)
    reels, images = stats["by_format"]["reel"], stats["by_format"]["image"]
    assert reels["n"] == 6 and reels["reliable"] and reels["reach_lift_vs_all"] is not None
    assert images["n"] == 3 and not images["reliable"] and images["reach_lift_vs_all"] is None
    assert stats["overall"]["n"] == 9


def test_stats_cover_timing_captions_hashtags_and_best_posts(account):
    seed_posts(account)
    stats = analytics.compute_stats(account)
    assert set(stats["by_caption_length"]) == {"short", "long"}
    assert "one_to_five" in stats["by_hashtag_count"] and "none" in stats["by_hashtag_count"]
    assert len(stats["best_posts"]) == 5 and stats["best_posts"][0]["kind"] == "reel"
    assert stats["cadence"]["posts"] == 9 and stats["cadence"]["longest_gap_days"] is not None


def test_posts_without_metrics_are_counted_but_not_averaged(account):
    make_media(account, "bare", posted_at=timezone.now() - timedelta(days=2))
    stats = analytics.compute_stats(account)
    assert stats["data"] == {"posts_in_period": 1, "posts_with_metrics": 0}
    assert stats["overall"]["median_reach"] is None


def test_the_account_report_reads_real_numbers_and_remembers_the_previous_one(account, owner):
    seed_posts(account)
    gateway = llm(ACCOUNT_REPORT, {**ACCOUNT_REPORT, "score": 62})
    first = analysis.start(account, owner.pk, ReportScope.ACCOUNT)
    assert analysis.run(first.pk) == "done"
    first.refresh_from_db()
    assert first.result["score"] == 58 and first.stats["overall"]["n"] == 9
    sent = gateway.requests[0][0].system[0].text
    assert '"median_reach"' in sent and "Never invent a number" in sent
    second = analysis.start(account, owner.pk, ReportScope.ACCOUNT, instruction="Focus on reels")
    assert second.previous_id == first.pk
    analysis.run(second.pk)
    assert "Focus on reels" in gateway.requests[1][0].system[0].text
    assert '"score": 58' in gateway.requests[1][0].system[0].text


def test_a_running_report_is_not_started_twice(account, owner):
    first = analysis.start(account, owner.pk, ReportScope.ACCOUNT)
    assert analysis.start(account, owner.pk, ReportScope.ACCOUNT).pk == first.pk


def test_a_stuck_report_is_reported_as_failed(account, owner):
    report = analysis.start(account, owner.pk, ReportScope.ACCOUNT)
    SocialReport.objects.filter(pk=report.pk).update(
        thinking_since=timezone.now() - timedelta(hours=1)
    )
    assert analysis.get(account.workspace_id, report.pk).status == "failed"


def test_a_failed_model_call_is_recorded_not_raised(account, owner):
    from core.errors import RetryableProviderError
    from llm.fake import FakeGateway
    from llm.service import LlmService, set_llm_service

    set_llm_service(LlmService(FakeGateway([RetryableProviderError("down", provider="x")])))
    report = analysis.start(account, owner.pk, ReportScope.ACCOUNT)
    assert analysis.run(report.pk) == "failed"
    report.refresh_from_db()
    assert report.status == "failed" and report.error_code == "provider_unavailable"


def test_a_post_analysis_compares_with_the_accounts_own_baseline(account, owner):
    seed_posts(account)
    target = measured(account, "star", "reel", 4000, 400, days_ago=6, caption="Backups explained")
    make_comment(account, "c1", "Great tip!", media=target)
    MediaMetricSnapshot.objects.create(
        workspace_id=account.workspace_id,
        media=target,
        day=timezone.now().date(),
        age_hours=144,
        metrics=target.metrics,
    )
    gateway = llm(MEDIA_REPORT)
    report = analysis.start(account, owner.pk, ReportScope.MEDIA, media=target)
    analysis.run(report.pk)
    target.refresh_from_db()
    assert target.analysis["verdict"] == "average" and target.analysis_at
    system = gateway.requests[0][0].system[0].text
    assert "percentile_among_same_format" in system and "Great tip!" in system
    assert "<untrusted_content" in system


def test_a_young_post_is_always_too_early(account, owner):
    young = measured(account, "new", "image", 100, 10, days_ago=0)
    llm({**MEDIA_REPORT, "verdict": "winner"})
    report = analysis.start(account, owner.pk, ReportScope.MEDIA, media=young)
    analysis.run(report.pk)
    young.refresh_from_db()
    assert young.analysis["verdict"] == "too_early"


def test_media_reports_need_a_post(account, owner):
    with pytest.raises(ValidationFailedError):
        analysis.start(account, owner.pk, ReportScope.MEDIA)


def test_a_plan_becomes_ideas_on_the_calendar_once(account, owner):
    llm(PLAN)
    report = analysis.start(account, owner.pk, ReportScope.PLAN, instruction="Grow leads")
    analysis.run(report.pk)
    report.refresh_from_db()
    created = analysis.apply_plan(report, owner.pk)
    assert [d.status for d in created] == [DraftStatus.IDEA, DraftStatus.IDEA]
    assert created[0].kind == "reel" and created[0].pillar == "education"
    assert created[0].scheduled_at > timezone.now()
    assert "Is your backup real?" in created[0].brief
    report.refresh_from_db()
    with pytest.raises(ConflictError):
        analysis.apply_plan(report, owner.pk)


def test_plan_times_use_the_workspace_time_zone(account, owner, workspace):
    workspace.time_zone = "America/Vancouver"
    workspace.save()
    llm(PLAN)
    report = analysis.start(account, owner.pk, ReportScope.PLAN)
    analysis.run(report.pk)
    report.refresh_from_db()
    first = analysis.apply_plan(report, owner.pk)[0]
    local = first.scheduled_at.astimezone(ZoneInfo("America/Vancouver"))
    assert (local.hour, local.minute) == (9, 30)


def test_the_model_sees_unanswered_comment_counts(account):
    make_comment(account, "c1", "Question?")
    make_comment(account, "c2", "Another?")
    community = analytics.community_summary(account)
    assert community["comments"] == 2 and community["unanswered_comments"] == 2
    json.dumps(community)  # everything the model gets must serialise
