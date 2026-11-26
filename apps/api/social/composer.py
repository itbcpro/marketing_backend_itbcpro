"""AI post studio: brief to caption, hashtags, slides or reel plan, and pictures.

Writing and drawing run in a background job so the screen can show progress. Text cards are drawn
from the brand colours (exact text, always on brand); an AI image is generated only when an image
service is connected. A failure to make pictures never loses the text.
"""

import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.errors import DomainError, ErrorCode, NotFoundError, ValidationFailedError
from core.logging import get_logger
from social import ai, checks, images, video
from social.models import DraftStatus, MediaKind, PostDraft, SocialAccount, SocialMedia
from social.schemas import PostCopy, ReelPlan

log = get_logger(__name__)

PROMPT_KEY = "social/instagram-post-writer"
STUCK_AFTER = timedelta(minutes=10)
MAX_BRIEF = 4000
MAX_FEEDBACK = 1000
TOP_POSTS = 5
VISUAL_MODES = ("text_cards", "ai_image", "none")
SLIDE_SIZE = "feed"
ORIENTATION = {"image": "portrait", "story": "portrait", "reel": "portrait", "carousel": "portrait"}
CROP = {
    "image": "meta_portrait",
    "story": "meta_story",
    "reel": "meta_story",
    "carousel": "meta_portrait",
}
EDITABLE = {DraftStatus.IDEA, DraftStatus.DRAFTED, DraftStatus.FAILED, DraftStatus.SCHEDULED}


def _enqueue(draft: PostDraft) -> None:
    from social.tasks import compose_draft_task

    transaction.on_commit(lambda: compose_draft_task.delay(str(draft.pk)))


def create_draft(
    account: SocialAccount,
    user_id: uuid.UUID | None,
    *,
    kind: str = "image",
    brief: str,
    pillar: str = "",
    visuals: str = "text_cards",
    scheduled_at: Any = None,
) -> PostDraft:
    text = brief.strip()
    if not text:
        raise ValidationFailedError("describe what the post is about")
    if len(text) > MAX_BRIEF:
        raise ValidationFailedError("the brief is too long")
    if kind not in {k.value for k in MediaKind}:
        raise ValidationFailedError("unknown post format")
    if visuals not in VISUAL_MODES:
        raise ValidationFailedError("unknown picture option")
    draft = PostDraft.objects.create(
        workspace_id=account.workspace_id,
        account=account,
        kind=kind,
        status=DraftStatus.DRAFTED,
        brief=text,
        pillar=pillar[:30],
        origin="ai",
        created_by_id=user_id,
        scheduled_at=scheduled_at,
        ai={"visuals": visuals},
        ai_status="thinking",
        ai_since=timezone.now(),
    )
    _enqueue(draft)
    return draft


def revise(draft: PostDraft, feedback: str, *, visuals: str | None = None) -> PostDraft:
    """Ask the AI to improve the current draft following the feedback."""
    text = feedback.strip()
    if not text or len(text) > MAX_FEEDBACK:
        raise ValidationFailedError("write what should change (up to 1000 characters)")
    if draft.status not in EDITABLE:
        raise ValidationFailedError("this post can no longer be changed")
    if visuals is not None and visuals not in VISUAL_MODES:
        raise ValidationFailedError("unknown picture option")
    draft.ai = {**draft.ai, "feedback": text, **({"visuals": visuals} if visuals else {})}
    draft.ai_status = "thinking"
    draft.ai_since = timezone.now()
    draft.ai_error_code = ""
    if draft.status == DraftStatus.SCHEDULED:
        draft.status = DraftStatus.DRAFTED
    draft.save()
    _enqueue(draft)
    return draft


def refresh_status(draft: PostDraft) -> PostDraft:
    if (
        draft.ai_status == "thinking"
        and draft.ai_since
        and timezone.now() - draft.ai_since > STUCK_AFTER
    ):
        draft.ai_status = "failed"
        draft.ai_error_code = ErrorCode.PROVIDER_UNAVAILABLE.value
        draft.save(update_fields=["ai_status", "ai_error_code", "updated_at"])
    return draft


