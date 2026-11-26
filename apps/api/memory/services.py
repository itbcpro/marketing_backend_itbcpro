"""Memory use cases: brand profile, items, hybrid retrieval and feedback capture."""

import re
import uuid
from dataclasses import dataclass
from typing import Any

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from pgvector.django import CosineDistance

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError
from identity.models import User
from memory.embeddings import get_embedder
from memory.models import (
    BrandProfile,
    FeedbackEvent,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
)

RRF_K = 60
CANDIDATES = 30
# Cosine distance under which a vector hit counts as related (tuned for HashEmbedder).
SEMANTIC_MAX_DISTANCE = 0.75
CHARS_PER_TOKEN = 4


# ---- brand profile ----------------------------------------------------------------------------


def current_brand_profile(workspace_id: uuid.UUID) -> BrandProfile | None:
    return BrandProfile.objects.filter(workspace_id=workspace_id).order_by("-version").first()


def save_brand_profile(
    *, workspace_id: uuid.UUID, content: dict[str, Any], user: User
) -> BrandProfile:
    with transaction.atomic():
        latest = current_brand_profile(workspace_id)
        profile = BrandProfile.objects.create(
            workspace_id=workspace_id,
            version=(latest.version + 1) if latest else 1,
            content=content,
            created_by=user,
        )
        record_event(
            action="brand_profile.saved",
            actor=Actor.human(user.pk),
            workspace_id=workspace_id,
            target_type="brand_profile",
            target_id=profile.pk,
            after={"version": profile.version},
        )
    return profile


def render_brand_profile(profile: BrandProfile | None) -> str:
    if profile is None or not profile.content:
        return "(no brand profile has been written yet)"
    lines: list[str] = []
    for section, value in profile.content.items():
        title = section.replace("_", " ").capitalize()
        body = "; ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        lines.append(f"- {title}: {body}")
    return "\n".join(lines)


# ---- items ------------------------------------------------------------------------------------


def remember(
    *,
    workspace_id: uuid.UUID,
    text: str,
    kind: str = MemoryKind.FACT,
    scope: str = MemoryScope.WORKSPACE,
    scope_ref: str = "",
    status: str = MemoryStatus.PROPOSED,
    is_hard_rule: bool = False,
    source: str = "user",
    evidence: list[str] | None = None,
    confidence: float = 0.5,
    supersedes: MemoryItem | None = None,
    actor: Actor | None = None,
) -> MemoryItem:
    embedding = get_embedder().embed([text])[0]
    with transaction.atomic():
        item = MemoryItem.objects.create(
            workspace_id=workspace_id,
            kind=kind,
            scope=scope,
            scope_ref=scope_ref,
            text=text.strip(),
            embedding=embedding,
            status=status,
            is_hard_rule=is_hard_rule,
            source=source,
            evidence=evidence or [],
            confidence=confidence,
            supersedes=supersedes,
            valid_from=timezone.now() if status == MemoryStatus.ACTIVE else None,
        )
        if supersedes is not None and status == MemoryStatus.ACTIVE:
            _retire(supersedes)
        record_event(
            action="memory.created",
            actor=actor or Actor.system("memory"),
            workspace_id=workspace_id,
            target_type="memory_item",
            target_id=item.pk,
            after={"kind": kind, "status": status, "source": source, "hard_rule": is_hard_rule},
        )
    return item


def _get(workspace_id: uuid.UUID, item_id: uuid.UUID) -> MemoryItem:
    item = MemoryItem.objects.filter(pk=item_id, workspace_id=workspace_id).first()
    if item is None:
        raise NotFoundError("memory item not found")
    return item


def _retire(item: MemoryItem) -> None:
    item.status = MemoryStatus.RETIRED
    item.valid_to = timezone.now()
    item.save(update_fields=["status", "valid_to", "updated_at"])


def activate(*, workspace_id: uuid.UUID, item_id: uuid.UUID, user: User) -> MemoryItem:
    item = _get(workspace_id, item_id)
    with transaction.atomic():
        item.status = MemoryStatus.ACTIVE
        item.valid_from = timezone.now()
        item.save(update_fields=["status", "valid_from", "updated_at"])
        if item.supersedes_id:
            _retire(item.supersedes)  # type: ignore[arg-type]
        record_event(
            action="memory.activated",
            actor=Actor.human(user.pk),
            workspace_id=workspace_id,
            target_type="memory_item",
            target_id=item.pk,
        )
    return item


def retire(
    *, workspace_id: uuid.UUID, item_id: uuid.UUID, user: User, reject: bool = False
) -> MemoryItem:
    item = _get(workspace_id, item_id)
    item.status = MemoryStatus.REJECTED if reject else MemoryStatus.RETIRED
    item.valid_to = timezone.now()
    item.save(update_fields=["status", "valid_to", "updated_at"])
    record_event(
        action="memory.rejected" if reject else "memory.retired",
        actor=Actor.human(user.pk),
        workspace_id=workspace_id,
        target_type="memory_item",
        target_id=item.pk,
    )
    return item


