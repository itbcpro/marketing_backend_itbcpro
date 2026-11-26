"""Fill the calendar: find free slots, make sure there are ideas, assign and start writing."""

import math
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from core.errors import DomainError
from core.logging import get_logger
from studio import clock, config_store, strategist, writer
from studio.channels.registry import get_channel
from studio.models import Brand, ContentIdea, ContentPiece, IdeaStatus, PieceStatus
from studio.slots import Assignment, History, IdeaView, assign, open_slots

log = get_logger(__name__)

LIVE = (
    PieceStatus.DRAFTING,
    PieceStatus.DRAFT,
    PieceStatus.SCHEDULED,
    PieceStatus.PUBLISHING,
    PieceStatus.MANUAL_DUE,
    PieceStatus.PUBLISHED,
)
HISTORY_LIMIT = 40
MAX_NEW_IDEAS = 20


@dataclass(slots=True)
class FillResult:
    slots: int = 0
    created: int = 0
    unfilled: int = 0
    ideas_made: int = 0
    idea_error: str = ""


def _history(brand_id: uuid.UUID) -> History:
    history = History()
    rows = ContentPiece.objects.filter(brand_id=brand_id, status__in=LIVE).order_by("-created_at")[
        :HISTORY_LIMIT
    ]
    for piece in rows:
        if piece.pillar:
            history.pillars[piece.pillar] += 1
        history.kinds.setdefault(piece.channel, Counter())[piece.kind] += 1
        if piece.idea_id:
            history.used.add((piece.idea_id, piece.channel))
    return history


def _taken(brand_id: uuid.UUID) -> list[tuple[str, datetime]]:
    rows = ContentPiece.objects.filter(
        brand_id=brand_id, status__in=LIVE, scheduled_at__isnull=False
    ).values_list("channel", "scheduled_at")
    return [(c, t) for c, t in rows if t is not None]


def _view(idea: ContentIdea) -> IdeaView:
    hints = tuple((str(h.get("channel", "")), str(h.get("kind", ""))) for h in idea.format_hints)
    return IdeaView(idea.pk, idea.pillar, idea.urgency, idea.score, idea.expires_at, hints)


def _capacity(
    ideas: list[ContentIdea], channels: list[str], used: set[tuple[uuid.UUID, str]]
) -> int:
    return sum(1 for i in ideas for c in channels if (i.pk, c) not in used)


def _ensure_ideas(
    brand: Brand,
    slots: int,
    buffer: int,
    history: History,
    channels: list[str],
    result: FillResult,
) -> list[ContentIdea]:
    ideas = strategist.usable_ideas(brand.pk)
    missing = slots - _capacity(ideas, channels, history.used)
    if missing <= 0:
        return ideas
    wanted = min(math.ceil(missing / max(len(channels), 1)) + buffer, MAX_NEW_IDEAS)
    try:
        made = strategist.generate(brand, wanted)
    except DomainError as exc:
        result.idea_error = exc.code.value
        return ideas
    result.ideas_made = len(made)
    return strategist.usable_ideas(brand.pk)


def _create(brand: Brand, item: Assignment, by_id: dict[uuid.UUID, ContentIdea]) -> ContentPiece:
    idea = by_id[item.idea_id]
    return writer.create(
        brand,
        None,
        channel=item.slot.channel,
        kind=item.kind,
        idea_id=idea.pk,
        pillar=item.pillar,
        scheduled_at=item.slot.at,
        origin="ai",
    )


def _close_used_ideas(brand_id: uuid.UUID, idea_ids: set[uuid.UUID], channels: list[str]) -> None:
    for idea in ContentIdea.objects.filter(brand_id=brand_id, pk__in=idea_ids):
        done = set(
            idea.pieces.exclude(status=PieceStatus.CANCELLED).values_list("channel", flat=True)
        )
        if set(channels) <= done:
            idea.status = IdeaStatus.USED
            idea.save(update_fields=["status", "updated_at"])


def fill_calendar(
    brand: Brand, *, now: datetime | None = None, horizon_days: int | None = None
) -> FillResult:
    """Create pieces (and ideas, when short of them) for every free slot in the horizon."""
    config = config_store.load(brand.pk)
    channels = config.enabled_channels()
    result = FillResult()
    if not channels:
        return result
    moment = now or timezone.now()
    horizon = horizon_days or config.automation.horizon_days
    slots = open_slots(config, clock.zone_of(brand.workspace_id), moment, horizon, _taken(brand.pk))
    result.slots = len(slots)
    if not slots:
        return result
    history = _history(brand.pk)
    ideas = _ensure_ideas(
        brand, len(slots), config.automation.idea_buffer, history, channels, result
    )
    supported = {c: get_channel(c).kind_names for c in channels}
    plan = assign(slots, [_view(i) for i in ideas], config, supported, history, moment)
    by_id = {i.pk: i for i in ideas}
    for item in plan:
        _create(brand, item, by_id)
    result.created = len(plan)
    result.unfilled = len(slots) - len(plan)
    _close_used_ideas(brand.pk, {a.idea_id for a in plan}, channels)
    return result
