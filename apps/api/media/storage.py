"""Private file storage behind a small port; the local-disk adapter is the only one for now."""

import hashlib
import os
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO, Protocol

from django.conf import settings


class MediaStorage(Protocol):
    def save_stream(
        self, workspace_id: uuid.UUID, chunks: Iterable[bytes], *, extension: str, max_bytes: int
    ) -> tuple[str, str, int]:
        """Store a stream; return ``(key, sha256, size)``. Raises ``OverflowError`` past the cap."""
        ...

    def open(self, key: str) -> BinaryIO: ...

    def path(self, key: str) -> Path: ...

    def delete(self, key: str) -> None: ...


class LocalMediaStorage:
    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or settings.MEDIA_ROOT).resolve()

    def path(self, key: str) -> Path:
        target = (self._root / key).resolve()
        if self._root not in target.parents:
            raise ValueError("storage key escapes the storage root")
        return target

    def save_stream(
        self, workspace_id: uuid.UUID, chunks: Iterable[bytes], *, extension: str, max_bytes: int
    ) -> tuple[str, str, int]:
        folder = self._root / str(workspace_id) / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f"{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp.open("wb") as handle:
                for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise OverflowError("file is too large")
                    digest.update(chunk)
                    handle.write(chunk)
            sha = digest.hexdigest()
            key = f"{workspace_id}/{sha[:2]}/{sha}{extension}"
            final = self.path(key)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp, final)
            return key, sha, size
        finally:
            temp.unlink(missing_ok=True)

    def open(self, key: str) -> BinaryIO:
        return self.path(key).open("rb")

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


def get_storage() -> MediaStorage:
    return LocalMediaStorage()
