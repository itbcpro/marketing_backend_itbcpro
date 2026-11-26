"""Write one piece for one channel, then make its pictures and run the checks.

The text is linted for machine-sounding habits; if any are found the writer gets one chance to fix
them. Pictures are made after the text and never cost the text when they fail. Everything runs in a
background job so the screen can show progress.
"""

import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.errors import DomainError, ErrorCode, ValidationFailedError
from core.logging import get_logger
from social import ai
from studio import checks, clock, config_store, context, design, strategist, voice
from studio.channels.registry import get_kind
from studio.models import Brand, ContentPiece, PieceStatus
from studio.schemas import PieceCopy
from studio.visuals import director

log = get_logger(__name__)

PROMPT_KEY = "studio/piece-writer"
AGENT = "content-writer"
STUCK_AFTER = timedelta(minutes=10)
MAX_FEEDBACK = 1000
MAX_BRIEF = 4000
VOICE_RETRIES = 1
EDITABLE = {PieceStatus.DRAFTING, PieceStatus.DRAFT, PieceStatus.FAILED, PieceStatus.SCHEDULED}


def enqueue(piece: ContentPiece) -> None:
    from studio.tasks import write_piece_task

    transaction.on_commit(lambda: write_piece_task.delay(str(piece.pk)))


def _idea_view(piece: ContentPiece) -> str:
    idea = piece.idea
    if idea is None:
        return ai.dumps({})
    return ai.dumps(
        {
            "title": idea.title,
            "angle": idea.angle,
            "why_now": idea.why_now,
            "pillar": idea.pillar,
            "needs_from_owner": idea.needs_from_owner,
        }
    )


def _signals_view(piece: ContentPiece) -> str:
    rows = strategist.signals_for(piece.idea) if piece.idea else []
    return ai.untrusted("trend signals", rows)


def _previous(piece: ContentPiece) -> str:
    if not piece.copy.get("body"):
        return ""
    keys = ("body", "hashtags", "first_comment", "slides", "video")
    return ai.dumps({k: piece.copy.get(k) for k in keys})


def _call(piece: ContentPiece, feedback: str) -> PieceCopy:
    config = config_store.load(piece.brand_id)
    plan = config.plan_for(piece.channel)
    local = (
        piece.scheduled_at.astimezone(clock.zone_of(piece.workspace_id))
        if piece.scheduled_at
        else None
    )
    return ai.run_prompt(
        piece.workspace_id,
        PROMPT_KEY,
        PieceCopy,
        memory_query=f"{piece.channel} {piece.kind} {piece.title or piece.brief[:100]}",
        user_message="Write the post.",
        channel=piece.channel,
        agent=AGENT,
        **context.llm_scope(piece.brand),
        strategy=context.strategy_view(config),
        network=piece.channel,
        kind=piece.kind,
        limits=context.limits_view(piece.channel, piece.kind),
        channel_notes=plan.instructions,
        idea=_idea_view(piece),
        signals=_signals_view(piece),
        brief=piece.brief,
        recent=context.recent_view(piece.brand_id, piece.channel),
        recent_designs=context.recent_designs(piece.brand_id, piece.channel),
        design_family=design.describe(str(piece.ai_state.get(design.KEY, ""))),
        scheduled_for=local.strftime("%A %d %B %Y, %H:%M") if local else "not scheduled yet",
        previous=_previous(piece),
        feedback=feedback,
    )


def _findings(piece: ContentPiece, copy: PieceCopy) -> list[voice.Finding]:
    config = config_store.load(piece.brand_id)
    scratch = ContentPiece(channel=piece.channel, kind=piece.kind, copy=_copy_dict(copy))
    return checks.voice_findings(scratch, config.voice.banned_phrases, config.voice.emoji)


def _copy_dict(copy: PieceCopy) -> dict[str, Any]:
    return {
        "body": copy.body,
        "hook": copy.hook,
        "hashtags": [h if h.startswith("#") else f"#{h}" for h in copy.hashtags],
        "cta": copy.cta,
        "first_comment": copy.first_comment,
        "alternate_hooks": copy.alternate_hooks,
        "slides": [s.model_dump() for s in copy.slides],
        "video": copy.video.model_dump() if copy.video else None,
        "visual": copy.visual.model_dump(),
        "rationale": copy.rationale,
    }


def write_copy(piece: ContentPiece) -> PieceCopy:
    """Text for the piece, with one repair pass when it reads as machine written."""
    feedback = str(piece.ai_state.get("feedback", ""))
    copy = _call(piece, feedback)
    for _ in range(VOICE_RETRIES):
        found = _findings(piece, copy)
        if not found:
            break
        draft = piece.copy
        piece.copy = _copy_dict(copy)
        try:
            copy = _call(piece, voice.feedback(found))
        finally:
            piece.copy = draft
    return copy


def _apply(piece: ContentPiece, copy: PieceCopy) -> None:
    piece.title = copy.title[:200]
    piece.copy = _copy_dict(copy)
    piece.ai_state = {**piece.ai_state, "feedback": ""}


def create(
    brand: Brand,
    user_id: uuid.UUID | None,
    *,
    channel: str,
    kind: str,
    brief: str = "",
    idea_id: uuid.UUID | None = None,
    pillar: str = "",
    scheduled_at: Any = None,
    origin: str = "user",
) -> ContentPiece:
    get_kind(channel, kind)
    text = brief.strip()
    if len(text) > MAX_BRIEF:
        raise ValidationFailedError("the brief is too long")
    if not text and idea_id is None:
        raise ValidationFailedError("describe what the post is about")
    piece = ContentPiece.objects.create(
        workspace_id=brand.workspace_id,
        brand=brand,
        idea_id=idea_id,
        channel=channel,
        kind=kind,
        status=PieceStatus.DRAFTING,
        brief=text,
        pillar=pillar[:40],
        scheduled_at=scheduled_at,
        origin=origin,
        created_by_id=user_id,
        ai_status="thinking",
        ai_since=timezone.now(),
    )
    family = design.pick(brand.pk, channel, piece.pk)
    piece.ai_state = {**piece.ai_state, design.KEY: family.name}
    piece.save(update_fields=["ai_state", "updated_at"])
    enqueue(piece)
    return piece


