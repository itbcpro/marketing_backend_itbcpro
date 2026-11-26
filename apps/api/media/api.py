"""Asset library: upload, import, link, list, edit, crop and delete images and videos."""

import uuid
from datetime import date, datetime
from typing import Any

from django.http import FileResponse, HttpRequest
from ninja import File, Form, Query, Router, Schema, Status
from ninja.files import UploadedFile

from core.api.pagination import DEFAULT_LIMIT, Page, paginate
from core.errors import ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from media import services
from media.models import MediaAsset, MediaKind, Rights
from media.specs import SPEC_BY_KEY, SPECS, check_all, spec_as_dict
from media.storage import get_storage
from workspaces.deps import resolve_workspace, resolve_workspace_for_file
from workspaces.permissions import Permission

router = Router(tags=["media"], auth=api_auth)

MAX_DERIVE_AT_ONCE = 12


class MediaOut(Schema):
    id: uuid.UUID
    kind: str
    source: str
    name: str
    description: str
    alt_text: str
    tags: list[str]
    mime_type: str
    file_format: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_seconds: float | None
    animated: bool
    has_alpha: bool
    youtube_video_id: str
    external: dict[str, Any]
    rights: str
    license_note: str
    license_expires_on: date | None
    ai_generated: bool
    derived_from_id: uuid.UUID | None
    variant: str
    has_file: bool
    created_at: datetime
    checks: list[dict[str, Any]]


class UploadResultOut(Schema):
    asset: MediaOut
    created: bool


class UrlIn(Schema):
    url: str
    title: str = ""


class YoutubeIn(Schema):
    video: str
    title: str = ""


class UpdateIn(Schema):
    name: str | None = None
    description: str | None = None
    alt_text: str | None = None
    tags: list[str] | None = None
    rights: str | None = None
    license_note: str | None = None
    license_expires_on: date | None = None
    ai_generated: bool | None = None


class DeriveIn(Schema):
    targets: list[str]
    focal_x: float = 0.5
    focal_y: float = 0.5


class DeriveResultOut(Schema):
    created: list[MediaOut]
    skipped: list[dict[str, str]]


class ListFilters(Schema):
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    kind: str | None = None
    q: str | None = None
    tag: str | None = None
    originals: bool = False


def _out(asset: MediaAsset) -> MediaOut:
    return MediaOut(
        id=asset.pk,
        kind=asset.kind,
        source=asset.source,
        name=asset.name,
        description=asset.description,
        alt_text=asset.alt_text,
        tags=list(asset.tags),
        mime_type=asset.mime_type,
        file_format=asset.file_format,
        size_bytes=asset.size_bytes,
        width=asset.width,
        height=asset.height,
        duration_seconds=asset.duration_seconds,
        animated=asset.animated,
        has_alpha=asset.has_alpha,
        youtube_video_id=asset.youtube_video_id,
        external=asset.external,
        rights=asset.rights,
        license_note=asset.license_note,
        license_expires_on=asset.license_expires_on,
        ai_generated=asset.ai_generated,
        derived_from_id=asset.derived_from_id,
        variant=asset.variant,
        has_file=bool(asset.storage_key),
        created_at=asset.created_at,
        checks=check_all(asset),
    )


def _user(request: HttpRequest) -> User:
    return request.auth  # type: ignore[attr-defined, no-any-return]


@router.get("/specs", response=list[dict[str, Any]], operation_id="media_specs")
def list_specs(request: HttpRequest) -> list[dict[str, Any]]:
    resolve_workspace(request, Permission.VIEW)
    return [spec_as_dict(s) for s in SPECS]


@router.post("/upload", response=UploadResultOut, operation_id="media_upload")
def upload(
    request: HttpRequest,
    file: File[UploadedFile],
    title: Form[str] = "",
    tags: Form[str] = "",
) -> UploadResultOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    asset, created = services.create_from_chunks(
        membership.workspace_id,
        _user(request).pk,
        file.chunks(),
        filename=file.name or "upload",
        source="upload",
        title=title,
        tags=[t for t in tags.split(",") if t.strip()],
    )
    return UploadResultOut(asset=_out(asset), created=created)


@router.post("/import-url", response=UploadResultOut, operation_id="media_import_url")
def import_url(request: HttpRequest, body: UrlIn) -> UploadResultOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    asset, created = services.import_from_url(
        membership.workspace_id, _user(request).pk, body.url.strip(), title=body.title
    )
    return UploadResultOut(asset=_out(asset), created=created)


