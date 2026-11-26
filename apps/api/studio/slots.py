"""Where posts go and which idea goes where: pure functions, no database, no clock of their own."""

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from studio.config import ChannelPlan, StudioConfig

MIN_LEAD = timedelta(hours=2)
CLASH_WINDOW = timedelta(minutes=90)
TIMELY_WINDOW_DAYS = 7.0
TIMELY_BONUS = 0.5
HINT_BONUS = 0.2
DEFICIT_WEIGHT = 1.0


@dataclass(frozen=True, slots=True)
class Slot:
    channel: str
    at: datetime


@dataclass(frozen=True, slots=True)
class IdeaView:
    id: uuid.UUID
    pillar: str
    urgency: str
    score: float
    expires_at: datetime | None
    hints: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Assignment:
    slot: Slot
    idea_id: uuid.UUID
    kind: str
    pillar: str


@dataclass(slots=True)
class History:
    """What the calendar already holds, used to keep the mix balanced."""

    pillars: Counter[str] = field(default_factory=Counter)
    kinds: dict[str, Counter[str]] = field(default_factory=dict)
    used: set[tuple[uuid.UUID, str]] = field(default_factory=set)


def weekly_pattern(plan: ChannelPlan) -> list[tuple[int, str]]:
    """The weekday and time of each weekly post, spread evenly over the allowed days and times."""
    pairs = sorted((d, t) for d in plan.days for t in plan.times)
    if not pairs or plan.posts_per_week == 0:
        return []
    count = min(plan.posts_per_week, len(pairs))
    return [pairs[(i * len(pairs)) // count] for i in range(count)]


def _clashes(channel: str, at: datetime, taken: Iterable[tuple[str, datetime]]) -> bool:
    return any(c == channel and abs(t - at) < CLASH_WINDOW for c, t in taken)


def open_slots(
    config: StudioConfig,
    zone: ZoneInfo,
    now: datetime,
    horizon_days: int,
    taken: Iterable[tuple[str, datetime]],
) -> list[Slot]:
    """Free posting times in the next ``horizon_days`` days for every enabled channel."""
    held = list(taken)
    first_day = now.astimezone(zone).date()
    slots: list[Slot] = []
    for channel in config.enabled_channels():
        pattern = weekly_pattern(config.plan_for(channel))
        for offset in range(horizon_days + 1):
            day = first_day + timedelta(days=offset)
            for weekday, clock in pattern:
                if day.weekday() != weekday:
                    continue
                hour, minute = (int(x) for x in clock.split(":"))
                at = datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(UTC)
                if at >= now + MIN_LEAD and not _clashes(channel, at, held):
                    slots.append(Slot(channel, at))
    return sorted(slots, key=lambda s: (s.at, s.channel))


def _targets(config: StudioConfig) -> dict[str, float]:
    total = sum(p.weight for p in config.pillars) or 1
    return {p.name: p.weight / total for p in config.pillars}


def _pillar_bonus(idea: IdeaView, targets: dict[str, float], counts: Counter[str]) -> float:
    if idea.pillar not in targets:
        return 0.0
    placed = sum(counts.values())
    actual = counts[idea.pillar] / (placed + 1)
    return (targets[idea.pillar] - actual) * DEFICIT_WEIGHT


def _timing_bonus(idea: IdeaView, slot: Slot, now: datetime) -> float:
    if idea.urgency != "timely":
        return 0.0
    days = max((slot.at - now).total_seconds() / 86400, 0.0)
    return TIMELY_BONUS * max(1 - days / TIMELY_WINDOW_DAYS, 0.0)


def _allowed_kinds(plan: ChannelPlan, supported: list[str]) -> list[str]:
    chosen = [k for k in plan.kinds if k in supported]
    return chosen or supported


def _pick_kind(idea: IdeaView, slot: Slot, allowed: list[str], usage: Counter[str]) -> str:
    for channel, kind in idea.hints:
        if channel == slot.channel and kind in allowed:
            return kind
    return min(allowed, key=lambda k: (usage[k], allowed.index(k)))


def assign(
    slots: list[Slot],
    ideas: list[IdeaView],
    config: StudioConfig,
    supported: dict[str, list[str]],
    history: History,
    now: datetime,
) -> list[Assignment]:
    """Give each slot, in time order, the idea that fits it best. Slots with no fit stay empty."""
    targets = _targets(config)
    counts = Counter(history.pillars)
    used = set(history.used)
    made: list[Assignment] = []
    for slot in slots:
        pool = [
            i
            for i in ideas
            if (i.id, slot.channel) not in used and (i.expires_at is None or i.expires_at > slot.at)
        ]
        if not pool:
            continue
        best = max(
            pool,
            key=lambda i: (
                i.score
                + _timing_bonus(i, slot, now)
                + _pillar_bonus(i, targets, counts)
                + (HINT_BONUS if any(c == slot.channel for c, _ in i.hints) else 0.0)
            ),
        )
        allowed = _allowed_kinds(config.plan_for(slot.channel), supported[slot.channel])
        usage = history.kinds.setdefault(slot.channel, Counter())
        kind = _pick_kind(best, slot, allowed, usage)
        usage[kind] += 1
        counts[best.pillar] += 1
        used.add((best.id, slot.channel))
        made.append(Assignment(slot, best.id, kind, best.pillar))
    return made
