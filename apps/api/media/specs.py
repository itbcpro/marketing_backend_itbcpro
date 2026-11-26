"""Destination specs as data: what each channel accepts for an image or a video.

A check returns machine-readable reasons; the screen turns them into sentences. Ratios use a
small tolerance because real files are rarely pixel-exact.
"""

from dataclasses import dataclass
from typing import Any

from media.models import MediaAsset, MediaKind

RATIO_TOLERANCE = 0.01
KB = 1024


@dataclass(frozen=True, slots=True)
class Spec:
    key: str
    label: str
    platform: str
    kind: str
    ratio: tuple[int, int] | None = None
    min_size: tuple[int, int] | None = None
    recommended: tuple[int, int] | None = None
    max_bytes: int | None = None
    formats: frozenset[str] = frozenset()
    min_seconds: float | None = None
    needs_youtube: bool = False
    note: str = ""


IMG_GOOGLE = frozenset({"JPEG", "PNG", "GIF"})
IMG_OPENAI = frozenset({"JPEG", "PNG", "WEBP"})
IMG_META = frozenset({"JPEG", "PNG"})
GOOGLE_MAX = 5120 * KB

SPECS: tuple[Spec, ...] = (
    Spec("google_landscape", "Google landscape image 1.91:1", "google", MediaKind.IMAGE, (191, 100), (600, 314), (1200, 628), GOOGLE_MAX, IMG_GOOGLE),
    Spec("google_square", "Google square image 1:1", "google", MediaKind.IMAGE, (1, 1), (300, 300), (1200, 1200), GOOGLE_MAX, IMG_GOOGLE),
    Spec("google_portrait", "Google portrait image 4:5", "google", MediaKind.IMAGE, (4, 5), (480, 600), (960, 1200), GOOGLE_MAX, IMG_GOOGLE),
    Spec("google_logo_square", "Google logo 1:1", "google", MediaKind.IMAGE, (1, 1), (128, 128), (1200, 1200), GOOGLE_MAX, IMG_GOOGLE),
    Spec("google_logo_landscape", "Google logo 4:1", "google", MediaKind.IMAGE, (4, 1), (512, 128), (1200, 300), GOOGLE_MAX, IMG_GOOGLE),
    Spec("google_video", "Google video (YouTube)", "google", MediaKind.VIDEO, min_seconds=10.0, needs_youtube=True),
    Spec("openai_chat_card", "OpenAI chat card image", "openai", MediaKind.IMAGE, None, (640, 640), (1024, 1024), 16 * 1024 * KB, IMG_OPENAI),
    Spec("meta_square", "Meta feed image 1:1", "meta", MediaKind.IMAGE, (1, 1), (600, 600), (1080, 1080), 30 * 1024 * KB, IMG_META),
    Spec("meta_portrait", "Meta feed image 4:5", "meta", MediaKind.IMAGE, (4, 5), (600, 750), (1080, 1350), 30 * 1024 * KB, IMG_META),
    Spec("meta_story", "Meta story or reel image 9:16", "meta", MediaKind.IMAGE, (9, 16), (540, 960), (1080, 1920), 30 * 1024 * KB, IMG_META),
    Spec("meta_landscape", "Meta link image 1.91:1", "meta", MediaKind.IMAGE, (191, 100), (600, 314), (1200, 628), 30 * 1024 * KB, IMG_META),
)  # fmt: skip

SPEC_BY_KEY = {spec.key: spec for spec in SPECS}
# Ratios a derived crop can be made for (logos and the OpenAI image are cropped the same way).
CROPPABLE = tuple(spec for spec in SPECS if spec.kind == MediaKind.IMAGE and spec.recommended)


def spec_as_dict(spec: Spec) -> dict[str, Any]:
    return {
        "key": spec.key,
        "label": spec.label,
        "platform": spec.platform,
        "kind": spec.kind,
        "ratio": list(spec.ratio) if spec.ratio else None,
        "min_size": list(spec.min_size) if spec.min_size else None,
        "recommended": list(spec.recommended) if spec.recommended else None,
        "max_kb": spec.max_bytes // KB if spec.max_bytes else None,
        "croppable": spec in CROPPABLE and spec.ratio is not None,
    }


def _reason(code: str, **params: Any) -> dict[str, Any]:
    return {"code": code, "params": params}


def check_asset(asset: MediaAsset, spec: Spec) -> dict[str, Any]:  # noqa: C901 - one flat checklist
    """Whether the asset fits the spec as it is, with the reasons when it does not."""
    reasons: list[dict[str, Any]] = []
    if asset.kind != spec.kind:
        return {"spec": spec.key, "ok": False, "reasons": [_reason("wrong_kind", want=spec.kind)]}
    if spec.kind == MediaKind.VIDEO:
        if spec.needs_youtube and not asset.youtube_video_id:
            reasons.append(_reason("needs_youtube"))
        if (
            spec.min_seconds
            and asset.duration_seconds
            and asset.duration_seconds < spec.min_seconds
        ):
            reasons.append(_reason("too_short", minimum=spec.min_seconds))
        return {"spec": spec.key, "ok": not reasons, "reasons": reasons}
    width, height = asset.width or 0, asset.height or 0
    if spec.formats and asset.file_format not in spec.formats:
        reasons.append(_reason("format", allowed=", ".join(sorted(spec.formats))))
    if spec.min_size and (width < spec.min_size[0] or height < spec.min_size[1]):
        reasons.append(
            _reason(
                "too_small",
                width=width,
                height=height,
                min_width=spec.min_size[0],
                min_height=spec.min_size[1],
            )
        )
    if spec.ratio and width and height:
        want = spec.ratio[0] / spec.ratio[1]
        got = width / height
        if abs(got - want) / want > RATIO_TOLERANCE:
            reasons.append(
                _reason("wrong_ratio", want=f"{spec.ratio[0]}:{spec.ratio[1]}", got=round(got, 3))
            )
    if spec.max_bytes and asset.size_bytes > spec.max_bytes:
        reasons.append(_reason("too_heavy", max_kb=spec.max_bytes // KB, kb=asset.size_bytes // KB))
    if asset.animated and spec.platform == "google":
        reasons.append(_reason("animated"))
    return {"spec": spec.key, "ok": not reasons, "reasons": reasons}


def check_all(asset: MediaAsset) -> list[dict[str, Any]]:
    return [check_asset(asset, spec) for spec in SPECS if spec.kind == asset.kind]