# ---- retrieval --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryHit:
    item: MemoryItem
    score: float
    why: str


def _usable(workspace_id: uuid.UUID, channel: str | None, agent: str | None) -> Any:
    now = timezone.now()
    scopes = Q(scope=MemoryScope.WORKSPACE)
    if channel:
        scopes |= Q(scope=MemoryScope.CHANNEL, scope_ref=channel)
    if agent:
        scopes |= Q(scope=MemoryScope.AGENT, scope_ref=agent)
    return MemoryItem.objects.filter(
        scopes,
        Q(valid_to__isnull=True) | Q(valid_to__gt=now),
        workspace_id=workspace_id,
        status=MemoryStatus.ACTIVE,
    )


def retrieve(
    *,
    workspace_id: uuid.UUID,
    query: str,
    channel: str | None = None,
    agent: str | None = None,
    limit: int = 8,
    token_budget: int = 1500,
    track_usage: bool = True,
) -> list[MemoryHit]:
    """Hybrid search: full-text and vector rankings fused with reciprocal rank fusion.

    Hard rules are always returned first regardless of relevance; everything is packed into
    ``token_budget`` (approximated as characters / 4).
    """
    base = _usable(workspace_id, channel, agent)
    hard = list(base.filter(is_hard_rule=True).order_by("created_at"))
    scores: dict[uuid.UUID, float] = {}
    why: dict[uuid.UUID, list[str]] = {}
    items: dict[uuid.UUID, MemoryItem] = {}

    def fuse(ranked: list[MemoryItem], label: str) -> None:
        for rank, item in enumerate(ranked):
            items[item.pk] = item
            scores[item.pk] = scores.get(item.pk, 0.0) + 1.0 / (RRF_K + rank + 1)
            why.setdefault(item.pk, []).append(label)

    rest = base.filter(is_hard_rule=False)
    if query.strip():
        text_query = SearchQuery(_any_of(query), config="english", search_type="websearch")
        fuse(
            list(
                rest.annotate(document=SearchVector("text", config="english"))
                .filter(document=text_query)
                .annotate(rank=SearchRank("document", text_query))
                .order_by("-rank")[:CANDIDATES]
            ),
            "keyword",
        )
        vector = get_embedder().embed([query])[0]
        fuse(
            list(
                rest.exclude(embedding__isnull=True)
                .annotate(distance=CosineDistance("embedding", vector))
                .filter(distance__lt=SEMANTIC_MAX_DISTANCE)
                .order_by("distance")[:CANDIDATES]
            ),
            "semantic",
        )
    for pk, item in items.items():
        scores[pk] *= 0.5 + item.confidence  # evidence-backed items outrank weak guesses

    ranked = sorted(items, key=lambda pk: scores[pk], reverse=True)
    hits = [MemoryHit(item, 1.0, "hard rule") for item in hard]
    hits += [MemoryHit(items[pk], scores[pk], "+".join(why[pk])) for pk in ranked]

    packed: list[MemoryHit] = []
    used = 0
    for hit in hits:
        cost = len(hit.item.text) // CHARS_PER_TOKEN + 1
        if not hit.item.is_hard_rule and (len(packed) >= limit or used + cost > token_budget):
            continue
        packed.append(hit)
        used += cost
    if track_usage and packed:
        MemoryItem.objects.filter(pk__in=[h.item.pk for h in packed]).update(
            usage_count=F("usage_count") + 1, last_used_at=timezone.now()
        )
    return packed


_WORD = re.compile(r"[A-Za-z0-9]+")


def _any_of(query: str) -> str:
    """Match any query word (ranked by how many match) instead of requiring all of them."""
    words = [w for w in _WORD.findall(query) if len(w) > 2]  # noqa: PLR2004 - drop tiny words
    return " or ".join(words) if words else query


def render_memories(hits: list[MemoryHit]) -> str:
    if not hits:
        return "(nothing learned yet)"
    lines = []
    for hit in hits:
        marker = "MUST" if hit.item.is_hard_rule else hit.item.kind
        lines.append(f"- [{marker}] {hit.item.text}")
    return "\n".join(lines)


# ---- feedback ---------------------------------------------------------------------------------


def _changed_fields(proposed: dict[str, Any], final: dict[str, Any]) -> list[str]:
    keys = set(proposed) | set(final)
    return sorted(k for k in keys if proposed.get(k) != final.get(k))


def record_feedback(payload: dict[str, Any]) -> None:
    """Outbox handler for ``action.decided``. Idempotent per action and verdict."""
    proposed, final = payload.get("proposed", {}), payload.get("final", {})
    FeedbackEvent.objects.get_or_create(
        workspace_id=payload["workspace_id"],
        action_id=payload["action_id"],
        verdict=payload["verdict"],
        defaults={
            "agent": payload.get("agent", ""),
            "action_kind": payload.get("kind", ""),
            "reason": payload.get("reason", ""),
            "note": payload.get("note", ""),
            "proposed": proposed,
            "final": final,
            "changed_fields": _changed_fields(proposed, final),
        },
    )
