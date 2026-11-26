"""Content studio API: what the studio can do here, its settings, and the routine."""

import uuid
from datetime import timedelta
from typing import Any

from django.db.models import Count
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from core.errors import NotFoundError, PermissionDeniedError
from identity.authentication import api_auth
from social import images
from studio import clock, config_store, runs
from studio.api_schemas import StudioRunOut, run_out
from studio.api_scope import brand_scope
from studio.channels.registry import all_channels
from studio.config import StudioConfig
from studio.models import (
    ContentIdea,
    ContentPiece,
    IdeaStatus,
    PieceStatus,
    RunKind,
    StudioRun,
    TrendSource,
)
from studio.publishers.ports import publisher_for
from studio.trends.ports import all_trend_sources
from studio.visuals.ports import picture_provider_keys, video_provider_keys
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission, role_allows

router = Router(tags=["studio"], auth=api_auth)

RUNS_SHOWN = 15


class StudioKindOut(Schema):
    kind: str
    max_chars: int
    hook_chars: int
    min_media: int
    max_media: int
    needs_video: bool
    orientation: str
    hashtags_max: int
    hashtags_recommended: int


class StudioAccountOut(Schema):
    id: str
    handle: str
    label: str


class StudioChannelOut(Schema):
    key: str
    kinds: list[StudioKindOut]
    publisher: bool
    accounts: list[StudioAccountOut]


class StudioSourceKindOut(Schema):
    kind: str
    needs_network: bool
    params_schema: dict[str, Any]


class StudioMetaOut(Schema):
    channels: list[StudioChannelOut]
    source_kinds: list[StudioSourceKindOut]
    picture_providers: list[str]
    video_providers: list[str]
    image_service_connected: bool


@router.get("/meta", response=StudioMetaOut, operation_id="studio_meta")
def meta(request: HttpRequest) -> StudioMetaOut:
    """Channels, source kinds and providers this installation knows, with connected accounts."""
    membership = resolve_workspace(request, Permission.VIEW)
    workspace_id = membership.workspace_id
    channels: list[StudioChannelOut] = []
    for spec in all_channels():
        publisher = publisher_for(spec.key)
        accounts = publisher.accounts(workspace_id) if publisher else []
        channels.append(
            StudioChannelOut(
                key=spec.key,
                kinds=[
                    StudioKindOut(
                        kind=k.kind,
                        max_chars=k.max_chars,
                        hook_chars=k.hook_chars,
                        min_media=k.min_media,
                        max_media=k.max_media,
                        needs_video=k.needs_video,
                        orientation=k.orientation,
                        hashtags_max=k.hashtags_max,
                        hashtags_recommended=k.hashtags_recommended,
                    )
                    for k in spec.kinds
                ],
                publisher=publisher is not None,
                accounts=[
                    StudioAccountOut(id=a.id, handle=a.handle, label=a.label) for a in accounts
                ],
            )
        )
    return StudioMetaOut(
        channels=channels,
        source_kinds=[
            StudioSourceKindOut(
                kind=s.kind,
                needs_network=s.needs_network,
                params_schema=s.params_model.model_json_schema(),
            )
            for s in all_trend_sources()
        ],
        picture_providers=picture_provider_keys(),
        video_providers=video_provider_keys(),
        image_service_connected=images.generator_for(workspace_id) is not None,
    )


class StudioOverviewOut(Schema):
    configured: bool
    automation_enabled: bool
    run_hour: int
    time_zone: str
    counts: dict[str, int]
    ideas_open: int
    sources_enabled: int
    last_run: StudioRunOut | None
    needs_you: int


@router.get("/overview", response=StudioOverviewOut, operation_id="studio_overview")
def overview(request: HttpRequest) -> StudioOverviewOut:
    scope = brand_scope(request, Permission.VIEW)
    workspace_id = scope.workspace_id
    config = config_store.load(scope.brand.pk)
    counts = {
        row["status"]: row["n"]
        for row in ContentPiece.objects.filter(brand_id=scope.brand.pk)
        .values("status")
        .annotate(n=Count("id"))
    }
    last = StudioRun.objects.filter(brand_id=scope.brand.pk).exclude(kind=RunKind.IDEAS).first()
    return StudioOverviewOut(
        configured=bool(config.enabled_channels()),
        automation_enabled=config.automation.enabled,
        run_hour=config.automation.run_hour,
        time_zone=str(clock.zone_of(workspace_id)),
        counts=counts,
        ideas_open=ContentIdea.objects.filter(
            brand_id=scope.brand.pk, status__in=[IdeaStatus.NEW, IdeaStatus.ACCEPTED]
        ).count(),
        sources_enabled=TrendSource.objects.filter(brand_id=scope.brand.pk, enabled=True).count(),
        last_run=run_out(last) if last else None,
        needs_you=counts.get(PieceStatus.DRAFT, 0) + counts.get(PieceStatus.MANUAL_DUE, 0),
    )


@router.get("/config", response=StudioConfig, operation_id="studio_config_get")
def get_config(request: HttpRequest) -> StudioConfig:
    scope = brand_scope(request, Permission.VIEW)
    return config_store.load(scope.brand.pk)


@router.put("/config", response=StudioConfig, operation_id="studio_config_save")
def save_config(request: HttpRequest, body: StudioConfig) -> StudioConfig:
    """Save the strategy. Anything that posts without a person needs the autonomy right."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    previous = config_store.load(scope.brand.pk)
    if config_store.grants_more_autonomy(body, previous) and not role_allows(
        scope.membership.role, Permission.CHANGE_AUTONOMY
    ):
        raise PermissionDeniedError()
    return config_store.save(scope.brand, body, request.auth)  # type: ignore[attr-defined]


@router.post(
    "/config/disable-automation", response=StudioConfig, operation_id="studio_disable_automation"
)
def disable_automation(request: HttpRequest) -> StudioConfig:
    """Panic button: nothing is approved or researched on its own until it is turned back on."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    config = config_store.load(scope.brand.pk)
    config.automation.enabled = False
    for plan in config.channels.values():
        if plan.publish_mode == "auto":
            plan.publish_mode = "review"
    return config_store.save(scope.brand, config, request.auth)  # type: ignore[attr-defined]


class StudioRunIn(Schema):
    pass


@router.post("/run", response=StudioRunOut, operation_id="studio_run_now")
def run_now(request: HttpRequest, body: StudioRunIn) -> StudioRunOut:
    """Research and plan now, exactly as the daily routine does."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    return run_out(runs.start(scope.brand, RunKind.MANUAL))


@router.get("/runs", response=list[StudioRunOut], operation_id="studio_runs")
def list_runs(request: HttpRequest) -> list[StudioRunOut]:
    scope = brand_scope(request, Permission.VIEW)
    since = timezone.now() - timedelta(days=30)
    rows = StudioRun.objects.filter(brand_id=scope.brand.pk, started_at__gte=since)
    return [run_out(r) for r in rows[:RUNS_SHOWN]]


@router.get("/runs/{run_id}", response=StudioRunOut, operation_id="studio_run_get")
def get_run(request: HttpRequest, run_id: uuid.UUID) -> StudioRunOut:
    scope = brand_scope(request, Permission.VIEW)
    found = StudioRun.objects.filter(pk=run_id, brand_id=scope.brand.pk).first()
    if found is None:
        raise NotFoundError("run not found")
    return run_out(found)