def _current_view(draft: PostDraft) -> str:
    if not draft.caption:
        return ""
    return ai.dumps(
        {
            "caption": draft.caption,
            "hashtags": draft.hashtags,
            "first_comment": draft.first_comment,
            "slides": draft.ai.get("slides", []),
            "reel": draft.ai.get("reel"),
        }
    )


def _top_posts(account: SocialAccount) -> list[dict[str, Any]]:
    rows = [
        m
        for m in SocialMedia.objects.filter(account=account, removed_at__isnull=True)
        .exclude(kind="story")
        .order_by("-posted_at")[:40]
        if m.metrics.get("engagement_rate") is not None
    ]
    rows.sort(key=lambda m: m.metrics.get("engagement_rate") or 0, reverse=True)
    return [
        {"kind": m.kind, "caption": m.caption[:300], "metrics": m.metrics} for m in rows[:TOP_POSTS]
    ]


def write_copy(draft: PostDraft) -> PostCopy:
    return ai.run_prompt(
        draft.workspace_id,
        PROMPT_KEY,
        PostCopy,
        memory_query=f"instagram {draft.kind} {draft.brief[:120]}",
        user_message="Write the post.",
        account=ai.dumps(ai.account_view(draft.account)),
        format=draft.kind,
        pillar=draft.pillar,
        brief=draft.brief,
        top_posts=ai.dumps(_top_posts(draft.account)),
        previous=_current_view(draft),
        feedback=str(draft.ai.get("feedback", "")),
    )


def _apply_copy(draft: PostDraft, copy: PostCopy) -> None:
    draft.title = copy.title[:200]
    draft.caption = copy.caption
    draft.hashtags = [h if h.startswith("#") else f"#{h}" for h in copy.hashtags]
    draft.first_comment = copy.first_comment
    draft.ai = {
        **draft.ai,
        "rationale": copy.rationale,
        "alternates": copy.alternates,
        "image_prompt": copy.image_prompt,
        "alt_text": copy.alt_text,
        "slides": [s.model_dump() for s in copy.slides],
        "reel": copy.reel.model_dump() if copy.reel else None,
        "feedback": "",
    }


def _cards(draft: PostDraft, copy: PostCopy, user_id: uuid.UUID | None) -> list[dict[str, Any]]:
    palette = images.palette_from(ai.brand_profile_content(draft.workspace_id))
    handle = f"@{draft.account.username}" if draft.account.username else ""
    entries: list[dict[str, Any]] = []
    slides = copy.slides or []
    if draft.kind != MediaKind.CAROUSEL or not slides:
        slides = []
    for number, slide in enumerate(slides, start=1):
        data = images.render_card(
            slide.headline,
            slide.body,
            palette=palette,
            handle=handle,
            position=f"{number}/{len(slides)}",
        )
        alt = slide.alt_text or slide.headline
        asset = images.store_picture(
            draft.workspace_id,
            user_id,
            data,
            name=f"{copy.title[:60]} slide {number}",
            alt_text=alt,
            ai_generated=False,
        )
        entries.append({"asset_id": str(asset.pk), "alt_text": alt})
    return entries


def _single_card(draft: PostDraft, copy: PostCopy, user_id: uuid.UUID | None) -> dict[str, Any]:
    palette = images.palette_from(ai.brand_profile_content(draft.workspace_id))
    size = images.CARD_SIZES["story" if draft.kind in {"story", "reel"} else "feed"]
    data = images.render_card(
        copy.title,
        copy.caption.split("\n", 1)[0][:160],
        palette=palette,
        size=size,
        handle=f"@{draft.account.username}" if draft.account.username else "",
    )
    asset = images.store_picture(
        draft.workspace_id,
        user_id,
        data,
        name=copy.title[:80],
        alt_text=copy.alt_text,
        ai_generated=False,
    )
    return {"asset_id": str(asset.pk), "alt_text": copy.alt_text}


def _ai_picture(draft: PostDraft, copy: PostCopy, user_id: uuid.UUID | None) -> dict[str, Any]:
    asset = images.generate_picture(
        draft.workspace_id,
        user_id,
        copy.image_prompt,
        orientation=ORIENTATION[draft.kind],
        name=copy.title[:80] or "AI picture",
        alt_text=copy.alt_text,
        crop=CROP[draft.kind],
    )
    return {"asset_id": str(asset.pk), "alt_text": copy.alt_text}


