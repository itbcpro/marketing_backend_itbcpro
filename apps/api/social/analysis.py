"""AI reports on an Instagram account: whole-account analysis, single-post analysis, content plans.

A report is created at once and written by a background job, so the screen shows progress and the
result survives a refresh. The numbers come from ``analytics`` (computed in code); the model
explains and advises and may only quote them.
"""

import uuid
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from core.errors import ConflictError, DomainError, ErrorCode, NotFoundError, ValidationFailedError
from core.logging import get_logger
from social import ai, analytics
from social.models import (
    DraftStatus,
    PostDraft,
    ReportScope,
    SocialAccount,
    SocialComment,
    SocialMedia,
    SocialReport,
)
from social.schemas import AccountAnalysis, ContentPlan, MediaAnalysis

log = get_logger(__name__)

PROMPTS = {
    ReportScope.ACCOUNT: "social/instagram-account-analysis",
    ReportScope.MEDIA: "social/instagram-media-analysis",
    ReportScope.PLAN: "social/instagram-content-plan",
}
STUCK_AFTER = timedelta(minutes=10)
MAX_INSTRUCTION = 1000
MIN_POST_AGE_HOURS = 48
MAX_PERIOD_DAYS = 365
COMMENT_SAMPLE = 30
RECENT_POSTS = 15


def _enqueue(report: SocialReport) -> None:
    from social.tasks import social_report_task

    transaction.on_commit(lambda: social_report_task.delay(str(report.pk)))


def start(
    account: SocialAccount,
    user_id: uuid.UUID | None,
    scope: str,
    *,
    media: SocialMedia | None = None,
    instruction: str = "",
    period_days: int = 90,
) -> SocialReport:
    text = instruction.strip()
    if len(text) > MAX_INSTRUCTION:
        raise ValidationFailedError("the request is too long")
    if not 7 <= period_days <= MAX_PERIOD_DAYS:  # noqa: PLR2004 - one week to a year
        raise ValidationFailedError("choose a period between 7 and 365 days")
    if scope == ReportScope.MEDIA and media is None:
        raise ValidationFailedError("choose a post to analyse")
    busy = SocialReport.objects.filter(
        account=account, scope=scope, media=media, status="thinking"
    ).first()
    if busy and busy.thinking_since and timezone.now() - busy.thinking_since < STUCK_AFTER:
        return busy
    previous = (
        SocialReport.objects.filter(account=account, scope=scope, media=media, status="done")
        .order_by("-created_at")
        .first()
    )
    report = SocialReport.objects.create(
        workspace_id=account.workspace_id,
        account=account,
        scope=scope,
        media=media,
        created_by_id=user_id,
        instruction=text,
        period_days=period_days,
        thinking_since=timezone.now(),
        previous=previous,
    )
    _enqueue(report)
    return report


def get(workspace_id: uuid.UUID, report_id: uuid.UUID) -> SocialReport:
    found = SocialReport.objects.filter(pk=report_id, workspace_id=workspace_id).first()
    if found is None:
        raise NotFoundError("report not found")
    if (
        found.status == "thinking"
        and found.thinking_since
        and timezone.now() - found.thinking_since > STUCK_AFTER
    ):
        found.status = "failed"
        found.error_code = ErrorCode.PROVIDER_UNAVAILABLE.value
        found.save(update_fields=["status", "error_code", "updated_at"])
    return found


def _scheduled_view(account: SocialAccount) -> list[dict[str, Any]]:
    rows = account.drafts.filter(
        status__in=[DraftStatus.SCHEDULED, DraftStatus.DRAFTED, DraftStatus.IDEA],
        scheduled_at__gte=timezone.now(),
    ).order_by("scheduled_at")[:30]
    return [
        {
            "when": d.scheduled_at.isoformat() if d.scheduled_at else "",
            "kind": d.kind,
            "title": d.title,
        }
        for d in rows
    ]


def _recent_view(account: SocialAccount) -> list[dict[str, Any]]:
    rows = account.media.filter(removed_at__isnull=True).exclude(kind="story")[:RECENT_POSTS]
    return [
        {
            "kind": m.kind,
            "posted_at": m.posted_at.date().isoformat(),
            "caption_start": m.caption[:120],
        }
        for m in rows
    ]


def _comments_view(media: SocialMedia) -> list[dict[str, Any]]:
    rows = SocialComment.objects.filter(media=media, from_owner=False).order_by("-posted_at")[
        :COMMENT_SAMPLE
    ]
    return [{"author": c.username, "text": c.text[:400], "likes": c.like_count} for c in rows]


def _write_account(report: SocialReport, account: SocialAccount) -> dict[str, Any]:
    stats = analytics.compute_stats(account, report.period_days)
    report.stats = stats
    last = report.previous.result if report.previous and report.previous.result else None
    result = ai.run_prompt(
        account.workspace_id,
        PROMPTS[ReportScope.ACCOUNT],
        AccountAnalysis,
        memory_query=f"instagram strategy {report.instruction}",
        user_message="Write the analysis.",
        account=ai.dumps(ai.account_view(account)),
        stats=ai.dumps(stats),
        last_report=ai.dumps(last) if last else "",
        instruction=report.instruction,
    )
    return result.model_dump(mode="json")


