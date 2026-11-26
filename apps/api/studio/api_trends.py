"""Content studio API: trend sources, the signals they collected, and content ideas."""

import uuid
from datetime import timedelta
from typing import Any

from django.db.models import Count
from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema, Status

from core.errors import NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from studio import config_store, runs, strategist, writer
from studio.api_schemas import (
    StudioIdeaOut,
    StudioPieceOut,
    StudioRunOut,
    StudioSourceOut,
    StudioTrendOut,
    idea_out,
    piece_out,
    run_out,
    source_out,
    trend_out,
)
from studio.api_scope import brand_scope
from studio.channels.registry import get_kind
from studio.models import ContentIdea, ContentPiece, RunKind, TrendItem, TrendSource
from studio.trends import collect
from studio.trends.ports import get_trend_source, validate_params
from studio.trends.suggest import suggestions
from workspaces.permissions import Permission

router = Router(tags=["studio-trends"], auth=api_auth)

MAX_PAGE = 100
MAX_SOURCES = 40


# --- sources ----------------------------------------------------------------------------------


def _source(brand_id: uuid.UUID, source_id: uuid.UUID) -> TrendSource:
    found = TrendSource.objects.filter(pk=source_id, brand_id=brand_id).first()
    if found is None:
        raise NotFoundError("source not found")
    return found


@router.get("/sources", response=list[StudioSourceOut], operation_id="studio_sources")
def list_sources(request: HttpRequest) -> list[StudioSourceOut]:
    scope = brand_scope(request, Permission.VIEW)
    return [source_out(s) for s in TrendSource.objects.filter(brand_id=scope.brand.pk)]


class StudioSourceIn(Schema):
    kind: str
    name: str = ""
    params: dict[str, Any] = {}
    enabled: bool = True


@router.post("/sources", response=StudioSourceOut, operation_id="studio_source_create")
def create_source(request: HttpRequest, body: StudioSourceIn) -> StudioSourceOut:
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    if TrendSource.objects.filter(brand_id=scope.brand.pk).count() >= MAX_SOURCES:
        raise ValidationFailedError("too many sources; remove one first")
    params = validate_params(body.kind, body.params)
    name = body.name.strip() or get_trend_source(body.kind).default_name(params)
    return source_out(
        TrendSource.objects.create(
            workspace_id=scope.workspace_id,
            brand=scope.brand,
            kind=body.kind,
            name=name[:120],
            params=params.model_dump(),
            enabled=body.enabled,
        )
    )


@router.post(
    "/sources/suggest", response=list[StudioSourceOut], operation_id="studio_sources_suggest"
)
def suggest_sources(request: HttpRequest) -> list[StudioSourceOut]:
    """Add sources built from the topics and country in the settings (nothing is added twice)."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    brand = scope.brand
    config = config_store.load(brand.pk)
    have = {
        (s.kind, str(sorted(s.params.items())))
        for s in TrendSource.objects.filter(brand_id=brand.pk)
    }
    added: list[TrendSource] = []
    for pick in suggestions(config.trends):
        if (pick.kind, str(sorted(pick.params.items()))) in have:
            continue
        added.append(
            TrendSource.objects.create(
                workspace_id=brand.workspace_id,
                brand=brand,
                kind=pick.kind,
                name=pick.name[:120],
                params=pick.params,
            )
        )
    return [source_out(s) for s in added]


class StudioSourceUpdateIn(Schema):
    name: str | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None


@router.patch("/sources/{source_id}", response=StudioSourceOut, operation_id="studio_source_update")
def update_source(
    request: HttpRequest, source_id: uuid.UUID, body: StudioSourceUpdateIn
) -> StudioSourceOut:
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    source = _source(scope.brand.pk, source_id)
    if body.name is not None and body.name.strip():
        source.name = body.name.strip()[:120]
    if body.params is not None:
        source.params = validate_params(source.kind, body.params).model_dump()
    if body.enabled is not None:
        source.enabled = body.enabled
    source.save()
    return source_out(source)


@router.delete("/sources/{source_id}", response={204: None}, operation_id="studio_source_delete")
def delete_source(request: HttpRequest, source_id: uuid.UUID) -> Status[None]:
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    _source(scope.brand.pk, source_id).delete()
    return Status(204, None)


@router.post(
    "/sources/{source_id}/refresh", response=StudioSourceOut, operation_id="studio_source_refresh"
)
def refresh_source(request: HttpRequest, source_id: uuid.UUID) -> StudioSourceOut:
    """Fetch one source now so the owner can see whether it works."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    source = _source(scope.brand.pk, source_id)
    avoid = config_store.load(scope.brand.pk).trends.avoid_topics
    try:
        collect.collect_source(source, avoid)
    except ValidationFailedError:
        raise
    except Exception as exc:
        source.last_error = getattr(getattr(exc, "code", None), "value", "unknown")
        source.last_fetched_at = timezone.now()
        source.save(update_fields=["last_error", "last_fetched_at", "updated_at"])
    return source_out(source)


# --- signals ----------------------------------------------------------------------------------


