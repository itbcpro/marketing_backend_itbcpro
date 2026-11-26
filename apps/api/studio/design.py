"""Keep the pictures of a channel from all looking the same.

Each new post is handed a *design family* (a distinct layout and look from an editable list)
that was not used by the latest posts of the channel. The writer adapts it to the idea and the
brand and may refine it, but never has to invent variety on its own. The list is a data file;
adding a family is adding an entry to it.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from studio.models import ContentPiece, PieceStatus

DATA = Path(__file__).parent / "data" / "design_families.json"
MEMORY = 10
KEY = "design_family"


@dataclass(frozen=True, slots=True)
class Family:
    name: str
    brief: str


@lru_cache(maxsize=1)
def families() -> tuple[Family, ...]:
    return tuple(Family(**item) for item in json.loads(DATA.read_text(encoding="utf-8")))


def _recent(brand_id: uuid.UUID, channel: str) -> list[str]:
    rows = (
        ContentPiece.objects.filter(brand_id=brand_id, channel=channel)
        .exclude(status=PieceStatus.CANCELLED)
        .order_by("-created_at")[:MEMORY]
    )
    return [str(p.ai_state.get(KEY, "")) for p in rows]


def pick(brand_id: uuid.UUID, channel: str, key: uuid.UUID) -> Family:
    """A family that the channel has not used lately (the least recently used if all were)."""
    recent = _recent(brand_id, channel)
    everything = list(families())
    unused = [f for f in everything if f.name not in recent]
    pool = unused or sorted(everything, key=lambda f: _last_seen(f.name, recent))[:3]
    digest = hashlib.sha1(key.bytes).digest()[0]  # noqa: S324 - spreads choices, not security
    return pool[digest % len(pool)]


def _last_seen(name: str, recent: list[str]) -> int:
    """Position in the recent list (0 = newest); a family not listed counts as oldest."""
    return -recent.index(name) if name in recent else -len(recent) - 1


def find(name: str) -> Family | None:
    return next((f for f in families() if f.name == name), None)


def describe(name: str) -> str:
    """The text handed to the writer for the family chosen for a post."""
    family = find(name)
    return f"{family.name}: {family.brief}" if family else "none chosen: use your own judgement"
