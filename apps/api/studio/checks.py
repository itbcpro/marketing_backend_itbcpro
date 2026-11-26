"""Automatic checks of a piece against its channel's limits, plus the voice findings.

Each result is machine readable (``code``, ``severity``, ``params``); the screen turns it into a
sentence. ``error`` blocks approval, ``warning`` advises.
"""

import re
from typing import Any

from media.models import MediaAsset
from studio import config_store, voice
from studio.channels.registry import get_kind
from studio.models import ContentPiece
from studio.visuals import logo

HASHTAG = re.compile(r"(?<!\w)#\w+")
LINK = re.compile(r"https?://\S+")
MIN_BODY = 20


def _result(code: str, severity: str, **params: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "params": params}


def _text_checks(piece: ContentPiece) -> list[dict[str, Any]]:
    spec = get_kind(piece.channel, piece.kind)
    body = str(piece.copy.get("body", "")).strip()
    tags = list(piece.copy.get("hashtags", []))
    results: list[dict[str, Any]] = []
    if spec.max_chars and not body:
        results.append(_result("body_empty", "error"))
    elif spec.max_chars and len(body) < MIN_BODY:
        results.append(_result("body_short", "warning", length=len(body)))
    if spec.max_chars and len(body) > spec.max_chars:
        results.append(_result("body_too_long", "error", length=len(body), max=spec.max_chars))
    first = body.split("\n", 1)[0]
    if spec.hook_chars and len(first) > spec.hook_chars:
        results.append(_result("hook_too_long", "warning", length=len(first), max=spec.hook_chars))
    count = len({t.lower() for t in [*tags, *HASHTAG.findall(body)]})
    if spec.hashtags_max and count > spec.hashtags_max:
        results.append(_result("hashtags_too_many", "error", count=count, max=spec.hashtags_max))
    elif spec.hashtags_recommended and count > spec.hashtags_recommended:
        results.append(
            _result("hashtags_many", "warning", count=count, recommended=spec.hashtags_recommended)
        )
    if not spec.links_in_body and LINK.search(body):
        results.append(_result("link_in_body", "warning"))
    return results


def _logo_check(piece: ContentPiece) -> list[dict[str, Any]]:
    """A post whose pictures should carry the logo but none is set up cannot go out unseen."""
    visuals = config_store.load(piece.brand_id).visuals
    if not piece.media or not visuals.logo_on_pictures:
        return []
    return (
        []
        if logo.find(piece.workspace_id, visuals, piece.brand_id)
        else [_result("logo_missing", "warning")]
    )


def _media_checks(piece: ContentPiece) -> list[dict[str, Any]]:
    spec = get_kind(piece.channel, piece.kind)
    count = len(piece.media)
    results: list[dict[str, Any]] = []
    if count < spec.min_media:
        results.append(_result("media_missing", "error", have=count, need=spec.min_media))
    if spec.max_media and count > spec.max_media:
        results.append(_result("media_too_many", "error", have=count, max=spec.max_media))
    missing_alt = [m for m in piece.media if not str(m.get("alt_text", "")).strip()]
    if missing_alt and not spec.needs_video:
        results.append(_result("alt_missing", "warning", count=len(missing_alt)))
    generated = _generated_count(piece)
    if generated:
        results.append(_result("ai_generated_media", "warning", count=generated))
    return results


def _generated_count(piece: ContentPiece) -> int:
    """How many of the post's pictures were made by an image model (text cards are not)."""
    ids = {str(m.get("asset_id")) for m in piece.media}
    if piece.cover_asset_id:
        ids.add(str(piece.cover_asset_id))
    valid = [i for i in ids if i and i != "None"]
    if not valid:
        return 0
    return MediaAsset.objects.filter(pk__in=valid, ai_generated=True).count()


def voice_findings(piece: ContentPiece, banned: list[str], emoji: str) -> list[voice.Finding]:
    slides = [f"{s.get('headline', '')}. {s.get('body', '')}" for s in piece.copy.get("slides", [])]
    found = voice.lint(str(piece.copy.get("body", "")), banned=banned, emoji=emoji)
    seen = {(f.code, f.match) for f in found}
    extra = voice.lint_fragments(slides, banned=banned, emoji=emoji)
    return [*found, *(f for f in extra if (f.code, f.match) not in seen)]


def run(piece: ContentPiece, findings: list[voice.Finding]) -> list[dict[str, Any]]:
    voice_results = [_result(f"voice_{f.code}", f.severity, match=f.match) for f in findings]
    return [*_text_checks(piece), *_media_checks(piece), *_logo_check(piece), *voice_results]


def has_errors(results: list[dict[str, Any]]) -> bool:
    return any(r["severity"] == "error" for r in results)