def _make_reel_video(draft: PostDraft, plan: ReelPlan, user_id: uuid.UUID | None) -> None:
    """Draw the reel from its plan, unless the person attached their own video."""
    own_video = draft.media and str(draft.media[0].get("asset_id")) != str(
        draft.ai.get("video_asset_id", "")
    )
    if own_video:
        return
    palette = images.palette_from(ai.brand_profile_content(draft.workspace_id))
    asset = video.store_reel(
        draft.workspace_id,
        user_id,
        video.render(plan, palette),
        name=f"{draft.title or 'Reel'}"[:80],
    )
    draft.media = [{"asset_id": str(asset.pk), "alt_text": ""}]
    draft.ai = {**draft.ai, "video_asset_id": str(asset.pk)}


def make_visuals(draft: PostDraft, copy: PostCopy, user_id: uuid.UUID | None) -> None:
    """Pictures for the draft. Failures are recorded on the draft and never lose the copy."""
    mode = str(draft.ai.get("visuals", "text_cards"))
    draft.ai = {**draft.ai, "visual_error": ""}
    if mode == "none":
        return
    try:
        if draft.kind == MediaKind.CAROUSEL:
            draft.media = _cards(draft, copy, user_id) or draft.media
        elif mode == "ai_image":
            entry = _ai_picture(draft, copy, user_id)
            _place(draft, entry)
        else:
            _place(draft, _single_card(draft, copy, user_id))
        if draft.kind == MediaKind.REEL and copy.reel is not None:
            _make_reel_video(draft, copy.reel, user_id)
    except DomainError as exc:
        draft.ai = {**draft.ai, "visual_error": exc.code.value}
        log.warning("social_visuals_failed", code=exc.code.value)


def _place(draft: PostDraft, entry: dict[str, Any]) -> None:
    if draft.kind == MediaKind.REEL:
        draft.cover_asset_id = uuid.UUID(entry["asset_id"])  # the video itself is the creator's
    else:
        draft.media = [entry]


def run(draft_id: uuid.UUID) -> str:
    draft = (
        PostDraft.objects.select_related("account", "account__workspace")
        .filter(pk=draft_id)
        .first()
    )
    if draft is None or draft.ai_status != "thinking":
        return "skipped"
    try:
        copy = write_copy(draft)
    except DomainError as exc:
        return _fail(draft, exc.code.value)
    except Exception:
        log.exception("social_compose_crashed")
        return _fail(draft, ErrorCode.UNKNOWN.value)
    _apply_copy(draft, copy)
    if draft.kind == MediaKind.REEL and copy.reel is None:
        draft.ai = {**draft.ai, "visual_error": "no_reel_plan"}
    make_visuals(draft, copy, draft.created_by_id)
    draft.checks = checks.run_checks(draft)
    draft.ai_status = "done"
    draft.ai_since = None
    draft.save()
    return "done"


def _fail(draft: PostDraft, code: str) -> str:
    draft.ai_status = "failed"
    draft.ai_error_code = code
    draft.ai_since = None
    draft.save(update_fields=["ai_status", "ai_error_code", "ai_since", "updated_at"])
    return "failed"


def regenerate_visuals(draft: PostDraft, user_id: uuid.UUID | None, mode: str) -> PostDraft:
    """Draw the pictures again from the current text (for example after switching to AI images)."""
    if mode not in VISUAL_MODES:
        raise ValidationFailedError("unknown picture option")
    if draft.status not in EDITABLE:
        raise ValidationFailedError("this post can no longer be changed")
    if not draft.caption:
        raise NotFoundError("write the post first")
    slides = draft.ai.get("slides", [])
    copy = PostCopy(
        title=draft.title or "Post",
        caption=draft.caption,
        alternates=[],
        hashtags=[],
        slides=slides,
        reel=draft.ai.get("reel"),
        image_prompt=str(draft.ai.get("image_prompt") or draft.title or "A clean branded photo")[
            :1200
        ],
        alt_text=str(draft.ai.get("alt_text") or "")[:500],
        rationale="",
    )
    draft.ai = {**draft.ai, "visuals": mode}
    make_visuals(draft, copy, user_id)
    draft.checks = checks.run_checks(draft)
    draft.save()
    return draft
