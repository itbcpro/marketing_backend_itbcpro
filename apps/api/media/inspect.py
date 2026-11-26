"""Read facts about an uploaded file. Images are decoded; MP4/MOV headers are parsed directly."""

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

from core.errors import ValidationFailedError

MAX_PIXELS = 64_000_000
IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"
BOX_HEADER = 8
LARGE_SIZE_MARKER = 1
LARGE_SIZE_BYTES = 8
MVHD_V1_MIN_BYTES = 32
MVHD_V0_MIN_BYTES = 20
FIXED_POINT = 65536
MIN_BOX_PAYLOAD = 12


@dataclass(frozen=True, slots=True)
class ImageFacts:
    file_format: str
    mime_type: str
    extension: str
    width: int
    height: int
    animated: bool
    has_alpha: bool


@dataclass(frozen=True, slots=True)
class VideoFacts:
    file_format: str
    mime_type: str
    extension: str
    width: int | None
    height: int | None
    duration_seconds: float | None


def inspect_image(path: Path) -> ImageFacts:
    """Decode the file. Anything that is not a real JPEG, PNG, WebP or GIF is refused."""
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            file_format = image.format or ""
            frames = getattr(image, "n_frames", 1)
            transposed = ImageOps.exif_transpose(image)
            width, height = transposed.size
            has_alpha = "A" in image.getbands() or "transparency" in image.info
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValidationFailedError("the file is not a valid image") from exc
    if file_format not in IMAGE_FORMATS:
        raise ValidationFailedError("use a JPEG, PNG, WebP or GIF image")
    if width * height > MAX_PIXELS:
        raise ValidationFailedError("the image is too large to process")
    mime, extension = IMAGE_FORMATS[file_format]
    return ImageFacts(file_format, mime, extension, width, height, frames > 1, has_alpha)


def _boxes(handle: BinaryIO, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield ``(type, payload_start, payload_end)`` for the boxes between two offsets."""
    position = start
    while position + BOX_HEADER <= end:
        handle.seek(position)
        header = handle.read(BOX_HEADER)
        if len(header) < BOX_HEADER:
            return
        size, box_type = struct.unpack(">I4s", header)
        payload = position + BOX_HEADER
        if size == LARGE_SIZE_MARKER:
            large = handle.read(LARGE_SIZE_BYTES)
            if len(large) < LARGE_SIZE_BYTES:
                return
            size = struct.unpack(">Q", large)[0]
            payload += 8
        elif size == 0:
            size = end - position
        if size < BOX_HEADER or position + size > end:
            return
        yield box_type, payload, position + size
        position += size


def _video_track_size(handle: BinaryIO, moov: tuple[int, int]) -> tuple[int, int] | None:
    for box_type, start, end in _boxes(handle, *moov):
        if box_type != b"trak":
            continue
        for inner_type, inner_start, inner_end in _boxes(handle, start, end):
            if inner_type != b"tkhd":
                continue
            handle.seek(inner_start)
            data = handle.read(inner_end - inner_start)
            version = data[0] if data else 0
            offset = 88 if version == 1 else 76
            if len(data) >= offset + 8:
                width, height = struct.unpack(">II", data[offset : offset + 8])
                if width and height:
                    return width // FIXED_POINT, height // FIXED_POINT
    return None


def _movie_duration(handle: BinaryIO, moov: tuple[int, int]) -> float | None:
    for box_type, start, end in _boxes(handle, *moov):
        if box_type != b"mvhd":
            continue
        handle.seek(start)
        data = handle.read(end - start)
        if not data:
            return None
        if data[0] == 1 and len(data) >= MVHD_V1_MIN_BYTES:
            timescale, duration = struct.unpack(">IQ", data[20:32])
        elif len(data) >= MVHD_V0_MIN_BYTES:
            timescale, duration = struct.unpack(">II", data[12:20])
        else:
            return None
        return round(duration / timescale, 2) if timescale else None
    return None


def inspect_video(path: Path) -> VideoFacts:
    """Accept MP4/MOV (header parsed here) and WebM (no header parsing)."""
    with path.open("rb") as handle:
        head = handle.read(12)
        if head.startswith(WEBM_MAGIC):
            return VideoFacts("WEBM", "video/webm", ".webm", None, None, None)
        if len(head) < MIN_BOX_PAYLOAD or head[4:8] != b"ftyp":
            raise ValidationFailedError("use an MP4, MOV or WebM video")
        brand = head[8:12]
        is_mov = brand.startswith(b"qt")
        size = path.stat().st_size
        moov: tuple[int, int] | None = None
        for box_type, start, end in _boxes(handle, 0, size):
            if box_type == b"moov":
                moov = (start, end)
                break
        if moov is None:
            raise ValidationFailedError("the video has no readable header")
        duration = _movie_duration(handle, moov)
        dims = _video_track_size(handle, moov)
    return VideoFacts(
        "MOV" if is_mov else "MP4",
        "video/quicktime" if is_mov else "video/mp4",
        ".mov" if is_mov else ".mp4",
        dims[0] if dims else None,
        dims[1] if dims else None,
        duration,
    )
