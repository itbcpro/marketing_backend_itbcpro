"""Reel videos drawn locally from the AI's reel plan: branded text scenes, fades and a progress bar.

No video vendor is needed. Every scene is a branded text card (exact text, AA contrast, kept
inside Instagram's safe zones), joined with ffmpeg into a 1080x1920 H.264 MP4 with a silent audio
track, which is what Instagram expects for a Reel. The result is stored as a normal media asset so
the usual checks, approval and publishing flow apply to it unchanged.
"""

import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]

from core.errors import RetryableProviderError, ValidationFailedError
from core.logging import get_logger
from media import services as media_services
from media.models import MediaAsset, MediaSource
from social import images
from social.schemas import ReelPlan

log = get_logger(__name__)

SIZE = images.CARD_SIZES["story"]
FPS = 30
MAX_SCENES = 8
MIN_SCENE, MAX_SCENE, HOOK_SCENE = 2.0, 5.0, 2.5
FADE = 0.3
BAR_HEIGHT = 10
BAR_FROM_BOTTOM = 420  # above the area Instagram covers with its own buttons and caption
RENDER_TIMEOUT = 240


@dataclass(frozen=True, slots=True)
class Scene:
    headline: str
    body: str
    seconds: float


def scenes_for(plan: ReelPlan) -> list[Scene]:
    """Hook, then one scene per on-screen line, then the call to action.

    Beats and the shot list are notes for whoever films the reel, so viewers never see them;
    they are only used as scene text when the plan has no on-screen lines at all.
    """
    lines = [t.strip() for t in plan.on_screen_text if t.strip()]
    if not lines:
        lines = [b.strip() for b in plan.beats if b.strip()]
    texts = [plan.hook.strip(), *lines]
    if plan.cta.strip():
        texts.append(plan.cta.strip())
    texts = texts[:MAX_SCENES]
    rest = max(plan.duration_seconds - HOOK_SCENE, MIN_SCENE)
    each = min(max(rest / max(len(texts) - 1, 1), MIN_SCENE), MAX_SCENE)
    return [
        Scene(headline, "", HOOK_SCENE if index == 0 else each)
        for index, headline in enumerate(texts)
    ]


def _filter(scenes: list[Scene], accent: str) -> tuple[str, float]:
    total = sum(s.seconds for s in scenes)
    parts: list[str] = []
    for index, scene in enumerate(scenes):
        out = max(scene.seconds - FADE, 0)
        parts.append(
            f"[{index}:v]scale={SIZE[0]}:{SIZE[1]},setsar=1,format=yuv420p,"
            f"fade=t=in:st=0:d={FADE},fade=t=out:st={out}:d={FADE}[v{index}]"
        )
    joined = "".join(f"[v{i}]" for i in range(len(scenes)))
    parts.append(f"{joined}concat=n={len(scenes)}:v=1:a=0[joined]")
    colour = accent.lstrip("#")
    parts.append(f"color=c=0x{colour}:s={SIZE[0]}x{BAR_HEIGHT}:r={FPS}:d={total}[bar]")
    parts.append(
        f"[joined][bar]overlay=x='-w+W*t/{total}':y=H-{BAR_FROM_BOTTOM},format=yuv420p[out]"
    )
    return ";".join(parts), total


def render(plan: ReelPlan, palette: images.Palette) -> bytes:
    """The reel as MP4 bytes."""
    scenes = scenes_for(plan)
    if not scenes[0].headline.strip():
        raise ValidationFailedError("the reel plan has no hook to show")
    with tempfile.TemporaryDirectory(prefix="reel-") as folder:
        root = Path(folder)
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error"]
        for index, scene in enumerate(scenes):
            card = images.render_card(scene.headline, scene.body, palette=palette, size=SIZE)
            (root / f"scene{index}.png").write_bytes(card)
            command += [
                "-framerate", str(FPS), "-loop", "1", "-t", str(scene.seconds),
                "-i", str(root / f"scene{index}.png"),
            ]  # fmt: skip
        graph, total = _filter(scenes, palette.accent)
        command += [
            "-f",
            "lavfi",
            "-t",
            str(total),
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        output = root / "reel.mp4"
        command += [
            "-filter_complex", graph, "-map", "[out]", "-map", f"{len(scenes)}:a",
            "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-shortest",
            "-movflags", "+faststart", str(output),
        ]  # fmt: skip
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=RENDER_TIMEOUT)  # noqa: S603
        except subprocess.CalledProcessError as exc:
            log.warning("social_video_failed", detail=exc.stderr.decode("utf-8", "replace")[-400:])
            raise RetryableProviderError(
                "the video could not be rendered", provider="ffmpeg"
            ) from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RetryableProviderError(
                "the video could not be rendered", provider="ffmpeg"
            ) from exc
        return output.read_bytes()


def store_reel(
    workspace_id: uuid.UUID, user_id: uuid.UUID | None, data: bytes, *, name: str
) -> MediaAsset:
    asset, _ = media_services.create_from_chunks(
        workspace_id,
        user_id,
        [data],
        filename=f"{name}.mp4",
        source=MediaSource.GENERATED,
        title=name,
    )
    return asset


__all__ = ["Scene", "render", "scenes_for", "store_reel"]