class StudioTrendFilters(Schema):
    days: int = 7
    include_ignored: bool = False
    limit: int = 50


@router.get("/trends", response=list[StudioTrendOut], operation_id="studio_trends")
def list_trends(request: HttpRequest, filters: Query[StudioTrendFilters]) -> list[StudioTrendOut]:
    scope = brand_scope(request, Permission.VIEW)
    since = timezone.now() - timedelta(days=max(1, min(filters.days, 60)))
    rows = TrendItem.objects.filter(brand_id=scope.brand.pk, created_at__gte=since)
    if not filters.include_ignored:
        rows = rows.filter(ignored=False)
    return [
        trend_out(t)
        for t in rows.order_by("-score", "-created_at")[: max(1, min(filters.limit, MAX_PAGE))]
    ]


class StudioIgnoreIn(Schema):
    ignored: bool


@router.patch("/trends/{trend_id}", response=StudioTrendOut, operation_id="studio_trend_update")
def update_trend(request: HttpRequest, trend_id: uuid.UUID, body: StudioIgnoreIn) -> StudioTrendOut:
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    item = TrendItem.objects.filter(pk=trend_id, brand_id=scope.brand.pk).first()
    if item is None:
        raise NotFoundError("signal not found")
    item.ignored = body.ignored
    item.save(update_fields=["ignored", "updated_at"])
    return trend_out(item)


class StudioRefreshOut(Schema):
    new_items: int
    failed: int


@router.post("/trends/refresh", response=StudioRefreshOut, operation_id="studio_trends_refresh")
def refresh_trends(request: HttpRequest) -> StudioRefreshOut:
    """Fetch every enabled source now."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    result = collect.collect_brand(scope.brand.pk)
    return StudioRefreshOut(new_items=result.new_items, failed=result.failed)


# --- ideas ------------------------------------------------------------------------------------


class StudioIdeaFilters(Schema):
    status: str = ""
    limit: int = 50


def _ideas_out(rows: list[ContentIdea]) -> list[StudioIdeaOut]:
    counts = {
        r["idea_id"]: r["n"]
        for r in ContentPiece.objects.filter(idea__in=rows)
        .exclude(status="cancelled")
        .values("idea_id")
        .annotate(n=Count("id"))
    }
    return [idea_out(i, strategist.signals_for(i), counts.get(i.pk, 0)) for i in rows]


@router.get("/ideas", response=list[StudioIdeaOut], operation_id="studio_ideas")
def list_ideas(request: HttpRequest, filters: Query[StudioIdeaFilters]) -> list[StudioIdeaOut]:
    scope = brand_scope(request, Permission.VIEW)
    rows = ContentIdea.objects.filter(brand_id=scope.brand.pk)
    rows = rows.filter(status=filters.status) if filters.status else rows.exclude(status="rejected")
    return _ideas_out(list(rows.order_by("-created_at")[: max(1, min(filters.limit, MAX_PAGE))]))


class StudioGenerateIn(Schema):
    count: int = 6
    instruction: str = ""


@router.post("/ideas/generate", response=StudioRunOut, operation_id="studio_ideas_generate")
def generate_ideas(request: HttpRequest, body: StudioGenerateIn) -> StudioRunOut:
    """Ask for fresh ideas from the current signals and strategy (runs in the background)."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    run = runs.start(scope.brand, RunKind.IDEAS, count=body.count, instruction=body.instruction)
    return run_out(run)


class StudioIdeaStatusIn(Schema):
    status: str


@router.patch("/ideas/{idea_id}", response=StudioIdeaOut, operation_id="studio_idea_update")
def update_idea(
    request: HttpRequest, idea_id: uuid.UUID, body: StudioIdeaStatusIn
) -> StudioIdeaOut:
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    idea = ContentIdea.objects.filter(pk=idea_id, brand_id=scope.brand.pk).first()
    if idea is None:
        raise NotFoundError("idea not found")
    return _ideas_out([strategist.set_status(idea, body.status)])[0]


class StudioTarget(Schema):
    channel: str
    kind: str


class StudioIdeaPiecesIn(Schema):
    targets: list[StudioTarget]


@router.post(
    "/ideas/{idea_id}/pieces", response=list[StudioPieceOut], operation_id="studio_idea_pieces"
)
def idea_to_pieces(
    request: HttpRequest, idea_id: uuid.UUID, body: StudioIdeaPiecesIn
) -> list[StudioPieceOut]:
    """Turn an idea into posts, one per chosen channel and post type."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    idea = ContentIdea.objects.filter(pk=idea_id, brand_id=scope.brand.pk).first()
    if idea is None:
        raise NotFoundError("idea not found")
    if not body.targets:
        raise ValidationFailedError("choose at least one channel")
    for target in body.targets:
        get_kind(target.channel, target.kind)
    return [
        piece_out(
            writer.create(
                scope.brand,
                request.auth.pk,  # type: ignore[attr-defined]
                channel=t.channel,
                kind=t.kind,
                idea_id=idea.pk,
                pillar=idea.pillar,
                origin="user",
            )
        )
        for t in body.targets
    ]
