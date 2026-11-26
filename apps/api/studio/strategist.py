"""Turn trend signals and the strategy into content ideas.

The model sees the signals as fenced outside text and has no tools. It refers to signals by number;
this module maps the numbers back to stored rows and drops anything that does not fit the channels
the owner actually runs.
"""

import re
import uuid
from datetime import timedelta
from typing import Any

from django.utils import timezone

from core.errors import ValidationFailedError
from social import ai
from studio import config_store, context
from studio.channels.registry import get_channel, is_known
from studio.config import StudioConfig
from studio.models import Brand, ContentIdea, IdeaStatus, TrendItem
from studio.schemas import FormatHint, IdeaBatch, IdeaOut
from studio.trends.collect import recent_items

PROMPT_KEY = "studio/trend-ideas"
AGENT = "content-strategist"
SIGNAL_LIMIT = 40
MAX_REQUEST = 20
MAX_INSTRUCTION = 1000
TRUSTED_SCORE_SCALE = 10.0
REPAIR_ROUNDS = 2
LABEL = re.compile(r"^[A-Za-z ]{3,20}:")
REPAIR_NOTE = (
    "Every title must be one plain statement with no colon, dash or question mark, and "
    "why_now must start with the reason itself, never with a label such as Evergreen."
)


def _signals_view(items: list[TrendItem]) -> str:
    rows = [
        {
            "n": index,
            "source": item.kind,
            "title": item.title,
            "summary": item.summary[:300],
            "strength": round(item.score, 2),
            "published": item.published_at.date().isoformat() if item.published_at else "",
        }
        for index, item in enumerate(items, start=1)
    ]
    return ai.untrusted("trend signals", rows)


def _valid_hints(hints: list[FormatHint], config: StudioConfig) -> list[dict[str, str]]:
    allowed = set(config.enabled_channels())
    clean: list[dict[str, str]] = []
    for hint in hints:
        if hint.channel not in allowed or not is_known(hint.channel):
            continue
        if get_channel(hint.channel).kind(hint.kind) is None:
            continue
        plan = config.plan_for(hint.channel)
        if plan.kinds and hint.kind not in plan.kinds:
            continue
        clean.append({"channel": hint.channel, "kind": hint.kind})
    return clean


def _to_idea(
    out: IdeaOut, items: list[TrendItem], config: StudioConfig, brand: Brand
) -> ContentIdea:
    linked = [str(items[n - 1].pk) for n in out.trends if 1 <= n <= len(items)]
    expires = (
        timezone.now() + timedelta(days=out.shelf_life_days) if out.urgency == "timely" else None
    )
    return ContentIdea(
        workspace_id=brand.workspace_id,
        brand_id=brand.pk,
        title=out.title,
        angle=out.angle,
        why_now=out.why_now,
        pillar=out.pillar[:40],
        urgency=out.urgency,
        expires_at=expires,
        score=out.score / TRUSTED_SCORE_SCALE,
        format_hints=_valid_hints(out.formats, config),
        trend_item_ids=linked,
        needs_from_owner=out.needs_from_owner,
        origin="ai",
    )


def _stiff(idea: IdeaOut) -> bool:
    """A title or reason that follows the "Heading: explanation" or question-headline shape."""
    title = idea.title.strip()
    if ":" in title or "|" in title or " - " in title or "—" in title or title.endswith("?"):
        return True
    return bool(LABEL.match(idea.why_now.strip()))


def _ask(brand: Brand, config: StudioConfig, items: list[TrendItem], count: int, note: str):
    return ai.run_prompt(
        brand.workspace_id,
        PROMPT_KEY,
        IdeaBatch,
        memory_query=f"content ideas {config.goals[:100]} {note[:100]}",
        user_message="Give the ideas.",
        channel="social",
        agent=AGENT,
        **context.llm_scope(brand),
        strategy=context.strategy_view(config),
        channels=context.channels_view(config),
        trends=_signals_view(items),
        recent=context.recent_view(brand.pk),
        performance=context.performance_view(brand.workspace_id, context.account_ids(config)),
        count=count,
        today=timezone.localdate().isoformat(),
        instruction=note.strip()[:MAX_INSTRUCTION],
    )


def generate(brand: Brand, count: int, instruction: str = "") -> list[ContentIdea]:
    """Ask for ``count`` ideas and store them as new. Needs at least one enabled channel."""
    config = config_store.load(brand.pk)
    if not config.enabled_channels():
        raise ValidationFailedError("turn on at least one channel first")
    wanted = max(1, min(count, MAX_REQUEST))
    items = recent_items(brand.pk, config.trends.max_age_days, SIGNAL_LIMIT)
    batch = _ask(brand, config, items, wanted, instruction)
    given = batch.ideas[:wanted]
    kept = [o for o in given if not _stiff(o)]
    rejected = len(given) - len(kept)
    for _ in range(REPAIR_ROUNDS):
        if rejected <= 0:
            break
        used = "; ".join(o.title for o in given)
        note = f"{instruction} {REPAIR_NOTE} Do not reuse these titles: {used}"
        given = _ask(brand, config, items, rejected, note).ideas[:rejected]
        good = [o for o in given if not _stiff(o)]
        kept += good
        rejected = len(given) - len(good)
    created = [_to_idea(o, items, config, brand) for o in kept]
    ContentIdea.objects.bulk_create(created)
    return created


def usable_ideas(brand_id: uuid.UUID) -> list[ContentIdea]:
    """Ideas that can still be turned into posts: new or accepted, not expired."""
    now = timezone.now()
    rows = ContentIdea.objects.filter(
        brand_id=brand_id, status__in=[IdeaStatus.NEW, IdeaStatus.ACCEPTED]
    )
    return [
        i for i in rows.order_by("-score", "-created_at") if not i.expires_at or i.expires_at > now
    ]


def set_status(idea: ContentIdea, status: str) -> ContentIdea:
    if status not in {IdeaStatus.NEW, IdeaStatus.ACCEPTED, IdeaStatus.REJECTED}:
        raise ValidationFailedError("an idea can be accepted or rejected")
    idea.status = status
    idea.save(update_fields=["status", "updated_at"])
    return idea


def signals_for(idea: ContentIdea) -> list[dict[str, Any]]:
    rows = TrendItem.objects.filter(brand_id=idea.brand_id, pk__in=idea.trend_item_ids)
    return [{"title": r.title, "summary": r.summary[:300], "url": r.url} for r in rows]