@router.post("/youtube", response=UploadResultOut, operation_id="media_link_youtube")
def link_youtube(request: HttpRequest, body: YoutubeIn) -> UploadResultOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    asset, created = services.link_youtube(
        membership.workspace_id, _user(request).pk, body.video, title=body.title
    )
    return UploadResultOut(asset=_out(asset), created=created)


@router.get("/", response=Page[MediaOut], operation_id="media_list")
def list_media(request: HttpRequest, filters: Query[ListFilters]) -> Page[MediaOut]:
    membership = resolve_workspace(request, Permission.VIEW)
    queryset = MediaAsset.objects.filter(workspace_id=membership.workspace_id)
    if filters.kind in {k.value for k in MediaKind}:
        queryset = queryset.filter(kind=filters.kind)
    if filters.q:
        queryset = queryset.filter(name__icontains=filters.q.strip()[:100])
    if filters.tag:
        queryset = queryset.filter(tags__contains=[filters.tag.strip().lower()])
    if filters.originals:
        queryset = queryset.filter(derived_from__isnull=True)
    rows, next_cursor = paginate(queryset, cursor=filters.cursor, limit=filters.limit)
    return Page[MediaOut](items=[_out(a) for a in rows], next_cursor=next_cursor)


@router.get("/{asset_id}", response=MediaOut, operation_id="media_get")
def get_media(request: HttpRequest, asset_id: uuid.UUID) -> MediaOut:
    membership = resolve_workspace(request, Permission.VIEW)
    return _out(services.get(membership.workspace_id, asset_id))


@router.get("/{asset_id}/file", auth=api_auth, operation_id="media_file", response={200: None})
def media_file(request: HttpRequest, asset_id: uuid.UUID) -> FileResponse:
    """The file itself, for the signed-in user's workspace only."""
    membership = resolve_workspace_for_file(request, Permission.VIEW)
    asset = services.get(membership.workspace_id, asset_id)
    if not asset.storage_key:
        raise ValidationFailedError("this asset has no file")
    response = FileResponse(get_storage().open(asset.storage_key), content_type=asset.mime_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Cache-Control"] = "private, max-age=3600"
    return response


@router.patch("/{asset_id}", response=MediaOut, operation_id="media_update")
def update_media(request: HttpRequest, asset_id: uuid.UUID, body: UpdateIn) -> MediaOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    asset = services.get(membership.workspace_id, asset_id)
    data = body.model_dump(exclude_unset=True)
    if "rights" in data and data["rights"] not in {r.value for r in Rights}:
        raise ValidationFailedError("unknown rights value")
    if "tags" in data:
        data["tags"] = services._clean_tags(data["tags"])
    if "name" in data:
        data["name"] = (data["name"] or "").strip()[: services.MAX_NAME]
        if len(data["name"]) < 1:
            raise ValidationFailedError("the name cannot be empty")
    for field, value in data.items():
        setattr(asset, field, value)
    asset.save()
    return _out(asset)


@router.delete("/{asset_id}", response={204: None}, operation_id="media_delete")
def delete_media(request: HttpRequest, asset_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    services.delete(services.get(membership.workspace_id, asset_id))
    return Status(204, None)


@router.post("/{asset_id}/derive", response=DeriveResultOut, operation_id="media_derive")
def derive_media(request: HttpRequest, asset_id: uuid.UUID, body: DeriveIn) -> DeriveResultOut:
    """Make crops of an image at the ratios and sizes destinations require."""
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    source = services.get(membership.workspace_id, asset_id)
    if not body.targets or len(body.targets) > MAX_DERIVE_AT_ONCE:
        raise ValidationFailedError("choose between 1 and 12 crops")
    created: list[MediaOut] = []
    skipped: list[dict[str, str]] = []
    for key in dict.fromkeys(body.targets):
        if key not in SPEC_BY_KEY:
            raise ValidationFailedError(f"unknown crop {key}")
        try:
            asset, made = services.derive(
                source, _user(request).pk, key, focal=(body.focal_x, body.focal_y)
            )
        except ValidationFailedError as exc:
            skipped.append({"target": key, "reason": exc.detail})
            continue
        if made:
            created.append(_out(asset))
        else:
            skipped.append({"target": key, "reason": "already exists"})
    return DeriveResultOut(created=created, skipped=skipped)