def _write_media(report: SocialReport, account: SocialAccount) -> dict[str, Any]:
    media = report.media
    if media is None:
        raise NotFoundError("post not found")
    baseline = analytics.baseline_for(media, report.period_days)
    report.stats = {"post": analytics.media_view(media), "baseline": baseline}
    result = ai.run_prompt(
        account.workspace_id,
        PROMPTS[ReportScope.MEDIA],
        MediaAnalysis,
        memory_query=f"instagram {media.kind} {media.caption[:80]}",
        user_message="Write the analysis of this post.",
        media=ai.dumps(report.stats["post"]),
        baseline=ai.dumps(baseline),
        comments=ai.untrusted("instagram_comments", _comments_view(media)),
        instruction=report.instruction,
    )
    data = result.model_dump(mode="json")
    age_hours = int((timezone.now() - media.posted_at).total_seconds() // 3600)
    if age_hours < MIN_POST_AGE_HOURS and data["verdict"] != "too_early":
        data["verdict"] = "too_early"
    media.analysis = data
    media.analysis_at = timezone.now()
    media.save(update_fields=["analysis", "analysis_at", "updated_at"])
    return data


def _write_plan(report: SocialReport, account: SocialAccount) -> dict[str, Any]:
    stats = analytics.compute_stats(account, report.period_days)
    report.stats = stats
    result = ai.run_prompt(
        account.workspace_id,
        PROMPTS[ReportScope.PLAN],
        ContentPlan,
        memory_query=f"instagram content plan {report.instruction}",
        user_message="Write the four-week plan.",
        account=ai.dumps(ai.account_view(account)),
        stats=ai.dumps(stats),
        recent_posts=ai.dumps(_recent_view(account)),
        scheduled=ai.dumps(_scheduled_view(account)),
        today=timezone.now().date().isoformat(),
        instruction=report.instruction,
    )
    return result.model_dump(mode="json")


WRITERS = {
    ReportScope.ACCOUNT: _write_account,
    ReportScope.MEDIA: _write_media,
    ReportScope.PLAN: _write_plan,
}


def run(report_id: uuid.UUID) -> str:
    report = (
        SocialReport.objects.select_related("account", "media", "previous")
        .filter(pk=report_id)
        .first()
    )
    if report is None or report.status != "thinking":
        return "skipped"
    try:
        report.result = WRITERS[ReportScope(report.scope)](report, report.account)
    except DomainError as exc:
        return _fail(report, exc.code.value)
    except Exception:
        log.exception("social_report_crashed", scope=report.scope)
        return _fail(report, ErrorCode.UNKNOWN.value)
    report.status = "done"
    report.thinking_since = None
    report.save()
    return "done"


def _fail(report: SocialReport, code: str) -> str:
    report.status = "failed"
    report.error_code = code
    report.thinking_since = None
    report.save(update_fields=["status", "error_code", "thinking_since", "updated_at"])
    return "failed"


# --- turning a plan into drafts ---------------------------------------------------------------


def _local_moment(day_offset: int, clock: str, tz: ZoneInfo) -> datetime:
    start = timezone.now().astimezone(tz).date() + timedelta(days=day_offset)
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime.combine(start, time(hour, minute), tzinfo=tz)


def apply_plan(report: SocialReport, user_id: uuid.UUID) -> list[PostDraft]:
    """Create one idea per plan item on the calendar. Allowed once per plan."""
    if report.scope != ReportScope.PLAN or report.status != "done" or not report.result:
        raise ValidationFailedError("this report is not a finished plan")
    if report.result.get("applied"):
        raise ConflictError("this plan is already on the calendar")
    tz = ZoneInfo(report.account.workspace.time_zone)
    drafts: list[PostDraft] = []
    with transaction.atomic():
        for item in report.result["items"]:
            moment = _local_moment(int(item["day"]), item["time"], tz)
            if moment <= timezone.now():
                moment += timedelta(days=1)
            drafts.append(
                PostDraft.objects.create(
                    workspace_id=report.workspace_id,
                    account=report.account,
                    kind=item["kind"],
                    status=DraftStatus.IDEA,
                    title=item["title"][:200],
                    brief=f"{item['hook']}\n\n{item['brief']}"[:4000],
                    pillar=item["pillar"][:30],
                    scheduled_at=moment,
                    origin="ai",
                    created_by_id=user_id,
                    ai={"plan_report": str(report.pk), "rationale": item["rationale"]},
                )
            )
        report.result = {**report.result, "applied": [str(d.pk) for d in drafts]}
        report.save(update_fields=["result", "updated_at"])
    return drafts