def revise(piece: ContentPiece, feedback: str) -> ContentPiece:
    text = feedback.strip()
    if not text or len(text) > MAX_FEEDBACK:
        raise ValidationFailedError("write what should change (up to 1000 characters)")
    if piece.status not in EDITABLE:
        raise ValidationFailedError("this post can no longer be changed")
    piece.ai_state = {**piece.ai_state, "feedback": text}
    piece.ai_status = "thinking"
    piece.ai_since = timezone.now()
    piece.ai_error_code = ""
    piece.status = PieceStatus.DRAFTING
    _reset_approval(piece)
    piece.save()
    enqueue(piece)
    return piece


def _reset_approval(piece: ContentPiece) -> None:
    piece.approved_by = None
    piece.approved_at = None


def refresh_status(piece: ContentPiece) -> ContentPiece:
    """A job that has been 'thinking' for too long is marked failed so the screen stops waiting."""
    if (
        piece.ai_status == "thinking"
        and piece.ai_since
        and timezone.now() - piece.ai_since > STUCK_AFTER
    ):
        piece.ai_status = "failed"
        piece.ai_error_code = ErrorCode.PROVIDER_UNAVAILABLE.value
        piece.status = PieceStatus.FAILED
        piece.save(update_fields=["ai_status", "ai_error_code", "status", "updated_at"])
    return piece


def run_checks(piece: ContentPiece) -> list[dict[str, Any]]:
    config = config_store.load(piece.brand_id)
    findings = checks.voice_findings(piece, config.voice.banned_phrases, config.voice.emoji)
    return checks.run(piece, findings)


def make_visuals(piece: ContentPiece, copy: PieceCopy) -> None:
    """Pictures for the piece; a video the person attached is kept."""
    kind = get_kind(piece.channel, piece.kind)
    if kind.needs_video and _has_own_video(piece):
        return
    outcome = director.make(
        piece,
        copy,
        piece.created_by_id,
        previous_video=_generated_video(piece),
    )
    if outcome.media or not kind.min_media:
        piece.media = outcome.media
    if outcome.cover_asset_id:
        piece.cover_asset_id = outcome.cover_asset_id
    piece.ai_state = {
        **piece.ai_state,
        "visual_errors": outcome.errors,
        "video_asset_id": str(outcome.video_asset_id or ""),
        "image_trace": outcome.trace,
    }


def _generated_video(piece: ContentPiece) -> uuid.UUID | None:
    raw = piece.ai_state.get("video_asset_id")
    return uuid.UUID(str(raw)) if raw else None


def _has_own_video(piece: ContentPiece) -> bool:
    if not piece.media:
        return False
    return str(piece.media[0].get("asset_id")) != str(piece.ai_state.get("video_asset_id", ""))


def run(piece_id: uuid.UUID) -> str:
    piece = ContentPiece.objects.select_related("idea", "brand").filter(pk=piece_id).first()
    if piece is None or piece.ai_status != "thinking":
        return "skipped"
    try:
        copy = write_copy(piece)
    except DomainError as exc:
        return _fail(piece, exc.code.value)
    except Exception:
        log.exception("studio_write_crashed")
        return _fail(piece, ErrorCode.UNKNOWN.value)
    _apply(piece, copy)
    make_visuals(piece, copy)
    piece.checks = run_checks(piece)
    piece.status = PieceStatus.DRAFT
    piece.ai_status = "done"
    piece.ai_since = None
    piece.save()
    from studio import automation

    automation.after_written(piece)
    return "done"


def _fail(piece: ContentPiece, code: str) -> str:
    piece.ai_status = "failed"
    piece.ai_error_code = code
    piece.ai_since = None
    piece.status = PieceStatus.FAILED
    piece.save(update_fields=["ai_status", "ai_error_code", "ai_since", "status", "updated_at"])
    return "failed"


def redraw(piece: ContentPiece, user_id: uuid.UUID | None) -> ContentPiece:
    """Make the pictures again from the current text (for example after changing the sources)."""
    if piece.status not in EDITABLE or not piece.copy.get("body", piece.copy.get("visual")):
        raise ValidationFailedError("write the post first")
    copy = PieceCopy.model_validate(_for_model(piece.copy, piece.title))
    piece.created_by_id = piece.created_by_id or user_id
    make_visuals(piece, copy)
    piece.checks = run_checks(piece)
    _reset_approval(piece)
    if piece.status == PieceStatus.SCHEDULED:
        piece.status = PieceStatus.DRAFT
    piece.save()
    return piece


def _for_model(copy: dict[str, Any], title: str) -> dict[str, Any]:
    return {
        "title": title or "Post",
        "body": copy.get("body", ""),
        "hook": copy.get("hook", ""),
        "hashtags": copy.get("hashtags", []),
        "cta": copy.get("cta", ""),
        "first_comment": copy.get("first_comment", ""),
        "alternate_hooks": copy.get("alternate_hooks", []),
        "slides": copy.get("slides", []),
        "video": copy.get("video"),
        "visual": copy.get("visual")
        or {
            "scene": title or "A real moment",
            "shot": "natural light",
            "alt_text": title or "Picture",
            "library_keywords": [],
        },
        "rationale": copy.get("rationale", ""),
    }
