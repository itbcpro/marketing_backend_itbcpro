"""Automatic checks of a post before it can go out.

Each check returns a machine-readable result (``code``, ``severity`` and parameters); the screen
turns them into sentences. ``error`` blocks publishing, ``warning`` advises, ``info`` informs.
"""

import re
import uuid
from typing import Any

from actions.models import Guardrail
from media.models import MediaAsset, MediaKind
from media.specs import SPEC_BY_KEY, check_asset
from social.models import MediaKind as PostKind
from social.models import PostDraft

MAX_CAPTION = 2200
MAX_HASHTAGS = 30
RECOMMENDED_HASHTAGS = 10
MAX_MENTIONS = 20
HOOK_CHARS = 125
CAROUSEL_MIN, CAROUSEL_MAX = 2, 10
REEL_MIN_SECONDS, REEL_MAX_SECONDS = 3, 90
MIN_FEED_RATIO, MAX_FEED_RATIO = 0.8, 1.91
STORY_RATIO = 9 / 16
RATIO_TOLERANCE = 0.03
HASHTAG = re.compile(r"(?<!\w)#\w+")
MENTION = re.compile(r"(?<!\w)@\w+")
LINK = re.compile(r"https?://\S+")


def _result(code: str, severity: str, **params: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "params": params}


def blocked_terms(workspace_id: uuid.UUID) -> list[str]:
    guardrail = Guardrail.objects.filter(
        workspace_id=workspace_id, key="blocked_terms", enabled=True
    ).first()
    return [str(t) for t in (guardrail.config.get("terms", []) if guardrail else []) if t]


def _caption_checks(draft: PostDraft) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    caption = draft.caption.strip()
    full = f"{caption} {' '.join(draft.hashtags)}"
    if not caption and draft.kind != PostKind.STORY:
        results.append(_result("caption_empty", "warning"))
    if len(caption) > MAX_CAPTION:
        results.append(_result("caption_too_long", "error", length=len(caption), max=MAX_CAPTION))
    first_line = caption.split("\n", 1)[0] if caption else ""
    if len(first_line) > HOOK_CHARS:
        results.append(_result("hook_too_long", "warning", length=len(first_line), max=HOOK_CHARS))
    tags = len(set(HASHTAG.findall(full.lower())))
    if tags > MAX_HASHTAGS:
        results.append(_result("hashtags_too_many", "error", count=tags, max=MAX_HASHTAGS))
    elif tags > RECOMMENDED_HASHTAGS:
        results.append(
            _result("hashtags_many", "warning", count=tags, recommended=RECOMMENDED_HASHTAGS)
        )
    if len(MENTION.findall(caption)) > MAX_MENTIONS:
        results.append(_result("mentions_too_many", "error", max=MAX_MENTIONS))
    if LINK.search(caption):
        results.append(_result("link_in_caption", "info"))
    blocked = [t for t in blocked_terms(draft.workspace_id) if t.lower() in full.lower()]
    if blocked:
        results.append(_result("blocked_terms", "error", terms=blocked))
    return results


def _assets(draft: PostDraft) -> list[MediaAsset]:
    ids = [m.get("asset_id") for m in draft.media if m.get("asset_id")]
    found = {
        str(a.pk): a for a in MediaAsset.objects.filter(workspace_id=draft.workspace_id, pk__in=ids)
    }
    return [found[str(i)] for i in ids if str(i) in found]


def _ratio_check(draft: PostDraft, asset: MediaAsset) -> dict[str, Any] | None:
    if not asset.width or not asset.height:
        return None
    ratio = asset.width / asset.height
    if draft.kind in {PostKind.REEL, PostKind.STORY}:
        if abs(ratio - STORY_RATIO) / STORY_RATIO > RATIO_TOLERANCE:
            return _result("ratio_vertical", "warning", ratio=round(ratio, 3), wanted="9:16")
        return None
    if not MIN_FEED_RATIO - 0.01 <= ratio <= MAX_FEED_RATIO + 0.01:
        return _result("ratio_feed", "error", ratio=round(ratio, 3), wanted="4:5 to 1.91:1")
    return None


def _media_checks(draft: PostDraft) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    assets = _assets(draft)
    if not assets:
        return [_result("no_media", "error")]
    if len(assets) != len(draft.media):
        results.append(_result("media_missing", "error"))
    if draft.kind == PostKind.CAROUSEL and not CAROUSEL_MIN <= len(assets) <= CAROUSEL_MAX:
        results.append(_result("carousel_count", "error", min=CAROUSEL_MIN, max=CAROUSEL_MAX))
    if draft.kind in {PostKind.IMAGE, PostKind.REEL, PostKind.STORY} and len(assets) > 1:
        results.append(_result("single_media_only", "error"))
    for asset in assets:
        results.extend(_asset_checks(draft, asset))
    if any(a.ai_generated for a in assets):
        results.append(_result("ai_generated", "info"))
    missing_alt = [
        m
        for m, a in zip(draft.media, assets, strict=False)
        if a.kind == MediaKind.IMAGE and not (m.get("alt_text") or a.alt_text)
    ]
    if missing_alt:
        results.append(_result("alt_missing", "warning", count=len(missing_alt)))
    return results


def _asset_checks(draft: PostDraft, asset: MediaAsset) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    expects_video = draft.kind == PostKind.REEL
    if expects_video and asset.kind != MediaKind.VIDEO:
        results.append(_result("reel_needs_video", "error"))
    if asset.kind == MediaKind.VIDEO and asset.youtube_video_id:
        results.append(_result("video_no_file", "error"))
    if asset.kind == MediaKind.VIDEO and asset.duration_seconds is not None:
        seconds = asset.duration_seconds
        if draft.kind == PostKind.REEL and seconds < REEL_MIN_SECONDS:
            results.append(_result("reel_too_short", "error", minimum=REEL_MIN_SECONDS))
        if draft.kind == PostKind.REEL and seconds > REEL_MAX_SECONDS:
            results.append(_result("reel_long", "warning", recommended=REEL_MAX_SECONDS))
    ratio = _ratio_check(draft, asset)
    if ratio:
        results.append(ratio)
    if asset.kind == MediaKind.IMAGE:
        spec = SPEC_BY_KEY["meta_portrait"]
        size = check_asset(asset, spec)
        if any(r["code"] == "too_small" for r in size["reasons"]):
            results.append(
                _result("image_small", "warning", width=asset.width, height=asset.height)
            )
    return results


def run_checks(draft: PostDraft) -> list[dict[str, Any]]:
    """All checks for a draft, errors first."""
    results = [*_caption_checks(draft), *_media_checks(draft)]
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(results, key=lambda r: order[r["severity"]])


def has_errors(results: list[dict[str, Any]]) -> bool:
    return any(r["severity"] == "error" for r in results)
